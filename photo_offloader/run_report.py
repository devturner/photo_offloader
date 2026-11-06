"""What an import did with each file: the summary it keeps, its saved report and debug package.

A report is a convenience on top of a verified import, so the importer must not let a failure here
change the import's result. The history row keeps only ``summarize``'s bounded result; the folder
keeps every file. Names and reasons come from the card, so the page and the spreadsheet list both
treat them as plain text.
"""

from __future__ import annotations

import collections
import csv
import errno
import json
import logging
import platform
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from photo_offloader import config
from photo_offloader.logs import logger

FAILED = "failed"
SAVED_RESULTS = ("copied", "skipped")
READ_ERROR = "Read error"
MAC_READ_ERROR = "Couldn't read the Mac's copy"
DRIVE_GONE = "The drive was disconnected during the backup"
NOT_OVERWRITTEN = "A different file is already on the drive, so it wasn't overwritten"

TYPE_LABELS = {"RAW": "RAW", "JPG": "JPG", "VIDEO": "Movies", "OTHER": "Other"}
TYPE_ORDER = ("RAW", "JPG", "VIDEO", "OTHER")  # chosen by the user: rows never change places

NAMES_SHOWN_IN_FULL = 30  # a row names all its files up to here...
NAMES_SHOWN_WHEN_LONG = 20  # ...and past that, the first 20 and how many more
# The copy tries every file, so these are the last files on the card, all failing. Fewer than this
# could be a few bad photos. All must be read errors, so a full Mac doesn't get blamed on the card.
CARD_TROUBLE_MIN_FILES = 20

REPORTS_KEPT = 20  # of each of the two kinds below
PROBLEMS_SUFFIX, OK_SUFFIX = "-problems", "-ok"
RUN_LOG_MAX_LINES = 50_000
LOG_EXCERPT_LINES = 2_000
LOG_EXCERPT_MAX_BYTES = 1024 * 1024
VOLUME_INFO_TIMEOUT_SECONDS = 15

SUMMARY_FILE, FILES_FILE, RUN_LOG_FILE = "summary.txt", "files.csv", "run.log"
REPORT_FILE, VOLUME_FILE = "report.json", "card-info.txt"
PACKAGE_FILES = (SUMMARY_FILE, FILES_FILE, RUN_LOG_FILE, VOLUME_FILE)
CSV_COLUMNS = ("type", "result", "card_path", "archive_path", "usb_path", "bytes", "sha256")


@dataclass
class FileOutcome:
    """What one import did with one card file. ``result``: copied, skipped or failed."""

    category: str
    source: Path
    result: str
    archive_path: str = ""
    sha256: str = ""
    reason: str = ""
    retried: bool = False  # saved, but only on the second try: a card can warn of failing this way


@dataclass
class RunFacts:
    """Everything the importer knows about a run when it ends."""

    source: str
    started_at: float
    outcome: str
    message: str
    dcim: Optional[Path]  # None for a USB backup on its own
    problems: bool  # the run failed, so its report is kept apart from clean runs'
    outcomes: Sequence[FileOutcome] = ()
    unreadable: Sequence[Path] = ()
    files_not_reached: Sequence[Tuple[str, Path]] = ()
    sizes: Mapping[str, int] = field(default_factory=dict)
    usb_outcomes: Sequence[FileOutcome] = ()
    usb_sizes: Mapping[str, int] = field(default_factory=dict)
    archive_roots: Sequence[Path] = ()
    usb_errors: Sequence[Tuple[str, str]] = ()  # those about the drive, not any one file
    log_lines: Sequence[str] = ()
    app_version: str = ""
    volume_info: str = ""


class LogCapture(logging.Handler):
    """Keeps this run's log lines, because rotation can remove them from the log before anyone looks."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: Deque[str] = collections.deque(maxlen=RUN_LOG_MAX_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def clear(self) -> None:
        self.lines.clear()


def failure_reason(exc: BaseException, source: Path) -> str:
    """Why a file wasn't saved, in words. Only a failure on the card's own file is a read error."""
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return "The Mac ran out of space"
        if isinstance(exc, FileExistsError):
            return "A different file appeared in the archive"
        if exc.filename == str(source):
            return READ_ERROR
        text = str(exc)
        if "Source changed" in text:
            return "Changed during the copy"
        if "erification failed" in text:
            return "The copy didn't match when read back"
        if exc.strerror:
            return f"Couldn't write to the Mac: {exc.strerror}"
    return str(exc) or type(exc).__name__


def usb_failure_reason(exc: BaseException, source: Path) -> str:
    """Why a file didn't reach the drive. The Mac's copy failing to read is a different worry."""
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return "The USB drive ran out of space"
        if exc.filename == str(source):
            return MAC_READ_ERROR
        if "Local source changed" in str(exc):
            return "The Mac's copy changed during the backup"
    return "Couldn't write to the drive"


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def _on_card(path: Path, dcim: Path) -> str:
    try:
        return path.relative_to(dcim.parent).as_posix()
    except ValueError:
        return str(path)


def _short_name(path: Path, dcim: Path) -> str:
    try:
        return path.relative_to(dcim).as_posix()
    except ValueError:
        return _on_card(path, dcim)


def _counted(counts: Mapping[str, int], template: str) -> str:
    """One value alone, or each with its count: the reader shouldn't have to read a lone "(16)"."""
    if len(counts) == 1:
        return next(iter(counts))
    return " · ".join(template.format(name=name, count=count) for name, count in counts.items())


def _names(
    failed_files: Sequence[FileOutcome], short_name: Callable[[Path], str]
) -> Tuple[List[str], int]:
    several_folders = len({item.source.parent for item in failed_files}) > 1
    names = [
        short_name(item.source) if several_folders else item.source.name for item in failed_files
    ]
    if len(names) <= NAMES_SHOWN_IN_FULL:
        return names, 0
    return names[:NAMES_SHOWN_WHEN_LONG], len(names) - NAMES_SHOWN_WHEN_LONG


def _type_row(
    category: str,
    items: Sequence[FileOutcome],
    sizes: Mapping[str, int],
    folder_name: Callable[[Path], str],
    short_name: Callable[[Path], str],
) -> Dict[str, object]:
    size = lambda item: int(sizes.get(str(item.source), 0))  # noqa: E731
    lost = [item for item in items if item.result == FAILED]
    safe = [item for item in items if item.result != FAILED]
    folders = collections.Counter(folder_name(item.source.parent) for item in lost)
    reasons = collections.Counter(item.reason or "Couldn't be copied" for item in lost)
    names, more = _names(lost, short_name)
    return {
        "type": TYPE_LABELS[category],
        "lost": len(lost),
        "lost_bytes": sum(map(size, lost)),
        "safe": len(safe),
        "safe_bytes": sum(map(size, safe)),
        "total": len(items),
        "total_bytes": sum(map(size, items)),
        "where": _counted(dict(sorted(folders.items())), "{count:,} in {name}"),
        "why": _counted(dict(reasons.most_common()), "{name} ({count:,})"),
        "names": names,
        "more": more,
    }


def _pair_counts(outcomes: Sequence[FileOutcome]) -> Tuple[int, int, int, List[FileOutcome]]:
    """(lost both, RAW with JPG left, JPG with RAW left, failed files with no partner)."""
    by_shot: Dict[Tuple[Path, str], Dict[str, FileOutcome]] = {}
    for item in outcomes:
        if item.category in ("RAW", "JPG"):
            shot = (item.source.parent, item.source.stem.lower())
            by_shot.setdefault(shot, {})[item.category] = item
    both = raw_left = jpg_left = 0
    alone = [
        item for item in outcomes if item.result == FAILED and item.category not in ("RAW", "JPG")
    ]
    for shot in by_shot.values():
        raw, jpg = shot.get("RAW"), shot.get("JPG")
        if raw is None or jpg is None:
            alone.extend(item for item in (raw, jpg) if item and item.result == FAILED)
        elif raw.result == FAILED and jpg.result == FAILED:
            both += 1
        elif raw.result == FAILED:
            raw_left += 1
        elif jpg.result == FAILED:
            jpg_left += 1
    return both, raw_left, jpg_left, alone


def _one_copy_text(raw_left: int, jpg_left: int) -> str:
    if raw_left and jpg_left:
        return f"{raw_left + jpg_left:,} still have one copy."
    if raw_left:
        return (
            "1 RAW still has its JPG."
            if raw_left == 1
            else f"{raw_left:,} RAW still have their JPG."
        )
    if jpg_left:
        return (
            "1 JPG still has its RAW."
            if jpg_left == 1
            else f"{jpg_left:,} JPG still have their RAW."
        )
    return ""


def _not_backed_up_text(alone: Sequence[FileOutcome]) -> str:
    if not alone:
        return ""
    noun = "movie" if all(item.category == "VIDEO" for item in alone) else "file"
    verb = "wasn't" if len(alone) == 1 else "weren't"
    return f"{_plural(len(alone), noun)} {verb} backed up."


def _pairs_line(outcomes: Sequence[FileOutcome], unknown: Sequence[str]) -> str:
    """The line separating photos with nothing left from those with one copy still saved.

    A folder that couldn't be read could hold anything, so nothing here may sound like it covers
    that folder: this line could persuade someone to format the card.
    """
    both, raw_left, jpg_left, alone = _pair_counts(outcomes)
    if both:
        noun = "photo" if both == 1 else "photos"
        lead = f"{both:,} {noun} lost both RAW and JPG."
    elif unknown:
        lead = "No photo lost both copies in the folders that could be read."
    else:
        lead = "No photo lost both copies."
    left_out = ""
    if both and len(unknown) == 1:
        left_out = f"This doesn't count {unknown[0]}, which couldn't be read."
    elif both and unknown:
        left_out = f"This doesn't count the {len(unknown)} folders that couldn't be read."
    parts = (lead, _one_copy_text(raw_left, jpg_left), _not_backed_up_text(alone), left_out)
    return " ".join(part for part in parts if part)


def _likely_cause(outcomes: Sequence[FileOutcome]) -> Optional[Dict[str, str]]:
    """Every file from some point on failing to read looks like the card or reader, not the photos."""
    first = next((i for i, item in enumerate(outcomes) if item.result == FAILED), None)
    if first is None or len(outcomes) - first < CARD_TROUBLE_MIN_FILES:
        return None
    tail = outcomes[first:]
    if any(item.result != FAILED or item.reason != READ_ERROR for item in tail):
        return None
    which = (
        f"Every file after {outcomes[first - 1].source.name} failed"
        if first
        else "Every file failed"
    )
    return {
        "lead": "The card may have stopped responding.",
        "detail": f"{which}, so this looks like the card or reader, not the photos. "
        "Try another reader before anything else.",
    }


def _totals(
    outcomes: Sequence[FileOutcome],
    sizes: Mapping[str, int],
    folder_name: Callable[[Path], str],
    short_name: Callable[[Path], str],
) -> Dict[str, object]:
    """What both kinds of report have in common."""
    size = lambda item: int(sizes.get(str(item.source), 0))  # noqa: E731
    by_type: Dict[str, List[FileOutcome]] = {}
    for item in outcomes:
        by_type.setdefault(item.category, []).append(item)
    lost = [item for item in outcomes if item.result == FAILED]
    lost_types = {item.category for item in lost}
    return {
        "safe": len(outcomes) - len(lost),
        "safe_bytes": sum(size(item) for item in outcomes if item.result != FAILED),
        "not_saved": len(lost),
        "not_saved_bytes": sum(map(size, lost)),
        "types": [
            _type_row(category, by_type[category], sizes, folder_name, short_name)
            for category in TYPE_ORDER
            if category in lost_types
        ],
    }


def summarize(
    outcomes: Sequence[FileOutcome],
    unreadable: Sequence[Path],
    sizes: Mapping[str, int],
    dcim: Path,
) -> Dict[str, object]:
    """What a report shows. It stays small however many files failed, for the history row."""
    lost = [item for item in outcomes if item.result == FAILED]
    unknown = [_on_card(path, dcim) for path in unreadable]
    problems = len(lost) + len(unknown)
    return {
        **_totals(
            outcomes, sizes, lambda path: _on_card(path, dcim), lambda path: _short_name(path, dcim)
        ),
        "unknown": unknown,
        "problems": problems,
        "read_errors_only": all(item.reason == READ_ERROR for item in lost),
        "pairs": _pairs_line(outcomes, unknown) if problems else "",
        "cause": _likely_cause(outcomes),
    }


def _in_archive(path: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(path)


def _usb_line(lost: Sequence[FileOutcome]) -> str:
    """Whether the failed files are safe. A Mac copy that can't be read must never be called that."""
    unreadable = sum(1 for item in lost if item.reason == MAC_READ_ERROR)
    if unreadable:
        verb = "couldn't be read on the Mac"
        return f"{_plural(unreadable, 'file')} {verb}, so check that copy. Back up again to retry."
    if len(lost) == 1:
        return "It's safe on the Mac, so the card isn't at risk. Back up again to retry it."
    return f"All {len(lost):,} are safe on the Mac, so the card isn't at risk. Back up again to retry them."


def summarize_usb(
    outcomes: Sequence[FileOutcome], sizes: Mapping[str, int], roots: Sequence[Path]
) -> Dict[str, object]:
    """A USB backup's report: the same shape as an import's, for the same page."""
    lost = [item for item in outcomes if item.result == FAILED]
    name = lambda path: _in_archive(path, roots)  # noqa: E731
    gone = sum(1 for item in lost if item.reason == DRIVE_GONE)
    return {
        **_totals(outcomes, sizes, name, name),
        "usb": True,
        "safe_label": "safe on USB",
        "unknown": [],
        "problems": len(lost),
        "mac_safe": not any(item.reason == MAC_READ_ERROR for item in lost),
        "pairs": _usb_line(lost) if lost else "",
        "cause": {
            "lead": f"{DRIVE_GONE}.",
            "detail": f"{_plural(gone, 'file')} weren't copied. Reconnect it and back up again.",
        }
        if gone
        else None,
    }


def usb_message(summary: Mapping[str, object]) -> str:
    """It has to say USB: that's how a finished import's report knows which step failed."""
    count = int(summary["not_saved"])  # type: ignore[call-overload]
    text = f"{_plural(count, 'file')} couldn't be backed up to USB."
    if not summary["mac_safe"]:
        return text
    return text + (" It's safe on the Mac." if count == 1 else " They're safe on the Mac.")


def dont_format_message(summary: Mapping[str, object]) -> str:
    """The instruction comes first, and it's short: a narrow dashboard cuts off a long line."""
    unknown = [Path(str(item)) for item in summary["unknown"]]  # type: ignore[attr-defined]
    folders = sum(1 for path in unknown if not path.suffix)
    files = int(summary["not_saved"]) + len(unknown) - folders  # type: ignore[call-overload]
    places = [
        _plural(count, noun) for count, noun in ((folders, "folder"), (files, "file")) if count
    ]
    verb = "read" if summary["read_errors_only"] else "copied"
    return f"Don't format this card: {' and '.join(places)} couldn't be {verb}."


# ---- the saved report ----------------------------------------------------------------------


def _csv_cell(value: object) -> str:
    """Names come from the card and this opens in a spreadsheet app, so nothing may be a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _result_text(item: FileOutcome) -> str:
    result = f"{FAILED}: {item.reason}" if item.result == FAILED else item.result
    return result + " on the second try" if item.retried else result


def _csv_rows(facts: RunFacts) -> Iterable[Dict[str, object]]:
    dcim = facts.dcim or Path()
    for item in facts.outcomes:
        yield {
            "type": TYPE_LABELS.get(item.category, item.category),
            "result": _result_text(item),
            "card_path": _on_card(item.source, dcim),
            "archive_path": item.archive_path,
            "bytes": facts.sizes.get(str(item.source), ""),
            "sha256": item.sha256,
        }
    for path in facts.unreadable:
        yield {
            "type": "Unknown",
            "result": "unreadable: contents unknown",
            "card_path": _on_card(path, dcim),
        }
    for category, path in facts.files_not_reached:
        yield {
            "type": TYPE_LABELS.get(category, category),
            "result": "not reached",
            "card_path": _on_card(path, dcim),
            "bytes": facts.sizes.get(str(path), ""),
        }
    for item in facts.usb_outcomes:
        yield {
            "type": TYPE_LABELS.get(item.category, item.category),
            "result": _result_text(item),
            "archive_path": item.source,
            "usb_path": item.archive_path,
            "bytes": facts.usb_sizes.get(str(item.source), ""),
            "sha256": item.sha256,
        }
    for source, message in facts.usb_errors:
        yield {"type": "USB", "result": f"{FAILED}: {message}", "archive_path": source}


def _write_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    # The byte-order mark makes Excel read accented names properly; Numbers ignores it.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, CSV_COLUMNS, restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})


def _next_steps(facts: RunFacts, usb: Mapping[str, object]) -> List[str]:
    if usb["problems"]:
        return [
            "",
            "What to do next",
            "1. Check the drive is connected and has room.",
            "2. Back up again.",
            "3. If a file on the drive differs from the Mac's, compare them before removing either.",
        ]
    if facts.problems and facts.dcim:
        return [
            "",
            "What to do next",
            "1. Keep the card and don't format it, unless every file above is safe on the Mac.",
            "2. Try again, or try another card reader.",
            "3. If the same files fail again, the card may be failing.",
        ]
    return []


def _summary_text(facts: RunFacts, summary: Mapping[str, object], usb: Mapping[str, object]) -> str:
    when = time.strftime("%b %-d %Y, %-I:%M %p", time.localtime(facts.started_at))
    title = f"Import from {facts.source}" if facts.dcim else facts.source
    lines = [
        "Photo Offloader debug package",
        f"Run:      {title}, {when}",
        f"Result:   {facts.outcome.upper()}" + (f". {facts.message}" if facts.message else ""),
        f"App:      {facts.app_version or 'unknown'}, macOS {platform.mac_ver()[0] or 'unknown'}",
        "",
    ]
    if facts.dcim:
        lines += [
            f"Safe on the Mac:   {_plural(int(summary['safe']), 'file')}",  # type: ignore[call-overload]
            f"Not saved:         {_plural(int(summary['not_saved']), 'file')}",  # type: ignore[call-overload]
        ]
    if facts.usb_outcomes:
        lines += [
            f"Not backed up:     {_plural(int(usb['not_saved']), 'file')}",  # type: ignore[call-overload]
        ]
    lines += [f"Unknown:           {item} couldn't be read" for item in summary["unknown"]]  # type: ignore[attr-defined]
    if facts.files_not_reached:
        lines.append(f"Not reached:       {_plural(len(facts.files_not_reached), 'file')}")
    if facts.usb_errors:
        lines.append(f"USB problems:      {len(facts.usb_errors):,}")
    line = summary["pairs"] or usb["pairs"]
    if line:
        lines += ["", str(line)]
    return "\n".join(lines + _next_steps(facts, usb)) + "\n"


def _folder_name(started_at: float, problems: bool, attempt: int = 1) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
    repeat = f"-{attempt}" if attempt > 1 else ""
    return stamp + repeat + (PROBLEMS_SUFFIX if problems else OK_SUFFIX)


def _new_folder(started_at: float, problems: bool) -> Path:
    config.RUN_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while True:
        folder = config.RUN_REPORTS_DIR / _folder_name(started_at, problems, attempt)
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            attempt += 1


def _prune() -> None:
    """Each kind has its own limit, so a run of clean imports can't push out a failure's report."""
    for suffix in (PROBLEMS_SUFFIX, OK_SUFFIX):
        folders = sorted(p for p in config.RUN_REPORTS_DIR.iterdir() if p.name.endswith(suffix))
        for folder in folders[:-REPORTS_KEPT]:
            shutil.rmtree(folder, ignore_errors=True)


def save_report(facts: RunFacts) -> Tuple[str, Optional[Dict[str, object]]]:
    """Save a run's report. Returns its folder's name and, if anything went wrong, its summary."""
    summary = summarize(facts.outcomes, facts.unreadable, facts.sizes, facts.dcim or Path())
    usb = summarize_usb(facts.usb_outcomes, facts.usb_sizes, facts.archive_roots)
    folder = _new_folder(facts.started_at, facts.problems)
    _write_csv(folder / FILES_FILE, _csv_rows(facts))
    (folder / SUMMARY_FILE).write_text(_summary_text(facts, summary, usb), encoding="utf-8")
    (folder / RUN_LOG_FILE).write_text(
        "".join(line + "\n" for line in facts.log_lines), encoding="utf-8"
    )
    if facts.volume_info:
        (folder / VOLUME_FILE).write_text(facts.volume_info, encoding="utf-8")
    (folder / REPORT_FILE).write_text(
        json.dumps({"source": facts.source, "started_at": facts.started_at}), encoding="utf-8"
    )
    _prune()
    # A USB backup only runs once a card is verified, so a run has one kind of problem or the other.
    return folder.name, next((s for s in (summary, usb) if s["problems"]), None)


def volume_info(volume: Path) -> str:
    """``diskutil info`` for a card. A failing card can make it hang, hence the timeout."""
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", str(volume)],
            capture_output=True,
            text=True,
            timeout=VOLUME_INFO_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"diskutil info failed: {exc}\n"
    return result.stdout or result.stderr


def write_type_list(folder: Path, type_label: str) -> Path:
    """The files of one type that weren't saved, as their own spreadsheet list."""
    if type_label not in TYPE_LABELS.values():
        raise ValueError(f"Unknown file type: {type_label}")
    with (folder / FILES_FILE).open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row["type"] == type_label and row["result"].startswith(FAILED)
        ]
    path = folder / f"files-{type_label}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)  # these cells were made safe when files.csv was written
    return path


# ---- the debug package ---------------------------------------------------------------------


def _log_tail(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(size - LOG_EXCERPT_MAX_BYTES, 0))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Left %s out of a debug package: %s", path, exc)
        return None
    return "\n".join(text.splitlines()[-LOG_EXCERPT_LINES:]) + "\n"


def _package_name(folder: Path, attempt: int) -> str:
    try:
        facts = json.loads((folder / REPORT_FILE).read_text(encoding="utf-8"))
        started_at, source = float(facts["started_at"]), str(facts["source"])
    except (OSError, ValueError, KeyError, TypeError):
        started_at, source = time.time(), ""
    stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(started_at))
    card = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-")
    parts = (
        ["PhotoOffloader", stamp]
        + ([card] if card else [])
        + ([str(attempt)] if attempt > 1 else [])
    )
    return "-".join(parts) + ".zip"


def _open_new_package(folder: Path, destination: Path) -> Tuple[Path, zipfile.ZipFile]:
    destination.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while True:
        path = destination / _package_name(folder, attempt)
        try:
            return path, zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED)
        except FileExistsError:
            attempt += 1


def build_package(
    folder: Path,
    destination: Path,
    extras: Optional[Mapping[str, Path]] = None,
    log_excerpts: Optional[Mapping[str, Path]] = None,
) -> Path:
    """Zip a saved report for someone to look at. Only what is named here goes in.

    Never photos, rclone's config or the dashboard's token, so the callers list what to add
    instead of pointing this at a folder.
    """
    path, package = _open_new_package(folder, destination)
    try:
        with package:
            for name in PACKAGE_FILES:
                if (folder / name).is_file():
                    package.write(folder / name, name)
            for name, extra in (extras or {}).items():
                if extra.is_file():
                    package.write(extra, name)
            for name, log in (log_excerpts or {}).items():
                text = _log_tail(log)
                if text is not None:
                    package.writestr(name, text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
