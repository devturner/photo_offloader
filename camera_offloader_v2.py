#!/usr/bin/env python3
"""Camera Card Offloader v2.4.

Conservative macOS media ingestion:
    discover -> lock -> snapshot -> scan -> capacity check -> copy -> verify
    -> report -> eject

The importer never deletes source media and never overwrites an existing
same-name destination. It verifies the Mac SSD archive first, then ejects the
card when enabled and independently synchronizes missing files to USB.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    from alive_progress import alive_bar
    HAS_ALIVE_PROGRESS = True
except ImportError:  # pragma: no cover
    HAS_ALIVE_PROGRESS = False

    class _NullProgress:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_value, traceback): return False
        def __call__(self, *args, **kwargs): return None

    def alive_bar(*args, **kwargs):
        return _NullProgress()

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]
LOCAL_DEST_ROOT = Path.home() / "Pictures" / "CameraImports"
LOCAL_RAW_DEST_ROOT = Path.home() / "Pictures" / "CameraImportsRaw"
USB_DEST_VOLUME = Path("/Volumes/Photos")
USB_DEST_ROOT = USB_DEST_VOLUME / "CameraImports"
USB_RAW_DEST_ROOT = USB_DEST_VOLUME / "CameraImportsRaw"
LOG_FILE_PATH = Path.home() / ".camera_transfer.log"
SESSION_LOCK_PATH = Path.home() / "Library" / "Caches" / "PhotoOffloader" / "import.lock"
WAIT_SECONDS = 30
EJECT_CARD_AFTER_SUCCESS = True
VERIFY_WITH_SHA256 = False
USE_FILE_BIRTH_TIME = True
MIN_FREE_SPACE_BUFFER_BYTES = 100 * 1024 * 1024
STATUS_FILE_PATH = Path.home() / "Library" / "Application Support" / "PhotoOffloader" / "status.json"
CATALOG_DB_PATH = Path.home() / "Library" / "Application Support" / "PhotoOffloaderData" / "catalog.sqlite3"
LEGACY_CATALOG_DB_PATH = Path.home() / "Library" / "Application Support" / "PhotoOffloader" / "catalog.sqlite3"
BUILD_INFO_PATH = Path(__file__).resolve().with_name("build_info.json")
VERSION_PATH = Path(__file__).resolve().with_name("VERSION")
_PROGRESS = {"state": "idle", "bytes_done": 0, "bytes_total": 0, "bytes_transferred": 0}
_PROGRESS_STARTED: Optional[float] = None


def update_status(**updates: object) -> None:
    """Atomically publish importer progress for the menu-bar dashboard."""
    global _PROGRESS_STARTED
    if _PROGRESS_STARTED is None:
        _PROGRESS_STARTED = time.monotonic()
    _PROGRESS.update(updates)
    elapsed = max(time.monotonic() - _PROGRESS_STARTED, 0.001)
    done = int(_PROGRESS.get("bytes_done", 0))
    total = int(_PROGRESS.get("bytes_total", 0))
    speed = done / elapsed
    transferred = int(_PROGRESS.get("bytes_transferred", 0))
    transfer_speed = transferred / elapsed
    remaining = max(total - done, 0)
    status = dict(_PROGRESS)
    status.update({
        "speed_bps": speed,
        "transfer_speed_bps": transfer_speed,
        "eta_seconds": remaining / speed if speed > 0 and total > done else 0,
        "elapsed_seconds": elapsed,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        STATUS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = STATUS_FILE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, STATUS_FILE_PATH)
    except OSError as exc:
        logger.debug("Could not publish dashboard status: %s", exc)


def add_progress_bytes(amount: int, transferred: bool = False) -> None:
    _PROGRESS["bytes_done"] = int(_PROGRESS.get("bytes_done", 0)) + amount
    if transferred:
        _PROGRESS["bytes_transferred"] = int(_PROGRESS.get("bytes_transferred", 0)) + amount
    update_status()

MEDIA_EXTENSIONS = {
    ".jpg": "JPG", ".jpeg": "JPG", ".heic": "JPG", ".heif": "JPG",
    ".nef": "RAW", ".arw": "RAW", ".cr2": "RAW", ".cr3": "RAW",
    ".dng": "RAW", ".raf": "RAW", ".orf": "RAW", ".rw2": "RAW",
    ".mov": "VIDEO", ".mp4": "VIDEO", ".avi": "VIDEO",
    ".mts": "VIDEO", ".m2ts": "VIDEO",
}

logger = logging.getLogger("camera_offloader")


@dataclass
class ImportStats:
    found: int = 0
    source_bytes: int = 0
    destination_copies: int = 0
    destination_skips: int = 0
    verified_copies: int = 0
    failed: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def copied(self) -> int: return self.destination_copies

    @property
    def skipped(self) -> int: return self.destination_skips

    @property
    def verified(self) -> int: return self.verified_copies

    @property
    def successful(self) -> bool: return self.failed == 0 and not self.errors


@dataclass
class DestinationResult:
    destination: Path
    status: str  # copied, skipped, failed
    path: Optional[Path] = None
    message: str = ""


@dataclass(frozen=True)
class VolumeIdentity:
    mount_point: Path
    device_identifier: Optional[str]
    volume_uuid: Optional[str]


@dataclass(frozen=True)
class DestinationSnapshot:
    destinations: Tuple[Path, ...]
    usb_expected: bool
    usb_identity: Optional[VolumeIdentity]


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    inode: int


_CATALOG_INITIALIZED_PATH: Optional[str] = None


def catalog_connection() -> sqlite3.Connection:
    """Open the persistent media/location catalog, initializing its schema once per path."""
    global _CATALOG_INITIALIZED_PATH
    CATALOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CATALOG_DB_PATH.exists() and LEGACY_CATALOG_DB_PATH.is_file():
        try:
            shutil.copy2(LEGACY_CATALOG_DB_PATH, CATALOG_DB_PATH)
            logger.info("Migrated legacy file catalog to persistent data location: %s", CATALOG_DB_PATH)
        except OSError as exc:
            logger.warning("Could not migrate legacy catalog; using the existing catalog location: %s", exc)
            return sqlite3.connect(LEGACY_CATALOG_DB_PATH, timeout=10)
    connection = sqlite3.connect(CATALOG_DB_PATH, timeout=10)
    catalog_path = str(CATALOG_DB_PATH.resolve())
    if _CATALOG_INITIALIZED_PATH != catalog_path:
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
            CREATE TABLE IF NOT EXISTS catalog_refreshes (
                root TEXT PRIMARY KEY,
                last_updated REAL NOT NULL,
                files_scanned INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_file_locations_root ON file_locations(root)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_file_locations_relative ON file_locations(root, relative_path)")
        connection.commit()
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


def catalog_row(path: Path, fingerprint: Optional[FileFingerprint] = None) -> Optional[tuple]:
    """Return cached catalog metadata only when the filesystem fingerprint still matches."""
    try:
        fingerprint = fingerprint or file_fingerprint(path)
        with catalog_db() as db:
            row = db.execute(
                "SELECT size, mtime_ns, inode, sha256, capture_month, camera_model, media_folder "
                "FROM file_locations WHERE path = ?", (str(path.resolve()),)
            ).fetchone()
        if row and row[:3] == (fingerprint.size, fingerprint.mtime_ns, fingerprint.inode):
            return row
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Catalog lookup failed for %s: %s", path, exc)
    return None


def catalog_record(path: Path, *, sha256: Optional[str] = None,
                   capture_month: Optional[str] = None,
                   camera_model: Optional[str] = None,
                   media_folder: Optional[str] = None) -> None:
    """Record a file location while preserving cached fields for an unchanged file."""
    try:
        fingerprint = file_fingerprint(path)
        resolved = path.resolve()
        known_root = next(
            (candidate.resolve() for candidate in (LOCAL_DEST_ROOT, LOCAL_RAW_DEST_ROOT, USB_DEST_ROOT, USB_RAW_DEST_ROOT)
             if resolved == candidate.resolve() or candidate.resolve() in resolved.parents),
            None,
        )
        if known_root is not None:
            root = str(known_root)
            relative = str(resolved.relative_to(known_root))
        else:
            # Camera-card paths are still catalogued by their absolute location.
            root = str(resolved.anchor)
            relative = str(resolved).lstrip("/")
        with catalog_db() as db:
            old = db.execute(
                "SELECT size, mtime_ns, inode, sha256, capture_month, camera_model, media_folder "
                "FROM file_locations WHERE path = ?", (str(resolved),)
            ).fetchone()
            same = old and old[:3] == (fingerprint.size, fingerprint.mtime_ns, fingerprint.inode)
            values = (
                str(resolved), root, relative, fingerprint.size, fingerprint.mtime_ns,
                fingerprint.inode,
                sha256 if sha256 is not None else (old[3] if same else None),
                capture_month if capture_month is not None else (old[4] if same else None),
                camera_model if camera_model is not None else (old[5] if same else None),
                media_folder if media_folder is not None else (old[6] if same else None),
                time.time(),
            )
            db.execute("""
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
            """, values)
    except (OSError, sqlite3.Error) as exc:
        # Catalog is an optimization, never a reason to interrupt a safe import.
        logger.debug("Could not update file catalog for %s: %s", path, exc)


def catalog_hash_matches(source: Path, destination: Path) -> Optional[bool]:
    """Use cached hashes only when both paths still match their indexed fingerprints."""
    source_row = catalog_row(source)
    destination_row = catalog_row(destination)
    if source_row and destination_row and source_row[3] and destination_row[3]:
        return source_row[3] == destination_row[3]
    return None

CARD_MANIFEST_DIR = ".photo-offloader"
CARD_MANIFEST_NAME = "manifest-v1.json"


def card_manifest_path(dcim_folder: Path) -> Path:
    """Return the optional sidecar manifest location at the card volume root."""
    return dcim_folder.resolve().parent / CARD_MANIFEST_DIR / CARD_MANIFEST_NAME


def load_card_manifest(dcim_folder: Path) -> Dict[str, dict]:
    """Read a card-side hash cache; corrupt or unavailable manifests are ignored."""
    path = card_manifest_path(dcim_folder)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") == 1 and isinstance(payload.get("files"), dict):
            return payload["files"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def save_card_manifest(dcim_folder: Path, files: Dict[str, dict]) -> bool:
    """Atomically persist optional card metadata without making import depend on writes."""
    path = card_manifest_path(dcim_folder)
    temp_path: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "files": files}, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.info("Card-side cache is unavailable; continuing with Mac catalog: %s", exc)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def card_manifest_entry(
    manifest: Dict[str, dict], source: Path, dcim_folder: Path,
) -> Optional[str]:
    """Return a card-cached hash only when relative path, size, and mtime still match."""
    try:
        relative = source.resolve().relative_to(dcim_folder.resolve()).as_posix()
        entry = manifest.get(relative)
        fingerprint = file_fingerprint(source)
        if (isinstance(entry, dict) and entry.get("size") == fingerprint.size
                and entry.get("mtime_ns") == fingerprint.mtime_ns
                and isinstance(entry.get("sha256"), str)):
            return entry["sha256"]
    except (OSError, ValueError):
        pass
    return None


def update_card_manifest_entry(
    manifest: Dict[str, dict], source: Path, dcim_folder: Path, digest: str,
) -> None:
    """Cache a verified source hash keyed by card-relative path and stable fingerprint."""
    try:
        relative = source.resolve().relative_to(dcim_folder.resolve()).as_posix()
        fingerprint = file_fingerprint(source)
        manifest[relative] = {
            "size": fingerprint.size,
            "mtime_ns": fingerprint.mtime_ns,
            "sha256": digest,
        }
    except (OSError, ValueError):
        pass


def hash_for_copy_decision(
    source: Path, destination: Path, dcim_folder: Path, manifest: Dict[str, dict],
) -> Tuple[str, str]:
    """Reuse trustworthy cached hashes and avoid database writes on cache hits."""
    # The card manifest is the cheapest source of truth. Only query SQLite when
    # the card-side fingerprint is absent or stale.
    source_hash = card_manifest_entry(manifest, source, dcim_folder)
    source_hash_was_cached = source_hash is not None
    source_row = None
    if source_hash is None:
        source_row = catalog_row(source)
        if source_row and source_row[3]:
            source_hash = str(source_row[3])
            source_hash_was_cached = True

    # Destination hashes are reusable only when its indexed fingerprint still
    # matches the current file. A missing/stale row requires a content hash.
    destination_row = catalog_row(destination)
    destination_hash = (
        str(destination_row[3])
        if destination_row and destination_row[3]
        else None
    )
    if source_hash is None:
        source_hash = sha256_file(source)
    if destination_hash is None:
        destination_hash = sha256_file(destination)

    # Persist hashes only when we had to establish them; do not open and commit
    # two more SQLite transactions for every unchanged duplicate on every insert.
    if not source_hash_was_cached:
        catalog_record(source, sha256=source_hash)
    if not destination_row or not destination_row[3]:
        catalog_record(destination, sha256=destination_hash)
    update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
    return source_hash, destination_hash


def load_build_info() -> Dict[str, str]:
    """Identify the exact source/build installed when this process was launched."""
    info: Dict[str, str] = {}
    try:
        loaded = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            info.update({str(key): str(value) for key, value in loaded.items()})
    except (OSError, ValueError):
        pass

    if not info.get("version"):
        try:
            info["version"] = VERSION_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            info["version"] = "unknown"

    # Source checkouts get useful provenance even when run without install.sh.
    if not info.get("git_commit") or not info.get("git_branch"):
        source_dir = Path(__file__).resolve().parent
        def git_value(*args: str) -> str:
            try:
                result = subprocess.run(
                    ["git", "-C", str(source_dir), *args],
                    check=False, capture_output=True, text=True, timeout=2,
                )
                return result.stdout.strip() if result.returncode == 0 else "unknown"
            except (OSError, subprocess.SubprocessError):
                return "unknown"

        info.setdefault("git_commit", git_value("rev-parse", "HEAD"))
        info.setdefault("git_branch", git_value("branch", "--show-current"))
        info.setdefault("git_tag", git_value("describe", "--tags", "--always"))
        if "git_dirty" not in info:
            info["git_dirty"] = str(bool(git_value("status", "--porcelain") not in ("", "unknown")))

    info.setdefault("build_time_utc", "unknown")
    info.setdefault("source_path", str(Path(__file__).resolve()))
    return info


def configure_logging(verbose: bool = False) -> None:
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    if not HAS_ALIVE_PROGRESS:
        logger.warning("alive-progress is not installed; progress output is disabled.")


def notify(title: str, text: str) -> None:
    script = """
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, text],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning("Notification failed: %s", result.stderr.strip())
    except OSError as exc:
        logger.warning("Could not show notification: %s", exc)


@contextmanager
def import_session_lock() -> Iterator[None]:
    SESSION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = SESSION_LOCK_PATH.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Photo Offloader import session is already running.") from exc
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            try:
                SESSION_LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass


def find_card_dcim() -> Path:
    matches: List[Path] = []
    for pattern in CARD_VOLUME_GLOBS:
        for raw in glob.glob(pattern):
            path = Path(raw)
            if path.is_dir():
                matches.append(path)
    matches = sorted(set(matches), key=lambda p: str(p).lower())
    default = str(matches[0].parent) if len(matches) == 1 else ""
    prompt = "Source card volume or DCIM folder"
    if default:
        prompt += f" [{default}]"
    entered = input(prompt + ": ").strip()
    if entered:
        return resolve_dcim_path(Path(entered).expanduser())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print("\nMultiple matching cards found:")
        for i, path in enumerate(matches, 1):
            print(f"  {i}. {path.parent}")
        try:
            return matches[int(input("Enter the number of the source card: ")) - 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Invalid card selection.") from exc
    raise RuntimeError("No recognized card was found.")


def resolve_dcim_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.name.lower() == "dcim" and path.is_dir():
        return path
    dcim = path / "DCIM"
    if dcim.is_dir():
        return dcim
    raise RuntimeError(f"DCIM folder not found at {dcim if path.name.lower() != 'dcim' else path}")


def find_media_files(dcim_folder: Path) -> Dict[str, List[Path]]:
    files = {"JPG": [], "RAW": [], "VIDEO": []}
    for path in dcim_folder.rglob("*"):
        try:
            if path.name.startswith(".") or not path.is_file() or path.is_symlink():
                continue
            category = MEDIA_EXTENSIONS.get(path.suffix.lower())
            if category:
                files[category].append(path)
        except OSError as exc:
            logger.error("Could not inspect %s: %s", path, exc)
    for category in files:
        files[category].sort(key=lambda p: str(p).lower())
    return files


def scan_with_retry(dcim_folder: Path) -> Dict[str, List[Path]]:
    files = find_media_files(dcim_folder)
    if any(files.values()):
        return files
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        logger.info("No matching files visible yet; checking again...")
        time.sleep(2)
        files = find_media_files(dcim_folder)
        if any(files.values()):
            break
    return files


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> FileFingerprint:
    info = path.stat()
    return FileFingerprint(info.st_size, info.st_mtime_ns, info.st_ino)


def files_match(source: Path, destination: Path, force_sha256: bool = False) -> bool:
    try:
        if source.stat().st_size != destination.stat().st_size:
            return False
        if VERIFY_WITH_SHA256 or force_sha256:
            return sha256_file(source) == sha256_file(destination)
        return True
    except OSError:
        return False


def install_without_overwrite(temp_path: Path, final_path: Path) -> bool:
    try:
        os.link(temp_path, final_path)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        unsupported = {errno.EXDEV, errno.EPERM, errno.EACCES,
                       getattr(errno, "EOPNOTSUPP", -1), getattr(errno, "ENOTSUP", -1)}
        if exc.errno not in unsupported:
            raise
    fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as output, temp_path.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _parse_exif_datetime_value(value: str) -> Optional[datetime]:
    text = value.strip().replace("Z", "+00:00").replace("/", "-")
    normalized = re.sub(
        r"^(\d{4})[-:](\d{2})[-:](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        r"\1-\2-\3 \4:\5:\6", text,
    )
    for candidate in (text, normalized):
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                     "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                     "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    return None


RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dcr", ".dng", ".erf", ".fff",
    ".gpr", ".iiq", ".k25", ".kdc", ".mef", ".mos", ".mrw", ".nef",
    ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2",
    ".srf", ".srw", ".x3f",
}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts"}


def get_exiftool_metadata_batch(paths: List[Path], tags: List[str], batch_size: int = 100) -> dict:
    """Read selected tags for many files per ExifTool process, keyed by absolute path."""
    exiftool = find_exiftool()
    if not exiftool or not paths:
        return {}
    results = {}
    total_batches = (len(paths) + batch_size - 1) // batch_size
    started = time.monotonic()
    logger.info(
        "ExifTool metadata phase started: %d uncached files in %d batches (batch size %d).",
        len(paths), total_batches, batch_size,
    )
    for offset in range(0, len(paths), batch_size):
        batch = paths[offset:offset + batch_size]
        batch_number = offset // batch_size + 1
        batch_started = time.monotonic()
        logger.info(
            "ExifTool heartbeat: starting batch %d/%d (%d files; %d/%d submitted).",
            batch_number, total_batches, len(batch), offset, len(paths),
        )
        try:
            result = subprocess.run(
                [exiftool, "-j", "-charset", "filename=UTF8", *tags, *map(str, batch)],
                check=False, capture_output=True, text=True, timeout=max(15, len(batch) // 2),
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.warning("ExifTool batch %d/%d returned no metadata after %.1fs: %s",
                               batch_number, total_batches, time.monotonic() - batch_started,
                               result.stderr.strip())
                continue
            records = json.loads(result.stdout)
            batch_results = 0
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict) or not record.get("SourceFile"):
                    continue
                results[str(Path(record["SourceFile"]).resolve())] = record
                batch_results += 1
            logger.info(
                "ExifTool heartbeat: finished batch %d/%d; metadata records %d/%d; "
                "%.1fs this batch, %.1fs total.",
                batch_number, total_batches, min(offset + len(batch), len(paths)), len(paths),
                time.monotonic() - batch_started, time.monotonic() - started,
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            logger.warning("ExifTool batch %d/%d failed after %.1fs: %s",
                           batch_number, total_batches, time.monotonic() - batch_started, exc)
    logger.info("ExifTool metadata phase complete: %d/%d records in %.1fs.",
                len(results), len(paths), time.monotonic() - started)
    return results


def get_exiftool_metadata(path: Path, tags: List[str]) -> dict:
    """Read selected metadata with ExifTool for formats Pillow cannot decode."""
    exiftool = find_exiftool()
    if not exiftool:
        logger.debug("exiftool is unavailable; metadata fallback skipped for %s", path)
        return {}
    try:
        result = subprocess.run(
            [exiftool, "-j", *tags, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug("ExifTool returned no metadata for %s: %s", path, result.stderr.strip())
            return {}
        metadata = json.loads(result.stdout)
        return metadata[0] if metadata and isinstance(metadata[0], dict) else {}
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not read ExifTool metadata for %s: %s", path, exc)
        return {}


def get_exif_datetime(path: Path) -> Optional[datetime]:
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return None

    # Pillow often cannot identify proprietary camera RAW formats such as Nikon NEF.
    if path.suffix.lower() in RAW_EXTENSIONS:
        metadata = get_exiftool_metadata(
            path, ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate"]
        )
        for tag in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
            value = metadata.get(tag)
            if isinstance(value, str):
                parsed = _parse_exif_datetime_value(value)
                if parsed:
                    return parsed
        return None

    if Image is not None:
        try:
            with Image.open(path) as image:
                exif = image.getexif()
                for tag in (36867, 36868, 306):
                    value = exif.get(tag) if exif else None
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="ignore")
                    if isinstance(value, str):
                        parsed = _parse_exif_datetime_value(value)
                        if parsed:
                            return parsed
        except Exception as exc:  # best effort
            logger.debug("Could not read EXIF date for %s: %s", path, exc)

    # Fallback is useful for formats that Pillow cannot open despite having EXIF.
    metadata = get_exiftool_metadata(
        path, ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate"]
    )
    for tag in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
        value = metadata.get(tag)
        if isinstance(value, str):
            parsed = _parse_exif_datetime_value(value)
            if parsed:
                return parsed
    return None


def normalize_camera_model(make: object, model: object) -> str:
    """Return a stable, filesystem-safe camera identifier from EXIF values."""
    def clean(value: object) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if not isinstance(value, str):
            return ""
        return re.sub(r"\\s+", " ", value).strip()

    make_text = clean(make)
    model_text = clean(model)
    make_prefix = re.sub(r"\s+(?:corporation|corp\.?|inc\.?|co\.?)$", "", make_text, flags=re.IGNORECASE)
    if make_text and model_text:
        combined = (
            model_text
            if model_text.lower().startswith(make_prefix.lower())
            else f"{make_prefix} {model_text}"
        )
    else:
        combined = model_text or make_prefix or make_text
    combined = re.sub(r"[^A-Za-z0-9._-]+", "-", combined)
    combined = re.sub(r"-+", "-", combined).strip(".-_")
    return combined or "Unknown-Camera"


@lru_cache(maxsize=1)
def find_exiftool() -> Optional[str]:
    """Find exiftool once per importer process, even with launchd’s minimal PATH."""
    exiftool = shutil.which("exiftool")
    if exiftool:
        return exiftool

    # launchd jobs often do not inherit Homebrew's PATH entries.
    for candidate in (
        Path("/opt/homebrew/bin/exiftool"),
        Path("/usr/local/bin/exiftool"),
        Path("/opt/local/bin/exiftool"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            logger.debug("Found exiftool at its standard install path: %s", candidate)
            return str(candidate)
    return None


def get_camera_model_from_exiftool(path: Path) -> str:
    """Read camera make/model from video or camera RAW metadata."""
    if path.suffix.lower() not in VIDEO_EXTENSIONS and path.suffix.lower() not in RAW_EXTENSIONS:
        return "Unknown-Camera"
    metadata = get_exiftool_metadata(path, ["-Make", "-Model"])
    return normalize_camera_model(metadata.get("Make"), metadata.get("Model"))


def get_camera_model(path: Path) -> str:
    """Read camera make/model once per unchanged file and cache it in SQLite."""
    cached = catalog_row(path)
    if cached and cached[5]:
        return str(cached[5])
    suffix = path.suffix.lower()
    model = ""
    if suffix in VIDEO_EXTENSIONS or suffix in RAW_EXTENSIONS:
        model = get_camera_model_from_exiftool(path)
    else:
        if Image is not None:
            try:
                with Image.open(path) as image:
                    exif = image.getexif()
                    if exif:
                        model = normalize_camera_model(exif.get(271), exif.get(272))
            except Exception as exc:  # best effort
                logger.debug("Could not read camera EXIF for %s: %s", path, exc)
        if not model or model == "Unknown-Camera":
            metadata = get_exiftool_metadata(path, ["-Make", "-Model"])
            model = normalize_camera_model(metadata.get("Make"), metadata.get("Model"))
    catalog_record(path, camera_model=model or "Unknown-Camera")
    return model or "Unknown-Camera"


def get_month_folder(source: Path) -> str:
    cached = catalog_row(source)
    if cached and cached[4]:
        return str(cached[4])
    timestamp = get_exif_datetime(source)
    if timestamp is None:
        info = source.stat()
        birth = getattr(info, "st_birthtime", None) if USE_FILE_BIRTH_TIME else None
        timestamp = datetime.fromtimestamp(birth if birth is not None else info.st_mtime)
    month = timestamp.strftime("%Y-%m")
    catalog_record(source, capture_month=month)
    return month


def get_media_type_folder(source: Path) -> str:
    """Map a supported media extension to its human-friendly destination folder."""
    category = MEDIA_EXTENSIONS.get(source.suffix.lower())
    return {"JPG": "Photos", "RAW": "Raws", "VIDEO": "Movies"}.get(category, "Other")


def local_root_for_category(category: str) -> Path:
    """Choose the top-level local archive by media category."""
    return LOCAL_RAW_DEST_ROOT if category == "RAW" else LOCAL_DEST_ROOT


def usb_root_for_category(category: str) -> Path:
    """Choose the matching top-level USB archive by media category."""
    return USB_RAW_DEST_ROOT if category == "RAW" else USB_DEST_ROOT


def build_destination_path(source: Path, dcim_folder: Path, destination_base: Path) -> Path:
    return (
        destination_base
        / get_month_folder(source)
        / get_camera_model(source)
        / get_media_type_folder(source)
        / source.name
    )


def copy_file_with_progress(source: Path, destination: Path) -> None:
    """Copy in chunks so progress and ETA update even for large video files."""
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while True:
            chunk = input_file.read(1024 * 1024)
            if not chunk:
                break
            output_file.write(chunk)
            add_progress_bytes(len(chunk), transferred=True)
        output_file.flush()
        os.fsync(output_file.fileno())
    shutil.copystat(source, destination, follow_symlinks=True)


def copy_one_to_destination(
    source: Path, dcim_folder: Path, destination_base: Path,
    card_manifest: Optional[Dict[str, dict]] = None,
) -> DestinationResult:
    before = file_fingerprint(source)
    manifest = card_manifest if card_manifest is not None else load_card_manifest(dcim_folder)
    update_status(state="scanning", phase="copy-decision", current_file=source.name,
                  current_path=str(source), message=f"Checking existing file: {source.name}",
                  decision_files_checked=int(_PROGRESS.get("decision_files_checked", 0)),
                  decision_files_total=int(_PROGRESS.get("decision_files_total", 0)))
    final_path = build_destination_path(source, dcim_folder, destination_base)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists() and final_path.is_file():
        try:
            source_hash, destination_hash = hash_for_copy_decision(
                source, final_path, dcim_folder, manifest
            )
            if source_hash == destination_hash:
                add_progress_bytes(before.size)
                update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
                update_status(state="copying", phase="copy", message="Existing file verified; continuing")
                return DestinationResult(destination_base, "skipped", final_path,
                                         "already exists and matches cached/verified hash")
        except OSError as exc:
            logger.warning("Could not verify existing file %s: %s", final_path, exc)
        stem, suffix = final_path.stem, final_path.suffix
        counter = 1
        while True:
            candidate = final_path.with_name(f"{stem}__conflict-{counter}{suffix}")
            if not candidate.exists():
                final_path = candidate
                break
            counter += 1
        logger.warning("Filename conflict for %s; using %s", source, final_path.name)

    update_status(state="copying", phase="copy", current_file=source.name,
                  current_path=str(source), message=f"Copying {source.name}")
    temp_path: Optional[Path] = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".photo-copy-", suffix=".tmp", dir=final_path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        copy_file_with_progress(source, temp_path)
        after_copy = file_fingerprint(source)
        if after_copy != before:
            raise IOError(f"Source changed during copy: {source}")
        if not files_match(source, temp_path):
            raise IOError(f"Verification failed for temporary copy: {source}")
        if not install_without_overwrite(temp_path, final_path):
            if files_match(source, final_path, force_sha256=True):
                return DestinationResult(destination_base, "skipped", final_path,
                                         "another process created a matching file")
            raise FileExistsError(f"Destination appeared during copy and does not match source: {final_path}")
        if not files_match(source, final_path):
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove failed destination %s", final_path)
            raise IOError(f"Final verification failed: {final_path}")

        # Camera media can arrive with owner-only or executable mode bits
        # (for example, 0700). Do not propagate those source permissions to
        # imported photos/videos. Imported media should be regular user files,
        # not executables, while remaining private to the importing user.
        os.chmod(final_path, stat.S_IRUSR | stat.S_IWUSR)
        # Persist content hashes after a successful copy so later card insertions
        # can compare cached source/destination hashes without rereading large RAWs.
        source_hash = sha256_file(source)
        destination_hash = sha256_file(final_path)
        if source_hash == destination_hash:
            catalog_record(source, sha256=source_hash)
            catalog_record(final_path, sha256=destination_hash)
            update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
        else:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove hash-mismatched destination %s", final_path)
            raise IOError(f"SHA-256 verification failed after copy: {source}")
        update_status(state="copying", phase="copy", message="Copy verified")
        return DestinationResult(destination_base, "copied", final_path, "copied and verified")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary file %s: %s", temp_path, exc)


def usb_is_mounted() -> bool:
    return USB_DEST_VOLUME.is_dir() and USB_DEST_VOLUME.is_mount()


def get_volume_identity(volume_path: Path) -> Optional[VolumeIdentity]:
    if not volume_path.is_dir():
        return None
    try:
        result = subprocess.run(["diskutil", "info", "-plist", str(volume_path)],
                                check=False, capture_output=True)
        if result.returncode != 0:
            return None
        info = plistlib.loads(result.stdout)
        return VolumeIdentity(volume_path.resolve(), info.get("DeviceIdentifier"), info.get("VolumeUUID"))
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def volume_identity_matches(expected: Optional[VolumeIdentity], current: Optional[VolumeIdentity]) -> bool:
    if expected is None:
        return True
    if current is None or expected.mount_point != current.mount_point:
        return False
    if expected.volume_uuid and current.volume_uuid:
        return expected.volume_uuid == current.volume_uuid
    if expected.device_identifier and current.device_identifier:
        return expected.device_identifier == current.device_identifier
    return expected.mount_point == current.mount_point


def snapshot_destinations(no_usb: bool = False) -> DestinationSnapshot:
    if no_usb or not usb_is_mounted():
        return DestinationSnapshot((LOCAL_DEST_ROOT,), False, None)
    identity = get_volume_identity(USB_DEST_VOLUME)
    return DestinationSnapshot((LOCAL_DEST_ROOT, USB_DEST_ROOT), True, identity)


def build_destinations() -> List[Path]:
    return list(snapshot_destinations().destinations)


def required_source_bytes(files: Dict[str, List[Path]]) -> int:
    total = 0
    for items in files.values():
        for path in items:
            total += path.stat().st_size
    return total


def available_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def validate_destinations(destinations: Sequence[Path], required_bytes: int = 0) -> None:
    for destination in destinations:
        try:
            destination.mkdir(parents=True, exist_ok=True)
            probe = destination / ".camera-offloader-write-test"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
            if required_bytes:
                free = available_bytes(destination)
                if free < required_bytes + MIN_FREE_SPACE_BUFFER_BYTES:
                    raise RuntimeError(
                        f"Insufficient free space at {destination}: {free} bytes available, "
                        f"{required_bytes} bytes required plus safety buffer."
                    )
        except OSError as exc:
            raise RuntimeError(f"Destination is not writable: {destination}: {exc}") from exc


def copy_category(files: Sequence[Path], dcim_folder: Path, destinations: Sequence[Path],
                  label: str, stats: ImportStats) -> None:
    if not files:
        return
    logger.info("%s: processing %d file(s)", label, len(files))
    update_status(state="scanning", phase="copy-decision", category=label,
                  decision_files_checked=0, decision_files_total=len(files),
                  message=f"Checking {len(files)} {label} file(s)")
    card_manifest = load_card_manifest(dcim_folder)
    with alive_bar(len(files), title=label, bar="filling", monitor=True, force_tty=False) as bar:
        for file_index, source in enumerate(files, start=1):
            update_status(state="scanning", phase="copy-decision", current_file=source.name,
                          current_path=str(source), category=label,
                          decision_files_checked=file_index - 1,
                          decision_files_total=len(files),
                          message=f"Checking existing files ({file_index}/{len(files)}): {source.name}")
            results: List[DestinationResult] = []
            for destination in destinations:
                try:
                    result = copy_one_to_destination(source, dcim_folder, destination, card_manifest)
                    results.append(result)
                    if result.status == "copied":
                        stats.destination_copies += 1
                        stats.verified_copies += 1
                    elif result.status == "skipped":
                        stats.destination_skips += 1
                        stats.verified_copies += 1
                    else:
                        stats.failed += 1
                        stats.errors.append((str(source), result.message or "copy failed"))
                except Exception as exc:
                    stats.failed += 1
                    message = f"{source} -> {destination}: {exc}"
                    stats.errors.append((str(source), message))
                    logger.exception(message)
            if len(results) != len(destinations):
                logger.error("One or more destinations failed for %s", source)
            update_status(state="scanning", phase="copy-decision",
                          decision_files_checked=file_index,
                          decision_files_total=len(files),
                          message=f"Checked {file_index} of {len(files)} {label} files")
            bar()
    save_card_manifest(dcim_folder, card_manifest)


def update_file_catalog() -> Dict[str, int]:
    """Index the local archive and mounted USB archive, refreshing cached metadata.

    This operation does not hash every file: hashes already verified by copy/backup
    operations are preserved for unchanged files, while new/changed files are
    indexed without a hash until a verification operation computes one.
    """
    roots: List[Path] = []
    for local_root in (LOCAL_DEST_ROOT, LOCAL_RAW_DEST_ROOT):
        if local_root.is_dir():
            roots.append(local_root)
        else:
            logger.info("Local archive root is not available for catalog update: %s", local_root)
    if usb_is_mounted():
        for usb_root in (USB_DEST_ROOT, USB_RAW_DEST_ROOT):
            if usb_root.is_dir():
                roots.append(usb_root)
    else:
        logger.info("USB volume is not mounted; skipping USB catalog refresh.")

    if not roots:
        raise RuntimeError("No archive locations are available to update the catalog.")

    totals = {"scanned": 0, "metadata_refreshed": 0, "roots": len(roots)}
    # Use the last successful refresh counts as an initial total estimate. These
    # counts are advisory only: roots can gain or lose files between refreshes.
    previous_counts: Dict[str, int] = {}
    try:
        with catalog_db() as db:
            previous_counts = {
                str(row[0]): int(row[1])
                for row in db.execute(
                    "SELECT root, files_scanned FROM catalog_refreshes WHERE status = 'complete'"
                )
            }
    except sqlite3.Error as exc:
        logger.debug("Could not read previous catalog counts for ETA estimate: %s", exc)
    estimated_total = sum(previous_counts.get(str(root.resolve()), 0) for root in roots)
    update_status(state="scanning", phase="catalog-update", catalog_stage="Preparing metadata",
                  catalog_scanned=0, catalog_total_scanned=0, catalog_estimated_total=estimated_total,
                  catalog_candidates=0, catalog_root="", catalog_root_index=0,
                  catalog_roots_total=len(roots),
                  source="; ".join(map(str, roots)), message="Preparing catalog metadata batches",
                  current_file="", current_path="")
    logger.info("Starting catalog update for %d root(s).", len(roots))

    for root_index, root in enumerate(roots, start=1):
        root = root.resolve()
        update_status(catalog_root=str(root), catalog_root_index=root_index,
                      catalog_roots_total=len(roots), catalog_scanned=0,
                      message=f"Scanning location {root_index} of {len(roots)}: {root}")
        logger.info("Scanning catalog root %d/%d: %s", root_index, len(roots), root)
        seen: List[str] = []
        root_scanned = 0
        candidates = []
        discovered = 0
        for candidate in root.rglob("*"):
            try:
                if (candidate.is_file() and not candidate.is_symlink()
                        and not any(part.startswith(".") for part in candidate.relative_to(root).parts)
                        and candidate.suffix.lower() in MEDIA_EXTENSIONS):
                    discovered += 1
                    fingerprint = file_fingerprint(candidate)
                    cached = catalog_row(candidate, fingerprint)
                    if not (cached and cached[4] and cached[5] and cached[6]):
                        candidates.append(candidate)
                    if discovered % 100 == 0:
                        update_status(catalog_stage="Finding files needing metadata",
                                      catalog_root=str(root), catalog_root_index=root_index,
                                      catalog_roots_total=len(roots),
                                      catalog_scanned=totals["scanned"],
                                      catalog_total_scanned=totals["scanned"],
                                      catalog_discovered=discovered,
                                      catalog_candidates=len(candidates),
                                      catalog_estimated_total=max(
                                          estimated_total,
                                          totals["scanned"] + discovered
                                          + sum(previous_counts.get(str(item.resolve()), 0)
                                                for item in roots[root_index:])
                                      ),
                                      current_file=candidate.name, current_path=str(candidate),
                                      message=f"Preparing metadata batch • {discovered:,} media files checked")
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Could not inspect catalog candidate %s: %s", candidate, exc)

        estimated_total = max(
            estimated_total,
            totals["scanned"] + discovered
            + sum(previous_counts.get(str(item.resolve()), 0) for item in roots[root_index:])
        )
        update_status(catalog_stage="Reading metadata batches", catalog_candidates=len(candidates),
                      catalog_discovered=discovered, catalog_estimated_total=estimated_total,
                      current_file="", current_path="",
                      message=f"Reading metadata for {len(candidates):,} new or changed files")
        batch_metadata = get_exiftool_metadata_batch(
            candidates,
            ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate", "-Make", "-Model"],
        )
        logger.info("ExifTool batch metadata available for %d/%d uncached files under %s",
                    len(batch_metadata), len(candidates), root)

        for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
            try:
                if (not path.is_file() or path.is_symlink()
                        or any(part.startswith(".") for part in path.relative_to(root).parts)
                        or path.suffix.lower() not in MEDIA_EXTENSIONS):
                    continue
                resolved_path = str(path.resolve())
                seen.append(resolved_path)
                totals["scanned"] += 1
                root_scanned += 1

                # The common case is an unchanged file already fully indexed.
                # Validate its fingerprint once and avoid redundant SQLite writes
                # and repeated metadata lookups for every cached field.
                fingerprint = file_fingerprint(path)
                cached = catalog_row(path, fingerprint)
                if cached and cached[4] and cached[5] and cached[6]:
                    if totals["scanned"] % 100 == 0:
                        update_status(catalog_stage="Indexing files",
                                      current_file=path.name, current_path=str(path),
                                      catalog_scanned=totals["scanned"],
                                      catalog_total_scanned=totals["scanned"],
                                      catalog_root_scanned=root_scanned,
                                      catalog_root_estimated_total=discovered,
                                      catalog_root=str(root),
                                      catalog_roots_total=len(roots),
                                      catalog_root_index=root_index,
                                      catalog_files_found=totals["scanned"],
                                      catalog_candidates=len(candidates),
                                      message=f"Indexing cached entries • {totals['scanned']:,} media files scanned")
                        logger.info("Catalog update progress: %d files scanned.", totals["scanned"])
                    continue

                update_status(catalog_stage="Indexing files",
                              current_file=path.name, current_path=str(path),
                              catalog_scanned=totals["scanned"],
                              catalog_total_scanned=totals["scanned"],
                              catalog_root_scanned=root_scanned,
                              catalog_root_estimated_total=discovered,
                              catalog_root=str(root),
                              catalog_roots_total=len(roots),
                              catalog_root_index=root_index,
                              catalog_files_found=totals["scanned"],
                              catalog_candidates=len(candidates),
                              message=f"Indexing {path.name} • {totals['scanned']:,} media files scanned")
                metadata = batch_metadata.get(resolved_path)
                if metadata:
                    timestamp = None
                    for tag in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
                        value = metadata.get(tag)
                        if isinstance(value, str):
                            timestamp = _parse_exif_datetime_value(value)
                            if timestamp:
                                break
                    if timestamp is None:
                        info = path.stat()
                        birth = getattr(info, "st_birthtime", None) if USE_FILE_BIRTH_TIME else None
                        timestamp = datetime.fromtimestamp(birth if birth is not None else info.st_mtime)
                    month = timestamp.strftime("%Y-%m")
                    model = normalize_camera_model(metadata.get("Make"), metadata.get("Model")) or "Unknown-Camera"
                    catalog_record(path, capture_month=month, camera_model=model,
                                   media_folder=get_media_type_folder(path))
                else:
                    # Preserve the established Pillow/ExifTool fallback when batch
                    # metadata is unavailable or ExifTool is not installed.
                    catalog_record(path, media_folder=get_media_type_folder(path))
                    get_month_folder(path)
                    get_camera_model(path)
                totals["metadata_refreshed"] += 1
                if totals["scanned"] % 100 == 0:
                    logger.info("Catalog update progress: %d files scanned.", totals["scanned"])
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Could not catalog %s: %s", path, exc)

        # Never reconcile an unmounted USB volume as empty; only scanned roots
        # have stale path records removed.
        try:
            with catalog_db() as db:
                db.execute("CREATE TEMP TABLE IF NOT EXISTS catalog_seen_paths (path TEXT PRIMARY KEY)")
                db.execute("DELETE FROM catalog_seen_paths")
                db.executemany("INSERT OR IGNORE INTO catalog_seen_paths(path) VALUES (?)",
                               ((item,) for item in seen))
                db.execute("DELETE FROM file_locations WHERE root = ? AND path NOT IN "
                           "(SELECT path FROM catalog_seen_paths)", (str(root),))
                db.execute("DELETE FROM catalog_seen_paths")
        except sqlite3.Error as exc:
            logger.warning("Could not reconcile catalog entries under %s: %s", root, exc)

        try:
            with catalog_db() as db:
                db.execute(
                    """INSERT INTO catalog_refreshes(root, last_updated, files_scanned, status)
                       VALUES (?, ?, ?, 'complete')
                       ON CONFLICT(root) DO UPDATE SET
                         last_updated=excluded.last_updated,
                         files_scanned=excluded.files_scanned,
                         status=excluded.status""",
                    (str(root), time.time(), len(seen)),
                )
        except sqlite3.Error as exc:
            logger.warning("Could not save catalog refresh timestamp for %s: %s", root, exc)

        logger.info("Catalog root updated: %s (%d media files).", root, len(seen))

    update_status(state="complete", phase="catalog-update", current_file="",
                  message=f"Catalog update complete: {totals['scanned']} files scanned")
    logger.info("Catalog update complete: scanned=%d roots=%d", totals["scanned"], totals["roots"])
    notify("Photo Offloader Catalog Updated",
           f"Scanned {totals['scanned']} media files across {totals['roots']} location(s).")
    return totals


def copy_local_archive_to_usb(expected_identity: Optional[VolumeIdentity] = None) -> ImportStats:
    """Copy only missing local archive files to USB; never overwrite mismatches."""
    if not usb_is_mounted():
        raise RuntimeError(f"USB backup volume is not mounted at {USB_DEST_VOLUME}")
    if expected_identity is None:
        expected_identity = get_volume_identity(USB_DEST_VOLUME)
    if not usb_is_mounted() or not volume_identity_matches(
        expected_identity, get_volume_identity(USB_DEST_VOLUME)
    ):
        raise RuntimeError("USB backup volume identity changed before backup started.")
    root_pairs = ((LOCAL_DEST_ROOT, USB_DEST_ROOT), (LOCAL_RAW_DEST_ROOT, USB_RAW_DEST_ROOT))
    files: List[Tuple[Path, Path]] = []
    for source_root, destination_root in root_pairs:
        if not source_root.is_dir():
            logger.info("Local archive root is not present; skipping USB backup root: %s", source_root)
            continue
        for path in source_root.rglob("*"):
            try:
                if (path.is_file() and not path.is_symlink()
                        and not any(part.startswith(".") for part in path.relative_to(source_root).parts)):
                    files.append((path, destination_root / path.relative_to(source_root)))
            except OSError as exc:
                logger.warning("Could not inspect local archive file %s: %s", path, exc)
    files.sort(key=lambda item: str(item[0]).lower())
    stats = ImportStats(found=len(files))
    pending: List[Tuple[Path, Path, FileFingerprint]] = []

    update_status(state="scanning", phase="USB backup", source="; ".join(str(pair[0]) for pair in root_pairs),
                  message="Comparing local archives with USB backup", current_file="")
    for source, destination in files:
        try:
            fingerprint = file_fingerprint(source)
            catalog_record(source)
            if destination.exists():
                cached_match = catalog_hash_matches(source, destination)
                if cached_match is None:
                    source_hash = sha256_file(source)
                    destination_hash = sha256_file(destination) if destination.is_file() else ""
                    catalog_record(source, sha256=source_hash)
                    if destination.is_file():
                        catalog_record(destination, sha256=destination_hash)
                    cached_match = bool(destination_hash) and source_hash == destination_hash
                if destination.is_file() and cached_match:
                    stats.destination_skips += 1
                    stats.verified_copies += 1
                else:
                    stats.failed += 1
                    message = f"USB file differs from local archive; not overwriting: {destination}"
                    stats.errors.append((str(source), message))
                    logger.error(message)
                continue
            pending.append((source, destination, fingerprint))
            stats.source_bytes += fingerprint.size
        except OSError as exc:
            stats.failed += 1
            stats.errors.append((str(source), str(exc)))
            logger.exception("Could not inspect backup candidate %s", source)

    if pending:
        validate_destinations([USB_DEST_ROOT, USB_RAW_DEST_ROOT])
        total_by_root: Dict[Path, int] = {USB_DEST_ROOT: 0, USB_RAW_DEST_ROOT: 0}
        for _, destination, fingerprint in pending:
            root = USB_RAW_DEST_ROOT if destination == USB_RAW_DEST_ROOT or USB_RAW_DEST_ROOT in destination.parents else USB_DEST_ROOT
            total_by_root[root] += fingerprint.size
        for root, required in total_by_root.items():
            if required:
                validate_destinations([root], required)
    global _PROGRESS_STARTED
    _PROGRESS_STARTED = time.monotonic()
    _PROGRESS.update({"state": "copying", "phase": "USB backup", "bytes_done": 0,
                      "bytes_total": stats.source_bytes, "bytes_transferred": 0,
                      "current_file": "", "current_path": "",
                      "message": f"Copying {len(pending)} missing file(s) to USB"})
    update_status()
    for source, destination, before in pending:
        temp_path: Optional[Path] = None
        if not usb_is_mounted():
            stats.failed += 1
            message = "USB backup volume was disconnected during the backup."
            stats.errors.append((str(USB_DEST_VOLUME), message))
            logger.error(message)
            break
        update_status(current_file=source.name, current_path=str(source),
                      message=f"USB backup: {source.name}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".photo-backup-", suffix=".tmp",
                                             dir=destination.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            copy_file_with_progress(source, temp_path)
            if file_fingerprint(source) != before:
                raise IOError(f"Local source changed during backup: {source}")
            source_hash = sha256_file(source)
            temp_hash = sha256_file(temp_path)
            if source_hash != temp_hash:
                raise IOError(f"SHA-256 verification failed for USB temporary copy: {source}")
            if not install_without_overwrite(temp_path, destination):
                if files_match(source, destination, force_sha256=True):
                    stats.destination_skips += 1
                    stats.verified_copies += 1
                    catalog_record(source, sha256=source_hash)
                    catalog_record(destination, sha256=source_hash)
                    continue
                raise FileExistsError(f"USB destination appeared and differs from source: {destination}")
            if sha256_file(destination) != source_hash:
                destination.unlink(missing_ok=True)
                raise IOError(f"SHA-256 verification failed for USB copy: {destination}")
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
            catalog_record(source, sha256=source_hash)
            catalog_record(destination, sha256=source_hash)
            stats.destination_copies += 1
            stats.verified_copies += 1
        except Exception as exc:
            stats.failed += 1
            stats.errors.append((str(source), f"{destination}: {exc}"))
            logger.exception("USB backup failed for %s", source)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary backup file %s", temp_path)

    if not usb_is_mounted() or not volume_identity_matches(
        expected_identity, get_volume_identity(USB_DEST_VOLUME)
    ):
        stats.failed += 1
        message = "USB backup volume was disconnected or replaced during the backup."
        if not any(existing_message == message for _, existing_message in stats.errors):
            stats.errors.append((str(USB_DEST_VOLUME), message))
        logger.error(message)

    update_status(state="complete" if stats.failed == 0 else "failed",
                  message=f"USB backup complete: {stats.destination_copies} copied, {stats.destination_skips} already matched"
                  if stats.failed == 0 else f"USB backup incomplete: {stats.failed} failure(s)",
                  current_file="", current_path="")
    return stats


def eject_volume(volume_path: Path) -> bool:
    if not volume_path.is_mount():
        logger.info("Skipping eject: %s is not mounted.", volume_path)
        return False
    try:
        result = subprocess.run(["diskutil", "eject", str(volume_path)], check=False,
                                capture_output=True, text=True)
        if result.returncode == 0:
            logger.info("Ejected: %s", volume_path)
            return True
        logger.error("Could not eject %s: %s", volume_path, result.stderr.strip() or result.stdout.strip())
        return False
    except OSError as exc:
        logger.error("Could not eject %s: %s", volume_path, exc)
        return False


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely import camera media to local and backup storage.")
    parser.add_argument("--source", type=Path, help="Camera volume or DCIM path.")
    parser.add_argument("--no-usb", action="store_true", help="Import to the Mac SSD only; do not start a USB backup.")
    parser.add_argument("--backup-usb", action="store_true", help="Sync missing files from the Mac archive to the USB backup; no camera card required.")
    parser.add_argument("--update-catalog", action="store_true", help="Initialize/update the SQLite catalog by scanning the local archive and mounted USB backup.")
    parser.add_argument("--no-eject", action="store_true", help="Do not eject the camera card.")
    parser.add_argument("--sha256", action="store_true", help="Use SHA-256 for all copy verification.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    return parser.parse_args(argv)


def print_summary(dcim_folder: Path, destinations: Sequence[Path], stats: ImportStats,
                  eject_attempted: bool, eject_success: bool) -> None:
    print("\n" + "=" * 64)
    print("CAMERA IMPORT SUMMARY")
    print("=" * 64)
    print(f"Source: {dcim_folder}")
    print("Destinations:")
    for destination in destinations:
        print(f"  - {destination}")
    print(f"Files found:          {stats.found}")
    print(f"Source bytes:         {stats.source_bytes}")
    print(f"Destination copies:   {stats.destination_copies}")
    print(f"Destination skips:    {stats.destination_skips}")
    print(f"Verified copies:      {stats.verified_copies}")
    print(f"Failures:             {stats.failed}")
    print(f"Eject:                {'SUCCESS' if eject_attempted and eject_success else 'FAILED' if eject_attempted else 'not requested'}")
    if stats.errors:
        print("\nErrors:")
        for source, message in stats.errors:
            print(f"  - {source}: {message}")
    print("=" * 64)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    global VERIFY_WITH_SHA256, _PROGRESS_STARTED
    VERIFY_WITH_SHA256 = args.sha256
    configure_logging(args.verbose)

    build_info = load_build_info()
    run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    logger.info(
        "Run provenance: run_id=%s version=%s git_commit=%s git_branch=%s "
        "git_tag=%s git_dirty=%s build_time_utc=%s python=%s executable=%s "
        "script=%s argv=%r",
        run_id, build_info.get("version", "unknown"),
        build_info.get("git_commit", "unknown"), build_info.get("git_branch", "unknown"),
        build_info.get("git_tag", "unknown"), build_info.get("git_dirty", "unknown"),
        build_info.get("build_time_utc", "unknown"), sys.version.split()[0],
        sys.executable, build_info.get("source_path", str(Path(__file__).resolve())),
        list(argv) if argv is not None else sys.argv[1:],
    )

    try:
        with import_session_lock():
            # Do not reset shared dashboard status until this process owns the
            # session lock; a duplicate launch must not hide the active session.
            logger.info("--- Starting new photo offloader session run_id=%s ---", run_id)
            _PROGRESS_STARTED = time.monotonic()
            _PROGRESS.update({"state": "scanning", "phase": "startup", "bytes_done": 0,
                              "bytes_total": 0, "bytes_transferred": 0, "current_file": "",
                              "source": "", "message": "Preparing Photo Offloader"})
            update_status()

            if args.update_catalog:
                logger.info("Starting explicit file catalog update.")
                catalog_stats = update_file_catalog()
                logger.info("Catalog update summary: %s", catalog_stats)
                print("Catalog update complete:")
                for key, value in catalog_stats.items():
                    print(f"  {key}: {value}")
                return 0

            if args.backup_usb:
                logger.info("Starting independent local-to-USB backup.")
                backup_stats = copy_local_archive_to_usb()
                print_summary(LOCAL_DEST_ROOT, [LOCAL_DEST_ROOT, LOCAL_RAW_DEST_ROOT, USB_DEST_ROOT, USB_RAW_DEST_ROOT], backup_stats, False, False)
                if backup_stats.failed:
                    notify("USB Backup Incomplete", f"{backup_stats.failed} backup failure(s).")
                    return 1
                notify("USB Backup Complete", f"{backup_stats.destination_copies} files copied; {backup_stats.destination_skips} already matched.")
                return 0

            if args.source:
                logger.info("Using explicit source path: %s", args.source)
                dcim_folder = resolve_dcim_path(args.source)
            else:
                dcim_folder = find_card_dcim()
            card_volume = dcim_folder.parent.resolve()
            logger.info("Source card volume: %s", card_volume)
            update_status(source=str(card_volume), state="scanning", phase="card-to-ssd",
                          message="Scanning camera card")
            logger.info("Scanning: %s", dcim_folder)
            time.sleep(1)
            files = scan_with_retry(dcim_folder)
            if not any(files.values()):
                raise RuntimeError(f"No supported media files found under {dcim_folder}")

            total_found = sum(len(items) for items in files.values())
            source_bytes = required_source_bytes(files)
            local_stats = ImportStats(found=total_found, source_bytes=source_bytes)
            logger.info("Scan complete: %d files (%d JPG, %d RAW, %d VIDEO)",
                        total_found, len(files["JPG"]), len(files["RAW"]), len(files["VIDEO"]))

            usb_expected = not args.no_usb and usb_is_mounted()
            usb_identity = get_volume_identity(USB_DEST_VOLUME) if usb_expected else None
            local_destinations = [LOCAL_DEST_ROOT, LOCAL_RAW_DEST_ROOT]
            validate_destinations(local_destinations, source_bytes)
            logger.info("Stage 1 destinations: %s and %s", LOCAL_DEST_ROOT, LOCAL_RAW_DEST_ROOT)
            if usb_expected:
                logger.info("USB backup is mounted; it will be synchronized after the local SSD import.")
            else:
                logger.info("USB backup not selected or not mounted; local SSD import only.")

            _PROGRESS_STARTED = time.monotonic()
            _PROGRESS.update({"bytes_total": source_bytes, "bytes_done": 0,
                              "bytes_transferred": 0, "state": "copying",
                              "phase": "card-to-ssd", "message": "Copying camera card to Mac SSD"})
            update_status()
            for category in ("JPG", "RAW", "VIDEO"):
                category_root = local_root_for_category(category)
                copy_category(files[category], dcim_folder, [category_root], category, local_stats)

            if local_stats.failed:
                print_summary(dcim_folder, local_destinations, local_stats, False, False)
                update_status(state="failed", phase="card-to-ssd",
                              message=f"Local SSD import failed: {local_stats.failed} failure(s)",
                              current_file="", current_path="")
                logger.error("Local SSD import failed; card was not ejected and USB backup was not started.")
                notify("Photo Import Incomplete", "Local SSD copy failed. Card remains mounted; USB backup was not started.")
                return 1

            # The card is safe to release as soon as the complete local archive is verified.
            eject_attempted = False
            eject_success = False
            if EJECT_CARD_AFTER_SUCCESS and not args.no_eject:
                eject_attempted = True
                eject_success = eject_volume(card_volume)

            backup_stats = ImportStats()
            if usb_expected:
                if not usb_is_mounted() or not volume_identity_matches(
                    usb_identity, get_volume_identity(USB_DEST_VOLUME)
                ):
                    backup_stats.failed += 1
                    message = "Configured USB backup volume was disconnected or replaced before backup."
                    backup_stats.errors.append((str(USB_DEST_VOLUME), message))
                    logger.error(message)
                else:
                    try:
                        backup_stats = copy_local_archive_to_usb(usb_identity)
                    except Exception as exc:
                        backup_stats.failed += 1
                        backup_stats.errors.append((str(USB_DEST_VOLUME), str(exc)))
                        logger.exception("USB backup stage failed: %s", exc)
            else:
                update_status(state="complete", phase="card-to-ssd",
                              message="Mac SSD import complete; USB backup skipped",
                              current_file="", current_path="")

            print_summary(dcim_folder, local_destinations + ([USB_DEST_ROOT, USB_RAW_DEST_ROOT] if usb_expected else []),
                          backup_stats if usb_expected else local_stats, eject_attempted, eject_success)
            if backup_stats.failed:
                update_status(state="failed", phase="usb-backup",
                              message=f"Local import succeeded; USB backup has {backup_stats.failed} failure(s)",
                              current_file="", current_path="")
                notify("USB Backup Incomplete", "The card was copied to the Mac. USB backup needs attention.")
                return 1
            if eject_attempted and not eject_success:
                logger.error("Local import and USB backup succeeded, but automatic card ejection failed.")
                notify("Photo Import Complete — Eject Failed", "Copies completed, but the camera card could not be ejected.")
                return 2
            logger.info("Photo Offloader finished. Local copies: %d; USB copies: %d; USB skips: %d.",
                        local_stats.destination_copies, backup_stats.destination_copies,
                        backup_stats.destination_skips)
            notify("Photo Import Complete", f"{total_found} media files copied to the Mac; USB backup " +
                   ("completed." if usb_expected else "was not run."))
            return 0
    except KeyboardInterrupt:
        update_status(state="canceled", message="Operation canceled", current_file="")
        logger.warning("Process canceled by user.")
        notify("Photo Offloader Canceled", "The operation was canceled.")
        return 130
    except RuntimeError as exc:
        if str(exc) == "Another Photo Offloader import session is already running.":
            logger.warning("Duplicate launch ignored because another Photo Offloader session owns the lock.")
            return 0
        update_status(state="failed", message=str(exc), current_file="")
        logger.exception("Fatal error: %s", exc)
        notify("Photo Offloader Stopped", str(exc))
        return 1
    except Exception as exc:
        update_status(state="failed", message=str(exc), current_file="")
        logger.exception("Fatal error: %s", exc)
        notify("Photo Offloader Stopped", str(exc))
        return 1

if __name__ == "__main__":
    sys.exit(main())
