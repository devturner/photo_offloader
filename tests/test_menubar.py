#!/usr/bin/env python3
"""Tests for photo_offloader.menubar.model: what the menu-bar menu offers in each state."""

import unittest

from photo_offloader.menubar import model
from photo_offloader.menubar.model import Snapshot


def keys(snapshot):
    return [entry.key for entry in model.menu_entries(snapshot)]


class TestMenuEntries(unittest.TestCase):
    def test_running_menu_follows_the_agreed_layout(self):
        # Layout the user chose on 2026-11-09: dashboard, then everyday actions, then the
        # service controls with Stop and Exit last.
        self.assertEqual(
            keys(Snapshot(running=True, dashboard=True)),
            [
                "open_dashboard",
                "close_dashboard",
                None,
                "pause",
                "view_log",
                None,
                "stop",
                "stop_exit",
            ],
        )

    def test_a_running_upload_can_be_paused_and_paused_uploads_resumed(self):
        for upload, title in (("running", "Pause Uploads"), ("paused", "Resume Uploads")):
            for running in (True, False):  # uploads carry on with the service stopped
                with self.subTest(upload=upload, running=running):
                    entries = model.menu_entries(Snapshot(running=running, upload=upload))
                    self.assertEqual(entries[1].key, None)
                    self.assertEqual((entries[2].key, entries[2].title), ("upload", title))

    def test_no_upload_item_without_an_upload(self):
        self.assertNotIn("upload", keys(Snapshot(running=True)))

    def test_stopped_menu_offers_start_and_stop_and_exit(self):
        self.assertEqual(
            keys(Snapshot(running=False)),
            ["open_dashboard", None, "view_log", None, "start", "stop_exit"],
        )

    def test_service_items_say_service_and_stop_and_exit_asks_first(self):
        titles = {e.key: e.title for e in model.menu_entries(Snapshot(running=True))}
        self.assertEqual(titles["stop"], "Stop Service…")
        self.assertEqual(titles["stop_exit"], "Stop and Exit…")
        titles = {e.key: e.title for e in model.menu_entries(Snapshot(running=False))}
        self.assertEqual(titles["start"], "Start Service")

    def test_close_dashboard_is_offered_only_while_one_is_open(self):
        for running in (True, False):
            self.assertIn("close_dashboard", keys(Snapshot(running=running, dashboard=True)))
            self.assertNotIn("close_dashboard", keys(Snapshot(running=running)))

    def test_stop_and_exit_is_always_offered(self):
        for running in (True, False):
            for transfer in (True, False):
                entry = next(
                    e
                    for e in model.menu_entries(Snapshot(running=running, transfer=transfer))
                    if e.key == "stop_exit"
                )
                self.assertTrue(entry.enabled)

    def test_the_import_log_can_always_be_opened(self):
        # User question 2026-11-04: the old UI had a debug/log view.
        for snapshot in (
            Snapshot(running=True),
            Snapshot(running=False),
            Snapshot(running=True, transfer=True),
        ):
            entry = next(e for e in model.menu_entries(snapshot) if e.key == "view_log")
            self.assertTrue(entry.enabled)

    def test_usb_backup_is_offered_only_when_a_drive_is_connected_and_nothing_is_running(self):
        self.assertIn("copy_usb", keys(Snapshot(running=True, usb=True)))
        self.assertNotIn("copy_usb", keys(Snapshot(running=True, usb=False)))
        self.assertNotIn("copy_usb", keys(Snapshot(running=True, usb=True, transfer=True)))

    def test_pause_becomes_resume(self):
        entries = {e.key: e for e in model.menu_entries(Snapshot(running=True, paused=True))}
        self.assertEqual(entries["pause"].title, "Resume Automatic Imports")
        entries = {e.key: e for e in model.menu_entries(Snapshot(running=True))}
        self.assertEqual(entries["pause"].title, "Pause Automatic Imports")

    def test_nothing_selectable_is_disabled_when_the_service_is_stopped(self):
        # Regression: after "Stop Photo Offloader" every item was greyed out, including Quit,
        # and there was no way to start the service again.
        entries = model.menu_entries(Snapshot(running=False, dashboard=True))
        self.assertTrue(all(e.enabled for e in entries if e.key is not None))

    def test_a_stopped_menu_hides_irrelevant_actions(self):
        entries = model.menu_entries(Snapshot(running=False, usb=True))
        self.assertEqual([e.key for e in entries if e.key in ("pause", "stop", "copy_usb")], [])

    def test_a_transfer_in_progress_does_not_disable_stopping(self):
        entries = {e.key: e for e in model.menu_entries(Snapshot(running=True, transfer=True))}
        self.assertTrue(entries["stop"].enabled)

    def test_separators_are_never_doubled_or_trailing(self):
        for running in (True, False):
            for paused in (True, False):
                for usb in (True, False):
                    for transfer in (True, False):
                        for dashboard in (True, False):
                            ks = keys(Snapshot(running, paused, transfer, usb, dashboard))
                            self.assertNotEqual(ks[-1], None)
                            self.assertNotEqual(ks[0], None)
                            self.assertFalse(
                                any(a is None and b is None for a, b in zip(ks, ks[1:]))
                            )


class TestTitleAndParsing(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(model.menu_title(Snapshot(running=True)), "📷")
        self.assertEqual(model.menu_title(Snapshot(running=True, paused=True)), "📷 ⏸")
        self.assertEqual(model.menu_title(Snapshot(running=False)), "📷 Off")
        self.assertEqual(model.menu_title(Snapshot(running=True, transfer=True)), "📷 ↻")

    def test_beside_the_app_icon_the_title_is_only_the_state(self):
        self.assertEqual(model.menu_title(Snapshot(running=True), icon=True), "")
        self.assertEqual(model.menu_title(Snapshot(running=True, paused=True), icon=True), " ⏸")
        self.assertEqual(model.menu_title(Snapshot(running=False), icon=True), " Off")
        self.assertEqual(model.menu_title(Snapshot(running=True, transfer=True), icon=True), " ↻")

    def test_parse_status_output(self):
        output = "Service:  running\nMenu bar: running\nImports:  paused\nTransfer: in progress\n"
        snap = model.parse_status(output, 0, usb=True)
        self.assertEqual(snap, Snapshot(running=True, paused=True, transfer=True, usb=True))

    def test_parse_status_of_a_stopped_service(self):
        snap = model.parse_status(
            "Service:  stopped\nMenu bar: running\nImports:  active\nTransfer: none\n", 3
        )
        self.assertEqual(snap, Snapshot(running=False))

    def test_unparseable_output_is_treated_as_stopped_not_as_a_crash(self):
        self.assertEqual(model.parse_status("", 1), Snapshot(running=False))
        self.assertEqual(model.parse_status("garbage", 127), Snapshot(running=False))


if __name__ == "__main__":
    unittest.main()
