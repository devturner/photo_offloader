"""Copying: per-file copy decisions, a card category, and the USB backup."""
from __future__ import annotations

import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from photo_offloader import card, catalog, config, fileops, media_info, progress, volumes
from photo_offloader.logs import logger


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


def hash_for_copy_decision(
    source: Path, destination: Path, dcim_folder: Path, manifest: Dict[str, dict],
) -> Tuple[str, str]:
    """Reuse trustworthy cached hashes and avoid database writes on cache hits."""
    # The card manifest is the cheapest source of truth. Only query SQLite when
    # the card-side fingerprint is absent or stale.
    source_hash = card.card_manifest_entry(manifest, source, dcim_folder)
    source_hash_was_cached = source_hash is not None
    source_row = None
    if source_hash is None:
        source_row = catalog.catalog_row(source)
        if source_row and source_row[3]:
            source_hash = str(source_row[3])
            source_hash_was_cached = True

    # Destination hashes are reusable only when its indexed fingerprint still
    # matches the current file. A missing/stale row requires a content hash.
    destination_row = catalog.catalog_row(destination)
    destination_hash = (
        str(destination_row[3])
        if destination_row and destination_row[3]
        else None
    )
    if source_hash is None:
        source_hash = fileops.sha256_file(source)
    if destination_hash is None:
        destination_hash = fileops.sha256_file(destination)

    # Persist hashes only when we had to establish them; do not open and commit
    # two more SQLite transactions for every unchanged duplicate on every insert.
    if not source_hash_was_cached:
        catalog.catalog_record(source, sha256=source_hash)
    if not destination_row or not destination_row[3]:
        catalog.catalog_record(destination, sha256=destination_hash)
    card.update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
    return source_hash, destination_hash


def copy_one_to_destination(
    source: Path, dcim_folder: Path, destination_base: Path,
    card_manifest: Optional[Dict[str, dict]] = None,
) -> DestinationResult:
    before = fileops.file_fingerprint(source)
    manifest = card_manifest if card_manifest is not None else card.load_card_manifest(dcim_folder)
    progress.update_status(state="scanning", phase="copy-decision", current_file=source.name,
                  current_path=str(source), message=f"Checking existing file: {source.name}",
                  decision_files_checked=int(progress._PROGRESS.get("decision_files_checked", 0)),
                  decision_files_total=int(progress._PROGRESS.get("decision_files_total", 0)))
    final_path = media_info.build_destination_path(source, dcim_folder, destination_base)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    replace_existing = False

    if final_path.exists() and final_path.is_file():
        try:
            source_hash, destination_hash = hash_for_copy_decision(
                source, final_path, dcim_folder, manifest
            )
            if source_hash == destination_hash:
                progress.add_progress_bytes(before.size)
                card.update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
                progress.update_status(state="copying", phase="copy", message="Existing file verified; continuing")
                return DestinationResult(destination_base, "skipped", final_path,
                                         "already exists and matches cached/verified hash")
        except OSError as exc:
            logger.warning("Could not verify existing file %s: %s", final_path, exc)
        if fileops.is_camera_index_file(source):
            replace_existing = True
            logger.info("Camera index file changed; replacing the previous copy: %s", final_path)
        else:
            stem, suffix = final_path.stem, final_path.suffix
            counter = 1
            while True:
                candidate = final_path.with_name(f"{stem}__conflict-{counter}{suffix}")
                if not candidate.exists():
                    final_path = candidate
                    break
                counter += 1
            logger.warning("Filename conflict for %s; using %s", source, final_path.name)

    progress.update_status(state="copying", phase="copy", current_file=source.name,
                  current_path=str(source), message=f"Copying {source.name}")
    temp_path: Optional[Path] = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".photo-copy-", suffix=".tmp", dir=final_path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        source_hash = fileops.copy_file_with_progress(source, temp_path)
        after_copy = fileops.file_fingerprint(source)
        if after_copy != before:
            raise IOError(f"Source changed during copy: {source}")
        if not source_hash:  # copy helper replaced/legacy: fall back to a second read
            source_hash = fileops.sha256_file(source)
        # Verify what is actually on the destination disk against what was read
        # from the card (cache-bypassing read where the OS supports it).
        if fileops.sha256_file(temp_path, nocache=True) != source_hash:
            raise IOError(f"Verification failed for temporary copy: {source}")
        if not fileops.install_file(temp_path, final_path, replace=replace_existing):
            if fileops.files_match(source, final_path, force_sha256=True):
                return DestinationResult(destination_base, "skipped", final_path,
                                         "another process created a matching file")
            raise FileExistsError(f"Destination appeared during copy and does not match source: {final_path}")
        if not fileops._same_file(temp_path, final_path) and fileops.sha256_file(final_path, nocache=True) != source_hash:
            # Hard-linked installs share the verified inode; only a fallback
            # copy (e.g. exFAT) is a fresh write that needs its own check.
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
        catalog.catalog_record(source, sha256=source_hash)
        catalog.catalog_record(final_path, sha256=source_hash)
        card.update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
        progress.update_status(state="copying", phase="copy", message="Copy verified")
        return DestinationResult(destination_base, "copied", final_path, "copied and verified")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary file %s: %s", temp_path, exc)


def _count_session_file(category: str, source: Path, copied: bool) -> None:
    """Session counters the dashboard shows while importing and keeps in history."""
    progress._PROGRESS["files_done"] = int(progress._PROGRESS.get("files_done", 0) or 0) + 1
    if copied:
        progress._PROGRESS["new_files"] = int(progress._PROGRESS.get("new_files", 0) or 0) + 1
        by_category = dict(progress._PROGRESS.get("new_by_category") or {})
        by_category[category] = int(by_category.get(category, 0)) + 1
        progress._PROGRESS["new_by_category"] = by_category
        try:
            progress._PROGRESS["new_bytes"] = int(progress._PROGRESS.get("new_bytes", 0) or 0) + source.stat().st_size
        except OSError:
            pass


def copy_category(files: Sequence[Path], dcim_folder: Path, destinations: Sequence[Path],
                  label: str, stats: ImportStats) -> None:
    if not files:
        return
    logger.info("%s: processing %d file(s)", label, len(files))
    progress.update_status(state="scanning", phase="copy-decision", category=label,
                  decision_files_checked=0, decision_files_total=len(files),
                  message=f"Checking {len(files)} {label} file(s)")
    card_manifest = card.load_card_manifest(dcim_folder)
    with progress.progress_bar(len(files), label) as bar:
        for file_index, source in enumerate(files, start=1):
            progress.update_status(state="scanning", phase="copy-decision", current_file=source.name,
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
            _count_session_file(label, source, any(r.status == "copied" for r in results))
            progress.update_status(state="scanning", phase="copy-decision",
                          decision_files_checked=file_index,
                          decision_files_total=len(files),
                          message=f"Checked {file_index} of {len(files)} {label} files")
            bar()
    card.save_card_manifest(dcim_folder, card_manifest)


def copy_local_archive_to_usb(expected_identity: Optional[volumes.VolumeIdentity] = None) -> ImportStats:
    """Copy only missing local archive files to USB; never overwrite mismatches."""
    if not volumes.usb_is_mounted():
        raise RuntimeError(f"USB backup volume is not mounted at {config.USB_DEST_VOLUME}")
    if expected_identity is None:
        expected_identity = volumes.get_volume_identity(config.USB_DEST_VOLUME)
    if not volumes.usb_is_mounted() or not volumes.volume_identity_matches(
        expected_identity, volumes.get_volume_identity(config.USB_DEST_VOLUME)
    ):
        raise RuntimeError("USB backup volume identity changed before backup started.")
    source_roots = (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT)
    files: List[Tuple[Path, Path]] = []
    for source_root in source_roots:
        if not source_root.is_dir():
            logger.info("Local archive root is not present; skipping USB backup source: %s", source_root)
            continue
        for path in source_root.rglob("*"):
            try:
                if (path.is_file() and not path.is_symlink()
                        and not any(part.startswith(".") for part in path.relative_to(source_root).parts)):
                    # Both local trees retain the same YYYY-MM/camera/{Photos,Raws,Movies}
                    # structure, so mirror both into the single consolidated USB root.
                    files.append((path, config.USB_DEST_ROOT / path.relative_to(source_root)))
            except OSError as exc:
                logger.warning("Could not inspect local archive file %s: %s", path, exc)
    files.sort(key=lambda item: str(item[0]).lower())
    stats = ImportStats(found=len(files))
    pending: List[Tuple[Path, Path, fileops.FileFingerprint]] = []
    replace_on_usb: Set[Path] = set()

    progress.update_status(state="scanning", phase="USB backup", stage="usb",
                  source="; ".join(str(root) for root in source_roots),
                  message="Comparing local archives with USB backup", current_file="")
    for source, destination in files:
        try:
            fingerprint = fileops.file_fingerprint(source)
            catalog.catalog_record(source)
            if destination.exists():
                cached_match = catalog.catalog_hash_matches(source, destination)
                if cached_match is None:
                    source_hash = fileops.sha256_file(source)
                    destination_hash = fileops.sha256_file(destination) if destination.is_file() else ""
                    catalog.catalog_record(source, sha256=source_hash)
                    if destination.is_file():
                        catalog.catalog_record(destination, sha256=destination_hash)
                    cached_match = bool(destination_hash) and source_hash == destination_hash
                if destination.is_file() and cached_match:
                    stats.destination_skips += 1
                    stats.verified_copies += 1
                elif destination.is_file() and fileops.is_camera_index_file(destination):
                    replace_on_usb.add(destination)
                    pending.append((source, destination, fingerprint))
                    stats.source_bytes += fingerprint.size
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
        volumes.validate_destinations([config.USB_DEST_ROOT], sum(item[2].size for item in pending))
    progress._PROGRESS_STARTED = time.monotonic()
    progress._PROGRESS.update({"state": "copying", "phase": "USB backup", "bytes_done": 0,
                      "bytes_total": stats.source_bytes, "bytes_transferred": 0,
                      "current_file": "", "current_path": "",
                      "message": f"Copying {len(pending)} missing file(s) to USB"})
    progress.update_status()
    for source, destination, before in pending:
        temp_path: Optional[Path] = None
        if not volumes.usb_is_mounted():
            stats.failed += 1
            message = "USB backup volume was disconnected during the backup."
            stats.errors.append((str(config.USB_DEST_VOLUME), message))
            logger.error(message)
            break
        progress.update_status(current_file=source.name, current_path=str(source),
                      message=f"USB backup: {source.name}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".photo-backup-", suffix=".tmp",
                                             dir=destination.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            source_hash = fileops.copy_file_with_progress(source, temp_path)
            if fileops.file_fingerprint(source) != before:
                raise IOError(f"Local source changed during backup: {source}")
            if not source_hash:
                source_hash = fileops.sha256_file(source)
            if fileops.sha256_file(temp_path, nocache=True) != source_hash:
                raise IOError(f"SHA-256 verification failed for USB temporary copy: {source}")
            if not fileops.install_file(temp_path, destination, replace=destination in replace_on_usb):
                if fileops.files_match(source, destination, force_sha256=True):
                    stats.destination_skips += 1
                    stats.verified_copies += 1
                    catalog.catalog_record(source, sha256=source_hash)
                    catalog.catalog_record(destination, sha256=source_hash)
                    continue
                raise FileExistsError(f"USB destination appeared and differs from source: {destination}")
            if not fileops._same_file(temp_path, destination) and fileops.sha256_file(destination, nocache=True) != source_hash:
                destination.unlink(missing_ok=True)
                raise IOError(f"SHA-256 verification failed for USB copy: {destination}")
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
            catalog.catalog_record(source, sha256=source_hash)
            catalog.catalog_record(destination, sha256=source_hash)
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

    if not volumes.usb_is_mounted() or not volumes.volume_identity_matches(
        expected_identity, volumes.get_volume_identity(config.USB_DEST_VOLUME)
    ):
        stats.failed += 1
        message = "USB backup volume was disconnected or replaced during the backup."
        if not any(existing_message == message for _, existing_message in stats.errors):
            stats.errors.append((str(config.USB_DEST_VOLUME), message))
        logger.error(message)

    progress.update_status(state="complete" if stats.failed == 0 else "failed",
                  message=f"USB backup complete: {stats.destination_copies} copied, {stats.destination_skips} already matched"
                  if stats.failed == 0 else f"USB backup incomplete: {stats.failed} failure(s)",
                  current_file="", current_path="")
    return stats
