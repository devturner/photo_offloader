"""Run the real dashboard page in Chromium against the real server and core, in a sandbox.

Nothing here touches the machine. ``tests.support.isolate_config`` redirects every path into
a temp dir, and the core's ``run`` and ``popen`` are fakes: launchctl and pgrep answer from
the sandbox's flags, and diskutil and every launched program are only recorded.

Each entry in ``SCENARIOS`` sets up one dashboard state by writing the same files the importer
and the Google sync write, so the state comes out of ``DashboardCore`` and not from a canned
JSON payload. The clock is frozen; ``Sandbox.advance`` moves it.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from photo_offloader import catalog, config, run_report, verify_report
from photo_offloader.dashboard import core
from photo_offloader.dashboard.server import DashboardServer
from tests import support

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = Exception
    sync_playwright = None

WEB_ROOT = support.REPO_ROOT / "photo_offloader" / "dashboard" / "web"
REQUIRE_BROWSER = "PHOTO_OFFLOADER_REQUIRE_BROWSER"
CARD = "NIKON Z 8"
REPORT_CARD = Path("/Volumes") / CARD
GB = 1024**3
MB = 1024**2


class RecordingCore(core.DashboardCore):
    """The real core, also noting which actions the page asked for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actions: List[tuple] = []

    def act(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.actions.append((name, params or {}))
        return super().act(name, params)


class Sandbox:
    """One fresh set of folders and one core; the shared server is pointed at it."""

    def __init__(self, testcase: unittest.TestCase, root: Path) -> None:
        self.root = root
        self.install = root / "install"
        self.data = root / "data"
        self.volumes = root / "Volumes"
        self.usb = self.volumes / "Photos"
        for folder in (self.install / "bin", self.install / "scripts", self.data, self.volumes):
            folder.mkdir(parents=True)
        for program in (config.SERVICE, config.TRIGGER, config.GOOGLE_SYNC, config.VERIFY):
            (self.install / program).write_text("")
        support.isolate_config(testcase, root)

        self.now = 1_800_000_000.0
        self.service_up = True
        self.importer_up = False
        self.google_up = False
        self.verify_up = False
        self.ran: List[List[str]] = []
        self.spawned: List[List[str]] = []
        self.paths = core.Paths(
            install_dir=self.install,
            data_dir=self.data,
            volumes_root=self.volumes,
            usb_volume=self.usb,
            local_archives=(root / "CameraImports", root / "CameraImportsRaw"),
            usb_archives=(self.usb / "CameraImports",),
            python=Path("/fake/python3"),
            downloads=root / "Downloads",
        )
        self.core = RecordingCore(
            self.paths, clock=lambda: self.now, run=self._run, popen=self._popen
        )

    def _run(self, argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        self.ran.append(list(argv))
        if "print" in argv:
            return subprocess.CompletedProcess(argv, 0 if self.service_up else 113, "", "")
        if argv[0].endswith("pgrep"):
            alive = self.importer_up
            if argv[-1].endswith(config.GOOGLE_SYNC):
                alive = self.google_up
            elif argv[-1].endswith(config.VERIFY):
                alive = self.verify_up
            return subprocess.CompletedProcess(argv, 0, "4242\n" if alive else "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _popen(self, argv: List[str], **_kwargs: Any) -> None:
        self.spawned.append(list(argv))

    # ---- what the other processes would have left behind
    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.core._cache.clear()

    def stamp(self, ago: float = 0.0) -> str:
        return datetime.fromtimestamp(self.now - ago).astimezone().isoformat(timespec="seconds")

    def add_card(self, name: str = CARD) -> None:
        (self.volumes / name / "DCIM").mkdir(parents=True, exist_ok=True)

    def add_usb(self) -> None:
        self.usb.mkdir(exist_ok=True)

    def set_up_google(self) -> None:
        self.paths.preferences_file.write_text(json.dumps({"google_photos": {"configured": True}}))

    def status(self, *, ago: float = 0.0, card: str = CARD, **fields: Any) -> None:
        payload = {"updated_at": self.stamp(ago), "source": str(self.volumes / card), **fields}
        self.paths.status_file.write_text(json.dumps(payload))

    def google_status(self, state: str, *, ago: float = 0.0, **fields: Any) -> None:
        payload = {"state": state, "updated_at": self.stamp(ago), **fields}
        self.paths.google_status_file.write_text(json.dumps(payload))

    def record(self, **fields: Any) -> None:
        row = {
            "kind": "import",
            "source": CARD,
            "outcome": "complete",
            "files": 2378,
            "copied": 18,
            "skipped": 2360,
            "bytes": 80 * GB,
            "usb": True,
            "started_at": self.now - 102,
            "finished_at": self.now - 2,
            "details": {"new": {"JPG": 9, "RAW": 9}, "new_bytes": 412 * MB, "usb_copied": 18},
        }
        catalog.record_import_run(**{**row, **fields})

    def transfer(self, **fields: Any) -> None:
        """An importer in the middle of a card: USB and Google Photos are both still to come."""
        self.importer_up = True
        self.add_card(fields.get("card", CARD))
        self.add_usb()
        self.set_up_google()
        self.status(usb_planned=True, **fields)


README_CARD_FILES = 2478
README_MB_PER_FILE = 25
README_COPY_SECONDS = 120
README_ALREADY_SAVED = 477
README_SAVED_BEFORE = 1200  # the card after the last import listed in Recent activity

COPY_PROGRESS = dict(
    bytes_done=34 * GB,
    bytes_total=80 * GB,
    files_done=1214,
    files_total=2378,
    eta_seconds=240,
    elapsed_seconds=119,
)


def _steady_speed_mb(second: int, slow_from: int = 20, slow_to: int = 26) -> int:
    """A believable copy speed: small changes, plus a slowdown part of the way through."""
    slowdown = 70 if slow_from <= second < slow_to else 0
    return round(160 + 18 * math.sin(second / 4) + 9 * math.sin(second * 1.7)) - slowdown


def _copying(s: Sandbox, **fields: Any) -> None:
    """Poll while bytes move, as an open page would, so the speed graph has samples."""
    moved = 0
    for second in range(45):
        moved += _steady_speed_mb(second) * MB
        s.advance(1)
        s.transfer(
            **{
                "state": "copying",
                "phase": "copy",
                "stage": "copy",
                "category": "RAW",
                "current_file": f"_DZ8{9400 + second}.NEF",
                "new_files": 3 + second,
                "bytes_transferred": moved,
                **COPY_PROGRESS,
                **fields,
            }
        )
        s.core.state()


def _past_import(s: Sandbox, days_ago: int, seconds: int, new_files: int, card_files: int) -> None:
    new_bytes = new_files * README_MB_PER_FILE * MB
    finished = s.now - days_ago * 86400
    s.record(
        files=card_files,
        copied=new_files,
        skipped=card_files - new_files,
        bytes=card_files * README_MB_PER_FILE * MB,
        started_at=finished - seconds,
        finished_at=finished,
        details={
            "new": {"JPG": new_files // 2, "RAW": new_files - new_files // 2},
            "new_bytes": new_bytes,
            "usb_copied": new_files,
        },
    )


def _readme_import(s: Sandbox) -> None:
    """The README's picture: a card part-way through, sized like a real one (about 25 MB a file).
    Some of its files are already saved. Every number comes from the speed samples and the same
    sums the importer does, so the panel's figures agree."""
    speeds = [_steady_speed_mb(second, 70, 82) * MB for second in range(README_COPY_SECONDS)]
    file_bytes = README_MB_PER_FILE * MB
    copied = sum(speeds)
    new_files = copied // file_bytes
    files_done = new_files + README_ALREADY_SAVED
    done = files_done * file_bytes
    total = README_CARD_FILES * file_bytes
    elapsed = README_COPY_SECONDS + 6  # the scan came first
    _past_import(s, days_ago=12, seconds=58, new_files=342, card_files=443)
    _past_import(s, days_ago=5, seconds=41, new_files=251, card_files=694)
    _past_import(s, days_ago=1, seconds=83, new_files=506, card_files=README_SAVED_BEFORE)
    with catalog.catalog_db() as db:
        db.executemany(
            "INSERT INTO google_photos_uploads (path, size, mtime_ns, inode, uploaded_at, remote,"
            " album) VALUES (?, 1, 1, 1, ?, 'gphotos', 'Photo Offloader')",
            [(f"/archive/{number}.jpg", s.now) for number in range(1633)],
        )
    s.transfer(
        state="copying",
        phase="copy",
        stage="copy",
        category="RAW",
        current_file=f"_DZ8{1000 + files_done}.NEF",
        files_done=files_done,
        files_total=README_CARD_FILES,
        new_files=new_files,
        expected_new_files=README_CARD_FILES - README_SAVED_BEFORE,
        expected_new_bytes=(README_CARD_FILES - README_SAVED_BEFORE) * file_bytes,
        bytes_done=done,
        bytes_total=total,
        bytes_transferred=copied,
        elapsed_seconds=elapsed,
        eta_seconds=(total - done) / (done / elapsed),
    )
    s.core.state()  # starts the graph; the clock is frozen, so no poll adds to it afterwards
    s.core._samples.extend(speeds)


def _stalled_copy(s: Sandbox) -> None:
    """Twenty seconds without a byte moved, between two stretches at full speed."""
    speeds = [75 * MB] * 40 + [0] * 20 + [75 * MB] * 30
    copied = sum(speeds)
    s.transfer(
        state="copying",
        phase="copy",
        stage="copy",
        category="RAW",
        bytes_done=copied,
        bytes_total=3 * copied,
        bytes_transferred=copied,
    )
    s.core.state()  # starts the graph; the clock is frozen, so no poll adds to it afterwards
    s.core._samples.extend(speeds)


def _uploading(s: Sandbox) -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="complete", ago=80, usb_planned=False)
    s.record(finished_at=s.now - 79, started_at=s.now - 180)
    s.google_up = True
    s.google_status("running", ago=75)
    for mb, files in ((55.6, 3), (87.7, 5), (121.8, 8), (175.1, 11), (204.1, 13)):
        stamp = datetime.fromtimestamp(s.now).strftime("%Y/%m/%d %H:%M:%S")
        s.paths.google_log_file.write_text(
            f"{stamp} INFO  :   {mb} MiB / 267.950 MiB, {int(mb / 2.68)}%, "
            f"8.8 MiB/s, ETA 7s (xfr#{files}/18)\n"
        )
        s.core.state()
        s.advance(1)


def _already_imported(s: Sandbox) -> None:
    """The one state with every step button showing."""
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="idle")
    s.paths.handled_state_file.write_text(str(s.volumes / CARD / "DCIM"))


def _starting_upload(s: Sandbox) -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="complete", ago=3, usb_planned=True, files_done=2378, files_total=2378)
    s.record()


def _finished(s: Sandbox) -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="complete", ago=50, usb_planned=True, files_done=2378, files_total=2378)
    s.record(
        finished_at=s.now - 49,
        started_at=s.now - 149,
        details={
            "new": {"JPG": 9, "RAW": 9},
            "new_bytes": 412 * MB,
            "usb_copied": 18,
            "copy_seconds": 72.0,
            "usb_seconds": 28.0,
            "usb_bytes": 412 * MB,
            "upload": {
                "outcome": "complete",
                "files": 9,
                "bytes": 130 * MB,
                "seconds": 47.0,
                "album": "Photo Offloader",
            },
        },
    )
    s.google_status("complete", ago=1)


def _finished_advanced(s: Sandbox) -> None:
    _finished(s)
    s.core.act("set_report_style", {"report_style": "advanced"})


def _failed(s: Sandbox, message: str = "Copy failed: the Mac disk is full") -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="failed", ago=5)
    s.record(outcome="failed", failed=3, message=message)


def _names(first: int, count: int, extension: str) -> List[str]:
    return [f"DSC_{first + index:04}.{extension}" for index in range(count)]


def _outcomes(
    category: str, folder: str, names: List[str], result: str = "copied", reason: str = ""
) -> List[run_report.FileOutcome]:
    root = REPORT_CARD / "DCIM" / folder
    return [run_report.FileOutcome(category, root / name, result, reason=reason) for name in names]


def _lost(category: str, folder: str, names: List[str], reason: str = run_report.READ_ERROR):
    return _outcomes(category, folder, names, run_report.FAILED, reason)


def _report_run(
    s: Sandbox,
    outcomes: List[run_report.FileOutcome],
    unreadable: Tuple[str, ...] = (),
    outcome: str = "failed",
) -> None:
    """A run's folder and history row, made by the code the importer uses."""
    s.add_card()
    s.add_usb()
    s.set_up_google()
    sizes = {
        str(item.source): (34 if item.category == "RAW" else 13 if item.category == "JPG" else 800)
        * MB
        for item in outcomes
    }
    dcim, paths = REPORT_CARD / "DCIM", [REPORT_CARD / "DCIM" / name for name in unreadable]
    summary = run_report.summarize(outcomes, paths, sizes, dcim)
    message = run_report.dont_format_message(summary) if outcome == "failed" else ""
    folder, summary = run_report.save_report(
        run_report.RunFacts(
            source=CARD,
            started_at=s.now - 252,
            outcome=outcome,
            message=message,
            dcim=dcim,
            problems=outcome == "failed",
            outcomes=outcomes,
            unreadable=paths,
            sizes=sizes,
            log_lines=["[2026-11-06 14:12:00] [INFO] An example log line"],
            app_version="2.5 (abc1234)",
        )
    )
    s.status(state="failed" if outcome == "failed" else "complete", ago=5, message=message)
    s.record(
        outcome=outcome,
        started_at=s.now - 252,
        finished_at=s.now - 2,
        files=len(outcomes),
        failed=summary["problems"] if summary else 0,
        message=message,
        details={"report": folder, **({"problems": summary} if summary else {})},
    )


def _few_problems(s: Sandbox) -> None:
    _report_run(
        s,
        _outcomes("JPG", "100NIKON", _names(1, 1044, "JPG"))
        + _outcomes("RAW", "100NIKON", _names(1, 1060, "NEF"))
        + _lost("RAW", "100NIKON", _names(1061, 2, "NEF"))
        + _outcomes("JPG", "100NIKON", _names(1061, 2, "JPG")),
        ("101NIKON",),
    )


def _many_problems(s: Sandbox) -> None:
    changed = "Changed during the copy"
    _report_run(
        s,
        _outcomes("JPG", "100NIKON", _names(1, 1029, "JPG"))
        + _lost("JPG", "100NIKON", _names(1030, 15, "JPG"))
        + _outcomes("RAW", "100NIKON", _names(1, 1040, "NEF"))
        + _lost("RAW", "100NIKON", _names(1030, 16, "NEF"))
        + _lost("RAW", "102NIKON", _names(1, 6, "NEF"), changed)
        + _outcomes("JPG", "102NIKON", _names(1, 6, "JPG")),
        ("101NIKON",),
    )


def _huge_problems(s: Sandbox) -> None:
    # The card died partway through. JPGs go first, so every RAW and movie after them fails too.
    _report_run(
        s,
        _outcomes("JPG", "099NIKON", _names(1, 855, "JPG"))
        + _outcomes("JPG", "100NIKON", _names(1, 2, "JPG"))
        + _lost("JPG", "100NIKON", _names(3, 604, "JPG"))
        + _lost("RAW", "100NIKON", _names(1, 606, "NEF"))
        + _lost("VIDEO", "101NIKON", _names(801, 37, "MOV")),
    )


def _usb_run(s: Sandbox, outcomes: List[run_report.FileOutcome]) -> None:
    """A USB backup's folder and history row, made by the code the importer uses."""
    s.add_card()
    s.add_usb()
    s.set_up_google()
    roots = [
        Path("/Users/you/Pictures/CameraImports"),
        Path("/Users/you/Pictures/CameraImportsRaw"),
    ]
    sizes = {str(item.source): (34 if item.category == "RAW" else 13) * MB for item in outcomes}
    usb = run_report.summarize_usb(outcomes, sizes, roots)
    failed = bool(usb["problems"])
    message = run_report.usb_message(usb) if failed else ""
    folder, summary = run_report.save_report(
        run_report.RunFacts(
            source="USB backup",
            started_at=s.now - 66,
            outcome="failed" if failed else "complete",
            message=message,
            dcim=None,
            problems=failed,
            usb_outcomes=outcomes,
            usb_sizes=sizes,
            archive_roots=roots,
            log_lines=["[2026-11-06 14:12:00] [INFO] An example log line"],
            app_version="2.5 (abc1234)",
        )
    )
    s.status(state="failed" if failed else "complete", ago=5, message=message)
    s.record(
        kind="usb_backup",
        source="",
        outcome="failed" if failed else "complete",
        started_at=s.now - 66,
        finished_at=s.now - 2,
        files=len(outcomes),
        copied=0,
        skipped=len(outcomes) - int(usb["not_saved"]),
        failed=int(usb["not_saved"]),
        message=message,
        details={"report": folder, **({"problems": summary} if summary else {})},
    )


def _usb_outcomes(
    category: str, folder: str, names: List[str], result: str = "skipped", reason: str = ""
) -> List[run_report.FileOutcome]:
    root = Path("/Users/you/Pictures") / folder
    return [
        run_report.FileOutcome(
            category, root / name, result, f"/Volumes/Photos/{name}", reason=reason
        )
        for name in names
    ]


def _usb_problems(s: Sandbox) -> None:
    raws, jpgs = "CameraImportsRaw/2026-11/NIKON-Z-8/Raws", "CameraImports/2026-11/NIKON-Z-8/Photos"
    _usb_run(
        s,
        _usb_outcomes("RAW", raws, _names(1, 1560, "NEF"))
        + _usb_outcomes(
            "RAW", raws, _names(1561, 2, "NEF"), run_report.FAILED, run_report.NOT_OVERWRITTEN
        )
        + _usb_outcomes("JPG", jpgs, _names(1, 1567, "JPG"))
        + _usb_outcomes(
            "JPG", jpgs, _names(1568, 1, "JPG"), run_report.FAILED, "Couldn't write to the drive"
        ),
    )


def _usb_clean(s: Sandbox) -> None:
    _usb_run(s, _usb_outcomes("JPG", "CameraImports/2026-11", _names(1, 20, "JPG")))


def _second_tries(s: Sandbox) -> None:
    _report_run(s, _outcomes("JPG", "100NIKON", _names(1, 16, "JPG")), outcome="complete")
    row = catalog.list_import_history(limit=1)[0]
    catalog.clear_import_history()
    s.record(
        started_at=s.now - 252,
        finished_at=s.now - 2,
        files=16,
        details={**row["details"], "second_tries": 3},
    )


def _clean_with_report(s: Sandbox) -> None:
    _report_run(s, _outcomes("JPG", "100NIKON", _names(1, 16, "JPG")), outcome="complete")


def _check_running(s: Sandbox) -> None:
    s.add_usb()
    _past_import(s, 1, 90, 20, 2742)
    s.verify_up = True
    config.VERIFY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.VERIFY_STATUS_PATH.write_text(
        json.dumps(
            {
                "where": "USB drive",
                "usb": True,
                "files_done": 3490,
                "files_total": 6300,
                "bytes_done": 76.4e9,
                "bytes_total": 138.8e9,
                "seconds_left": 900,
            }
        )
    )


def _check_during_import(s: Sandbox) -> None:
    _check_running(s)
    _copying(s)


def _record_check(s: Sandbox, **fields: Any) -> None:
    s.record(
        **{
            "kind": "verify",
            "source": "",
            "copied": 0,
            "skipped": 0,
            "files": 6300,
            "bytes": 138_800_000_000,
            "started_at": s.now - 1092,
            "details": {"whole": True},
            **fields,
        }
    )


def _check_row(relative: str, problem: str, where: str, other: str) -> Dict[str, Any]:
    return {
        "where": where,
        "problem": problem,
        "other_copy": other,
        "path": f"/Mac/{relative}",
        "relative_path": relative,
        "bytes": 34_000_000,
        "expected_sha256": "aa",
        "actual_sha256": "",
    }


def _failed_check(s: Sandbox, rows: List[Dict[str, Any]]) -> None:
    name = verify_report.save_report(rows, s.now)
    _record_check(
        s,
        outcome="failed",
        failed=len(rows),
        message=verify_report.message_for(rows),
        details={"whole": True, "check": verify_report.details_for(rows, name)},
    )


def _check_problems(s: Sandbox) -> None:
    _failed_check(
        s,
        [
            _check_row(
                "2026-10/NIKON-Z-8/Raws/_DZ80412.NEF",
                verify_report.DAMAGED,
                verify_report.USB,
                verify_report.OTHER_GOOD,
            ),
            _check_row(
                "2026-07/Canon/Photos/IMG_7255.JPG",
                verify_report.UNREADABLE,
                verify_report.MAC,
                verify_report.OTHER_GOOD,
            ),
        ],
    )


def _check_deleted(s: Sandbox) -> None:
    rows = [
        _check_row(
            f"2026-10/NIKON-Z-8/Raws/_DZ8{index:04d}.NEF",
            verify_report.MISSING,
            verify_report.MAC,
            verify_report.OTHER_GOOD,
        )
        for index in range(1102)
    ] + [
        _check_row(
            f"2026-07/Canon/Raws/IMG_{index:04d}.CR2",
            verify_report.ONLY_ON_DRIVE,
            verify_report.USB,
            verify_report.OTHER_ONLY,
        )
        for index in range(279)
    ]
    _failed_check(s, rows)


SCENARIOS: Dict[str, Callable[[Sandbox], None]] = {
    "idle": lambda s: s.status(state="idle"),
    "check_running": _check_running,
    "check_during_import": _check_during_import,
    "check_clean": lambda s: _record_check(s, message="All 6,300 copies match."),
    "check_problems": _check_problems,
    "check_deleted": _check_deleted,
    "card_mounted": lambda s: s.add_card(),
    "stalled_copy": _stalled_copy,
    "already_imported": _already_imported,
    "scan": lambda s: s.transfer(
        state="scanning",
        phase="card-to-ssd",
        stage="scan",
        message="Scanning camera card",
        elapsed_seconds=2,
    ),
    "read_photo_info": lambda s: s.transfer(
        state="scanning",
        stage="metadata",
        message="Reading photo info",
        files_total=2378,
        elapsed_seconds=6,
    ),
    "checking": lambda s: s.transfer(
        state="scanning",
        phase="copy-decision",
        stage="copy",
        category="RAW",
        current_file="_DZ89400.NEF",
        **COPY_PROGRESS,
    ),
    "copying": _copying,
    "readme_import": _readme_import,
    # The longest values a slot should have to hold: a big card with thousands of new files.
    "copying_big_card": lambda s: _copying(
        s,
        bytes_done=1023 * GB,
        bytes_total=1999 * GB,
        files_done=12378,
        files_total=23456,
        new_files=1027,
        elapsed_seconds=37380,
        eta_seconds=36000,
    ),
    "usb_backup": lambda s: s.transfer(
        state="copying",
        phase="usb-backup",
        stage="usb",
        current_file="_DZ89400.NEF",
        bytes_transferred=2 * GB,
        **COPY_PROGRESS,
    ),
    "finishing_import": lambda s: s.transfer(
        state="complete",
        files_done=2378,
        files_total=2378,
        new_files=18,
        bytes_total=80 * GB,
        elapsed_seconds=100,
    ),
    "starting_upload": _starting_upload,
    "uploading": _uploading,
    "finished": _finished,
    "finished_advanced": _finished_advanced,
    "failed": _failed,
    "failed_on_usb": lambda s: _failed(s, "USB backup failed: the drive was removed"),
    "failed_unreadable_card": lambda s: _failed(
        s, "Don't format this card: 2 folders couldn't be read."
    ),
    "few_problems": _few_problems,
    "many_problems": _many_problems,
    "huge_problems": _huge_problems,
    "clean_with_report": _clean_with_report,
    "usb_problems": _usb_problems,
    "usb_clean": _usb_clean,
    "second_tries": _second_tries,
    "stopped": lambda s: setattr(s, "service_up", False),
    "paused": lambda s: s.paths.pause_file.touch(),
}

# Dashboard states in the order one import goes through them.
ONE_RUN = (
    "card_mounted",
    "scan",
    "read_photo_info",
    "checking",
    "copying",
    "usb_backup",
    "finishing_import",
    "starting_upload",
    "uploading",
    "finished",
)


class Browser:
    """One Chromium for the whole test module; every page gets its own browser context."""

    playwright: Any = None
    chromium: Any = None

    @classmethod
    def start(cls) -> None:
        """Called from setUpModule. Skips the module without Playwright or its browser."""
        try:
            if sync_playwright is None:
                raise RuntimeError(
                    "Playwright is not installed (pip install -r requirements-dev.txt)"
                )
            cls.playwright = sync_playwright().start()
            cls.chromium = cls.playwright.chromium.launch()
        except (RuntimeError, PlaywrightError) as error:
            cls.stop()
            if os.environ.get(REQUIRE_BROWSER):
                raise  # CI: a browser job that skips everything would look green
            raise unittest.SkipTest(f"no browser for the dashboard tests: {error}") from error

    @classmethod
    def stop(cls) -> None:
        if cls.chromium:
            cls.chromium.close()
        if cls.playwright:
            cls.playwright.stop()
        cls.playwright = cls.chromium = None


class BrowserTestCase(unittest.TestCase):
    """Shows dashboard states in a real page and fails on any uncaught script or console error."""

    maxDiff = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.loads = 0
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "PHOTO_OFFLOADER_STATUS_FILE",
            "PHOTO_OFFLOADER_PAUSE_FILE",
            "PHOTO_OFFLOADER_IMPORT_LOG",
            "PHOTO_OFFLOADER_TRIGGER_LOG",
        ):
            os.environ.pop(name, None)
        self.context: Any = None
        self.page: Any = None
        self.page_errors: List[str] = []
        self.sandbox = self._new_sandbox()
        self.server = DashboardServer(self.sandbox.core, WEB_ROOT)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self._close_page)

    def tearDown(self) -> None:
        self.assertEqual(self.page_errors, [], "the page logged errors")

    def _new_sandbox(self) -> Sandbox:
        self.loads += 1
        return Sandbox(self, self.tmp / str(self.loads))

    def _close_page(self) -> None:
        if self.context:
            self.context.close()
        self.context = self.page = None

    def load(self, scenario: str) -> Sandbox:
        """Put the dashboard in this state, in fresh folders. An open page sees it on its next poll."""
        self.sandbox = self._new_sandbox()
        SCENARIOS[scenario](self.sandbox)
        self.server.core = self.sandbox.core
        return self.sandbox

    def open_page(
        self,
        *,
        width: int = 960,
        height: int = 860,
        color_scheme: str = "light",
        reduced_motion: str = "reduce",
    ) -> Any:
        """A new page with empty browser storage, once it has rendered the current state."""
        self._close_page()
        self.context = Browser.chromium.new_context(
            viewport={"width": width, "height": height},
            color_scheme=color_scheme,
            reduced_motion=reduced_motion,
        )
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("console", self._note_console)
        self.page.goto(self.server.url)
        self.wait_for_render()
        return self.page

    def _note_console(self, message: Any) -> None:
        if message.type == "error":
            self.page_errors.append(message.text)

    def wait_for_render(self) -> None:
        # Recent activity is the last thing refresh() fills in.
        self.page.wait_for_selector("#history > *", state="attached")

    def show(self, scenario: str, **page_options: Any) -> Any:
        self.load(scenario)
        return self.open_page(**page_options)

    def refresh(self) -> None:
        """Render the current state now instead of waiting for the page's one-second poll."""
        self.page.evaluate("refresh(true)")
