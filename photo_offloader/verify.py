"""Recheck the saved copies against the hashes recorded when they were saved.

A copy is verified once, when it's made. This finds what happens to it afterwards: a failing disk,
a deleted file, a copy that has quietly stopped matching the other one. It only reports. Nothing
here changes a saved file or the catalog, because which copy is right is for a person to decide.

It's its own program, without the import session lock or status.json, because a check takes many
minutes: a card inserted meanwhile must still import, and the trigger and the dashboard take any
running importer to mean an import is under way. Its progress has a file of its own.
"""

from __future__ import annotations

import argparse
import fcntl
import signal
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from photo_offloader import (
    awake,
    catalog,
    catalog_refresh,
    config,
    fileops,
    importer,
    jsonfile,
    logs,
    settings,
    verify_report,
    volumes,
)
from photo_offloader.logs import logger
from photo_offloader.verify_report import (
    BOTH,
    CHANGED,
    COPIES_DIFFER,
    DAMAGED,
    MAC,
    MISSING,
    ONLY_ON_DRIVE,
    OTHER_BAD,
    OTHER_GOOD,
    OTHER_ONLY,
    OTHER_UNKNOWN,
    OTHER_UNSURE,
    UNREADABLE,
    USB,
    plural,
)

PROGRESS_SECONDS = 5.0  # between lines in the terminal
STATUS_SECONDS = 1.0  # between updates of the progress file
MEASURED_SPEED_AFTER_SECONDS = 3.0  # before this, a location's own speed is mostly noise
SPEED_SAVED_AFTER_SECONDS = 10.0  # a shorter read mostly measures opening files, not the disk
EXIT_OK, EXIT_PROBLEMS, EXIT_CANCELED = 0, 1, 130


@dataclass
class Problem:
    where: str
    kind: str
    path: Path
    relative: str
    expected: str = ""
    actual: str = ""
    other: str = ""
    size: int = 0


@dataclass
class _Entry:
    path: Path
    size: int = 0
    mtime_ns: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class _Location:
    name: str
    files: Dict[str, _Entry] = field(default_factory=dict)
    actual: Dict[str, str] = field(default_factory=dict)  # read, and not found to be wrong
    unverified: Set[str] = field(default_factory=set)  # read, but with nothing to compare it to
    gone: Set[str] = field(default_factory=set)  # noted as deleted, and it is
    finished: bool = False


@dataclass
class ArchiveCheck:
    problems: List[Problem] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=lambda: {MAC: 0, USB: 0})
    not_checked: int = 0
    waiting_for_backup: int = 0
    kept_on_drive: int = 0
    bytes_read: int = 0
    usb_skipped: bool = False
    drive_gone: bool = False
    canceled: bool = False
    error: str = ""
    speeds: Dict[str, float] = field(default_factory=dict)  # bytes a second, for the next estimate
    drive_id: str = ""


class _Busy(RuntimeError):
    """Another check is running."""


def _load_location(name: str, roots: Sequence[Path]) -> _Location:
    location = _Location(name)
    for root in roots:
        resolved = root.resolve()
        for relative, (size, mtime_ns, sha256) in catalog.archive_records(resolved).items():
            location.files[relative] = _Entry(resolved / relative, size, mtime_ns, sha256)
        # Files the catalog doesn't list can still be compared with their other copy.
        for path in catalog_refresh.media_files(resolved):
            location.files.setdefault(str(path.relative_to(resolved)), _Entry(path))
    return location


def _problem_with(entry: _Entry, expected: Optional[str]) -> Tuple[Optional[str], str]:
    """What's wrong with this copy, if anything, and the hash of what was read."""
    try:
        fingerprint = fileops.file_fingerprint(entry.path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING, ""
    except OSError as exc:
        logger.error("Could not read %s: %s", entry.path, exc)
        return UNREADABLE, ""
    try:
        # Otherwise an upload's or a viewer's earlier read can answer for the disk.
        if not fileops.forget_cached_pages(entry.path):
            logger.debug("%s may be read from memory, not from the disk.", entry.path)
        actual = fileops.sha256_file(entry.path, nocache=True)
    except OSError as exc:
        logger.error("Could not read %s: %s", entry.path, exc)
        return UNREADABLE, ""
    if expected is None or actual == expected:
        return None, actual
    untouched = entry.sha256 and fileops.card_fingerprint_unchanged(
        {"size": entry.size, "mtime_ns": entry.mtime_ns}, fingerprint
    )
    return (DAMAGED if untouched else CHANGED), actual


def _drive_still_there(identity: Optional[volumes.VolumeIdentity]) -> bool:
    return volumes.usb_is_mounted() and volumes.volume_identity_matches(
        identity, volumes.get_volume_identity(config.USB_DEST_VOLUME)
    )


def seconds_left(
    done_here: float,
    left_here: float,
    elapsed: float,
    usual_speed: float,
    later_bytes: float = 0,
    later_speed: float = verify_report.USB_BYTES_PER_SECOND,
) -> int:
    """Time left: this location at its own speed if there's one yet, and the drive still to come."""
    measured = done_here / elapsed if elapsed >= MEASURED_SPEED_AFTER_SECONDS and done_here else 0
    here = left_here / (measured or usual_speed)
    return round(here + later_bytes / later_speed)


def _write_status(**fields: object) -> None:
    try:
        jsonfile.write_json(config.VERIFY_STATUS_PATH, fields)
    except OSError as exc:  # only the dashboard's progress: the check itself goes on
        logger.debug("Could not save the check's progress: %s", exc)


class _Progress:
    """A line every few seconds in the terminal and a file for the dashboard: this takes minutes."""

    def __init__(
        self,
        mac: _Location,
        usb: _Location,
        usb_planned: bool,
        speeds: Optional[Dict[str, float]] = None,
    ) -> None:
        self.speeds = {**verify_report.USUAL_SPEEDS, **(speeds or {})}
        self.sizes = {location.name: _total(location) for location in (mac, usb)}
        self.counts = {location.name: len(location.files) for location in (mac, usb)}
        self.usb_planned = usb_planned
        self.where = MAC
        self.files = sum(self.counts.values())
        self.total = sum(self.sizes.values())
        self.done_files = 0
        self.done_bytes = 0
        self.done_here = 0
        self.started_here = self.printed = self.saved = time.monotonic()

    def start(self, name: str) -> None:
        self.where = name
        self.done_here = 0
        self.started_here = time.monotonic()
        print(
            f"Checking {plural(self.counts[name], 'file')} on the {name} "
            f"({self.sizes[name] / 1e9:.1f} GB)"
        )
        self._save()

    def _save(self) -> None:
        on_mac = self.where == MAC
        _write_status(
            where=self.where,
            usb=self.usb_planned,
            files_done=self.done_files,
            files_total=self.files,
            bytes_done=self.done_bytes,
            bytes_total=self.total,
            seconds_left=seconds_left(
                self.done_here,
                self.sizes[self.where] - self.done_here,
                time.monotonic() - self.started_here,
                self.speeds[self.where],
                self.sizes[USB] if on_mac else 0,
                self.speeds[USB],
            ),
        )

    def advance(self, entry: _Entry) -> None:
        self.done_files += 1
        self.done_bytes += entry.size
        self.done_here += entry.size
        now = time.monotonic()
        if now - self.saved >= STATUS_SECONDS:
            self.saved = now
            self._save()
        if now - self.printed >= PROGRESS_SECONDS:
            self.printed = now
            print(
                f"  {self.done_files:,} of {self.files:,} files, "
                f"{self.done_bytes / 1e9:.1f} of {self.total / 1e9:.1f} GB",
                flush=True,
            )


def _total(location: _Location) -> int:
    return sum(entry.size for entry in location.files.values())


def _check_location(
    check: ArchiveCheck,
    location: _Location,
    other: _Location,
    shown: _Progress,
    noted: Set[str] = frozenset(),  # type: ignore[assignment]
    identity: Optional[volumes.VolumeIdentity] = None,
) -> None:
    on_drive = location.name == USB
    shown.start(location.name)
    started, read_before = time.monotonic(), check.bytes_read
    for relative in sorted(location.files):
        entry = location.files[relative]
        other_entry = other.files.get(relative)
        expected = entry.sha256 or (other_entry.sha256 if other_entry else None)
        kind, actual = _problem_with(entry, expected)
        if kind == MISSING and relative in noted:
            location.gone.add(relative)
            shown.advance(entry)
            continue
        if kind and on_drive and not _drive_still_there(identity):
            # Everything left would look missing or different, and none of it would be true.
            logger.error("The USB drive went away during the check.")
            check.drive_gone = True  # and its speed so far may be that of a drive going away
            return
        check.checked[location.name] += 1
        check.bytes_read += entry.size
        shown.advance(entry)
        if kind:
            _add_problem(
                check,
                Problem(
                    location.name,
                    kind,
                    entry.path,
                    relative,
                    expected or "",
                    actual,
                    "",
                    entry.size,
                ),
            )
            continue
        location.actual[relative] = actual
        if expected is None:
            location.unverified.add(relative)
            check.not_checked += 1
    location.finished = True
    elapsed = time.monotonic() - started
    if elapsed >= SPEED_SAVED_AFTER_SECONDS:
        check.speeds[location.name] = (check.bytes_read - read_before) / elapsed


def _add_problem(check: ArchiveCheck, problem: Problem) -> None:
    check.problems.append(problem)
    logger.error("%s, %s: %s", problem.where, problem.kind, problem.path)


def _compare_copies(check: ArchiveCheck, mac: _Location, usb: _Location, noted: Set[str]) -> None:
    """Each copy can match its own hash and still differ: a backup records a differing file's hash."""
    for relative in sorted(mac.actual.keys() & usb.actual.keys()):
        if mac.actual[relative] != usb.actual[relative]:
            _add_problem(
                check,
                Problem(
                    BOTH,
                    COPIES_DIFFER,
                    mac.files[relative].path,
                    relative,
                    mac.actual[relative],
                    usb.actual[relative],
                    OTHER_UNSURE,
                    mac.files[relative].size,
                ),
            )
    for relative in sorted(usb.actual.keys() - mac.files.keys() - noted):
        # A catalog update forgets a deleted file, so this may be all that's left to say about it.
        entry = usb.files[relative]
        _add_problem(
            check,
            Problem(USB, ONLY_ON_DRIVE, entry.path, relative, other=OTHER_ONLY, size=entry.size),
        )


def _settle_deleted(check: ArchiveCheck, mac: _Location, usb: _Location, noted: Set[str]) -> None:
    """Count the noted files, and drop the ones the note no longer describes."""
    back = {name for name in noted if name in mac.files and name not in mac.gone}
    # A catalog update forgets a file gone from both places, so this is the last trace of the only
    # copy. Say so once: no drive is ever going to bring it back on its own.
    lost = sorted(name for name in noted - back if usb.finished and name not in usb.files)
    for relative in lost:
        path = config.USB_DEST_ROOT.resolve() / relative
        _add_problem(check, Problem(USB, MISSING, path, relative, other=OTHER_BAD))
    check.kept_on_drive = len(noted) - len(back) - len(lost)
    if back or lost:
        try:
            verify_report.forget_deleted(back.union(lost))
        except OSError as exc:  # the same files are looked at again next time
            logger.warning("Could not update the list of deleted files: %s", exc)


def _other_copy(problem: Problem, mac: _Location, usb: _Location) -> str:
    other = usb if problem.where == MAC else mac
    if problem.relative in other.actual and problem.relative not in other.unverified:
        return OTHER_GOOD
    return OTHER_BAD if other.finished else OTHER_UNKNOWN


def check_archives(check: Optional[ArchiveCheck] = None) -> ArchiveCheck:
    """Read every saved copy, filling in ``check`` as it goes so a cancel keeps what was found."""
    check = check if check is not None else ArchiveCheck()
    noted = verify_report.deleted_files()
    mac = _load_location(MAC, (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT))
    usb = _Location(USB)
    identity = None
    if volumes.usb_is_mounted() and config.USB_DEST_ROOT.is_dir():
        identity = volumes.get_volume_identity(config.USB_DEST_VOLUME)
        check.drive_id = (identity.volume_uuid or "") if identity else ""
        usb = _load_location(USB, (config.USB_DEST_ROOT,))
    else:
        check.usb_skipped = True
        logger.info("The USB drive isn't connected, so only the Mac's copies are being checked.")
    history = catalog.list_import_history(limit=catalog.IMPORT_HISTORY_MAX_ROWS)
    speeds = verify_report.measured_speeds(history, check.drive_id or None)
    shown = _Progress(mac, usb, not check.usb_skipped, speeds)
    _check_location(check, mac, usb, shown, noted)
    if not check.usb_skipped:
        _check_location(check, usb, mac, shown, identity=identity)
        check.waiting_for_backup = len(mac.actual.keys() - usb.files.keys())
        _compare_copies(check, mac, usb, noted)
    _settle_deleted(check, mac, usb, noted)
    for problem in check.problems:
        problem.other = problem.other or _other_copy(problem, mac, usb)
    return check


# ---- saying what it found ---------------------------------------------------------------------


def _copies(count: int) -> str:
    return f"{count:,} " + ("copy" if count == 1 else "copies")


def _clean_text(check: ArchiveCheck, total: int) -> str:
    matched = total - check.not_checked
    if not total:
        return "There were no saved files to check."
    if not matched:
        return "Nothing could be compared."
    return f"All {_copies(matched)} match." if matched > 1 else "The 1 copy matches."


def describe(check: ArchiveCheck) -> str:
    """One line for the terminal and for the dashboard's Recent activity."""
    if check.error:
        return f"Could not finish the check: {check.error}"
    total = sum(check.checked.values())
    parts: List[str] = []
    if check.canceled:
        parts.append(f"Canceled after {_copies(total)}.")
    if check.drive_gone:
        parts.append(
            f"The USB drive was disconnected or replaced, so the check stopped after {_copies(total)}."
        )
    if check.problems:
        parts.append(verify_report.message_for(_report_rows(check)))
    elif not (check.canceled or check.drive_gone):
        parts.append(_clean_text(check, total))
    if check.usb_skipped:
        parts.append("The USB drive wasn't connected, so only the Mac was checked.")
    if check.not_checked:
        parts.append(f"{plural(check.not_checked, 'file')} had no saved hash to compare with.")
    if check.kept_on_drive:
        verb = "is" if check.kept_on_drive == 1 else "are"
        parts.append(f"{plural(check.kept_on_drive, 'file')} {verb} only on the USB drive.")
    if check.waiting_for_backup:
        parts.append(f"{plural(check.waiting_for_backup, 'file')} still to back up to the drive.")
    return " ".join(parts)


def _report_rows(check: ArchiveCheck) -> List[Dict[str, object]]:
    return [
        {
            "where": problem.where,
            "problem": problem.kind,
            "other_copy": problem.other,
            "path": str(problem.path),
            "relative_path": problem.relative,
            "bytes": problem.size,
            "expected_sha256": problem.expected,
            "actual_sha256": problem.actual,
        }
        for problem in check.problems
    ]


def _save_report(check: ArchiveCheck, started_at: float) -> str:
    return verify_report.save_report(_report_rows(check), started_at)


def _saved_report(check: ArchiveCheck, started_at: float) -> str:
    if not check.problems:
        return ""
    try:
        return _save_report(check, started_at)
    except OSError as exc:  # the log already names each file
        logger.exception("Could not save the check's report: %s", exc)
        return ""


def _exit_code(check: ArchiveCheck) -> int:
    if check.canceled:
        return EXIT_CANCELED
    return EXIT_PROBLEMS if check.problems or check.drive_gone or check.error else EXIT_OK


def _record(check: ArchiveCheck, started_at: float, message: str, code: int, report: str) -> None:
    whole = not (check.canceled or check.drive_gone or check.error)
    details: Dict[str, object] = {"whole": whole}
    if check.problems:
        details["check"] = verify_report.details_for(_report_rows(check), report)
    if check.speeds:
        details["speeds"] = {name: round(speed) for name, speed in check.speeds.items()}
    if check.drive_id:
        details["drive_id"] = check.drive_id
    catalog.record_import_run(
        kind="verify",
        outcome={EXIT_OK: "complete", EXIT_CANCELED: "canceled"}.get(code, "failed"),
        started_at=started_at,
        finished_at=time.time(),
        files=sum(check.checked.values()),
        skipped=check.not_checked,
        failed=len(check.problems),
        bytes=check.bytes_read,
        usb=bool(check.checked[USB]),
        message=message,
        details=details,
    )


def _run() -> int:
    started_at = time.time()
    check = ArchiveCheck()
    logger.info("Starting a check of the saved copies.")
    # Before the archives are listed, so a check started from the dashboard shows straight away.
    _write_status(where="", usb=False)
    try:
        config.check_settings()
        check_archives(check)
    except settings.SettingsProblem as exc:
        logger.error("%s", exc)
        check.error = str(exc)
    except KeyboardInterrupt:
        check.canceled = True
    except (OSError, sqlite3.Error) as exc:  # never mistake a broken catalog for an empty one
        logger.exception("The check could not finish: %s", exc)
        check.error = str(exc)
    finally:
        config.VERIFY_STATUS_PATH.unlink(missing_ok=True)
    message, code = describe(check), _exit_code(check)
    logger.info("Check finished: %s", message)
    print(f"\n{message}")
    report = _saved_report(check, started_at)
    if report:
        print(f"Every problem is listed in {verify_report.report_csv(report)}")
    if check.problems:
        print(
            "Nothing was changed. Each problem says whether the other copy is good: restore from "
            "that copy by hand, or from the card if it still has the photo."
        )
        logs.notify("Saved Copies Need Attention", message)
    _record(check, started_at, message, code, report)
    return code


@contextmanager
def _only_one_check() -> Iterator[None]:
    config.VERIFY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.VERIFY_LOCK_PATH.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _Busy from exc
        yield


def main(argv: Optional[Sequence[str]] = None) -> int:
    argparse.ArgumentParser(
        description="Reread every saved copy and compare it with the hash recorded when it was "
        "saved. Only reports: nothing is changed."
    ).parse_args(argv)
    importer.configure_logging()
    previous = signal.signal(signal.SIGTERM, importer._raise_keyboard_interrupt)
    try:
        with _only_one_check():
            awake.keep_awake()
            return _run()
    except _Busy:
        print("A check is already running.")
        return EXIT_PROBLEMS
    finally:
        signal.signal(signal.SIGTERM, previous)
