"""Photo Offloader importer.

Conservative macOS media ingestion:
    discover -> lock -> snapshot -> scan -> capacity check -> copy -> verify
    -> report -> eject

The importer never deletes source media and never overwrites an existing
same-name destination. It verifies the Mac SSD archive first, then ejects the
card when enabled and independently synchronizes missing files to USB.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from photo_offloader import (
    awake,
    card,
    card_identity,
    card_status,
    catalog,
    catalog_refresh,
    config,
    logs,
    media_info,
    progress,
    run_report,
    transfer,
    volumes,
)
from photo_offloader.logs import logger

# What the current run has learned so far; turned into a history row by main().
_RUN: Dict[str, object] = {}
# This run's own log lines, which go in its report.
_RUN_LOG = run_report.LogCapture()


@contextlib.contextmanager
def _timed(name: str) -> Iterator[None]:
    """Keep how long a step of this run took, however it ends, for its history row."""
    started = time.monotonic()
    try:
        yield
    finally:
        _RUN[name] = time.monotonic() - started


def _finish_run_record(exit_code: int) -> None:
    kind = _RUN.get("kind")
    if not kind:
        return  # duplicate launch, or it never got as far as owning a session
    if kind == "import" and not _RUN.get("source"):
        return  # no card was ever resolved, so there is nothing worth recording
    outcome = {0: "complete", 2: "complete", 130: "canceled"}.get(exit_code, "failed")
    message = str(progress._PROGRESS.get("message", "") or "")
    if exit_code == 2 and not message:
        message = "Copies completed, but the card could not be ejected."
    details = _run_details()
    if kind in ("import", "usb_backup"):
        details.update(_save_run_report(outcome, message))
    catalog.record_import_run(
        kind=str(kind),
        outcome=outcome,
        source=str(_RUN.get("source", "")),
        started_at=float(_RUN.get("started_at", time.time())),
        finished_at=time.time(),
        files=int(_RUN.get("files", 0)),
        copied=int(_RUN.get("copied", 0)),
        skipped=int(_RUN.get("skipped", 0)),
        failed=int(_RUN.get("failed", 0)),
        bytes=int(_RUN.get("bytes", 0)),
        usb=bool(_RUN.get("usb", False)),
        message=message,
        details=details,
    )
    if kind in ("import", "usb_backup"):
        _note_safe_cards()


def _files_not_reached(
    files: Dict[str, List[Path]], outcomes: Sequence[run_report.FileOutcome]
) -> List[Tuple[str, Path]]:
    done = {item.source for item in outcomes}
    return [
        (category, path) for category, paths in files.items() for path in paths if path not in done
    ]


def _archive_roots() -> Tuple[Path, ...]:
    return (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT)


def _usb_summary(stats: transfer.ImportStats) -> Dict[str, object]:
    return run_report.summarize_usb(stats.outcomes, stats.sizes, _archive_roots())


def _run_facts(outcome: str, message: str) -> Optional[run_report.RunFacts]:
    """What a run's report holds. An import lists its card's files, and any USB ones that failed."""
    alone = _RUN.get("kind") == "usb_backup"
    dcim = _RUN.get("dcim")
    if not alone and not isinstance(dcim, Path):
        return None  # it never found a card
    stats, usb = _RUN.get("stats"), _RUN.get("usb_stats")
    outcomes = stats.outcomes if isinstance(stats, transfer.ImportStats) else []
    usb_outcomes = usb.outcomes if isinstance(usb, transfer.ImportStats) else []
    usb_errors = usb.errors if isinstance(usb, transfer.ImportStats) else []
    if not alone:
        usb_outcomes = [item for item in usb_outcomes if item.result == run_report.FAILED]
    per_file = {str(item.source) for item in usb_outcomes}
    problems = outcome == "failed"
    usb_failed = alone or bool(usb_errors)
    build = load_build_info()
    return run_report.RunFacts(
        source="USB backup" if alone else str(_RUN["source"]),
        started_at=float(_RUN.get("started_at", time.time())),  # type: ignore[arg-type]
        outcome=outcome,
        message=message,
        dcim=None if alone else dcim,  # type: ignore[arg-type]
        problems=problems,
        outcomes=outcomes,
        unreadable=list(_RUN.get("unreadable", [])),  # type: ignore[call-overload]
        files_not_reached=_files_not_reached(
            _RUN.get("scan_files", {}),  # type: ignore[arg-type]
            outcomes,
        ),
        sizes=_RUN.get("sizes", {}),  # type: ignore[arg-type]
        usb_outcomes=usb_outcomes,
        usb_sizes=usb.sizes if isinstance(usb, transfer.ImportStats) else {},
        archive_roots=_archive_roots(),
        usb_errors=[error for error in usb_errors if error[0] not in per_file],
        log_lines=list(_RUN_LOG.lines),
        app_version=f"{build.get('version', 'unknown')} ({build.get('git_commit', '')[:7]})",
        volume_info=run_report.volume_info(
            config.USB_DEST_VOLUME if usb_failed else dcim.parent  # type: ignore[union-attr]
        )
        if problems
        else "",
    )


def _save_run_report(outcome: str, message: str) -> Dict[str, object]:
    """Save what this run did with each file. Whatever goes wrong here, the run's result stands."""
    try:
        facts = _run_facts(outcome, message)
        if facts is None:
            return {}
        folder, summary = run_report.save_report(facts)
    except Exception as exc:
        logger.exception("Could not save this run's report: %s", exc)
        return {}
    second_tries = sum(1 for item in facts.outcomes if item.retried)
    return {
        "report": folder,
        **({"problems": summary} if summary else {}),
        **({"second_tries": second_tries} if second_tries else {}),
    }


def _run_details() -> Dict[str, object]:
    """What this session itself did, for the dashboard's Recent activity."""
    details: Dict[str, object] = {}
    if _RUN.get("kind") == "import":
        details["new"] = {
            k: v for k, v in dict(progress._PROGRESS.get("new_by_category") or {}).items() if v
        }
        details["new_bytes"] = int(progress._PROGRESS.get("new_bytes", 0) or 0)
        for name in ("copy_seconds", "usb_seconds"):
            if name in _RUN:
                details[name] = round(float(_RUN[name]), 1)  # type: ignore[arg-type]
        if "usb_bytes" in _RUN:
            details["usb_bytes"] = _RUN["usb_bytes"]
        if _RUN.get("card_id"):
            details["card_id"] = _RUN["card_id"]
        if _RUN.get("local_verified"):
            # A run that failed later, on USB, still has its photos safe on the Mac.
            details["local_verified"] = True
    if "usb_copied" in _RUN:
        details["usb_copied"] = int(_RUN["usb_copied"])  # type: ignore[call-overload]
    return details


def load_build_info() -> Dict[str, str]:
    """Identify the exact source/build installed when this process was launched."""
    info: Dict[str, str] = {}
    try:
        loaded = json.loads(config.BUILD_INFO_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            info.update({str(key): str(value) for key, value in loaded.items()})
    except (OSError, ValueError):
        pass  # a source checkout has no build file: the version and git lookups below fill in

    if not info.get("version"):
        try:
            info["version"] = config.VERSION_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            info["version"] = "unknown"

    # Source checkouts get useful provenance even when run without install.sh.
    if not info.get("git_commit") or not info.get("git_branch"):
        source_dir = Path(__file__).resolve().parent

        def git_value(*args: str) -> str:
            try:
                result = subprocess.run(
                    ["git", "-C", str(source_dir), *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
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


def launch_google_photos_sync() -> None:
    """Start optional Google Photos uploads without making import success depend on cloud access."""
    if not config.GOOGLE_PHOTOS_SYNC_PATH.is_file():
        logger.info("Google Photos helper is not installed; skipping optional cloud upload.")
        return
    try:
        log_path = config.GOOGLE_LAUNCHER_LOG_PATH
        logs.rotate_log_file(log_path, max_bytes=1024 * 1024, backups=1)
        with log_path.open("a", encoding="utf-8") as output:
            subprocess.Popen(
                [sys.executable, str(config.GOOGLE_PHOTOS_SYNC_PATH), "--sync"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info("Started optional Google Photos upload in the background.")
    except OSError as exc:
        logger.warning("Could not start optional Google Photos upload: %s", exc)


def configure_logging(verbose: bool = False) -> None:
    config.LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE_PATH,
        maxBytes=logs.LOG_MAX_BYTES,
        backupCount=logs.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    _RUN_LOG.setFormatter(formatter)
    logger.addHandler(_RUN_LOG)
    if not progress.HAS_ALIVE_PROGRESS:
        logger.warning("alive-progress is not installed; progress output is disabled.")


class SessionBusyError(RuntimeError):
    """Another importer owns the session lock (a duplicate launch, not a failure)."""


@contextmanager
def import_session_lock() -> Iterator[None]:
    config.SESSION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = config.SESSION_LOCK_PATH.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SessionBusyError(
                "Another Photo Offloader import session is already running."
            ) from exc
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            try:
                config.SESSION_LOCK_PATH.unlink(missing_ok=True)
            except OSError as exc:  # harmless: the flock is already released
                logger.debug("Could not remove the session lock file: %s", exc)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely import camera media to local and backup storage."
    )
    parser.add_argument("--source", type=Path, help="Camera volume or DCIM path.")
    parser.add_argument(
        "--no-usb",
        action="store_true",
        help="Import to the Mac SSD only; do not start a USB backup.",
    )
    parser.add_argument(
        "--backup-usb",
        action="store_true",
        help="Sync missing files from the Mac archive to the USB backup; no camera card required.",
    )
    parser.add_argument(
        "--update-catalog",
        action="store_true",
        help="Initialize/update the SQLite catalog by scanning the local archive and mounted USB backup.",
    )
    parser.add_argument("--no-eject", action="store_true", help="Do not eject the camera card.")
    parser.add_argument(
        "--sha256", action="store_true", help="Use SHA-256 for all copy verification."
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    return parser.parse_args(argv)


def print_summary(
    dcim_folder: Path,
    destinations: Sequence[Path],
    stats: transfer.ImportStats,
    eject_attempted: bool,
    eject_success: bool,
) -> None:
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
    print(
        f"Eject:                {'SUCCESS' if eject_attempted and eject_success else 'FAILED' if eject_attempted else 'not requested'}"
    )
    if stats.errors:
        print("\nErrors:")
        for source, message in stats.errors:
            print(f"  - {source}: {message}")
    print("=" * 64)


def _raise_keyboard_interrupt(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"terminated by signal {signum}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the importer, treating SIGTERM (service stop, launchd) like Ctrl-C.

    That routes a stop request through the existing cancel path: the session lock
    is released, temp files are cleaned up, and the dashboard sees "canceled"
    instead of a status file frozen on "copying".
    """
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except ValueError:  # not the main thread (e.g. called from a test harness)
        pass
    _RUN.clear()
    _RUN_LOG.clear()
    try:
        exit_code = _main(argv)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    _finish_run_record(exit_code)
    return exit_code


def _log_provenance(argv: Optional[Sequence[str]]) -> str:
    build_info = load_build_info()
    run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    logger.info(
        "Run provenance: run_id=%s version=%s git_commit=%s git_branch=%s "
        "git_tag=%s git_dirty=%s build_time_utc=%s python=%s executable=%s "
        "script=%s argv=%r",
        run_id,
        build_info.get("version", "unknown"),
        build_info.get("git_commit", "unknown"),
        build_info.get("git_branch", "unknown"),
        build_info.get("git_tag", "unknown"),
        build_info.get("git_dirty", "unknown"),
        build_info.get("build_time_utc", "unknown"),
        sys.version.split()[0],
        sys.executable,
        build_info.get("source_path", str(Path(__file__).resolve())),
        list(argv) if argv is not None else sys.argv[1:],
    )
    return run_id


def _run_catalog_update() -> int:
    _RUN["kind"] = "catalog"
    logger.info("Starting explicit file catalog update.")
    catalog_stats = catalog_refresh.update_file_catalog()
    logger.info("Catalog update summary: %s", catalog_stats)
    print("Catalog update complete:")
    for key, value in catalog_stats.items():
        print(f"  {key}: {value}")
    return 0


def _remove_usb_markers() -> None:
    for marker in (config.USB_YIELD_PATH, config.USB_RESUME_PATH):
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", marker, exc)


def _pause_usb_backup(stats: transfer.ImportStats) -> int:
    """Leave word for the trigger to carry on after the card. This isn't a finished run."""
    _RUN.clear()
    _remove_usb_markers()
    identity = volumes.get_volume_identity(config.USB_DEST_VOLUME)
    try:
        text = f"{identity.volume_uuid if identity else ''}\n"
        text += f"{stats.destination_copies}\n{stats.to_copy}\n"
        temporary = config.USB_RESUME_PATH.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(config.USB_RESUME_PATH)
    except OSError as exc:  # the drive still shows as behind, and Back up now still works
        logger.warning("Could not save where the USB backup stopped: %s", exc)
    progress.update_status(
        state="paused", message="USB backup paused for a camera card", current_file=""
    )
    return 0


def _run_usb_backup() -> int:
    _RUN.update(kind="usb_backup", usb=True)
    logger.info("Starting independent local-to-USB backup.")
    _remove_usb_markers()  # a request left from an earlier run must not stop this one at once
    backup_stats = transfer.copy_local_archive_to_usb(may_pause=True)
    if backup_stats.paused:
        return _pause_usb_backup(backup_stats)
    _RUN.update(
        usb_stats=backup_stats,
        files=backup_stats.found,
        copied=backup_stats.destination_copies,
        skipped=backup_stats.destination_skips,
        failed=backup_stats.failed,
    )
    print_summary(
        config.LOCAL_DEST_ROOT,
        [config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT, config.USB_DEST_ROOT],
        backup_stats,
        False,
        False,
    )
    if backup_stats.failed:
        summary = _usb_summary(backup_stats)
        message = (
            run_report.usb_message(summary)
            if summary["problems"]
            else f"USB backup incomplete: {backup_stats.failed} failure(s)"
        )
        progress.update_status(message=message)
        logs.notify("USB Backup Incomplete", message)
        return 1
    logs.notify(
        "USB Backup Complete",
        f"{backup_stats.destination_copies} files copied; "
        f"{backup_stats.destination_skips} already matched.",
    )
    return 0


def _remember_card(dcim_folder: Path, card_volume: Path) -> None:
    """Note which card this is, so it can be told apart and its earlier self, formatted, forgotten."""
    identity = card_identity.read(card_volume)
    name = str(identity.pop("volume_name", "") or card_volume.name)
    logger.info(
        "Card: %s %s", name.strip(), card_identity.label(identity) or "(size and kind unknown)"
    )
    try:
        card.remember_identity(dcim_folder, identity, name)
        card_id = _RUN.get("card_id")
        if card_id:
            card_status.forget_formatted(str(card_id), identity, name)
    except OSError as exc:  # only reporting; the import goes on
        logger.warning("Could not note which card this is: %s", exc)


def _note_safe_cards() -> None:
    """Say when a card has become safe to format: now, or after a backup days later."""
    newly = card_status.record()
    if len(newly) == 1:
        status = newly[0]
        name = " · ".join(part for part in (status.camera, status.label) if part)
        logs.notify(
            "Safe to Format",
            f"{name}: all {status.files:,} files are on the Mac and the USB drive.",
        )
    elif newly:
        logs.notify(
            "Safe to Format",
            f"{len(newly)} cards now have every file on the Mac and the USB drive.",
        )


def _scan_card(
    args: argparse.Namespace,
) -> Tuple[Path, Path, Dict[str, List[Path]], List[Path]]:
    """Find the card, list its media, reconcile the card manifest and read photo info.

    Also returns what could not be read, because that makes the import incomplete.
    """
    if args.source:
        logger.info("Using explicit source path: %s", args.source)
        dcim_folder = card.resolve_dcim_path(args.source)
    else:
        dcim_folder = card.find_card_dcim()
    card_volume = dcim_folder.parent.resolve()
    _RUN.update(source=card_volume.name, dcim=dcim_folder, card_id=card.card_volume_id(dcim_folder))
    logger.info("Source card volume: %s", card_volume)
    _remember_card(dcim_folder, card_volume)
    progress.update_status(
        source=str(card_volume),
        state="scanning",
        phase="card-to-ssd",
        stage="scan",
        files_total=0,
        files_done=0,
        new_files=0,
        new_media_files=0,
        expected_new_files=None,
        expected_new_bytes=None,
        new_by_category={},
        new_bytes=0,
        usb_planned=False,
        message="Scanning camera card",
    )
    logger.info("Scanning: %s", dcim_folder)
    time.sleep(1)
    unreadable: List[Path] = []
    files = card.scan_with_retry(dcim_folder, unreadable)
    _RUN.update(unreadable=unreadable, scan_files=files)
    if not any(files.values()):
        if unreadable:
            raise RuntimeError(
                run_report.dont_format_message(
                    run_report.summarize([], unreadable, {}, dcim_folder)
                )
            )
        raise RuntimeError(f"No supported media files found under {dcim_folder}")

    total_found = sum(len(items) for items in files.values())
    # Now, while the card still answers: a report gives the size of any file it later fails to read.
    sizes = {str(path): path.stat().st_size for paths in files.values() for path in paths}
    _RUN.update(files=total_found, bytes=sum(sizes.values()), sizes=sizes)
    logger.info(
        "Scan complete: %d files (%d JPG, %d RAW, %d VIDEO, %d other)",
        total_found,
        len(files["JPG"]),
        len(files["RAW"]),
        len(files["VIDEO"]),
        len(files["OTHER"]),
    )
    if files["OTHER"]:
        kinds: Dict[str, int] = {}
        for path in files["OTHER"]:
            extension = path.suffix.lower() or "(no extension)"
            kinds[extension] = kinds.get(extension, 0) + 1
        logger.info(
            "Unrecognized file types will be copied to 'Other': %s",
            ", ".join(f"{ext} x{count}" for ext, count in sorted(kinds.items())),
        )
    # Reconcile the live card inventory with the previous manifest. The
    # manifest is diagnostic/cache data only; copy decisions still verify destinations.
    reconciliation = card.reconcile_card_manifest(dcim_folder, files)
    logger.info("Card manifest reconciliation: %s", reconciliation)
    expected_new = card.expected_new_files(dcim_folder, files)
    logger.info("Files expected to be new (files, bytes): %s", expected_new)
    if expected_new:
        progress.update_status(
            expected_new_files=expected_new[0], expected_new_bytes=expected_new[1]
        )
    # Record discovery immediately, but do not advance successful timestamps
    # until destination copies have been verified.
    card.update_card_scan_history(dcim_folder, files, "scanning", 0, total_found)
    metadata_started = time.monotonic()
    prefetched = media_info.prefetch_card_metadata(files)
    if prefetched:
        logger.info(
            "Read photo info for %d new RAW/video files in %.1fs.",
            prefetched,
            time.monotonic() - metadata_started,
        )
    return dcim_folder, card_volume, files, unreadable


def _check_local_space(files: Dict[str, List[Path]], dcim_folder: Path) -> None:
    """Make sure the files that look new fit, before anything is copied.

    A file already saved takes no space, so it doesn't count. The list of saved files is only an
    estimate: if it's wrong and the disk fills up, the copy of that file fails and is reported.
    """
    manifest = card.load_card_manifest(dcim_folder)
    volumes.validate_space(
        {
            config.LOCAL_DEST_ROOT: card.bytes_to_copy(
                [path for category in ("JPG", "VIDEO", "OTHER") for path in files[category]],
                manifest,
                dcim_folder,
            ),
            config.LOCAL_RAW_DEST_ROOT: card.bytes_to_copy(files["RAW"], manifest, dcim_folder),
        }
    )
    logger.info(
        "Stage 1 destinations: %s and %s", config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT
    )


def _backup_after_import(usb_identity: Optional[volumes.VolumeIdentity]) -> transfer.ImportStats:
    """Stage 2: mirror the verified Mac archive to the same USB drive seen at the start."""
    backup_stats = transfer.ImportStats()
    if not volumes.usb_is_mounted() or not volumes.volume_identity_matches(
        usb_identity, volumes.get_volume_identity(config.USB_DEST_VOLUME)
    ):
        backup_stats.failed += 1
        message = "Configured USB backup volume was disconnected or replaced before backup."
        backup_stats.errors.append((str(config.USB_DEST_VOLUME), message))
        logger.error(message)
        return backup_stats
    try:
        backup_stats = transfer.copy_local_archive_to_usb(usb_identity)
        _RUN["usb_copied"] = backup_stats.destination_copies
        if backup_stats.destination_copies:  # with nothing to copy, the counter is still the card's
            _RUN["usb_bytes"] = int(progress._PROGRESS.get("bytes_transferred", 0) or 0)
    except Exception as exc:
        backup_stats.failed += 1
        backup_stats.errors.append((str(config.USB_DEST_VOLUME), str(exc)))
        logger.exception("USB backup stage failed: %s", exc)
    return backup_stats


def _copy_card_to_mac(
    dcim_folder: Path,
    files: Dict[str, List[Path]],
    local_stats: transfer.ImportStats,
    usb_expected: bool,
) -> None:
    """Stage 1: card -> Mac archive."""
    progress._PROGRESS_STARTED = time.monotonic()
    progress._PROGRESS.update(
        {
            "bytes_total": local_stats.source_bytes,
            "bytes_done": 0,
            "bytes_transferred": 0,
            "state": "copying",
            "stage": "copy",
            "files_total": local_stats.found,
            "files_done": 0,
            "usb_planned": bool(usb_expected),
            "phase": "card-to-ssd",
            "message": "Copying camera card to Mac SSD",
        }
    )
    progress.update_status()
    for category in card.MEDIA_CATEGORIES:
        category_root = media_info.local_root_for_category(category)
        transfer.copy_category(files[category], dcim_folder, [category_root], category, local_stats)
    transfer.retry_read_errors(dcim_folder, local_stats)

    _RUN.update(
        copied=local_stats.destination_copies,
        skipped=local_stats.destination_skips,
        failed=local_stats.failed,
    )


def _report_local_failure(
    dcim_folder: Path,
    files: Dict[str, List[Path]],
    destinations: List[Path],
    local_stats: transfer.ImportStats,
) -> None:
    print_summary(dcim_folder, destinations, local_stats, False, False)
    message = run_report.dont_format_message(
        run_report.summarize(
            local_stats.outcomes,
            _RUN.get("unreadable", []),  # type: ignore[arg-type]
            {},
            dcim_folder,
        )
    )
    progress.update_status(
        state="failed",
        phase="card-to-ssd",
        message=message,
        current_file="",
        current_path="",
    )
    card.update_card_scan_history(
        dcim_folder, files, "incomplete", local_stats.verified_copies, local_stats.failed
    )
    logger.error("Local SSD import failed; card was not ejected and USB backup was not started.")
    logs.notify("Photo Import Incomplete", message)


def _new_files_text(copied: int) -> str:
    if not copied:
        return "No new files: the Mac already had everything on this card."
    return f"{copied:,} new file{'' if copied == 1 else 's'} copied to the Mac."


def _report_outcome(
    local_stats: transfer.ImportStats,
    backup_stats: transfer.ImportStats,
    usb_expected: bool,
    eject_attempted: bool,
    eject_success: bool,
) -> int:
    """Say how a verified local import ended and start the upload. Returns the exit code.

    The upload starts whatever happened to USB or the eject: each stage stands alone, and the
    photos are verified on the Mac, so those problems must not also keep them from Google Photos.
    """
    launch_google_photos_sync()
    if backup_stats.failed:
        summary = _usb_summary(backup_stats)
        progress.update_status(
            state="failed",
            phase="usb-backup",
            message=run_report.usb_message(summary)
            if summary["problems"]
            else f"Local import succeeded; USB backup has {backup_stats.failed} failure(s)",
            current_file="",
            current_path="",
        )
        logs.notify(
            "USB Backup Incomplete", "The card was copied to the Mac. USB backup needs attention."
        )
        return 1
    if eject_attempted and not eject_success:
        logger.error("Local import and USB backup succeeded, but automatic card ejection failed.")
        logs.notify(
            "Photo Import Complete — Eject Failed",
            "Copies completed, but the camera card could not be ejected.",
        )
        return 2
    logger.info(
        "Photo Offloader finished. Local copies: %d; USB copies: %d; USB skips: %d.",
        local_stats.destination_copies,
        backup_stats.destination_copies,
        backup_stats.destination_skips,
    )
    logs.notify(
        "Photo Import Complete",
        f"{_new_files_text(local_stats.destination_copies)} USB backup "
        + ("completed." if usb_expected else "was not run."),
    )
    return 0


def _import_card(args: argparse.Namespace) -> int:
    dcim_folder, card_volume, files, unreadable = _scan_card(args)
    local_stats = transfer.ImportStats(found=int(_RUN["files"]), source_bytes=int(_RUN["bytes"]))
    local_stats.add_unreadable(unreadable)
    _RUN["stats"] = local_stats

    usb_expected = not args.no_usb and volumes.usb_is_mounted()
    usb_identity = volumes.get_volume_identity(config.USB_DEST_VOLUME) if usb_expected else None
    local_destinations = [config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT]
    _check_local_space(files, dcim_folder)
    if usb_expected:
        logger.info("USB backup is mounted; it will be synchronized after the local SSD import.")
    else:
        logger.info("USB backup not selected or not mounted; local SSD import only.")

    with _timed("copy_seconds"):
        _copy_card_to_mac(dcim_folder, files, local_stats, usb_expected)
    if local_stats.failed:
        _report_local_failure(dcim_folder, files, local_destinations, local_stats)
        return 1

    _RUN["local_verified"] = True
    card.update_card_scan_history(dcim_folder, files, "complete", local_stats.verified_copies, 0)
    # The card is safe to release as soon as the complete local archive is verified.
    eject_attempted = bool(config.EJECT_CARD_AFTER_SUCCESS and not args.no_eject)
    eject_success = volumes.eject_volume(card_volume) if eject_attempted else False

    # Stage 2: Mac archive -> USB.
    _RUN["usb"] = bool(usb_expected)
    if usb_expected:
        with _timed("usb_seconds"):
            backup_stats = _backup_after_import(usb_identity)
        _RUN["usb_stats"] = backup_stats
    else:
        backup_stats = transfer.ImportStats()
        progress.update_status(
            state="complete",
            phase="card-to-ssd",
            message="Mac SSD import complete; USB backup skipped",
            current_file="",
            current_path="",
        )

    print_summary(
        dcim_folder,
        local_destinations + ([config.USB_DEST_ROOT] if usb_expected else []),
        backup_stats if usb_expected else local_stats,
        eject_attempted,
        eject_success,
    )
    return _report_outcome(local_stats, backup_stats, usb_expected, eject_attempted, eject_success)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config.VERIFY_WITH_SHA256 = args.sha256
    configure_logging(args.verbose)
    run_id = _log_provenance(argv)
    try:
        with import_session_lock():
            # Do not reset shared dashboard status until this process owns the
            # session lock; a duplicate launch must not hide the active session.
            logger.info("--- Starting new photo offloader session run_id=%s ---", run_id)
            awake.keep_awake()
            _RUN.update(kind="import", started_at=time.time())
            progress._PROGRESS_STARTED = time.monotonic()
            progress._PROGRESS.update(
                {
                    "state": "scanning",
                    "phase": "startup",
                    "bytes_done": 0,
                    "bytes_total": 0,
                    "bytes_transferred": 0,
                    "current_file": "",
                    "source": "",
                    "message": "Preparing Photo Offloader",
                }
            )
            progress.update_status()
            config.check_settings()
            if args.update_catalog:
                return _run_catalog_update()
            if args.backup_usb:
                return _run_usb_backup()
            return _import_card(args)
    except KeyboardInterrupt:
        progress.update_status(state="canceled", message="Operation canceled", current_file="")
        logger.warning("Process canceled by user.")
        logs.notify("Photo Offloader Canceled", "The operation was canceled.")
        return 130
    except SessionBusyError:
        logger.warning(
            "Duplicate launch ignored because another Photo Offloader session owns the lock."
        )
        return 0
    except Exception as exc:
        progress.update_status(state="failed", message=str(exc), current_file="")
        logger.exception("Fatal error: %s", exc)
        logs.notify("Photo Offloader Stopped", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
