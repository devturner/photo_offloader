"""Browser tests for what a failed import shows: its problem list, the card warning, the package."""

from __future__ import annotations

import re
import unittest

from tests.browser.harness import Browser, BrowserTestCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None  # setUpModule skips every test


def setUpModule() -> None:
    Browser.start()


def tearDownModule() -> None:
    Browser.stop()


class TestProblemList(BrowserTestCase):
    def rows(self):
        return self.page.locator(".problem-list details")

    def test_a_failed_run_opens_its_problems_by_itself(self):
        self.show("few_problems")
        expect(self.page.locator(".problems-report")).to_be_visible()
        expect(self.page.locator(".pairs")).to_have_text(
            "No photo lost both copies in the folders that could be read. "
            "2 RAW still have their JPG."
        )

    def test_once_closed_it_stays_closed(self):
        self.show("few_problems")
        self.page.locator("#history .row").first.click()
        expect(self.page.locator(".problems-report")).to_have_count(0)
        self.refresh()
        self.page.wait_for_timeout(100)
        expect(self.page.locator(".problems-report")).to_have_count(0)

    def test_rows_keep_a_fixed_order_and_an_unreadable_folder_is_unknown(self):
        self.show("many_problems")
        names = self.page.locator(".problem-list .tname").all_inner_texts()
        self.assertEqual(names, ["RAW", "JPG", "Unknown"])

    def test_a_row_unfolds_to_name_its_files_and_stays_open_across_a_redraw(self):
        self.show("many_problems")
        self.rows().first.locator("summary").click()
        names = self.rows().first.locator(".names")
        expect(names).to_contain_text("100NIKON/DSC_1030.NEF")
        expect(names).to_contain_text("102NIKON/DSC_0001.NEF")
        expect(names).to_be_visible()
        self.page.wait_for_timeout(50)  # the browser reports a row's toggle a moment later
        # Forget what's drawn, as a change in the history would.
        self.page.evaluate("delete sigs.history; renderHistory(LAST.generated_at)")
        expect(self.rows().first.locator(".names")).to_be_visible()

    def test_a_long_row_says_how_many_more_and_opens_them_in_a_list(self):
        self.show("huge_problems")
        raw = self.rows().first
        raw.locator("summary").click()
        expect(raw.locator(".more")).to_have_text("and 586 more")
        raw.get_by_role("button", name="Open these 606 RAW files in a list").click()
        expect(self.page.locator("#toast")).to_have_class(re.compile("show"))
        self.assertRegex(self.sandbox.spawned[-1][1], r"files-RAW\.csv$")

    def test_a_card_that_stopped_answering_is_the_first_thing_said(self):
        self.show("huge_problems")
        expect(self.page.locator(".cause")).to_contain_text("DSC_0002.JPG")

    def test_a_clean_run_only_offers_the_package(self):
        self.show("clean_with_report")
        expect(self.page.locator(".problems-report")).to_have_count(0)
        self.page.locator("#history .row").first.click()
        expect(self.page.locator(".report-note")).to_contain_text("Nothing went wrong")
        expect(self.page.get_by_role("button", name="Try again")).to_have_count(0)

    def test_file_names_are_never_treated_as_markup(self):
        self.show("few_problems")
        self.page.evaluate(
            """() => {
                const r = HISTORY[0];
                r.details.problems.types[0].names = ['<img src=x onerror=window.hacked=1>.NEF'];
                delete sigs.history; renderHistory(LAST.generated_at);
            }"""
        )
        self.rows().first.locator("summary").click()
        expect(self.page.locator(".names img")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.hacked || null"))


class TestCardWarning(BrowserTestCase):
    def card(self):
        return self.page.locator("#pipeline .step").first

    def test_the_card_box_says_dont_format(self):
        self.show("few_problems")
        expect(self.card()).to_contain_text("Don't format: files not saved")
        self.assertEqual(self.card().get_attribute("data-state"), "error")

    def test_eject_asks_first_and_keep_mounted_does_nothing(self):
        self.show("few_problems")
        self.card().get_by_role("button", name="Eject").click()
        expect(self.card().locator(".ask")).to_contain_text("Eject it anyway?")
        self.assertEqual(self.sandbox.core.actions, [])
        self.card().get_by_role("button", name="Keep mounted").click()
        expect(self.card().locator(".ask")).to_have_count(0)
        self.assertEqual(self.sandbox.core.actions, [])

    def test_the_question_survives_the_next_poll(self):
        self.show("few_problems")
        self.card().get_by_role("button", name="Eject").click()
        self.refresh()
        self.page.wait_for_timeout(100)
        expect(self.card().locator(".ask")).to_be_visible()

    def test_answering_eject_ejects(self):
        self.show("few_problems")
        self.card().get_by_role("button", name="Eject").click()
        self.card().locator(".ask").get_by_role("button", name="Eject").click()
        expect(self.page.locator("#toast")).to_have_class(re.compile("show"))
        self.assertEqual(self.sandbox.core.actions[-1][0], "eject_card")

    def test_try_again_imports_the_card(self):
        self.show("few_problems")
        self.page.locator(".report-actions").get_by_role("button", name="Try again").click()
        expect(self.page.locator("#toast")).to_have_class(re.compile("show"))
        self.assertEqual(self.sandbox.core.actions[-1][0], "import_card")


class TestDebugPackage(BrowserTestCase):
    def open_sheet(self):
        self.show("few_problems")
        self.page.get_by_role("button", name="Save debug package…").click()
        expect(self.page.locator("#save-dialog")).to_be_visible()

    def test_the_sheet_warns_about_names_and_previews_each_file(self):
        self.open_sheet()
        expect(self.page.locator(".privacy")).to_contain_text("Mac username")
        expect(self.page.locator("#preview")).to_contain_text("Safe on the Mac")
        self.page.get_by_role("radio", name="files.csv").click()
        expect(self.page.locator("#preview")).to_contain_text("type,result,card_path")

    def test_saving_says_where_and_can_show_it_in_finder(self):
        self.open_sheet()
        self.page.get_by_role("button", name="Save to Downloads").click()
        expect(self.page.locator("#saved-name")).to_have_text(
            re.compile(r"^PhotoOffloader-.*\.zip")
        )
        self.page.get_by_role("button", name="Show in Finder").click()
        self.page.wait_for_timeout(200)
        self.assertEqual(self.sandbox.spawned[-1][:2], ["/usr/bin/open", "-R"])

    def test_cancel_saves_nothing(self):
        self.open_sheet()
        self.page.get_by_role("button", name="Cancel").click()
        expect(self.page.locator("#save-dialog")).to_be_hidden()
        self.assertFalse((self.sandbox.root / "Downloads").exists())


if __name__ == "__main__":
    unittest.main()
