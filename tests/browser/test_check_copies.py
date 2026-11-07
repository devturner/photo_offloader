"""Browser tests for checking the saved copies: the menu item, its warning, the running row, results."""

from __future__ import annotations

import json
import unittest

from photo_offloader import config, verify_report
from tests.browser.harness import Browser, BrowserTestCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None  # setUpModule skips every test


def setUpModule() -> None:
    Browser.start()


def tearDownModule() -> None:
    Browser.stop()


class TestMenuAndWarning(BrowserTestCase):
    def open_warning(self):
        self.page.click("#menu-button")
        self.page.click("#m-verify")
        expect(self.page.locator("#verify-dialog")).to_be_visible()

    def test_the_item_is_there_and_asks_before_it_starts(self):
        self.show("idle")
        self.open_warning()
        self.assertFalse(self.sandbox.spawned)
        expect(self.page.locator("#verify-time")).to_contain_text("only the Mac")

    def test_not_now_starts_nothing(self):
        self.show("idle")
        self.open_warning()
        self.page.click("#verify-cancel")
        expect(self.page.locator("#verify-dialog")).to_be_hidden()
        self.assertFalse(self.sandbox.spawned)

    def test_start_check_starts_it(self):
        self.show("idle")
        self.open_warning()
        self.page.click("#verify-go")
        expect(self.page.locator("#verify-dialog")).to_be_hidden()
        expect(self.page.locator("#toast")).to_contain_text("started")
        self.assertTrue(self.sandbox.spawned[-1][-1].endswith(config.VERIFY))

    def test_with_the_drive_it_warns_that_the_drive_cant_be_ejected(self):
        self.load("idle")
        self.sandbox.add_usb()
        self.open_page()
        self.wait_for_render()
        self.open_warning()
        expect(self.page.locator("#verify-eject")).to_be_visible()

    def test_without_the_drive_that_line_is_not_there(self):
        self.show("idle")
        self.open_warning()
        expect(self.page.locator("#verify-eject")).to_be_hidden()

    def test_the_item_is_off_during_an_import_and_during_a_check(self):
        for state in ("copying", "check_running"):
            self.show(state)
            self.page.click("#menu-button")
            expect(self.page.locator("#m-verify")).to_be_disabled()
            self.page.keyboard.press("Escape")


class TestRunningRow(BrowserTestCase):
    def test_it_is_pinned_at_the_top_of_recent_activity(self):
        self.show("check_running")
        first = self.page.locator("#history > *").first
        expect(first).to_contain_text("Checking saved copies")
        expect(first).to_contain_text("55% · USB drive · 3,490 of 6,300 copies · about 15 min left")

    def test_it_leaves_the_top_panel_alone(self):
        self.show("check_running")
        expect(self.page.locator("#hero h2")).not_to_contain_text("Checking")

    def test_an_import_takes_the_panel_and_the_row_stays_where_it_is(self):
        self.show("check_during_import")
        expect(self.page.locator("#hero h2")).to_have_text("Copying new files")
        expect(self.page.locator("#history > *").first).to_contain_text("Checking saved copies")

    def test_progress_updates_without_redrawing_the_row(self):
        """So its Stop button keeps keyboard focus and nothing flickers."""
        self.show("check_running")
        self.page.evaluate("document.querySelector('#check-stop').dataset.same = '1'")
        status = json.loads(config.VERIFY_STATUS_PATH.read_text())
        config.VERIFY_STATUS_PATH.write_text(
            json.dumps({**status, "files_done": 4000, "bytes_done": 90e9})
        )
        self.sandbox.advance(3)
        self.refresh()
        expect(self.page.locator("#history > *").first).to_contain_text("4,000 of 6,300")
        self.assertEqual(
            self.page.evaluate("document.querySelector('#check-stop').dataset.same"), "1"
        )

    def test_stop_asks_for_the_check_to_stop(self):
        self.show("check_running")
        self.page.click("#check-stop")
        expect(self.page.locator("#toast")).to_contain_text("Stopping")
        self.assertEqual(self.sandbox.core.actions[-1][0], "stop_verify")

    def test_it_shows_with_nothing_yet_in_the_history(self):
        self.load("idle")
        self.sandbox.verify_up = True
        self.open_page()
        expect(self.page.locator("#history > *").first).to_contain_text("Checking saved copies")
        expect(self.page.locator("#history .empty")).to_have_count(0)


class TestResults(BrowserTestCase):
    def test_a_clean_result_is_just_a_row(self):
        self.show("check_clean")
        row = self.page.locator("#history .row").first
        expect(row).to_contain_text("Saved copies checked")
        expect(row).to_contain_text("All 6,300 copies match")
        expect(self.page.locator(".check-report")).to_have_count(0)

    def test_problems_open_by_themselves_with_each_files_other_copy(self):
        self.show("check_problems")
        report = self.page.locator(".check-report")
        expect(report).to_contain_text("2026-10/NIKON-Z-8/Raws/_DZ80412.NEF")
        expect(report).to_contain_text("Damaged")
        expect(report).to_contain_text("Good, on the Mac")
        expect(report).to_contain_text("Good, on the USB drive")
        expect(self.page.locator("#check-mark")).to_have_count(0)
        expect(self.page.locator("#hero h2")).to_have_text("Saved copies need attention")

    def test_a_files_name_is_never_markup(self):
        self.load("idle")
        name = verify_report.save_report(
            [
                {
                    "where": verify_report.MAC,
                    "problem": verify_report.DAMAGED,
                    "other_copy": verify_report.OTHER_GOOD,
                    "path": "/x",
                    "relative_path": "<img src=x onerror=window.pwned=1>.jpg",
                    "bytes": 1,
                    "expected_sha256": "",
                    "actual_sha256": "",
                }
            ],
            self.sandbox.now,
        )
        rows = verify_report.report_rows(name)
        self.sandbox.record(
            kind="verify",
            outcome="failed",
            source="",
            details={"whole": True, "check": verify_report.details_for(rows, name)},
        )
        self.open_page()
        expect(self.page.locator(".check-report")).to_contain_text("<img src=x")
        self.assertIsNone(self.page.evaluate("window.pwned || null"))
        expect(self.page.locator(".check-report img")).to_have_count(0)

    def test_files_deleted_from_the_mac_are_grouped_by_folder(self):
        self.show("check_deleted")
        report = self.page.locator(".check-report")
        expect(report).to_contain_text("2026-10/NIKON-Z-8/Raws")
        expect(report).to_contain_text("1,102 files")
        expect(report).to_contain_text("2026-07/Canon/Raws")
        expect(report).not_to_contain_text("_DZ80412.NEF")
        expect(self.page.locator(".run-report .report")).to_have_count(0)

    def test_every_heading_fits_at_narrow_width(self):
        self.show("check_deleted", width=540, height=860)
        wrap = self.page.locator(".check-table-wrap").first
        self.assertLessEqual(
            wrap.evaluate("e => e.scrollWidth"), wrap.evaluate("e => e.clientWidth")
        )

    def test_the_button_notes_them_and_the_row_becomes_a_pass(self):
        self.show("check_deleted")
        self.page.click("#check-mark")
        row = self.page.locator("#history .row").first
        expect(row).to_contain_text("Saved copies checked")
        expect(row).to_contain_text("1,381 files deleted from the Mac on purpose")
        self.assertEqual(len(verify_report.deleted_files()), 1381)
        expect(self.page.locator("#check-mark")).to_have_count(0)

    def test_open_full_list_opens_the_saved_report(self):
        self.show("check_deleted")
        self.page.click("#check-list")
        expect(self.page.locator("#toast")).to_contain_text("Opening")
        self.assertTrue(self.sandbox.spawned[-1][1].endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
