"""Notifications only go out when nothing else would show what happened."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import logs, presence
from tests.support import isolate_config
from tests.test_logs import REAL_NOTIFY


def launchctl(returncode):
    return subprocess.CompletedProcess(["launchctl"], returncode, "", "")


class TestPresence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolate_config(self, Path(tmp.name))

    def test_the_service_is_running_when_launchd_knows_it(self):
        with patch.object(presence.subprocess, "run", return_value=launchctl(0)) as run:
            self.assertTrue(presence.service_running())
        self.assertIn(presence.SERVICE_LABEL, run.call_args.args[0][-1])

    def test_the_service_is_stopped_when_launchd_does_not(self):
        with patch.object(presence.subprocess, "run", return_value=launchctl(113)):
            self.assertFalse(presence.service_running())

    def test_unable_to_tell_counts_as_running(self):
        # This decides whether someone hears about a failure, so doubt must not silence it.
        for error in (OSError("no launchctl"), subprocess.TimeoutExpired("launchctl", 5)):
            with patch.object(presence.subprocess, "run", side_effect=error):
                self.assertTrue(presence.service_running())

    def page(self, idle):
        info = {"idle_seconds": idle} if idle is not None else None
        return patch.object(presence, "find_running_instance", return_value=info)

    def test_a_page_polling_every_second_is_being_shown(self):
        with self.page(1.2):
            self.assertTrue(presence.dashboard_showing())

    def test_a_hidden_page_polls_every_five_seconds_so_is_not(self):
        with self.page(4.5):
            self.assertFalse(presence.dashboard_showing())

    def test_a_server_with_its_window_closed_is_not(self):
        with self.page(600.0):
            self.assertFalse(presence.dashboard_showing())

    def test_no_server_is_not(self):
        with self.page(None):
            self.assertFalse(presence.dashboard_showing())

    def test_it_looks_at_the_installed_dashboards_record(self):
        with patch.object(presence, "find_running_instance", return_value=None) as find:
            presence.dashboard_showing()
        self.assertEqual(find.call_args.args[0].name, "dashboard.json")
        self.assertEqual(find.call_args.args[0].parent, presence.config.INSTALL_DIR)


class TestNotifyOnlyWhenNeeded(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolate_config(self, Path(tmp.name))

    def notify(self, running, showing):
        with (
            patch.object(presence, "service_running", return_value=running),
            patch.object(presence, "dashboard_showing", return_value=showing),
            patch.object(logs.subprocess, "run", return_value=launchctl(0)) as run,
        ):
            REAL_NOTIFY("Title", "Text")
        return run.called

    def test_it_notifies_with_the_service_running_and_no_dashboard(self):
        self.assertTrue(self.notify(True, False))

    def test_it_stays_quiet_with_the_dashboard_showing(self):
        self.assertFalse(self.notify(True, True))

    def test_it_stays_quiet_with_the_service_stopped(self):
        self.assertFalse(self.notify(False, False))

    def test_what_it_held_back_is_still_logged(self):
        with self.assertLogs("camera_offloader", "INFO") as logged:
            self.notify(True, True)
        self.assertIn("Title", "".join(logged.output))

    def test_a_failed_check_still_notifies(self):
        with (
            patch.object(presence, "service_running", side_effect=RuntimeError("odd")),
            patch.object(logs.subprocess, "run", return_value=launchctl(0)) as run,
        ):
            REAL_NOTIFY("Title", "Text")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
