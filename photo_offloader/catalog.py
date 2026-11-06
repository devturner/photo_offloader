"""The SQLite catalog: file locations and hashes, import history, Google Photos uploads."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from photo_offloader import config, fileops
from photo_offloader.logs import logger

_CATALOG_INITIALIZED_PATH: Optional[str] = None


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS file_locations (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            sha256 TEXT,
            capture_month TEXT,
            camera_model TEXT,
            media_folder TEXT,
            indexed_at REAL NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS google_photos_uploads (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            uploaded_at REAL NOT NULL,
            remote TEXT NOT NULL,
            album TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS catalog_refreshes (
            root TEXT PRIMARY KEY,
            last_updated REAL NOT NULL,
            files_scanned INTEGER NOT NULL,
            status TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS import_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            finished_at REAL NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL,
            files INTEGER NOT NULL DEFAULT 0,
            copied INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0,
            usb INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT ''
        )
    """)
    history_columns = {row[1] for row in connection.execute("PRAGMA table_info(import_history)")}
    if "details" not in history_columns:
        # Per-session breakdown (new files by type, bytes, USB copies) as JSON.
        connection.execute(
            "ALTER TABLE import_history ADD COLUMN details TEXT NOT NULL DEFAULT '{}'"
        )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_file_locations_root ON file_locations(root)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_locations_relative ON file_locations(root, relative_path)"
    )
    connection.commit()


def catalog_connection() -> sqlite3.Connection:
    """Open the persistent media/location catalog, initializing its schema once per path."""
    global _CATALOG_INITIALIZED_PATH
    config.CATALOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not config.CATALOG_DB_PATH.exists() and config.LEGACY_CATALOG_DB_PATH.is_file():
        try:
            shutil.copy2(config.LEGACY_CATALOG_DB_PATH, config.CATALOG_DB_PATH)
            logger.info(
                "Migrated legacy file catalog to persistent data location: %s",
                config.CATALOG_DB_PATH,
            )
        except OSError as exc:
            logger.warning(
                "Could not migrate legacy catalog; using the existing catalog location: %s", exc
            )
            return sqlite3.connect(config.LEGACY_CATALOG_DB_PATH, timeout=10)
    connection = sqlite3.connect(config.CATALOG_DB_PATH, timeout=10)
    catalog_path = str(config.CATALOG_DB_PATH.resolve())
    try:
        connection.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as exc:  # only a speed setting: the default is safe, just slower
        logger.debug("Could not set catalog synchronous mode: %s", exc)
    if _CATALOG_INITIALIZED_PATH != catalog_path:
        try:
            # Readers (the dashboard) no longer block behind the importer's writes.
            connection.execute("PRAGMA journal_mode=WAL")  # persistent per database file
        except sqlite3.Error as exc:  # e.g. a filesystem without shared-memory support
            logger.debug("Could not enable WAL for the catalog: %s", exc)
        _create_schema(connection)
        _CATALOG_INITIALIZED_PATH = catalog_path
    return connection


@contextmanager
def catalog_db() -> Iterator[sqlite3.Connection]:
    """Use a catalog connection for one transaction and always close its file handles."""
    connection = catalog_connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


IMPORT_HISTORY_MAX_ROWS = 500


def record_import_run(
    *,
    kind: str,
    outcome: str,
    started_at: float,
    finished_at: float,
    source: str = "",
    files: int = 0,
    copied: int = 0,
    skipped: int = 0,
    failed: int = 0,
    bytes: int = 0,
    usb: bool = False,  # noqa: A002
    message: str = "",
    details: Optional[Dict[str, object]] = None,
) -> None:
    """Persist one finished run so the dashboard can show history across restarts.

    History is a convenience: any database problem is logged and swallowed so it
    can never fail or hide a real import.
    """
    try:
        with catalog_db() as db:
            db.execute(
                """INSERT INTO import_history
                   (started_at, finished_at, kind, source, outcome, files, copied, skipped,
                    failed, bytes, usb, message, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    float(started_at),
                    float(finished_at),
                    kind,
                    source,
                    outcome,
                    int(files),
                    int(copied),
                    int(skipped),
                    int(failed),
                    int(bytes),
                    int(bool(usb)),
                    str(message)[:500],
                    json.dumps(details or {}, sort_keys=True),
                ),
            )
            db.execute(
                """DELETE FROM import_history WHERE id NOT IN
                   (SELECT id FROM import_history ORDER BY id DESC LIMIT ?)""",
                (IMPORT_HISTORY_MAX_ROWS,),
            )
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not record import history: %s", exc)


def record_upload_on_import(
    upload: Dict[str, object], *, started_at: float, within_seconds: float
) -> None:
    """Keep an upload with the import it followed, since the sync's status file is overwritten.

    The newest import counts if it finished successfully no more than ``within_seconds`` before
    the upload began, and has no finished upload already. It may finish after the upload began:
    the importer starts the sync, then writes its history row. Like all history, a database
    problem is logged and swallowed.
    """
    try:
        with catalog_db() as db:
            row = db.execute(
                """SELECT id, finished_at, details FROM import_history
                   WHERE kind = 'import' AND outcome = 'complete' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if row is None or started_at - float(row[1]) > within_seconds:
                return
            details = json.loads(row[2] or "{}")
            if (details.get("upload") or {}).get("outcome") == "complete":
                return
            details["upload"] = upload
            db.execute(
                "UPDATE import_history SET details = ? WHERE id = ?",
                (json.dumps(details, sort_keys=True), row[0]),
            )
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("Could not record the upload in import history: %s", exc)


def list_import_history(limit: int = 20) -> List[Dict[str, object]]:
    """Most recent runs first."""
    try:
        with catalog_db() as db:
            rows = db.execute(
                """SELECT id, started_at, finished_at, kind, source, outcome, files, copied,
                          skipped, failed, bytes, usb, message, details
                   FROM import_history ORDER BY id DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read import history: %s", exc)
        return []
    return [
        {
            "id": r[0],
            "started_at": r[1],
            "finished_at": r[2],
            "duration_seconds": max(r[2] - r[1], 0.0),
            "kind": r[3],
            "source": r[4],
            "outcome": r[5],
            "files": r[6],
            "copied": r[7],
            "skipped": r[8],
            "failed": r[9],
            "bytes": r[10],
            "usb": bool(r[11]),
            "message": r[12],
            "details": _parse_details(r[13]),
        }
        for r in rows
    ]


def _parse_details(text: object) -> Dict[str, object]:
    try:
        value = json.loads(str(text or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def clear_import_history() -> int:
    """Delete all recorded runs; returns how many were removed."""
    try:
        with catalog_db() as db:
            return db.execute("DELETE FROM import_history").rowcount
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not clear import history: %s", exc)
        return 0


def mark_google_photos_uploaded_many(paths: Sequence[Path], remote: str, album: str) -> int:
    """Record cloud uploads for many files using a single connection/transaction."""
    now = time.time()
    rows = []
    for path in paths:
        try:
            fingerprint = fileops.file_fingerprint(path)
            rows.append(
                (
                    str(path.resolve()),
                    fingerprint.size,
                    fingerprint.mtime_ns,
                    fingerprint.inode,
                    now,
                    remote,
                    album,
                )
            )
        except OSError as exc:
            logger.debug("Skipping upload record for vanished file %s: %s", path, exc)
    if rows:
        with catalog_db() as db:
            db.executemany(
                """INSERT INTO google_photos_uploads
                   (path, size, mtime_ns, inode, uploaded_at, remote, album)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime_ns=excluded.mtime_ns, inode=excluded.inode,
                     uploaded_at=excluded.uploaded_at, remote=excluded.remote, album=excluded.album""",
                rows,
            )
    return len(rows)


def mark_google_photos_uploaded(path: Path, remote: str, album: str) -> None:
    """Record successful cloud upload for this exact local-file fingerprint."""
    mark_google_photos_uploaded_many([path], remote, album)


def google_photos_uploaded_fingerprints() -> Dict[str, Tuple[int, int, int]]:
    """Return {resolved path: (size, mtime_ns, inode)} for every recorded upload in one query."""
    try:
        with catalog_db() as db:
            rows = db.execute(
                "SELECT path, size, mtime_ns, inode FROM google_photos_uploads"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("Could not read Google Photos upload records: %s", exc)
        return {}
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def google_photos_upload_record(path: Path) -> Optional[dict]:
    """Return upload details only when the file still matches its uploaded fingerprint."""
    try:
        fingerprint = fileops.file_fingerprint(path)
        with catalog_db() as db:
            row = db.execute(
                "SELECT size, mtime_ns, inode, uploaded_at, remote, album "
                "FROM google_photos_uploads WHERE path = ?",
                (str(path.resolve()),),
            ).fetchone()
        if row and row[:3] == (fingerprint.size, fingerprint.mtime_ns, fingerprint.inode):
            return {"uploaded_at": row[3], "remote": row[4], "album": row[5]}
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read Google Photos upload record for %s: %s", path, exc)
    return None


ROW_COLUMNS = "size, mtime_ns, inode, sha256, capture_month, camera_model, media_folder"


def row_matches(row: Optional[tuple], fingerprint: fileops.FileFingerprint) -> bool:
    """Whether a catalog row (ROW_COLUMNS order) still describes the file on disk."""
    return (
        bool(row)
        and row[2] == fingerprint.inode
        and fileops.card_fingerprint_unchanged({"size": row[0], "mtime_ns": row[1]}, fingerprint)
    )


def catalog_row(
    path: Path, fingerprint: Optional[fileops.FileFingerprint] = None
) -> Optional[tuple]:
    """Return cached catalog metadata only when the filesystem fingerprint still matches."""
    try:
        fingerprint = fingerprint or fileops.file_fingerprint(path)
        with catalog_db() as db:
            row = db.execute(
                f"SELECT {ROW_COLUMNS} FROM file_locations WHERE path = ?", (str(path.resolve()),)
            ).fetchone()
        if row_matches(row, fingerprint):
            return row
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Catalog lookup failed for %s: %s", path, exc)
    return None


def catalog_rows_under(roots: Sequence[Path]) -> Dict[str, tuple]:
    """Every catalogued file under these archive roots, keyed by resolved path, in one query.

    Rows are returned as stored; check them against the file with row_matches().
    """
    resolved = [str(root.resolve()) for root in roots]
    try:
        with catalog_db() as db:
            rows = db.execute(
                f"SELECT path, {ROW_COLUMNS} FROM file_locations WHERE root IN ({','.join('?' * len(resolved))})",
                resolved,
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Bulk catalog lookup failed: %s", exc)
        return {}
    return {row[0]: tuple(row[1:]) for row in rows}


def _catalog_upsert(
    db: sqlite3.Connection,
    path: Path,
    sha256: Optional[str] = None,
    capture_month: Optional[str] = None,
    camera_model: Optional[str] = None,
    media_folder: Optional[str] = None,
) -> None:
    fingerprint = fileops.file_fingerprint(path)
    resolved = path.resolve()
    known_root = next(
        (
            candidate.resolve()
            for candidate in (
                config.LOCAL_DEST_ROOT,
                config.LOCAL_RAW_DEST_ROOT,
                config.USB_DEST_ROOT,
            )
            if resolved == candidate.resolve() or candidate.resolve() in resolved.parents
        ),
        None,
    )
    if known_root is not None:
        root = str(known_root)
        relative = str(resolved.relative_to(known_root))
    else:
        # Camera-card paths are still catalogued by their absolute location.
        root = str(resolved.anchor)
        relative = str(resolved).lstrip("/")
    old = db.execute(
        "SELECT size, mtime_ns, inode, sha256, capture_month, camera_model, media_folder "
        "FROM file_locations WHERE path = ?",
        (str(resolved),),
    ).fetchone()
    same = (
        bool(old)
        and old[2] == fingerprint.inode
        and fileops.card_fingerprint_unchanged({"size": old[0], "mtime_ns": old[1]}, fingerprint)
    )
    values = (
        str(resolved),
        root,
        relative,
        fingerprint.size,
        fingerprint.mtime_ns,
        fingerprint.inode,
        sha256 if sha256 is not None else (old[3] if same else None),
        capture_month if capture_month is not None else (old[4] if same else None),
        camera_model if camera_model is not None else (old[5] if same else None),
        media_folder if media_folder is not None else (old[6] if same else None),
        time.time(),
    )
    db.execute(
        """
        INSERT INTO file_locations
        (path, root, relative_path, size, mtime_ns, inode, sha256,
         capture_month, camera_model, media_folder, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            root=excluded.root, relative_path=excluded.relative_path,
            size=excluded.size, mtime_ns=excluded.mtime_ns, inode=excluded.inode,
            sha256=excluded.sha256, capture_month=excluded.capture_month,
            camera_model=excluded.camera_model, media_folder=excluded.media_folder,
            indexed_at=excluded.indexed_at
    """,
        values,
    )


def catalog_record(
    path: Path,
    *,
    sha256: Optional[str] = None,
    capture_month: Optional[str] = None,
    camera_model: Optional[str] = None,
    media_folder: Optional[str] = None,
) -> None:
    """Record a file location while preserving cached fields for an unchanged file."""
    try:
        with catalog_db() as db:
            _catalog_upsert(db, path, sha256, capture_month, camera_model, media_folder)
    except (OSError, sqlite3.Error) as exc:
        # Catalog is an optimization, never a reason to interrupt a safe import.
        logger.debug("Could not update file catalog for %s: %s", path, exc)


def catalog_record_many(entries: Sequence[Tuple[Path, Dict[str, Optional[str]]]]) -> int:
    """Record many files in one transaction; files that vanished are skipped."""
    written = 0
    try:
        with catalog_db() as db:
            for path, fields in entries:
                try:
                    _catalog_upsert(db, path, **fields)
                    written += 1
                except OSError as exc:
                    logger.debug("Could not catalog %s: %s", path, exc)
    except sqlite3.Error as exc:
        logger.debug("Could not update file catalog in bulk: %s", exc)
        return 0
    return written


def catalog_hash_matches(source: Path, destination: Path) -> Optional[bool]:
    """Use cached hashes only when both paths still match their indexed fingerprints."""
    source_row = catalog_row(source)
    destination_row = catalog_row(destination)
    if source_row and destination_row and source_row[3] and destination_row[3]:
        return source_row[3] == destination_row[3]
    return None
