"""Rebuild the catalog from the Mac archive and USB backup (--update-catalog)."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from photo_offloader import card, catalog, config, fileops, logs, media_info, progress, volumes
from photo_offloader.logs import logger


def update_file_catalog() -> Dict[str, int]:
    """Index the local archive and mounted USB archive, refreshing cached metadata.

    This operation does not hash every file: hashes already verified by copy/backup
    operations are preserved for unchanged files, while new/changed files are
    indexed without a hash until a verification operation computes one.
    """
    roots: List[Path] = []
    for local_root in (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT):
        if local_root.is_dir():
            roots.append(local_root)
        else:
            logger.info("Local archive root is not available for catalog update: %s", local_root)
    if volumes.usb_is_mounted() and config.USB_DEST_ROOT.is_dir():
        roots.append(config.USB_DEST_ROOT)
    else:
        logger.info("USB volume is not mounted; skipping USB catalog refresh.")

    if not roots:
        raise RuntimeError("No archive locations are available to update the catalog.")

    totals = {"scanned": 0, "metadata_refreshed": 0, "roots": len(roots)}
    # Use the last successful refresh counts as an initial total estimate. These
    # counts are advisory only: roots can gain or lose files between refreshes.
    previous_counts: Dict[str, int] = {}
    try:
        with catalog.catalog_db() as db:
            previous_counts = {
                str(row[0]): int(row[1])
                for row in db.execute(
                    "SELECT root, files_scanned FROM catalog_refreshes WHERE status = 'complete'"
                )
            }
    except sqlite3.Error as exc:
        logger.debug("Could not read previous catalog counts for ETA estimate: %s", exc)
    estimated_total = sum(previous_counts.get(str(root.resolve()), 0) for root in roots)
    progress.update_status(state="scanning", phase="catalog-update", stage="catalog", catalog_stage="Preparing metadata",
                  catalog_scanned=0, catalog_total_scanned=0, catalog_estimated_total=estimated_total,
                  catalog_candidates=0, catalog_root="", catalog_root_index=0,
                  catalog_roots_total=len(roots),
                  source="; ".join(map(str, roots)), message="Preparing catalog metadata batches",
                  current_file="", current_path="")
    logger.info("Starting catalog update for %d root(s).", len(roots))

    for root_index, root in enumerate(roots, start=1):
        root = root.resolve()
        progress.update_status(catalog_root=str(root), catalog_root_index=root_index,
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
                        and candidate.suffix.lower() in card.MEDIA_EXTENSIONS):
                    discovered += 1
                    fingerprint = fileops.file_fingerprint(candidate)
                    cached = catalog.catalog_row(candidate, fingerprint)
                    if not (cached and cached[4] and cached[5] and cached[6]):
                        candidates.append(candidate)
                    if discovered % 100 == 0:
                        progress.update_status(catalog_stage="Finding files needing metadata",
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
        progress.update_status(catalog_stage="Reading metadata batches", catalog_candidates=len(candidates),
                      catalog_discovered=discovered, catalog_estimated_total=estimated_total,
                      current_file="", current_path="",
                      message=f"Reading metadata for {len(candidates):,} new or changed files")
        batch_metadata = media_info.get_exiftool_metadata_batch(
            candidates,
            ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate", "-Make", "-Model"],
        )
        logger.info("ExifTool batch metadata available for %d/%d uncached files under %s",
                    len(batch_metadata), len(candidates), root)

        for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
            try:
                if (not path.is_file() or path.is_symlink()
                        or any(part.startswith(".") for part in path.relative_to(root).parts)
                        or path.suffix.lower() not in card.MEDIA_EXTENSIONS):
                    continue
                resolved_path = str(path.resolve())
                seen.append(resolved_path)
                totals["scanned"] += 1
                root_scanned += 1

                # The common case is an unchanged file already fully indexed.
                # Validate its fingerprint once and avoid redundant SQLite writes
                # and repeated metadata lookups for every cached field.
                fingerprint = fileops.file_fingerprint(path)
                cached = catalog.catalog_row(path, fingerprint)
                if cached and cached[4] and cached[5] and cached[6]:
                    if totals["scanned"] % 100 == 0:
                        progress.update_status(catalog_stage="Indexing files",
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

                progress.update_status(catalog_stage="Indexing files",
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
                            timestamp = media_info._parse_exif_datetime_value(value)
                            if timestamp:
                                break
                    if timestamp is None:
                        info = path.stat()
                        birth = getattr(info, "st_birthtime", None) if config.USE_FILE_BIRTH_TIME else None
                        timestamp = datetime.fromtimestamp(birth if birth is not None else info.st_mtime)
                    month = timestamp.strftime("%Y-%m")
                    model = media_info.normalize_camera_model(metadata.get("Make"), metadata.get("Model")) or "Unknown-Camera"
                    catalog.catalog_record(path, capture_month=month, camera_model=model,
                                   media_folder=media_info.get_media_type_folder(path))
                else:
                    # Preserve the established Pillow/ExifTool fallback when batch
                    # metadata is unavailable or ExifTool is not installed.
                    catalog.catalog_record(path, media_folder=media_info.get_media_type_folder(path))
                    media_info.get_month_folder(path)
                    media_info.get_camera_model(path)
                totals["metadata_refreshed"] += 1
                if totals["scanned"] % 100 == 0:
                    logger.info("Catalog update progress: %d files scanned.", totals["scanned"])
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Could not catalog %s: %s", path, exc)

        # Never reconcile an unmounted USB volume as empty; only scanned roots
        # have stale path records removed.
        try:
            with catalog.catalog_db() as db:
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
            with catalog.catalog_db() as db:
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

    progress.update_status(state="complete", phase="catalog-update", current_file="",
                  message=f"Catalog update complete: {totals['scanned']} files scanned")
    logger.info("Catalog update complete: scanned=%d roots=%d", totals["scanned"], totals["roots"])
    logs.notify("Photo Offloader Catalog Updated",
           f"Scanned {totals['scanned']} media files across {totals['roots']} location(s).")
    return totals
