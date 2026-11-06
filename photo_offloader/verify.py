"""Recheck the saved copies against the hashes recorded when they were saved.

A copy is verified once, when it's made. This finds what happens to it afterwards: a failing disk,
a deleted file, a copy that has quietly stopped matching the other one. It only reports. Nothing
here changes a file or the catalog, because which copy is right is for a person to decide.

It's its own program, without the import session lock or status.json, because a check takes many
minutes: a card inserted meanwhile must still import, and the trigger and the dashboard take any
running importer to mean an import is under way.
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
    catalog,
    catalog_refresh,
    config,
    fileops,
    importer,
    logs,
    run_report,
    volumes,
)
from photo_offloader.logs import logger

MAC = "Mac"
USB = "USB drive"
BOTH = "Both"

DAMAGED = "damaged"  # the contents changed, but not the size or date: nothing meant to do that
CHANGED = "changed"  # the contents differ, and something has also touched the file
MISSING = "missing"
UNREADABLE = "unreadable"
ONLY_ON_DRIVE = "only on the drive"
COPIES_DIFFER = "copies differ"
KINDS_IN_ORDER = (DAMAGED, CHANGED, MISSING, UNREADABLE, ONLY_ON_DRIVE, COPIES_DIFFER)

OTHER_GOOD = "good"
OTHER_BAD = "no good copy"
OTHER_UNKNOWN = "not checked"
OTHER_UNSURE = "can't tell which is right"

REPORTS_KEPT = 20
REPORT_COLUMNS = (
    "where",
    "problem",
    "other_copy",
    "path",
    "relative_path",
    "expected_sha256",
    "actual_sha256",
)
PROGRESS_SECONDS = 5.0
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
    finished: bool = False


@dataclass
class ArchiveCheck:
    problems: List[Problem] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=lambda: {MAC: 0, USB: 0})
    not_checked: int = 0
    waiting_for_backup: int = 0
    bytes_read: int = 0
    usb_skipped: bool = False
    drive_gone: bool = False
    canceled: bool = False
    error: str = ""


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


class _Progress:
    """A line every few seconds: a full check runs for many minutes."""

    def __init__(self, location: _Location) -> None:
        self.name = location.name
        self.files = len(location.files)
        self.total = sum(entry.size for entry in location.files.values())
        self.done_files = 0
        self.done_bytes = 0
        self.last = time.monotonic()
        print(
            f"Checking {_plural(self.files, 'file')} on the {self.name} ({self.total / 1e9:.1f} GB)"
        )

    def advance(self, entry: _Entry) -> None:
        self.done_files += 1
        self.done_bytes += entry.size
        now = time.monotonic()
        if now - self.last >= PROGRESS_SECONDS:
            self.last = now
            print(
                f"  {self.done_files:,} of {self.files:,} files, "
                f"{self.done_bytes / 1e9:.1f} of {self.total / 1e9:.1f} GB",
                flush=True,
            )


def _check_location(
    check: ArchiveCheck,
    location: _Location,
    other: _Location,
    identity: Optional[volumes.VolumeIdentity] = None,
) -> None:
    on_drive = location.name == USB
    shown = _Progress(location)
    for relative in sorted(location.files):
        entry = location.files[relative]
        other_entry = other.files.get(relative)
        expected = entry.sha256 or (other_entry.sha256 if other_entry else None)
        kind, actual = _problem_with(entry, expected)
        if kind and on_drive and not _drive_still_there(identity):
            # Everything left would look missing or different, and none of it would be true.
            logger.error("The USB drive went away during the check.")
            check.drive_gone = True
            return
        check.checked[location.name] += 1
        check.bytes_read += entry.size
        shown.advance(entry)
        if kind:
            _add_problem(
                check, Problem(location.name, kind, entry.path, relative, expected or "", actual)
            )
            continue
        location.actual[relative] = actual
        if expected is None:
            location.unverified.add(relative)
            check.not_checked += 1
    location.finished = True


def _add_problem(check: ArchiveCheck, problem: Problem) -> None:
    check.problems.append(problem)
    logger.error("%s, %s: %s", problem.where, problem.kind, problem.path)


def _compare_copies(check: ArchiveCheck, mac: _Location, usb: _Location) -> None:
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
                ),
            )
    for relative in sorted(usb.actual.keys() - mac.files.keys()):
        # A catalog update forgets a deleted file, so this may be all that's left to say about it.
        _add_problem(
            check, Problem(USB, ONLY_ON_DRIVE, usb.files[relative].path, relative, other=OTHER_BAD)
        )


def _other_copy(problem: Problem, mac: _Location, usb: _Location) -> str:
    other = usb if problem.where == MAC else mac
    if problem.relative in other.actual and problem.relative not in other.unverified:
        return OTHER_GOOD
    return OTHER_BAD if other.finished else OTHER_UNKNOWN


def check_archives(check: Optional[ArchiveCheck] = None) -> ArchiveCheck:
    """Read every saved copy, filling in ``check`` as it goes so a cancel keeps what was found."""
    check = check if check is not None else ArchiveCheck()
    mac = _load_location(MAC, (config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT))
    usb = _Location(USB)
    identity = None
    if volumes.usb_is_mounted() and config.USB_DEST_ROOT.is_dir():
        identity = volumes.get_volume_identity(config.USB_DEST_VOLUME)
        usb = _load_location(USB, (config.USB_DEST_ROOT,))
    else:
        check.usb_skipped = True
        logger.info("The USB drive isn't connected, so only the Mac's copies are being checked.")
    _check_location(check, mac, usb)
    if not check.usb_skipped:
        _check_location(check, usb, mac, identity)
        check.waiting_for_backup = len(mac.files.keys() - usb.files.keys())
        _compare_copies(check, mac, usb)
    for problem in check.problems:
        problem.other = problem.other or _other_copy(problem, mac, usb)
    return check


# ---- saying what it found ---------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def _copies(count: int) -> str:
    return f"{count:,} " + ("copy" if count == 1 else "copies")


def _problems_text(check: ArchiveCheck) -> str:
    counts = [
        f"{sum(1 for problem in check.problems if problem.kind == kind):,} {kind}"
        for kind in KINDS_IN_ORDER
        if any(problem.kind == kind for problem in check.problems)
    ]
    return f"{_plural(len(check.problems), 'problem')}: {', '.join(counts)}."


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
        parts.append(_problems_text(check))
    elif not (check.canceled or check.drive_gone):
        parts.append(_clean_text(check, total))
    if check.usb_skipped:
        parts.append("The USB drive wasn't connected, so only the Mac was checked.")
    if check.not_checked:
        parts.append(f"{_plural(check.not_checked, 'file')} had no saved hash to compare with.")
    if check.waiting_for_backup:
        parts.append(f"{_plural(check.waiting_for_backup, 'file')} still to back up to the drive.")
    return " ".join(parts)


def _save_report(check: ArchiveCheck, started_at: float) -> Path:
    folder = config.VERIFY_REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / (time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at)) + ".csv")
    run_report.write_csv(
        report,
        REPORT_COLUMNS,
        (
            {
                "where": problem.where,
                "problem": problem.kind,
                "other_copy": problem.other,
                "path": problem.path,
                "relative_path": problem.relative,
                "expected_sha256": problem.expected,
                "actual_sha256": problem.actual,
            }
            for problem in check.problems
        ),
    )
    for old in sorted(folder.glob("*.csv"))[:-REPORTS_KEPT]:
        old.unlink(missing_ok=True)
    return report


def _saved_report(check: ArchiveCheck, started_at: float) -> Optional[Path]:
    if not check.problems:
        return None
    try:
        return _save_report(check, started_at)
    except OSError as exc:  # the log already names each file
        logger.exception("Could not save the check's report: %s", exc)
        return None


def _exit_code(check: ArchiveCheck) -> int:
    if check.canceled:
        return EXIT_CANCELED
    return EXIT_PROBLEMS if check.problems or check.drive_gone or check.error else EXIT_OK


def _record(check: ArchiveCheck, started_at: float, message: str, code: int) -> None:
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
    )


def _run() -> int:
    started_at = time.time()
    check = ArchiveCheck()
    logger.info("Starting a check of the saved copies.")
    try:
        check_archives(check)
    except KeyboardInterrupt:
        check.canceled = True
    except (OSError, sqlite3.Error) as exc:  # never mistake a broken catalog for an empty one
        logger.exception("The check could not finish: %s", exc)
        check.error = str(exc)
    message, code = describe(check), _exit_code(check)
    logger.info("Check finished: %s", message)
    print(f"\n{message}")
    report = _saved_report(check, started_at)
    if report:
        print(f"Every problem is listed in {report}")
    if check.problems:
        print(
            "Nothing was changed. Each problem says whether the other copy is good: restore from "
            "that copy by hand, or from the card if it still has the photo."
        )
        logs.notify("Archive Check Found Problems", message)
    _record(check, started_at, message, code)
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
            return _run()
    except _Busy:
        print("A check is already running.")
        return EXIT_PROBLEMS
    finally:
        signal.signal(signal.SIGTERM, previous)
