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
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

from photo_offloader import catalog, config
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
        for program in (config.SERVICE, config.TRIGGER, config.GOOGLE_SYNC):
            (self.install / program).write_text("")
        support.isolate_config(testcase, root)

        self.now = 1_800_000_000.0
        self.service_up = True
        self.importer_up = False
        self.google_up = False
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
        )
        self.core = RecordingCore(
            self.paths, clock=lambda: self.now, run=self._run, popen=self._popen
        )

    def _run(self, argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        self.ran.append(list(argv))
        if "print" in argv:
            return subprocess.CompletedProcess(argv, 0 if self.service_up else 113, "", "")
        if argv[0].endswith("pgrep"):
            alive = self.google_up if argv[-1].endswith(config.GOOGLE_SYNC) else self.importer_up
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


def _uploading(s: Sandbox) -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="complete", ago=80, usb_planned=False)
    s.record(finished_at=s.now - 79, started_at=s.now - 180)
    s.google_up = True
    s.google_status("running", ago=75)
    for mb, files in ((55.6, 3), (87.7, 5), (121.8, 8), (175.1, 11), (204.1, 13)):
        s.paths.google_log_file.write_text(
            f"2026/11/04 18:50:00 INFO  :   {mb} MiB / 267.950 MiB, {int(mb / 2.68)}%, "
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
    _starting_upload(s)
    s.google_status("complete")


def _failed(s: Sandbox, message: str = "Copy failed: the Mac disk is full") -> None:
    s.add_card()
    s.add_usb()
    s.set_up_google()
    s.status(state="failed", ago=5)
    s.record(outcome="failed", failed=3, message=message)


SCENARIOS: Dict[str, Callable[[Sandbox], None]] = {
    "idle": lambda s: s.status(state="idle"),
    "card_mounted": lambda s: s.add_card(),
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
    "failed": _failed,
    "failed_on_usb": lambda s: _failed(s, "USB backup failed: the drive was removed"),
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

    def open_page(self, *, width: int = 960, height: int = 860, color_scheme: str = "light") -> Any:
        """A new page with empty browser storage, once it has rendered the current state."""
        self._close_page()
        self.context = Browser.chromium.new_context(
            viewport={"width": width, "height": height},
            color_scheme=color_scheme,
            reduced_motion="reduce",
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
