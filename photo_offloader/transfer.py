"""Copying: per-file copy decisions, a card category, and the USB backup."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from photo_offloader import (
    card,
    catalog,
    config,
    fileops,
    logs,
    media_info,
    progress,
    run_report,
    volumes,
)
from photo_offloader.logs import logger

READ_RETRY_GIVE_UP_AFTER = 10  # failures in a row on the second try: the card has stopped answering


@dataclass
class ImportStats:
    found: int = 0
    source_bytes: int = 0
    destination_copies: int = 0
    destination_skips: int = 0
    verified_copies: int = 0
    failed: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)
    outcomes: List[run_report.FileOutcome] = field(default_factory=list)  # card imports only

    def add_unreadable(self, paths: Sequence[Path]) -> None:
        """Count what the card scan couldn't read: its files were never even considered."""
        self.failed += len(paths)
        self.errors.extend((str(path), "could not be read") for path in paths)

    @property
    def copied(self) -> int:
        return self.destination_copies

    @property
    def skipped(self) -> int:
        return self.destination_skips

    @property
    def verified(self) -> int:
        return self.verified_copies

    @property
    def successful(self) -> bool:
        return self.failed == 0 and not self.errors


@dataclass
class DestinationResult:
    destination: Path
    status: str  # copied, skipped, failed
    path: Optional[Path] = None
    message: str = ""
    sha256: str = ""


def hash_for_copy_decision(
    source: Path,
    destination: Path,
    dcim_folder: Path,
    manifest: Dict[str, dict],
    trust_manifest: bool = False,
) -> Tuple[str, str]:
    """Reuse trustworthy cached hashes and avoid database writes on cache hits."""
    # The card manifest is the cheapest source of truth. Only query SQLite when
    # its fingerprint is absent or stale.
    source_hash = card.card_manifest_entry(manifest, source, dcim_folder)
    source_hash_was_cached = source_hash is not None
    # card.card_is_intact covers this card's manifest only, not the catalog's rows.
    needs_end_check = not (trust_manifest and source_hash_was_cached)
    source_row = None
    if source_hash is None:
        source_row = catalog.catalog_row(source)
        if source_row and source_row[3]:
            source_hash = str(source_row[3])
            source_hash_was_cached = True

    # Destination hashes are reusable only when its indexed fingerprint still
    # matches the current file. A missing/stale row requires a content hash.
    destination_row = catalog.catalog_row(destination)
    destination_hash = str(destination_row[3]) if destination_row and destination_row[3] else None
    if source_hash is None:
        source_hash = fileops.sha256_file(source)
    if destination_hash is None:
        destination_hash = fileops.sha256_file(destination)

    # Both caches outlive a format, so a new photo with an old one's path, size and time could look
    # saved. Unless the card is known not to have been formatted, look at the files themselves.
    if (
        needs_end_check
        and source_hash_was_cached
        and source_hash == destination_hash
        and not fileops.ends_match(source, destination)
    ):
        logger.warning("Cached hash doesn't fit %s; hashing it and %s", source, destination)
        source_hash = fileops.sha256_file(source)
        destination_hash = fileops.sha256_file(destination)
        source_hash_was_cached = False
        destination_row = None

    # Persist hashes only when we had to establish them; do not open and commit
    # two more SQLite transactions for every unchanged duplicate on every insert.
    if not source_hash_was_cached:
        catalog.catalog_record(source, sha256=source_hash)
    if not destination_row or not destination_row[3]:
        catalog.catalog_record(destination, sha256=destination_hash)
    card.update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
    return source_hash, destination_hash


def conflict_free_path(path: Path) -> Path:
    """The first ``name__conflict-N.ext`` next to ``path`` that does not exist yet."""
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}__conflict-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _existing_conflict_copies(path: Path) -> List[Path]:
    """The ``name__conflict-N.ext`` files next to ``path``, whatever numbers they have."""
    pattern = re.compile(rf"{re.escape(path.stem)}__conflict-\d+{re.escape(path.suffix)}")
    return sorted(item for item in path.parent.iterdir() if pattern.fullmatch(item.name))


def _skip_if_already_copied(
    source: Path,
    dcim_folder: Path,
    destination_base: Path,
    final_path: Path,
    manifest: Dict[str, dict],
    size: int,
    trust_manifest: bool,
) -> Optional[DestinationResult]:
    """The "skipped" result when ``final_path`` holds the same bytes as ``source``, otherwise None."""
    try:
        source_hash, destination_hash = hash_for_copy_decision(
            source, final_path, dcim_folder, manifest, trust_manifest
        )
        if source_hash == destination_hash:
            progress.add_progress_bytes(size)
            card.update_card_manifest_entry(manifest, source, dcim_folder, source_hash)
            progress.note_status(
                state="scanning",
                phase="copy-decision",
                message="Existing file verified; continuing",
            )
            return DestinationResult(
                destination_base,
                "skipped",
                final_path,
                "already exists and matches cached/verified hash",
                source_hash,
            )
    except OSError as exc:
        logger.warning("Could not verify existing file %s: %s", final_path, exc)
    return None


def _copy_and_verify(
    source: Path,
    dcim_folder: Path,
    destination_base: Path,
    final_path: Path,
    manifest: Dict[str, dict],
    before: fileops.FileFingerprint,
) -> DestinationResult:
    """Copy through a temporary file and install it only once its hash has been verified."""
    temp_path: Optional[Path] = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".photo-copy-", suffix=".tmp", dir=final_path.parent
        )
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
        if not fileops.install_file(temp_path, final_path):
            if fileops.files_match(source, final_path, force_sha256=True):
                return DestinationResult(
                    destination_base,
                    "skipped",
                    final_path,
                    "another process created a matching file",
                )
            raise FileExistsError(
                f"Destination appeared during copy and does not match source: {final_path}"
            )
        if (
            not fileops._same_file(temp_path, final_path)
            and fileops.sha256_file(final_path, nocache=True) != source_hash
        ):
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
        return DestinationResult(
            destination_base, "copied", final_path, "copied and verified", source_hash
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary file %s: %s", temp_path, exc)


def copy_one_to_destination(
    source: Path,
    dcim_folder: Path,
    destination_base: Path,
    card_manifest: Optional[Dict[str, dict]] = None,
    trust_manifest: bool = False,
) -> DestinationResult:
    before = fileops.file_fingerprint(source)
    manifest = card_manifest if card_manifest is not None else card.load_card_manifest(dcim_folder)
    progress.note_status(
        state="scanning",
        phase="copy-decision",
        current_file=source.name,
        current_path=str(source),
        message=f"Checking existing file: {source.name}",
        decision_files_checked=int(progress._PROGRESS.get("decision_files_checked", 0)),
        decision_files_total=int(progress._PROGRESS.get("decision_files_total", 0)),
    )
    final_path = media_info.build_destination_path(source, dcim_folder, destination_base)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists() and final_path.is_file():
        skipped = _skip_if_already_copied(
            source, dcim_folder, destination_base, final_path, manifest, before.size, trust_manifest
        )
        if skipped:
            return skipped
        # This file may be a conflict copy from an earlier import. Without this check, every
        # re-insert of the card would add another copy, and send it to USB and Google Photos.
        for copy in _existing_conflict_copies(final_path):
            skipped = _skip_if_already_copied(
                source, dcim_folder, destination_base, copy, manifest, before.size, trust_manifest
            )
            if skipped:
                return skipped
        final_path = conflict_free_path(final_path)
        logger.warning("Filename conflict for %s; using %s", source, final_path.name)

    progress.update_status(
        state="copying",
        phase="copy",
        current_file=source.name,
        current_path=str(source),
        message=f"Copying {source.name}",
    )
    return _copy_and_verify(source, dcim_folder, destination_base, final_path, manifest, before)


def _count_new_file(category: str, source: Path) -> None:
    progress._PROGRESS["new_files"] = int(progress._PROGRESS.get("new_files", 0) or 0) + 1
    progress._PROGRESS["new_media_files"] = (
        int(progress._PROGRESS.get("new_media_files", 0) or 0) + 1
    )
    by_category = dict(progress._PROGRESS.get("new_by_category") or {})
    by_category[category] = int(by_category.get(category, 0)) + 1
    progress._PROGRESS["new_by_category"] = by_category
    try:
        progress._PROGRESS["new_bytes"] = (
            int(progress._PROGRESS.get("new_bytes", 0) or 0) + source.stat().st_size
        )
    except OSError:
        pass  # only a display counter: the copy itself has already been verified


def _count_session_file(category: str, source: Path, copied: bool) -> None:
    """Session counters the dashboard shows while importing and keeps in history."""
    progress._PROGRESS["files_done"] = int(progress._PROGRESS.get("files_done", 0) or 0) + 1
    if copied:
        _count_new_file(category, source)


def _count_result(result: DestinationResult, source: Path, label: str, stats: ImportStats) -> None:
    if result.status == "copied":
        stats.destination_copies += 1
        stats.verified_copies += 1
    elif result.status == "skipped":
        stats.destination_skips += 1
        stats.verified_copies += 1
    else:
        stats.failed += 1
        stats.errors.append((str(source), result.message or "copy failed"))
    stats.outcomes.append(
        run_report.FileOutcome(
            label,
            source,
            result.status,
            str(result.path or ""),
            result.sha256,
            result.message if result.status == run_report.FAILED else "",
        )
    )


def copy_category(
    files: Sequence[Path],
    dcim_folder: Path,
    destinations: Sequence[Path],
    label: str,
    stats: ImportStats,
) -> None:
    if not files:
        return
    logger.info("%s: processing %d file(s)", label, len(files))
    progress.update_status(
        state="scanning",
        phase="copy-decision",
        category=label,
        decision_files_checked=0,
        decision_files_total=len(files),
        message=f"Checking {len(files)} {label} file(s)",
    )
    card_manifest = card.load_card_manifest(dcim_folder)
    trust_manifest = card.card_is_intact(dcim_folder)
    logger.info("%s: every file from the last import is on the card: %s", label, trust_manifest)
    logs.reset_timings()
    started = time.perf_counter()
    with catalog.reuse_connection(), progress.progress_bar(len(files), label) as bar:
        for file_index, source in enumerate(files, start=1):
            progress.note_status(
                state="scanning",
                phase="copy-decision",
                current_file=source.name,
                current_path=str(source),
                category=label,
                decision_files_checked=file_index - 1,
                decision_files_total=len(files),
                message=f"Checking existing files ({file_index}/{len(files)}): {source.name}",
            )
            results: List[DestinationResult] = []
            for destination in destinations:
                try:
                    result = copy_one_to_destination(
                        source, dcim_folder, destination, card_manifest, trust_manifest
                    )
                    results.append(result)
                    _count_result(result, source, label, stats)
                except Exception as exc:
                    stats.failed += 1
                    message = f"{source} -> {destination}: {exc}"
                    stats.errors.append((str(source), message))
                    stats.outcomes.append(
                        run_report.FileOutcome(
                            label,
                            source,
                            run_report.FAILED,
                            reason=run_report.failure_reason(exc, source),
                        )
                    )
                    logger.exception(message)
            if len(results) != len(destinations):
                logger.error("One or more destinations failed for %s", source)
            _count_session_file(label, source, any(r.status == "copied" for r in results))
            progress.note_status(
                state="scanning",
                phase="copy-decision",
                decision_files_checked=file_index,
                decision_files_total=len(files),
                message=f"Checked {file_index} of {len(files)} {label} files",
            )
            bar()
    progress.update_status()
    logger.info(
        "Time for %s, %d file(s): %s",
        label,
        len(files),
        logs.timing_summary(time.perf_counter() - started),
    )
    card.save_card_manifest(dcim_folder, card_manifest)


def _mark_recovered(index: int, result: DestinationResult, stats: ImportStats) -> None:
    """A file saved on its second try now counts like any other saved file."""
    item = stats.outcomes[index]
    stats.failed -= 1
    stats.errors = [error for error in stats.errors if error[0] != str(item.source)]
    stats.verified_copies += 1
    if result.status == "copied":
        stats.destination_copies += 1
        _count_new_file(item.category, item.source)
    else:
        stats.destination_skips += 1
    stats.outcomes[index] = run_report.FileOutcome(
        item.category,
        item.source,
        result.status,
        str(result.path or ""),
        result.sha256,
        retried=True,
    )
    logger.warning("%s needed a second try; now saved and verified", item.source)


def retry_read_errors(dcim_folder: Path, stats: ImportStats) -> int:
    """Try once more the files the card failed to give up. Returns how many were then saved.

    Readers and cards can fail for a moment. Only read errors are tried: a full disk won't clear up.
    A file goes through the same verified copy, so nothing can be overwritten. With no timeout on a
    read yet (R11) a dying card can take a long time per file, hence giving up on a run of failures.
    """
    to_try = [
        index
        for index, item in enumerate(stats.outcomes)
        if item.result == run_report.FAILED and item.reason == run_report.READ_ERROR
    ]
    if not to_try:
        return 0
    logger.info("Trying %d file(s) again after read errors.", len(to_try))
    progress.update_status(
        state="copying", phase="copy", message=f"Trying {len(to_try)} file(s) again"
    )
    time.sleep(config.READ_RETRY_PAUSE_SECONDS)
    if not dcim_folder.is_dir():
        logger.error("The card has gone, so its failed files can't be tried again.")
        return 0
    manifest = card.load_card_manifest(dcim_folder)
    recovered = failures_in_a_row = 0
    for index in to_try:
        item = stats.outcomes[index]
        root = media_info.local_root_for_category(item.category)
        try:
            result = copy_one_to_destination(item.source, dcim_folder, root, manifest)
        except Exception as exc:
            item.reason = run_report.failure_reason(exc, item.source)
            logger.error("%s failed again: %s", item.source, exc)
            failures_in_a_row += 1
            if failures_in_a_row >= READ_RETRY_GIVE_UP_AFTER:
                logger.error("%d failures in a row; not trying any more.", failures_in_a_row)
                break
            continue
        failures_in_a_row = 0
        recovered += 1
        _mark_recovered(index, result, stats)
    card.save_card_manifest(dcim_folder, manifest)
    return recovered


def _local_archive_files(source_roots: Sequence[Path]) -> List[Tuple[Path, Path]]:
    """(local file, USB destination) for every visible file in the local archives."""
    files: List[Tuple[Path, Path]] = []
    for source_root in source_roots:
        if not source_root.is_dir():
            logger.info(
                "Local archive root is not present; skipping USB backup source: %s", source_root
            )
            continue
        for path in source_root.rglob("*"):
            try:
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not any(
                        part.startswith(".") for part in path.relative_to(source_root).parts
                    )
                ):
                    # Both local trees retain the same YYYY-MM/camera/{Photos,Raws,Movies}
                    # structure, so mirror both into the single consolidated USB root.
                    files.append((path, config.USB_DEST_ROOT / path.relative_to(source_root)))
            except OSError as exc:
                logger.warning("Could not inspect local archive file %s: %s", path, exc)
    files.sort(key=lambda item: str(item[0]).lower())
    return files


def _compare_with_usb(
    files: Sequence[Tuple[Path, Path]],
    source_roots: Sequence[Path],
    stats: ImportStats,
) -> Tuple[List[Tuple[Path, Path, fileops.FileFingerprint]], Set[Path]]:
    """Decide what the USB backup must copy, reading and writing the catalog once each.

    Files already on USB are compared by recorded SHA-256 when both recordings still
    match the files; a missing hash is computed once and recorded for next time.
    """
    rows = catalog.catalog_rows_under([*source_roots, config.USB_DEST_ROOT])
    updates: List[Tuple[Path, Dict[str, Optional[str]]]] = []

    def known_hash(path: Path, fingerprint: fileops.FileFingerprint) -> Optional[str]:
        row = rows.get(str(path.resolve()))
        if catalog.row_matches(row, fingerprint):
            return row[3]
        updates.append((path, {}))  # new or changed: (re)index it
        return None

    pending: List[Tuple[Path, Path, fileops.FileFingerprint]] = []
    replace_on_usb: Set[Path] = set()
    for source, destination in files:
        try:
            fingerprint = fileops.file_fingerprint(source)
            source_hash = known_hash(source, fingerprint)
            if not destination.exists():
                pending.append((source, destination, fingerprint))
                stats.source_bytes += fingerprint.size
                continue
            matches = False
            if destination.is_file():
                destination_hash = known_hash(destination, fileops.file_fingerprint(destination))
                if source_hash is None:
                    source_hash = fileops.sha256_file(source)
                    updates.append((source, {"sha256": source_hash}))
                if destination_hash is None:
                    destination_hash = fileops.sha256_file(destination)
                    updates.append((destination, {"sha256": destination_hash}))
                matches = source_hash == destination_hash
            if matches:
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
        except OSError as exc:
            stats.failed += 1
            stats.errors.append((str(source), str(exc)))
            logger.exception("Could not inspect backup candidate %s", source)
    catalog.catalog_record_many(updates)
    return pending, replace_on_usb


def _copy_one_to_usb(
    source: Path, destination: Path, before: fileops.FileFingerprint, replace: bool
) -> bool:
    """Copy one archive file to USB through a verified temp file.

    Returns True when copied, False when a matching file appeared meanwhile; raises on
    any mismatch so the caller can record a failure. Never overwrites a differing
    file unless ``replace`` (camera index files).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".photo-backup-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        source_hash = fileops.copy_file_with_progress(source, temp_path)
        if fileops.file_fingerprint(source) != before:
            raise IOError(f"Local source changed during backup: {source}")
        if not source_hash:
            source_hash = fileops.sha256_file(source)
        if fileops.sha256_file(temp_path, nocache=True) != source_hash:
            raise IOError(f"SHA-256 verification failed for USB temporary copy: {source}")
        if not fileops.install_file(temp_path, destination, replace=replace):
            if not fileops.files_match(source, destination, force_sha256=True):
                raise FileExistsError(
                    f"USB destination appeared and differs from source: {destination}"
                )
            copied = False
        else:
            if (
                not fileops._same_file(temp_path, destination)
                and fileops.sha256_file(destination, nocache=True) != source_hash
            ):
                destination.unlink(missing_ok=True)
                raise IOError(f"SHA-256 verification failed for USB copy: {destination}")
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
            copied = True
        catalog.catalog_record(source, sha256=source_hash)
        catalog.catalog_record(destination, sha256=source_hash)
        return copied
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary backup file %s", temp_path)


def copy_local_archive_to_usb(
    expected_identity: Optional[volumes.VolumeIdentity] = None,
) -> ImportStats:
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
    files = _local_archive_files(source_roots)
    stats = ImportStats(found=len(files))
    progress.update_status(
        state="scanning",
        phase="USB backup",
        stage="usb",
        source="; ".join(str(root) for root in source_roots),
        message="Comparing local archives with USB backup",
        current_file="",
    )
    pending, replace_on_usb = _compare_with_usb(files, source_roots, stats)

    if pending:
        volumes.validate_destinations([config.USB_DEST_ROOT], sum(item[2].size for item in pending))
    progress._PROGRESS_STARTED = time.monotonic()
    progress._PROGRESS.update(
        {
            "state": "copying",
            "phase": "USB backup",
            "bytes_done": 0,
            "bytes_total": stats.source_bytes,
            "bytes_transferred": 0,
            "current_file": "",
            "current_path": "",
            "message": f"Copying {len(pending)} missing file(s) to USB",
        }
    )
    progress.update_status()
    for source, destination, before in pending:
        if not volumes.usb_is_mounted():
            stats.failed += 1
            message = "USB backup volume was disconnected during the backup."
            stats.errors.append((str(config.USB_DEST_VOLUME), message))
            logger.error(message)
            break
        progress.update_status(
            current_file=source.name, current_path=str(source), message=f"USB backup: {source.name}"
        )
        try:
            copied = _copy_one_to_usb(
                source, destination, before, replace=destination in replace_on_usb
            )
            stats.destination_copies += int(copied)
            stats.destination_skips += int(not copied)
            stats.verified_copies += 1
        except Exception as exc:
            stats.failed += 1
            stats.errors.append((str(source), f"{destination}: {exc}"))
            logger.exception("USB backup failed for %s", source)

    if not volumes.usb_is_mounted() or not volumes.volume_identity_matches(
        expected_identity, volumes.get_volume_identity(config.USB_DEST_VOLUME)
    ):
        stats.failed += 1
        message = "USB backup volume was disconnected or replaced during the backup."
        if not any(existing_message == message for _, existing_message in stats.errors):
            stats.errors.append((str(config.USB_DEST_VOLUME), message))
        logger.error(message)

    progress.update_status(
        state="complete" if stats.failed == 0 else "failed",
        message=f"USB backup complete: {stats.destination_copies} copied, {stats.destination_skips} already matched"
        if stats.failed == 0
        else f"USB backup incomplete: {stats.failed} failure(s)",
        current_file="",
        current_path="",
    )
    return stats
