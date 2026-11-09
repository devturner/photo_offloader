"""Browser tests for dashboard/web/index.html: what the page shows and what its controls do.

Needs Playwright and its Chromium (``requirements-dev.txt``, then ``python -m playwright install
chromium``). Without them this module skips, so a plain ``unittest discover`` still works. Set
PHOTO_OFFLOADER_TEST_SCREENSHOTS to a folder to also save a picture of every state.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from typing import Dict, List

from photo_offloader import catalog
from tests.browser.harness import CARD, MB, ONE_RUN, SCENARIOS, Browser, BrowserTestCase

try:
    from playwright.sync_api import expect
except ImportError:
    expect = None  # setUpModule skips every test

NATIVE_WINDOW = {"width": 960, "height": 860}
NARROW = {"width": 540, "height": 860}
TRANSFER_STATES = (
    "scan",
    "read_photo_info",
    "checking",
    "copying",
    "copying_big_card",
    "usb_backup",
    "finishing_import",
    "starting_upload",
    "uploading",
)
SUMMARY_STATES = ("finished", "failed", "failed_on_usb", "failed_unreadable_card")
RESTING_STATES = ("idle", "card_mounted", "already_imported", "paused")
SLOT_NAMES = ["Progress", "Speed", "Files", "Elapsed", "Remaining"]
HOSTILE = '<img src=x onerror="window.__pwned=1">'
DARK_BACKGROUND, LIGHT_BACKGROUND = "rgb(28, 28, 30)", "rgb(245, 245, 247)"


def setUpModule() -> None:
    Browser.start()


def tearDownModule() -> None:
    Browser.stop()


class PageTestCase(BrowserTestCase):
    def slots(self) -> Dict[str, str]:
        return self.page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.slot')]
            .map(s => [s.querySelector('.k').textContent, s.querySelector('.v').textContent]))""")

    def steps(self) -> List[str]:
        return self.page.evaluate(
            "[...document.querySelectorAll('.stages span')].map(s => s.dataset.state)"
        )

    def background(self) -> str:
        return self.page.evaluate("getComputedStyle(document.body).backgroundColor")

    def theme(self):
        return self.page.evaluate("document.documentElement.dataset.theme || null")

    def click_action(self, selector_or_locator, action: str):
        """Click and wait until the server has answered this action."""
        target = (
            self.page.locator(selector_or_locator)
            if isinstance(selector_or_locator, str)
            else selector_or_locator
        )
        with self.page.expect_response(
            lambda r: r.url.endswith("/api/action/" + action)
        ) as response:
            target.click()
        self.assertTrue(response.value.ok, f"{action}: {response.value.status}")

    def assert_acted(self, *actions) -> None:
        self.assertEqual(self.sandbox.core.actions, list(actions))


class TestFixedPanel(PageTestCase):
    """One panel of one size for the whole run; nothing below it moves."""

    def test_the_panel_has_one_height_in_every_state(self):
        for window in (NATIVE_WINDOW, NARROW):
            heights = {}
            for state in SCENARIOS:
                if state != "stopped":
                    self.show(state, **window)
                    heights[state] = self.page.locator("#hero").bounding_box()["height"]
            with self.subTest(width=window["width"]):
                self.assertEqual(len(set(heights.values())), 1, heights)

    def test_nothing_moves_while_one_page_follows_a_whole_run(self):
        self.show("idle", **NATIVE_WINDOW)
        where = (
            "[document.querySelector('#hero').getBoundingClientRect().top,"
            " document.querySelector('#hero').getBoundingClientRect().height,"
            " document.querySelector('#pipeline').getBoundingClientRect().top]"
        )
        start = self.page.evaluate(where)
        for state in ONE_RUN:
            with self.subTest(state=state):
                self.load(state)
                self.refresh()
                self.assertEqual(self.page.evaluate(where), start)

    def test_nothing_in_the_panel_is_cut_off_at_the_bottom(self):
        overflow = """() => { const hero = document.querySelector('#hero').getBoundingClientRect().bottom;
            return Math.max(...[...document.querySelectorAll('#hero *')]
                .map(e => e.getBoundingClientRect().bottom)) - hero; }"""
        for window in (NATIVE_WINDOW, NARROW):
            for state in TRANSFER_STATES + SUMMARY_STATES:
                with self.subTest(state=state, width=window["width"]):
                    self.show(state, **window)
                    self.assertLessEqual(self.page.evaluate(overflow), 0)

    def test_the_stopped_banner_replaces_the_panel(self):
        self.show("stopped")
        expect(self.page.locator("#hero")).to_be_hidden()
        expect(self.page.locator("#banner")).to_be_visible()
        self.assertEqual(self.page.locator("#pill-text").text_content(), "Stopped")
        self.click_action("#banner-start", "service_start")
        self.assertIn(
            ["/bin/bash", str(self.sandbox.paths.service_script), "start"], self.sandbox.ran
        )


class TestSlots(PageTestCase):
    def test_an_import_counts_new_files_against_the_estimate(self):
        for fields, expected in (
            ({"new_files": 737, "new_files_expected": 1278}, "737 of 1,278 new"),
            (
                {"new_files": 14, "new_files_expected": 12},
                "14 of 14 new",
            ),  # never past its own total
            ({"new_files": 0, "new_files_expected": 0}, "Nothing new"),
            ({"new_files": 737, "new_files_expected": None}, "737 new"),
            ({"new_files": 0, "new_files_expected": None}, "0 new"),
        ):
            with self.subTest(**fields):
                self.show("idle")
                text = self.page.evaluate("v => newFilesText(v)", fields)
                self.assertEqual(text, expected)

    def test_only_the_import_counts_new_files(self):
        self.show("usb_backup")
        self.assertEqual(self.slots()["Files"], "1,214 of 2,378")
        self.show("uploading")
        self.assertNotIn("new", self.slots()["Files"])

    def test_no_card_repeats_the_progress_bar_above_it(self):
        for state in ("copying", "usb_backup", "uploading"):
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(self.page.locator(".step .mini").count(), 0)

    def test_all_five_slots_are_there_while_anything_runs(self):
        for state in TRANSFER_STATES:
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(list(self.slots()), SLOT_NAMES)

    def test_a_value_not_known_yet_is_a_dash_not_missing(self):
        self.show("scan")
        self.assertEqual(
            self.slots(),
            {"Progress": "—", "Speed": "—", "Files": "—", "Elapsed": "2s", "Remaining": "—"},
        )
        self.assertEqual(self.page.locator(".slot .v.none").count(), 4)

    def test_copying_fills_every_slot(self):
        self.show("copying")
        self.assertEqual(
            self.slots(),
            {
                "Progress": "43% · 34.0 of 80.0 GB",
                "Speed": "153.1 MB/s",
                "Files": "47 new",
                "Elapsed": "1m 59s",
                "Remaining": "4m 00s",
            },
        )
        self.assertEqual(self.page.locator(".slot .v.none").count(), 0)

    def test_a_resting_panel_has_no_slots_or_steps(self):
        for state in RESTING_STATES:
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(self.slots(), {})
                self.assertEqual(self.steps(), [])


class TestProgressBar(PageTestCase):
    """With nothing to measure yet, the bar glows so the app doesn't look stalled."""

    WAITING_STATES = ("scan", "read_photo_info", "starting_upload")

    def glow(self):
        """The fill's width as a share of the track, its animation, and how far that animation is
        from where it would be if it had run since the page opened (0 is in step)."""
        return self.page.evaluate(
            """() => { const fill = document.querySelector('#hero .bar > div');
                const share = fill.getBoundingClientRect().width / fill.parentElement.getBoundingClientRect().width;
                const a = fill.getAnimations()[0];
                if (!a) return [share, null, null];
                const at = a.effect.getComputedTiming().progress * GLOW_MS;
                const off = Math.abs(at - performance.now() % GLOW_MS);
                return [share, a.animationName, Math.min(off, GLOW_MS - off)]; }"""
        )

    def test_the_whole_bar_glows_while_there_is_nothing_to_measure(self):
        for state in self.WAITING_STATES:
            with self.subTest(state=state):
                self.show(state, reduced_motion="no-preference")
                share, animation, _ = self.glow()
                self.assertEqual((share, animation), (1, "glow"))

    def test_a_refresh_does_not_restart_the_glow(self):
        # The panel is rebuilt each second as Elapsed changes, which is what made the old slider flash.
        sandbox = self.load("starting_upload")
        sandbox.google_up = True
        sandbox.google_status(
            "running", ago=5
        )  # Preparing upload: Elapsed counts, nothing to measure
        self.open_page(reduced_motion="no-preference")
        for _ in range(3):
            self.page.evaluate("document.querySelector('#hero .bar > div').dataset.old = '1'")
            self.page.wait_for_timeout(350)
            sandbox.advance(1)
            self.refresh()
            self.page.wait_for_function("!document.querySelector('#hero .bar > div').dataset.old")
            self.assertLess(self.glow()[2], 100)

    def test_with_reduce_motion_it_is_a_still_empty_track(self):
        self.show("starting_upload")  # the harness asks for reduced motion by default
        opacity = self.page.evaluate(
            "getComputedStyle(document.querySelector('#hero .bar > div')).opacity"
        )
        self.assertEqual((self.glow()[1], opacity), (None, "0"))

    def test_the_bar_fills_and_stops_glowing_once_there_is_something_to_measure(self):
        self.show("copying", reduced_motion="no-preference")
        share, animation, _ = self.glow()
        self.assertLess(share, 1)
        self.assertIsNone(animation)


class TestStepStrip(PageTestCase):
    EXPECTED = {
        "scan": ["active", "pending", "pending", "pending", "pending"],
        "read_photo_info": ["done", "active", "pending", "pending", "pending"],
        "checking": ["done", "done", "active", "pending", "pending"],
        "copying": ["done", "done", "active", "pending", "pending"],
        "usb_backup": ["done", "done", "done", "active", "pending"],
        "finishing_import": ["done", "done", "done", "done", "pending"],
        "starting_upload": ["done", "done", "done", "done", "active"],
        "uploading": ["done", "done", "done", "done", "active"],
    }

    def test_each_state_marks_the_right_steps(self):
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(self.steps(), expected)
                self.assertEqual(
                    self.page.locator(".stages span").all_text_contents(),
                    [
                        "Scan card",
                        "Read photo info",
                        "Copy new files",
                        "Back up to USB",
                        "Upload to Google Photos",
                    ],
                )

    def test_steps_that_will_not_run_are_skipped(self):
        sandbox = self.load("idle")
        sandbox.add_card()
        sandbox.importer_up = True
        sandbox.status(
            state="copying", stage="copy", usb_planned=False, bytes_done=1, bytes_total=2
        )
        self.open_page()
        self.assertEqual(self.steps(), ["done", "done", "active", "skipped", "skipped"])
        expect(self.page.locator(".stages span[data-state=skipped]").first).to_have_css(
            "text-decoration-line", "line-through"
        )

    def test_each_step_state_looks_different(self):
        look = """() => Object.fromEntries([...document.querySelectorAll('.stages span')].map(s => {
            const c = getComputedStyle(s);
            return [s.dataset.state, [c.borderTopColor, c.textDecorationLine].join(' ')]; }))"""
        sandbox = self.load("idle")
        sandbox.transfer(state="copying", phase="usb-backup", stage="usb")
        self.open_page()
        looks = self.page.evaluate(look)
        self.assertEqual(sorted(looks), ["active", "done", "pending"])
        self.assertEqual(len(set(looks.values())), 3, looks)


class TestNothingTruncated(PageTestCase):
    """Every slot and step fits; each of these was once cut off with an ellipsis."""

    # Too wide for its box, or wrapped onto more lines than the box can show.
    CUT_OFF = """() => [...document.querySelectorAll('.slot .k, .slot .v, .stages span')].filter(e => {
        const text = document.createRange();
        text.selectNodeContents(e);
        return e.scrollWidth > e.clientWidth
            || text.getBoundingClientRect().bottom > e.getBoundingClientRect().bottom;
    }).map(e => e.textContent)"""

    def assert_nothing_cut_off(self, window) -> None:
        for state in TRANSFER_STATES + SUMMARY_STATES:
            with self.subTest(state=state, width=window["width"]):
                self.show(state, **window)
                self.assertEqual(self.page.evaluate(self.CUT_OFF), [])

    def test_in_the_native_window(self):
        self.assert_nothing_cut_off(NATIVE_WINDOW)

    def test_at_the_narrowest_layout(self):
        self.assert_nothing_cut_off(NARROW)


class TestMenu(PageTestCase):
    def open_menu(self, state="already_imported", **page_options):
        self.show(state, **page_options)
        self.page.click("#menu-button")
        expect(self.page.locator("#menu")).to_be_visible()

    def test_it_is_hidden_until_asked_for(self):
        self.show("idle")
        expect(self.page.locator("#menu")).to_be_hidden()
        self.page.click("#menu-button")
        expect(self.page.locator("#menu")).to_be_visible()
        self.assertEqual(self.page.get_attribute("#menu-button", "aria-expanded"), "true")

    def test_escape_and_clicking_elsewhere_close_it(self):
        self.open_menu()
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#menu")).to_be_hidden()
        self.page.click("#menu-button")
        self.page.click("footer")
        expect(self.page.locator("#menu")).to_be_hidden()

    def test_switches_keep_it_open_and_show_the_change(self):
        for selector, action, flag in (
            ("#m-eject", "toggle_auto_eject", "auto_eject_file"),
            ("#m-auto-usb", "toggle_auto_usb_backup", "auto_usb_backup_file"),
            ("#m-terminal", "toggle_terminal_output", "terminal_output_file"),
        ):
            with self.subTest(action=action):
                self.open_menu()
                self.assertEqual(self.page.get_attribute(selector, "aria-checked"), "false")
                self.click_action(selector, action)
                expect(self.page.locator(selector)).to_have_attribute("aria-checked", "true")
                expect(self.page.locator("#menu")).to_be_visible()
                self.assertTrue(getattr(self.sandbox.paths, flag).exists())
                self.assert_acted((action, {}))

    def test_actions_close_it_and_call_the_server(self):
        for selector, action, params in (
            ("#m-pause", "pause", {}),
            ("#m-catalog", "update_catalog", {}),
            ("#m-usb", "usb_backup", {}),
            ("#m-google", "google_sync", {}),
            ("[data-log=dashboard]", "open_log", {"log": "dashboard"}),
        ):
            with self.subTest(action=action):
                self.open_menu()
                self.click_action(selector, action)
                expect(self.page.locator("#menu")).to_be_hidden()
                self.assert_acted((action, params))

    def test_pause_turns_into_resume(self):
        self.open_menu("paused")
        expect(self.page.locator("#m-pause")).to_have_text("Resume automatic imports")
        self.click_action("#m-pause", "resume")
        self.assert_acted(("resume", {}))

    def test_transfers_that_would_collide_are_disabled_while_importing(self):
        self.open_menu("copying")
        for selector in ("#m-catalog", "#m-usb", "#m-google"):
            with self.subTest(selector=selector):
                expect(self.page.locator(selector)).to_be_disabled()
        expect(self.page.locator("#m-pause")).to_be_enabled()

    def test_a_backup_and_an_upload_each_get_a_row(self):
        self.show("backup_and_upload")
        expect(self.page.locator("#hero h2")).to_have_text("Backing up and uploading")
        expect(self.page.locator("#hero .lane-name")).to_have_text(["USB backup", "Google Photos"])
        expect(self.page.locator("#hero .lane-figures").nth(1)).to_contain_text("7.0 MB/s")

    def test_a_backup_and_upload_panel_stays_the_panels_size(self):
        heights = []
        for state in ("idle", "backup_and_upload", "backup_paused"):
            self.show(state)
            heights.append(self.page.locator("#hero").bounding_box()["height"])
        self.assertEqual(len(set(heights)), 1, heights)
        self.show("backup_and_upload")
        content = self.page.evaluate("document.querySelector('#hero').scrollHeight")
        self.assertLessEqual(content, heights[0] + 1, "the two rows must fit in the panel")

    def test_a_drive_that_is_behind_offers_a_backup_only_when_plugged_in(self):
        self.open_menu("usb_behind")
        expect(self.page.locator("#m-usb")).to_be_enabled()
        expect(self.page.locator('.step[data-state="warn"] h3')).to_have_text("12 files behind")
        self.open_menu("usb_unplugged_behind")
        expect(self.page.locator("#m-usb")).to_be_disabled()
        expect(self.page.locator('.step[data-state="warn"] h3')).to_have_text("Not connected")

    def test_stop_asks_first_and_offers_a_choice_during_a_transfer(self):
        self.open_menu("already_imported")
        self.page.click("#m-service")
        expect(self.page.locator("#menu")).to_be_hidden()
        expect(self.page.locator("#stop-title")).to_have_text("Stop Photo Offloader?")
        self.assert_acted()
        self.click_action(
            self.page.locator("#stop-actions").get_by_role("button", name="Stop"), "service_stop"
        )
        self.assert_acted(("service_stop", {}))

        self.open_menu("copying")
        self.page.click("#m-service")
        expect(self.page.locator("#stop-title")).to_have_text("A transfer is in progress")
        self.click_action(
            self.page.get_by_role("button", name="Stop after transfer"), "service_stop"
        )
        self.assert_acted(("service_stop", {"mode": "wait"}))


class TestAppearance(PageTestCase):
    def choose(self, theme: str) -> None:
        self.click_action(f"[data-theme-choice={theme}]", "set_theme")

    def test_choosing_a_theme_applies_it_and_keeps_the_menu_open(self):
        self.show("idle")
        self.assertEqual(self.background(), LIGHT_BACKGROUND)
        self.page.click("#menu-button")
        self.choose("dark")
        self.assertEqual(self.theme(), "dark")
        self.assertEqual(self.background(), DARK_BACKGROUND)
        expect(self.page.locator("#menu")).to_be_visible()
        expect(self.page.locator("[data-theme-choice=dark]")).to_have_attribute(
            "aria-checked", "true"
        )
        expect(self.page.locator("[data-theme-choice=system]")).to_have_attribute(
            "aria-checked", "false"
        )

    def test_auto_follows_the_system_and_a_choice_overrides_it(self):
        self.show("idle", color_scheme="dark")
        self.assertIsNone(self.theme())
        self.assertEqual(self.background(), DARK_BACKGROUND)
        self.page.click("#menu-button")
        self.choose("light")
        self.assertEqual(self.theme(), "light")
        self.assertEqual(self.background(), LIGHT_BACKGROUND)
        self.choose("system")
        self.assertIsNone(self.theme())
        self.assertEqual(self.background(), DARK_BACKGROUND)

    def test_the_choice_survives_a_reload_and_a_new_window(self):
        self.show("idle")
        self.page.click("#menu-button")
        self.choose("dark")
        self.assertEqual(self.page.evaluate("localStorage.getItem('po-theme')"), "dark")

        self.page.reload()
        self.wait_for_render()
        self.assertEqual(self.theme(), "dark")

        self.open_page()  # new browser storage: only the server can remember it now
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        self.assertEqual(self.background(), DARK_BACKGROUND)


class TestStepButtons(PageTestCase):
    def press(self, step: str, label: str, action: str) -> None:
        self.show("already_imported")
        button = self.page.locator(".step", has_text=step).get_by_role("button", name=label)
        self.click_action(button, action)
        self.assert_acted((action, {}))

    def test_import_now_starts_the_trigger(self):
        self.press("Camera card", "Import now", "import_card")
        self.assertEqual(
            self.sandbox.spawned, [["/bin/bash", str(self.sandbox.paths.trigger_script)]]
        )

    def test_eject_card_ejects_the_card(self):
        self.press("Camera card", "Eject", "eject_card")
        self.assertIn(
            ["/usr/sbin/diskutil", "eject", str(self.sandbox.volumes / CARD)], self.sandbox.ran
        )

    def test_back_up_now_starts_the_backup(self):
        self.press("USB backup", "Back up now", "usb_backup")
        self.assertEqual(
            self.sandbox.spawned, [["/fake/python3", str(self.sandbox.paths.backup_launcher)]]
        )

    def test_eject_usb_ejects_the_drive(self):
        self.press("USB backup", "Eject", "eject_usb")
        self.assertIn(["/usr/sbin/diskutil", "eject", str(self.sandbox.usb)], self.sandbox.ran)

    def test_sync_now_starts_the_upload(self):
        self.press("Google Photos", "Sync now", "google_sync")
        self.assertEqual(
            self.sandbox.spawned,
            [["/fake/python3", str(self.sandbox.paths.google_script), "--sync"]],
        )

    def buttons(self) -> List[str]:
        return self.page.locator(".step .action").all_text_contents()

    def test_buttons_only_show_when_they_apply(self):
        self.show("already_imported")
        self.assertEqual(
            self.buttons(), ["Import now", "Eject", "Back up now", "Eject", "Sync now"]
        )
        self.show("copying")
        self.assertEqual(self.buttons(), [])
        self.show("idle")
        self.assertEqual(self.buttons(), [])

    def test_a_card_not_handled_yet_can_only_be_ejected(self):
        self.show("card_mounted")
        self.assertEqual(self.buttons(), ["Eject"])


class TestCardTextIsNeverMarkup(PageTestCase):
    """Volume and file names come from the card, so they can say anything."""

    def assert_inert(self) -> None:
        self.page.wait_for_timeout(100)  # a blocked image would run onerror about now
        self.assertFalse(self.page.evaluate("'__pwned' in window"), "script from the card ran")
        self.assertEqual(self.page.evaluate("document.querySelectorAll('img').length"), 0)

    def test_a_volume_name(self):
        sandbox = self.load("idle")
        sandbox.add_card(HOSTILE)
        self.open_page()
        self.assert_inert()
        expect(self.page.locator("#hero h2")).to_have_text(f"{HOSTILE} is mounted")
        expect(self.page.locator(".step h3").first).to_have_text(HOSTILE)

    def test_a_file_name_during_a_copy(self):
        sandbox = self.load("idle")
        sandbox.transfer(
            state="copying",
            stage="copy",
            card=HOSTILE,
            current_file=HOSTILE,
            bytes_done=1,
            bytes_total=2,
        )
        self.open_page()
        self.assert_inert()
        expect(self.page.locator("#hero p")).to_contain_text(HOSTILE)
        expect(self.page.locator(".step", has_text="Mac archive").locator(".detail")).to_have_text(
            HOSTILE
        )

    def test_the_history_and_a_failure_message(self):
        sandbox = self.load("idle")
        sandbox.record(source=HOSTILE, outcome="failed", message=HOSTILE)
        self.open_page()
        self.assert_inert()
        expect(self.page.locator("#hero h2")).to_contain_text(HOSTILE)
        expect(self.page.locator("#hero p")).to_have_text(HOSTILE)
        expect(self.page.locator("#history .t")).to_contain_text(HOSTILE)
        expect(self.page.locator("#history .s")).to_contain_text(HOSTILE)


class TestReadmeScreenshot(PageTestCase):
    """The README's picture has to read like a real import, so its numbers must agree."""

    def test_the_numbers_on_the_panel_agree_with_each_other(self):
        self.show("readme_import")
        status = json.loads(self.sandbox.paths.status_file.read_text())
        history = self.sandbox.core.state()["activity"]["speed_history"]
        copied, expected = status["bytes_transferred"], status["expected_new_bytes"]
        self.assertAlmostEqual(
            sum(history) / copied, 1, places=2, msg="the graph adds up to the copies"
        )
        self.assertAlmostEqual(copied / status["new_files"] / MB, 25, delta=0.5)
        self.assertAlmostEqual(expected / status["expected_new_files"] / MB, 25, delta=0.5)
        self.assertEqual(
            self.slots(),
            {
                "Progress": "58% · 18.0 of 31.2 GB",
                "Speed": "155.3 MB/s",
                "Files": "737 of 1,278 new",
                "Elapsed": "2m 06s",
                "Remaining": "1m 27s",
            },
        )
        self.assertIn("1,278 new files of 2,478", self.page.locator("#pipeline").inner_text())

    def test_its_card_could_follow_the_imports_listed_below_it(self):
        self.show("readme_import")
        status = json.loads(self.sandbox.paths.status_file.read_text())
        rows = catalog.list_import_history(limit=3)  # newest first
        for newer, older in zip(rows, rows[1:]):
            self.assertEqual(newer["files"], older["files"] + newer["copied"])
        self.assertEqual(status["files_total"] - rows[0]["files"], status["expected_new_files"])

    def test_it_shows_a_believable_install(self):
        self.show("readme_import")
        self.assertGreaterEqual(self.page.locator("#history > *").count(), 3)
        self.assertNotIn("Nothing uploaded yet", self.page.locator("#pipeline").inner_text())


class TestReports(PageTestCase):
    """A finished run's report, in the panel and from Recent activity, in either style."""

    def cards(self):
        return self.page.evaluate("""() => [...document.querySelectorAll('.place')]
            .map(c => [...c.children].map(e => e.textContent))""")

    def table(self, within="#hero"):
        return self.page.evaluate(
            """within => [...document.querySelectorAll(within + ' .report-table tr')]
                .map(r => [...r.children].map(c => c.textContent))""",
            within,
        )

    def choose(self, style):
        self.page.click("#menu-button")
        self.click_action(f"[data-report-choice={style}]", "set_report_style")
        self.page.click("#menu-button")

    def test_basic_is_the_default(self):
        self.show("finished")
        self.assertEqual(
            self.cards(),
            [
                ["Mac archive", "18 files · 412.0 MB", "1m 12s at 5.7 MB/s"],
                ["USB backup", "18 files · 412.0 MB", "28s at 14.7 MB/s"],
                ["Google Photos", "9 files · 130.0 MB", "47s at 2.8 MB/s"],
            ],
        )
        self.assertEqual(self.table(), [])

    def test_basic_shows_where_the_time_went(self):
        self.show("finished")
        widths = self.page.evaluate(
            "[...document.querySelectorAll('.timebar > div')].map(s => parseFloat(s.style.width))"
        )
        self.assertEqual([round(w) for w in widths], [49, 19, 32])
        legend = self.page.locator(".legend").inner_text()
        for part in ("Copy 1m 12s", "USB backup 28s", "Upload 47s", "Total 2m 27s"):
            self.assertIn(part, legend)

    def test_advanced_is_a_table_of_the_steps(self):
        self.show("finished")
        self.choose("advanced")
        expect(self.page.locator("#hero .report-table")).to_be_visible()
        self.assertEqual(self.cards(), [])
        self.assertEqual(
            self.table(),
            [
                ["Step", "Result", "Size", "Time", "Speed"],
                [
                    "Scan and read photo info",
                    "2,378 on card · 2,360 already saved",
                    "80.0 GB",
                    "<1s",
                    "—",
                ],
                ["Copy to Mac", "18 new: 9 photos, 9 RAW", "412.0 MB", "1m 12s", "5.7 MB/s"],
                ["Back up to USB", "18 files copied", "412.0 MB", "28s", "14.7 MB/s"],
                [
                    "Upload to Google Photos",
                    "9 files to Photo Offloader",
                    "130.0 MB",
                    "47s",
                    "2.8 MB/s",
                ],
                ["Total", "Start to finish", "", "2m 27s", ""],
            ],
        )

    def test_the_choice_is_kept_after_reopening_the_page(self):
        self.show("finished")
        self.choose("advanced")
        self.assertEqual(self.sandbox.core.report_style(), "advanced")
        self.open_page()
        expect(self.page.locator("#hero .report-table")).to_be_visible()
        expect(self.page.locator("[data-report-choice=advanced]")).to_have_attribute(
            "aria-checked", "true"
        )

    def test_a_failed_step_stands_out_and_says_so(self):
        self.show("failed_on_usb")
        failed = self.page.locator("#hero .place[data-state=error]")
        expect(failed).to_have_count(1)
        expect(failed.locator(".main")).to_have_text("Stopped")

    def test_a_step_under_half_a_second_does_not_read_as_no_time_at_all(self):
        sandbox = self.load("idle")
        sandbox.record(details={"copy_seconds": 0.2, "new_bytes": 5})
        self.open_page()
        self.assertIn("<1s", self.page.locator(".place").first.inner_text())

    def test_an_older_run_shows_dashes_for_what_was_not_saved(self):
        sandbox = self.load("idle")
        sandbox.record(usb=False, details={"new": {"JPG": 1}, "new_bytes": 5})
        self.open_page()
        self.assertEqual(self.cards()[0], ["Mac archive", "1 file · 5 B", "—"])
        self.assertEqual(self.cards()[1], ["USB backup", "Skipped", "—"])

    def rows(self):
        return self.page.locator("#history .row")

    def test_clicking_a_past_run_expands_its_report_under_it(self):
        self.show("readme_import")
        self.assertEqual(self.page.locator(".run-report").count(), 0)
        self.rows().nth(2).click()
        expect(
            self.page.locator("#history .run").nth(2).locator(".run-report .place")
        ).to_have_count(3)
        self.assertEqual(self.rows().nth(2).get_attribute("aria-expanded"), "true")
        self.assertEqual(self.rows().nth(0).get_attribute("aria-expanded"), "false")
        self.assertEqual(self.page.locator("dialog[open]").count(), 0)

    def row_top(self, index):
        return self.rows().nth(index).bounding_box()["y"]

    def test_an_open_report_pushes_the_runs_below_it_down_until_it_closes(self):
        self.show("readme_import", **NATIVE_WINDOW)
        above, below = self.row_top(0), self.row_top(1)
        self.rows().first.click()
        height = self.page.locator(".run-report").bounding_box()["height"]
        self.assertEqual(self.row_top(0), above)
        self.assertAlmostEqual(self.row_top(1), below + height, delta=1)
        self.rows().first.click()
        expect(self.page.locator(".run-report")).to_have_count(0)
        self.assertEqual(self.row_top(1), below)

    def _hold_report_folds(self):
        """Pause each report fold as it starts, so a test can seek it without racing the clock.

        Counting drawn frames failed on busy CI runners, which drew as few as 4 in 300 ms.
        """
        self.page.evaluate(
            """() => {
                const real = Element.prototype.animate;
                Element.prototype.animate = function (...args) {
                    const animation = real.apply(this, args);
                    if (this.classList.contains('run-report')) {
                        animation.pause();
                        window.heldFold = animation;
                    }
                    return animation;
                };
            }"""
        )

    def _heights_through_held_fold(self):
        """The report's height at points through the fold, then let the fold finish."""
        return self.page.evaluate(
            """() => {
                const fold = window.heldFold, report = fold.effect.target;
                const heights = [0, 45, 90, 135, 179].map(at => {
                    fold.currentTime = at;
                    return report.getBoundingClientRect().height;
                });
                fold.finish();
                return heights;
            }"""
        )

    def test_a_report_unfolds_and_folds_instead_of_jumping(self):
        self.load("readme_import")
        self.open_page(reduced_motion="no-preference")
        self._hold_report_folds()
        self.rows().first.click()
        opening = self._heights_through_held_fold()
        self.assertEqual(opening, sorted(opening), opening)
        self.assertGreaterEqual(len(set(opening)), 3, opening)  # it stops at its padding early
        self.rows().first.click()
        closing = self._heights_through_held_fold()
        self.assertEqual(closing, sorted(closing, reverse=True), closing)
        self.assertGreaterEqual(len(set(closing)), 3, closing)
        expect(self.page.locator(".run-report")).to_have_count(0)

    def run_opacities(self):
        return self.page.evaluate(
            "[...document.querySelectorAll('#history .run')].map(r => +getComputedStyle(r).opacity)"
        )

    def test_the_other_runs_dim_while_a_report_is_open(self):
        self.show("readme_import")
        self.page.mouse.move(0, 0)
        self.assertEqual(self.run_opacities(), [1, 1, 1])
        self.rows().nth(1).click()
        self.page.mouse.move(0, 0)
        opacities = self.run_opacities()
        self.assertEqual(opacities[1], 1)
        self.assertLess(opacities[0], 0.7)
        self.assertLess(opacities[2], 0.7)
        self.rows().nth(1).click()
        self.page.mouse.move(0, 0)
        self.assertEqual(self.run_opacities(), [1, 1, 1])

    def test_a_dimmed_run_comes_back_when_it_is_pointed_at_or_focused(self):
        self.show("readme_import")
        self.rows().nth(1).click()
        self.rows().nth(0).hover()
        self.assertEqual(self.run_opacities()[0], 1)
        self.page.mouse.move(0, 0)
        self.rows().nth(2).focus()
        self.assertEqual(self.run_opacities()[2], 1)

    def test_clearing_the_list_forgets_the_open_report(self):
        self.show("readme_import")
        self.rows().first.click()
        expect(self.page.locator("#history.has-open")).to_have_count(1)
        self.page.click("#clear-history")
        self.click_action("#clear-history", "clear_history")
        expect(self.page.locator("#history.has-open")).to_have_count(0)

    def test_a_mouse_click_leaves_no_focus_ring_on_the_row(self):
        self.show("readme_import")
        self.rows().first.click()
        self.assertFalse(self.page.evaluate("document.activeElement.matches('.row')"))

    def test_a_focus_ring_stays_inside_the_row(self):
        self.show("readme_import")
        self.page.keyboard.press("Tab")
        self.rows().first.focus()
        self.page.keyboard.press("Enter")
        expect(self.rows().first).to_be_focused()
        self.assertLess(
            self.page.evaluate(
                "parseFloat(getComputedStyle(document.activeElement).outlineOffset)"
            ),
            0,
        )

    def test_clicking_it_again_closes_it(self):
        self.show("readme_import")
        self.rows().first.click()
        self.rows().first.click()
        expect(self.page.locator(".run-report")).to_have_count(0)

    def test_only_one_report_is_open_at_a_time(self):
        self.show("readme_import")
        self.rows().nth(0).click()
        self.rows().nth(1).click()
        expect(self.page.locator(".run-report")).to_have_count(1)
        expect(self.page.locator("#history .run").nth(1).locator(".run-report")).to_have_count(1)

    def test_an_open_report_stays_open_when_the_page_refreshes(self):
        self.show("readme_import")
        self.rows().first.focus()
        self.page.keyboard.press("Enter")
        self.sandbox.advance(120)  # the list is drawn again as its "ago" times change
        self.refresh()
        expect(self.page.locator(".run-report")).to_have_count(1)
        expect(self.rows().first).to_be_focused()

    def test_an_open_report_uses_the_chosen_style_and_follows_a_change(self):
        self.show("readme_import")
        self.rows().first.click()
        expect(self.page.locator(".run-report .place")).to_have_count(3)
        self.choose("advanced")
        expect(self.page.locator(".run-report .report-table")).to_be_visible()
        self.assertGreater(len(self.table(".run-report")), 3)

    def test_opening_a_report_leaves_the_rest_of_the_page_alone(self):
        self.show("readme_import", **NATIVE_WINDOW)
        where = """() => ['#hero', '#pipeline', '#history'].map(s => {
            const r = document.querySelector(s).getBoundingClientRect(); return [r.top, r.height]; })"""
        before, text = self.page.evaluate(where), self.page.locator("#hero").inner_text()
        self.rows().first.click()
        expect(self.page.locator(".run-report")).to_have_count(1)
        self.assertEqual(self.page.evaluate(where), before)
        self.assertEqual(self.page.locator("#hero").inner_text(), text)

    def test_a_report_low_in_a_long_list_scrolls_into_view(self):
        sandbox = self.load("idle")
        for _ in range(12):
            sandbox.record()
        self.open_page(**NATIVE_WINDOW)
        self.rows().last.click()
        inside = """() => { const list = document.querySelector('#history').getBoundingClientRect(),
            report = document.querySelector('.run-report').getBoundingClientRect();
            return report.bottom <= list.bottom + 1 && report.top >= list.top - 1; }"""
        self.assertTrue(self.page.evaluate(inside))

    def test_a_past_run_can_be_opened_from_the_keyboard(self):
        self.show("readme_import")
        self.rows().first.focus()
        self.page.keyboard.press("Enter")
        expect(self.page.locator(".run-report")).to_have_count(1)
        self.page.keyboard.press("Enter")
        expect(self.page.locator(".run-report")).to_have_count(0)

    def test_a_report_treats_card_text_as_text(self):
        sandbox = self.load("idle")
        sandbox.record(details={"upload": {"outcome": "complete", "files": 0, "album": HOSTILE}})
        self.open_page()
        self.rows().first.click()
        expect(self.page.locator(".run-report")).to_contain_text(HOSTILE)
        self.assertIsNone(self.page.evaluate("window.__pwned || null"))

    def test_both_styles_fit_the_panel_at_both_widths(self):
        overflow = """() => { const hero = document.querySelector('#hero').getBoundingClientRect();
            return [...document.querySelectorAll('#hero *')].filter(e => {
                const r = e.getBoundingClientRect();
                return r.bottom > hero.bottom + 0.5 || r.right > hero.right + 0.5; }).length; }"""
        for style in ("basic", "advanced"):
            for window in (NATIVE_WINDOW, NARROW):
                for state in SUMMARY_STATES:
                    with self.subTest(style=style, state=state, width=window["width"]):
                        sandbox = self.load(state)
                        sandbox.core.act("set_report_style", {"report_style": style})
                        self.open_page(**window)
                        self.assertEqual(self.page.evaluate(overflow), 0)


class TestSpeedGraph(PageTestCase):
    """The graph fills its width for a run of any length, and shows the trend, not each poll."""

    def setUp(self):
        super().setUp()
        self.show("idle")

    def points(self, values, progress=1):
        """The (x, y) pairs of the drawn line, control points included."""
        line = self.page.evaluate("([v, p]) => sparkPaths(v, 600, 44, p).line", [values, progress])
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", line)]
        return list(zip(numbers[::2], numbers[1::2]))

    def on_the_line(self, values, progress=1):
        """Only the points the line passes through: each curve segment has two control points first."""
        return self.points(values, progress)[::3]

    def height(self, values):
        """How tall the line is once it has settled, leaving out its first few seconds."""
        heights = [y for _, y in self.points(values)[15:]]
        return max(heights) - min(heights)

    def test_a_short_run_starts_at_the_left_edge_and_grows_to_the_right(self):
        points = self.points([100] * 4)
        self.assertEqual(points[0][0], 0)
        self.assertLess(points[-1][0], 300)

    def test_ten_seconds_is_enough_to_fill_the_width(self):
        self.assertLess(self.points([100] * 9)[-1][0], 600)
        self.assertEqual(self.points([100] * 10)[-1][0], 600)

    def test_a_longer_run_fills_the_whole_width(self):
        for samples in (10, 30, 60, 90, 300):
            with self.subTest(samples=samples):
                points = self.points([100] * samples)
                self.assertEqual((points[0][0], points[-1][0]), (0, 600))

    def test_a_very_long_run_shows_only_the_latest_ninety_seconds(self):
        points = self.points([10**9] * 100 + [100] * 300)
        self.assertEqual(
            {y for _, y in points}, {points[0][1]}, "the old spike still sets the scale"
        )
        self.assertEqual((points[0][0], points[-1][0]), (0, 600))

    def test_a_jagged_speed_is_drawn_as_a_calm_line(self):
        jagged = [100, 200] * 20
        self.assertLess(self.height(jagged), 19 / 2)  # 19 is the height of the raw zigzag

    def test_a_real_slowdown_still_shows(self):
        self.assertGreater(self.height([200] * 20 + [20] * 20), 25)

    def test_a_long_run_is_smoothed_as_much_for_its_width_as_a_short_one(self):
        # Eight-second swings: a fixed five-second average would leave most of this in a full graph.
        swings = ([100] * 4 + [200] * 4) * 12
        self.assertLess(self.height(swings), 19 / 2)

    def test_a_half_minute_slowdown_still_shows_on_a_full_graph(self):
        self.assertGreater(self.height([200] * 30 + [20] * 30 + [200] * 30), 25)

    def test_the_curve_stays_inside_the_graph(self):
        for _, y in self.points([0, 500, 0, 900, 10, 700, 0] * 5):
            self.assertTrue(0 <= y <= 44, y)

    def test_a_full_graph_holds_ninety_seconds(self):
        self.assertEqual(len(self.on_the_line([100] * 300)), 89)  # 88 one-second gaps

    def test_a_new_speed_cannot_reshape_the_line_already_drawn(self):
        speeds = [100 + (i * 37) % 90 for i in range(60)]
        before = self.page.evaluate("v => smoothedSpeeds(v)", speeds)
        after = self.page.evaluate("v => smoothedSpeeds(v)", speeds + [900])
        self.assertEqual(after[:-1], before)

    def test_the_tip_glides_from_the_last_speed_to_the_newest(self):
        speeds = [100] * 5 + [200]
        start, middle, end = (self.on_the_line(speeds, p)[-1] for p in (0, 0.5, 1))
        self.assertLess(start[0], middle[0])
        self.assertLess(middle[0], end[0])
        self.assertGreater(start[1], middle[1], "a higher speed is further up the graph")
        self.assertGreater(middle[1], end[1])

    def test_a_glide_ends_where_the_next_one_begins(self):
        speeds = [100 + (i * 37) % 90 for i in range(40)]
        for label, values in (("stretching", speeds), ("scrolling", speeds * 6)):
            with self.subTest(label):
                ended = self.on_the_line(values, 1)
                # 900 would rescale the graph, so the next speed stays inside the old peak.
                begun = self.on_the_line(values + [120], 0)
                for was, now in zip(ended[-20:], begun[-20:]):
                    self.assertAlmostEqual(was[0], now[0], delta=2.5)
                    self.assertAlmostEqual(was[1], now[1], delta=0.5)

    def test_while_scrolling_the_line_always_fills_the_width(self):
        values = [100 + (i * 37) % 90 for i in range(240)]
        for progress in (0, 0.25, 0.5, 1):
            with self.subTest(progress=progress):
                points = self.points(values, progress)
                self.assertEqual((points[0][0], points[-1][0]), (0, 600))

    def test_a_graph_with_nothing_to_draw_draws_nothing(self):
        self.assertIsNone(self.page.evaluate("sparkPaths([], 600, 44)"))
        self.assertIsNone(self.page.evaluate("sparkPaths([100], 600, 44, 0)"))

    def test_the_line_rises_from_the_bottom_at_the_left_edge(self):
        points = self.points([100])
        self.assertEqual(points[0], (0, 42), "zero speed")
        self.assertEqual(points[-1][1], 4, "the first speed is the top of the scale")

    def _tip_heights_while_gliding(self, script):
        """Run ``script`` on the graph, then how far up the tip is (0 to 1) on each frame, with
        the fraction of the glide done by that frame."""
        return self.page.evaluate(
            """script => new Promise(done => {
                const line = speedGraph.svg.querySelector('path.line'), seen = [];
                (0, eval)(script);
                const began = performance.now();
                const look = () => {
                    const at = (performance.now() - began) / POLL_MS;
                    const y = Number((line.getAttribute('d') || '').trim().split(' ').pop());
                    if (line.getAttribute('d')) seen.push([(42 - y) / 38, at]);
                    if (at < 1.2) requestAnimationFrame(look); else done(seen);
                };
                requestAnimationFrame(look);
            })""",
            script,
        )

    def assert_rises_with_the_glide(self, script):
        self.open_page(reduced_motion="no-preference")
        frames = self._tip_heights_while_gliding(script)
        self.assertGreater(len(frames), 5)
        for height, done in frames:
            self.assertLess(height, done + 0.25, f"jumped ahead of the glide: {frames[:6]}")
        self.assertAlmostEqual(frames[-1][0], 1, places=1)

    def test_the_first_speed_rises_smoothly_instead_of_jumping(self):
        self.assert_rises_with_the_glide("speedGraph.update([]); speedGraph.update([150000000])")

    def test_speed_after_nothing_but_zeros_rises_smoothly_too(self):
        self.assert_rises_with_the_glide(
            "speedGraph.update([0, 0, 0]); speedGraph.update([0, 0, 0, 150000000])"
        )

    def _drawn_after(self, *histories):
        """The heights (0 to 1) of the points on the line once each history has been shown."""
        self.open_page()  # reduced motion: each update draws at once
        line = self.page.evaluate(
            """histories => {
                histories.forEach(h => speedGraph.update(h));
                return speedGraph.svg.querySelector('path.line').getAttribute('d');
            }""",
            list(histories),
        )
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", line)]
        return [(42 - y) / 38 for y in numbers[1::2][::3]]

    def test_a_long_stall_does_not_blow_up_the_tail_of_the_speed_before_it(self):
        fast = 150 * MB
        before = [fast] * 60
        # The server keeps 130 samples, so the fast part has scrolled off and the tail is all
        # that is left to scale by.
        during = [fast] * 20 + [0] * 110
        heights = self._drawn_after(before, during)
        self.assertLess(max(heights), 0.5, heights[:5])

    def test_speed_after_a_long_stall_is_back_at_the_same_height(self):
        fast = 150 * MB
        heights = self._drawn_after([fast] * 60, [fast] * 20 + [0] * 110, [0] * 90 + [fast] * 40)
        self.assertGreater(heights[-1], 0.9)

    def test_a_new_graph_does_not_keep_the_old_runs_scale(self):
        heights = self._drawn_after([500 * MB] * 30, [], [10 * MB] * 5)
        self.assertGreater(heights[-1], 0.9)

    def _a_new_speed_arrives(self):
        self.sandbox.advance(1)
        status = json.loads(self.sandbox.paths.status_file.read_text())
        status["bytes_transferred"] += 150 * MB
        self.sandbox.paths.status_file.write_text(json.dumps(status))

    def _animation_frames_asked_for_over(self, milliseconds):
        return self.page.evaluate(
            """ms => new Promise(done => {
                const real = window.requestAnimationFrame;
                let asked = 0;
                window.requestAnimationFrame = (f) => { asked++; return real.call(window, f); };
                setTimeout(() => { window.requestAnimationFrame = real; done(asked); }, ms);
            })""",
            milliseconds,
        )

    def _changes_over(self, milliseconds):
        return self.page.evaluate(
            """ms => new Promise(done => {
                const line = document.querySelector('.spark path.line');
                let last = line.getAttribute('d'), changes = 0;
                const end = performance.now() + ms;
                const look = () => {
                    const now = line.getAttribute('d');
                    if (now !== last) { changes++; last = now; }
                    if (performance.now() < end) requestAnimationFrame(look); else done(changes);
                };
                requestAnimationFrame(look);
            })""",
            milliseconds,
        )

    def test_the_line_glides_when_a_new_speed_arrives_then_stops(self):
        self.load("readme_import")
        self.open_page(reduced_motion="no-preference")
        self.page.wait_for_timeout(1200)  # the glide from opening the page has finished
        self.assertEqual(self._changes_over(300), 0, "nothing new, so nothing should move")
        self._a_new_speed_arrives()
        self.refresh()
        self.assertGreater(self._changes_over(500), 5, "it should move on many frames, not once")
        self.page.wait_for_timeout(800)
        self.assertEqual(self._changes_over(300), 0, "it should stop once it has caught up")
        self.page.wait_for_timeout(50)
        self.assertEqual(self._animation_frames_asked_for_over(300), 0, "idle should cost nothing")

    def test_a_refresh_with_nothing_new_does_not_start_a_glide(self):
        self.load("readme_import")
        self.open_page(reduced_motion="no-preference")
        self.page.wait_for_timeout(1200)
        before = self.page.evaluate("document.querySelector('.spark path.line').getAttribute('d')")
        history = self.sandbox.core.state()["activity"]["speed_history"]
        asked = self.page.evaluate(
            """history => new Promise(done => {
                const real = window.requestAnimationFrame;
                let asked = 0;
                window.requestAnimationFrame = (f) => { asked++; return real.call(window, f); };
                speedGraph.update(history);
                setTimeout(() => { window.requestAnimationFrame = real; done(asked); }, 100);
            })""",
            history,
        )
        self.assertEqual(asked, 0)
        self.assertEqual(
            self.page.evaluate("document.querySelector('.spark path.line').getAttribute('d')"),
            before,
        )

    def test_reduce_motion_draws_a_new_speed_without_gliding(self):
        self.show("readme_import")  # the harness asks for reduced motion by default
        self._a_new_speed_arrives()
        self.page.evaluate(
            "window.__before = document.querySelector('.spark path.line').getAttribute('d')"
        )
        self.refresh()
        self.page.wait_for_function(
            "document.querySelector('.spark path.line').getAttribute('d') !== window.__before"
        )
        self.assertEqual(self._changes_over(300), 0)

    def test_a_refresh_keeps_the_same_graph_on_the_page(self):
        self.show("readme_import")
        self.page.evaluate("document.querySelector('.spark').dataset.mark = 'kept'")
        self._a_new_speed_arrives()
        self.refresh()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.get_attribute(".spark", "data-mark"), "kept")

    def test_the_area_closes_under_the_line_it_fills(self):
        area = self.page.evaluate("sparkPaths([100, 100, 100], 600, 44).area")
        self.assertTrue(area.endswith(" L0 44 Z"), area)
        self.assertNotIn("L600 44", area)


@unittest.skipUnless(
    os.environ.get("PHOTO_OFFLOADER_TEST_SCREENSHOTS"),
    "set PHOTO_OFFLOADER_TEST_SCREENSHOTS to a folder to save screenshots",
)
class TestScreenshots(PageTestCase):
    def test_save_every_state(self):
        folder = Path(os.environ["PHOTO_OFFLOADER_TEST_SCREENSHOTS"])
        folder.mkdir(parents=True, exist_ok=True)
        self.show("readme_import", color_scheme="dark", **NATIVE_WINDOW)
        self.page.locator("#history .row").first.click()
        self.page.screenshot(path=str(folder / "report-from-history-dark.png"))
        self.page.click("#menu-button")
        self.page.screenshot(path=str(folder / "menu-dark.png"))
        for state in SCENARIOS:
            for scheme in ("light", "dark"):
                self.show(state, color_scheme=scheme, **NATIVE_WINDOW)
                self.page.screenshot(path=str(folder / f"{state}-{scheme}.png"))
            self.show(state, **NARROW)
            self.page.screenshot(path=str(folder / f"{state}-narrow.png"))


if __name__ == "__main__":
    unittest.main()
