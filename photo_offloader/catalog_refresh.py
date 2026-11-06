"""Rebuild the catalog from the Mac archive and USB backup (--update-catalog)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from photo_offloader import card, catalog, config, fileops, logs, media_info, progress, volumes
from photo_offloader.logs import logger

PROGRESS_EVERY = 100  # files between dashboard updates


def update_file_catalog() -> Dict[str, int]:
    """Index the local archive and mounted USB archive, refreshing cached metadata.

    This operation does not hash every file: hashes already verified by copy/backup
    operations are preserved for unchanged files, while new/changed files are
    indexed without a hash until a verification operation computes one. Each root
    is walked once, read from the catalog in one query, sent to ExifTool in
    batches and written back in one transaction.
    """
    roots = _catalog_roots()
    if not roots:
        raise RuntimeError("No archive locations are available to update the catalog.")
    totals = {"scanned": 0, "metadata_refreshed": 0, "roots": len(roots)}
    progress.update_status(
        state="scanning",
        phase="catalog-update",
        stage="catalog",
        catalog_stage="Finding files",
        catalog_scanned=0,
        source="; ".join(map(str, roots)),
        message="Preparing catalog update",
        current_file="",
        current_path="",
    )
    logger.info("Starting catalog update for %d root(s).", len(roots))
    for root_index, root in enumerate(roots, start=1):
        _refresh_root(root.resolve(), root_index, len(roots), totals)

    progress.update_status(
        state="complete",
        phase="catalog-update",
        current_file="",
        message=f"Catalog update complete: {totals['scanned']} files scanned",
    )
    logger.info("Catalog update complete: scanned=%d roots=%d", totals["scanned"], totals["roots"])
    logs.notify(
        "Photo Offloader Catalog Updated",
        f"Scanned {totals['scanned']} media files across {totals['roots']} location(s).",
    )
    return totals


def _catalog_roots() -> List[Path]:
    roots: List[Path] = []
    for local_root in (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT):
        if local_root.is_dir():
            roots.append(local_root)
        else:
            logger.info("Local archive root is not available for catalog update: %s", local_root)
    if volumes.usb_is_mounted() and config.USB_DEST_ROOT.is_dir():
        roots.append(config.USB_DEST_ROOT)
    else:
        # Never reconcile an unmounted USB volume as empty: its records are kept.
        logger.info("USB volume is not mounted; skipping USB catalog refresh.")
    return roots


def _media_files(root: Path) -> List[Path]:
    files = []
    for path in root.rglob("*"):
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(part.startswith(".") for part in path.relative_to(root).parts)
                and path.suffix.lower() in card.MEDIA_EXTENSIONS
            ):
                files.append(path)
        except OSError as exc:
            logger.warning("Could not inspect catalog candidate %s: %s", path, exc)
    return sorted(files, key=lambda item: str(item).lower())


def _refresh_root(root: Path, root_index: int, roots_total: int, totals: Dict[str, int]) -> None:
    progress.update_status(
        catalog_stage="Finding files",
        catalog_scanned=totals["scanned"],
        message=f"Scanning location {root_index} of {roots_total}: {root}",
    )
    logger.info("Scanning catalog root %d/%d: %s", root_index, roots_total, root)
    files = _media_files(root)
    rows = catalog.catalog_rows_under([root])

    # Files whose catalog entry is missing, stale or lacks month/camera/folder.
    stale: List[Path] = []
    for path in files:
        try:
            row = rows.get(str(path.resolve()))
            if not (
                catalog.row_matches(row, fileops.file_fingerprint(path))
                and row[4]
                and row[5]
                and row[6]
            ):
                stale.append(path)
        except OSError as exc:
            logger.warning("Could not inspect catalog candidate %s: %s", path, exc)

    progress.update_status(
        catalog_stage="Reading photo info",
        catalog_scanned=totals["scanned"],
        message=f"Reading photo info for {len(stale):,} new or changed files",
    )
    records = (
        media_info.get_exiftool_metadata_batch(
            stale, [f"-{tag}" for tag in media_info.EXIF_DATE_TAGS] + ["-Make", "-Model"]
        )
        if stale
        else {}
    )
    logger.info(
        "ExifTool batch metadata available for %d/%d uncached files under %s",
        len(records),
        len(stale),
        root,
    )

    entries: List[Tuple[Path, Dict[str, Optional[str]]]] = []
    fallback: List[Path] = []
    for path in stale:
        folder = media_info.get_media_type_folder(path)
        record = records.get(str(path.resolve()))
        try:
            if record:
                month, model = media_info.month_and_model(path, record)
                entries.append(
                    (
                        path,
                        {
                            "capture_month": month,
                            "camera_model": model or "Unknown-Camera",
                            "media_folder": folder,
                        },
                    )
                )
            else:
                entries.append((path, {"media_folder": folder}))
                fallback.append(path)
        except OSError as exc:
            logger.warning("Could not catalog %s: %s", path, exc)
    totals["metadata_refreshed"] += catalog.catalog_record_many(entries)
    # Without ExifTool (or for files it could not read) keep the per-file Pillow/ExifTool path.
    for index, path in enumerate(fallback, start=1):
        try:
            media_info.get_month_folder(path)
            media_info.get_camera_model(path)
        except OSError as exc:
            logger.warning("Could not catalog %s: %s", path, exc)
        if index % PROGRESS_EVERY == 0:
            progress.update_status(
                catalog_stage="Indexing files",
                current_file=path.name,
                catalog_scanned=totals["scanned"] + index,
            )

    totals["scanned"] += len(files)
    progress.update_status(
        catalog_stage="Indexing files",
        catalog_scanned=totals["scanned"],
        message=f"Indexed {totals['scanned']:,} media files",
    )
    _forget_missing_files(root, files)
    _record_refresh(root, len(files))
    logger.info("Catalog root updated: %s (%d media files).", root, len(files))


def _forget_missing_files(root: Path, files: List[Path]) -> None:
    """Drop catalog rows under a scanned root for files that are no longer there."""
    try:
        with catalog.catalog_db() as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS catalog_seen_paths (path TEXT PRIMARY KEY)")
            db.execute("DELETE FROM catalog_seen_paths")
            db.executemany(
                "INSERT OR IGNORE INTO catalog_seen_paths(path) VALUES (?)",
                ((str(path.resolve()),) for path in files),
            )
            db.execute(
                "DELETE FROM file_locations WHERE root = ? AND path NOT IN "
                "(SELECT path FROM catalog_seen_paths)",
                (str(root),),
            )
            db.execute("DELETE FROM catalog_seen_paths")
    except sqlite3.Error as exc:
        logger.warning("Could not reconcile catalog entries under %s: %s", root, exc)


def _record_refresh(root: Path, files_scanned: int) -> None:
    try:
        with catalog.catalog_db() as db:
            db.execute(
                """INSERT INTO catalog_refreshes(root, last_updated, files_scanned, status)
                   VALUES (?, ?, ?, 'complete')
                   ON CONFLICT(root) DO UPDATE SET
                     last_updated=excluded.last_updated,
                     files_scanned=excluded.files_scanned,
                     status=excluded.status""",
                (str(root), time.time(), files_scanned),
            )
    except sqlite3.Error as exc:
        logger.warning("Could not save catalog refresh timestamp for %s: %s", root, exc)
