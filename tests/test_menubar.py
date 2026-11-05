#!/usr/bin/env python3
"""Tests for photo_offloader.menubar.model: what the menu-bar menu offers in each state."""
import unittest

from photo_offloader.menubar import model
from photo_offloader.menubar.model import Snapshot


def keys(snapshot):
    return [entry.key for entry in model.menu_entries(snapshot)]


class TestMenuEntries(unittest.TestCase):
    def test_running_menu_is_short(self):
        self.assertEqual(keys(Snapshot(running=True)),
                         ["open_dashboard", "pause", None, "view_log", None, "stop", "quit"])

    def test_the_import_log_can_always_be_opened(self):
        # User question 2026-11-04: the old UI had a debug/log view.
        for snapshot in (Snapshot(running=True), Snapshot(running=False), Snapshot(running=True, transfer=True)):
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

    def test_a_stopped_service_can_always_be_started_and_the_menu_bar_can_always_be_quit(self):
        # Regression: after "Stop Photo Offloader" every item was greyed out, including Quit,
        # and there was no way to start the service again.
        entries = model.menu_entries(Snapshot(running=False))
        actionable = {e.key: e for e in entries if e.key not in (None, "info")}
        self.assertTrue(actionable["start"].enabled)
        self.assertTrue(actionable["quit"].enabled)
        self.assertTrue(actionable["open_dashboard"].enabled)
        self.assertTrue(all(e.enabled for e in actionable.values()),
                        "nothing selectable may be disabled when the service is stopped")

    def test_a_stopped_menu_explains_itself_and_hides_irrelevant_actions(self):
        entries = model.menu_entries(Snapshot(running=False, usb=True))
        info = [e for e in entries if e.key == "info"]
        self.assertEqual(len(info), 1)
        self.assertIn("stopped", info[0].title.lower())
        self.assertFalse(info[0].enabled)
        self.assertEqual([e.key for e in entries if e.key in ("pause", "stop", "copy_usb")], [])

    def test_quit_is_labelled_so_it_cannot_be_mistaken_for_stopping(self):
        for snapshot in (Snapshot(running=True), Snapshot(running=False)):
            quit_entry = next(e for e in model.menu_entries(snapshot) if e.key == "quit")
            self.assertIn("Menu Bar", quit_entry.title)

    def test_a_transfer_in_progress_does_not_disable_stopping(self):
        entries = {e.key: e for e in model.menu_entries(Snapshot(running=True, transfer=True))}
        self.assertTrue(entries["stop"].enabled)

    def test_separators_are_never_doubled_or_trailing(self):
        for running in (True, False):
            for paused in (True, False):
                for usb in (True, False):
                    for transfer in (True, False):
                        ks = keys(Snapshot(running, paused, transfer, usb))
                        self.assertNotEqual(ks[-1], None)
                        self.assertNotEqual(ks[0], None)
                        self.assertFalse(any(a is None and b is None for a, b in zip(ks, ks[1:])))


class TestTitleAndParsing(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(model.menu_title(Snapshot(running=True)), "📷")
        self.assertEqual(model.menu_title(Snapshot(running=True, paused=True)), "📷 ⏸")
        self.assertEqual(model.menu_title(Snapshot(running=False)), "📷 Off")
        self.assertEqual(model.menu_title(Snapshot(running=True, transfer=True)), "📷 ↻")

    def test_parse_status_output(self):
        output = "Service:  running\nMenu bar: running\nImports:  paused\nTransfer: in progress\n"
        snap = model.parse_status(output, 0, usb=True)
        self.assertEqual(snap, Snapshot(running=True, paused=True, transfer=True, usb=True))

    def test_parse_status_of_a_stopped_service(self):
        snap = model.parse_status("Service:  stopped\nMenu bar: running\nImports:  active\nTransfer: none\n", 3)
        self.assertEqual(snap, Snapshot(running=False))

    def test_unparseable_output_is_treated_as_stopped_not_as_a_crash(self):
        self.assertEqual(model.parse_status("", 1), Snapshot(running=False))
        self.assertEqual(model.parse_status("garbage", 127), Snapshot(running=False))


if __name__ == "__main__":
    unittest.main()
