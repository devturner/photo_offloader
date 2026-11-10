#!/usr/bin/env python3
"""Tests for the dashboard launcher (photo_offloader.dashboard.launcher): single instance, window or browser."""

import fcntl
import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader.dashboard import core
from photo_offloader.dashboard import launcher as ui
from photo_offloader.dashboard import server as srv
from tests import support


class FakeWebview:
    """Stands in for pywebview: records the window and 'closes' it immediately."""

    def __init__(self, on_start=None):
        self.windows, self.started, self.on_start = [], False, on_start

    def create_window(self, title, url, **kwargs):
        self.windows.append((title, url, kwargs))

    def start(self):
        self.started = True
        if self.on_start:
            self.on_start()


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.install, self.data = root / "install", root / "data"
        self.install.mkdir()
        self.data.mkdir()
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>Photo Offloader</title>")
        support.isolate_config(
            self,
            root,
            CATALOG_DB_PATH=self.data / "catalog.sqlite3",
            LEGACY_CATALOG_DB_PATH=root / "legacy.sqlite3",
        )
        self.paths = core.Paths(
            install_dir=self.install,
            data_dir=self.data,
            volumes_root=root / "Volumes",
            usb_volume=root / "Volumes" / "Photos",
            local_archives=(root / "a", root / "b"),
            usb_archives=(root / "Volumes" / "Photos" / "c",),
            python=Path("/usr/bin/python3"),
        )
        self.record = self.install / "dashboard.json"
        self.opened = []
        self.servers = []

    def launch(self, **kwargs):
        defaults = dict(
            paths=self.paths,
            web_root=self.web,
            open_browser=self.opened.append,
            webview_module=None,
            idle_timeout=0.4,
            activate=lambda pid: False,
        )
        defaults.update(kwargs)
        return ui.main([], **defaults)

    def existing_server(self, mode="browser"):
        server = srv.DashboardServer(
            core.DashboardCore(self.paths), self.web, token="existing-token"
        )
        server.start()
        self.addCleanup(server.stop)
        srv.write_instance_file(self.record, server, mode=mode)
        return server


class TestTheApp(LauncherCase):
    """Photo Offloader.app: opening it starts everything; other programs only want the dashboard."""

    def open_app(self, *args):
        self.events = []
        self.opened = self.events  # the browser opening is recorded in order with the start
        return ui.app_main(
            list(args),
            start_service=lambda: self.events.append("start"),
            paths=self.paths,
            web_root=self.web,
            open_browser=self.events.append,
            webview_module=None,
            idle_timeout=0.4,
            activate=lambda pid: False,
        )

    def test_opening_the_app_starts_the_service_then_shows_the_dashboard(self):
        self.assertEqual(self.open_app(), 0)
        self.assertEqual(self.events[0], "start")
        self.assertEqual(len(self.events), 2, self.events)
        self.assertIn("127.0.0.1", self.events[1])

    def test_programs_that_only_want_the_dashboard_leave_the_service_alone(self):
        for args in (["--dashboard"], ["--if-closed"]):
            with self.subTest(args=args):
                self.assertEqual(self.open_app(*args), 0)
                self.assertNotIn("start", self.events)
                self.assertEqual(len(self.events), 1, self.events)

    def test_a_failed_start_still_shows_the_dashboard(self):
        # The dashboard then says the service is stopped and offers Start.
        def failing_start():
            self.events.append("start")
            raise OSError("service.sh is missing")

        self.events = []
        ui.app_main(
            [],
            start_service=failing_start,
            paths=self.paths,
            web_root=self.web,
            open_browser=self.events.append,
            webview_module=None,
            idle_timeout=0.4,
            activate=lambda pid: False,
        )
        self.assertEqual(self.events[0], "start")
        self.assertEqual(len(self.events), 2, self.events)


class TestStartingTheDashboard(LauncherCase):
    def test_with_pywebview_it_opens_a_native_window_on_a_live_server(self):
        reachable = []

        def during_window():
            info = json.loads(self.record.read_text())
            connection = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=3)
            connection.request(
                "GET",
                "/api/state",
                headers={srv.TOKEN_HEADER: info["token"], "Host": f"127.0.0.1:{info['port']}"},
            )
            reachable.append(connection.getresponse().status)

        webview = FakeWebview(on_start=during_window)
        self.assertEqual(self.launch(webview_module=webview), 0)
        ((title, url, kwargs),) = webview.windows
        self.assertEqual(title, "Photo Offloader")
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/#t=[\w-]+$")
        self.assertTrue(webview.started)
        self.assertEqual(reachable, [200])
        self.assertEqual(self.opened, [], "a native window must not also open a browser tab")

    def test_the_native_window_has_a_fixed_size(self):
        # User request 2026-11-04: fixed-size window; Recent activity scrolls inside it.
        webview = FakeWebview()
        self.launch(webview_module=webview)
        ((_title, _url, kwargs),) = webview.windows
        self.assertEqual((kwargs["width"], kwargs["height"]), ui.WINDOW_SIZE)
        self.assertIs(kwargs["resizable"], False)

    def test_the_instance_record_is_private_and_removed_on_exit(self):
        seen = {}

        def during_window():
            seen["mode"] = stat.S_IMODE(self.record.stat().st_mode)
            seen["info"] = json.loads(self.record.read_text())

        self.launch(webview_module=FakeWebview(on_start=during_window))
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual((seen["info"]["pid"], seen["info"]["mode"]), (os.getpid(), "window"))
        self.assertFalse(self.record.exists(), "a stale record must not outlive the dashboard")

    def test_without_pywebview_it_opens_the_browser_and_exits_when_the_tab_is_gone(self):
        started = time.monotonic()
        self.assertEqual(self.launch(), 0)
        self.assertEqual(len(self.opened), 1)
        self.assertRegex(self.opened[0], r"^http://127\.0\.0\.1:\d+/#t=")
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(self.record.exists())

    def test_the_mode_can_be_forced_to_browser_even_if_pywebview_is_installed(self):
        webview = FakeWebview()
        with patch.dict(os.environ, {"PHOTO_OFFLOADER_DASHBOARD_MODE": "browser"}):
            self.launch(webview_module=webview)
        self.assertEqual(webview.windows, [])
        self.assertEqual(len(self.opened), 1)

    def test_missing_page_files_fail_clearly_instead_of_opening_a_broken_window(self):
        (self.web / "index.html").unlink()
        with patch("builtins.print") as printed:
            self.assertEqual(self.launch(), 1)
        self.assertIn("install.sh", " ".join(str(c) for c in printed.call_args_list))
        self.assertEqual(self.opened, [])

    def test_a_stale_record_from_a_dead_dashboard_is_ignored(self):
        self.record.write_text(json.dumps({"pid": 1, "port": 1, "token": "dead", "mode": "window"}))
        webview = FakeWebview()
        self.assertEqual(self.launch(webview_module=webview), 0)
        self.assertEqual(len(webview.windows), 1)


class TestSingleInstance(LauncherCase):
    def test_a_second_launch_reopens_the_existing_dashboard_instead_of_starting_another(self):
        existing = self.existing_server(mode="browser")
        webview = FakeWebview()
        self.assertEqual(self.launch(webview_module=webview), 0)
        self.assertEqual(webview.windows, [], "no second server or window")
        self.assertEqual(self.opened, [f"http://127.0.0.1:{existing.port}/#t=existing-token"])
        self.assertEqual(json.loads(self.record.read_text())["port"], existing.port)

    def test_if_closed_does_nothing_while_a_page_is_showing_the_dashboard(self):
        # The trigger asks for the dashboard at every import start. If a window or tab is already
        # polling, there is nothing to open (and no reason to steal focus).
        existing = self.existing_server(mode="browser")
        connection = http.client.HTTPConnection("127.0.0.1", existing.port, timeout=3)
        connection.request(
            "GET",
            "/api/state",
            headers={srv.TOKEN_HEADER: "existing-token", "Host": f"127.0.0.1:{existing.port}"},
        )
        connection.getresponse().read()
        self.assertEqual(
            ui.main(
                ["--if-closed"],
                paths=self.paths,
                web_root=self.web,
                open_browser=self.opened.append,
                webview_module=None,
                activate=lambda pid: False,
            ),
            0,
        )
        self.assertEqual(self.opened, [])

    def test_if_closed_reopens_when_nothing_has_polled_lately(self):
        # Browser mode keeps the server alive for a few minutes after its tab closes.
        existing = self.existing_server(mode="browser")
        time.sleep(0.3)
        with patch.object(ui, "PAGE_OPEN_WITHIN_SECONDS", 0.1):
            self.assertEqual(
                ui.main(
                    ["--if-closed"],
                    paths=self.paths,
                    web_root=self.web,
                    open_browser=self.opened.append,
                    webview_module=None,
                    activate=lambda pid: False,
                ),
                0,
            )
        self.assertEqual(self.opened, [f"http://127.0.0.1:{existing.port}/#t=existing-token"])

    def test_an_existing_native_window_is_brought_to_the_front(self):
        self.existing_server(mode="window")
        activated = []
        self.assertEqual(self.launch(activate=lambda pid: activated.append(pid) or True), 0)
        self.assertEqual(activated, [os.getpid()])
        self.assertEqual(self.opened, [], "activating the window is enough")

    def test_if_the_window_cannot_be_activated_fall_back_to_the_browser(self):
        existing = self.existing_server(mode="window")
        self.assertEqual(self.launch(activate=lambda pid: False), 0)
        self.assertEqual(self.opened, [f"http://127.0.0.1:{existing.port}/#t=existing-token"])

    def test_simultaneous_launches_are_serialised_by_a_lock(self):
        # The menu bar and the trigger can both ask for the dashboard at the same moment.
        lock_fd = os.open(self.install / "dashboard.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        done = threading.Event()
        thread = threading.Thread(target=lambda: (self.launch(), done.set()), daemon=True)
        thread.start()
        time.sleep(0.4)
        self.assertFalse(done.is_set() or self.opened, "launch must wait for the lock holder")
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        thread.join(15)
        self.assertTrue(done.is_set())
        self.assertEqual(len(self.opened), 1)


if __name__ == "__main__":
    unittest.main()
