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
SUMMARY_STATES = ("finished", "failed", "failed_on_usb")
RESTING_STATES = ("idle", "card_mounted", "already_imported", "paused")
SLOT_NAMES = ["Progress", "Speed", "Files", "Elapsed", "Remaining"]
SUMMARY_SLOT_NAMES = ["Progress", "Average speed", "Files", "Took", "Remaining"]
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
    def test_all_five_slots_are_there_while_anything_runs(self):
        for state in TRANSFER_STATES:
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(list(self.slots()), SLOT_NAMES)

    def test_a_summary_has_five_slots_too(self):
        for state in SUMMARY_STATES:
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(list(self.slots()), SUMMARY_SLOT_NAMES)

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
                "Files": "1,214 of 2,378 · 47 new",
                "Elapsed": "1m 59s",
                "Remaining": "4m 00s",
            },
        )
        self.assertEqual(self.page.locator(".slot .v.none").count(), 0)

    def test_the_finished_summary_totals_the_run(self):
        self.show("finished")
        self.assertEqual(
            self.slots(),
            {
                "Progress": "100% · 412.0 MB",
                "Average speed": "4.1 MB/s",
                "Files": "18 new · 2,378 on card",
                "Took": "1m 40s",
                "Remaining": "Done",
            },
        )

    def test_a_run_that_did_not_finish_is_not_called_done(self):
        self.show("failed")
        self.assertEqual(self.slots()["Remaining"], "—")
        sandbox = self.load("idle")
        sandbox.record(outcome="canceled")
        self.open_page()
        self.assertEqual(self.slots()["Remaining"], "—")

    def test_a_resting_panel_has_no_slots_or_steps(self):
        for state in RESTING_STATES:
            with self.subTest(state=state):
                self.show(state)
                self.assertEqual(self.slots(), {})
                self.assertEqual(self.steps(), [])


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
        "finished": ["done", "done", "done", "done", "done"],
        "failed": ["done", "done", "error", "skipped", "skipped"],
        "failed_on_usb": ["done", "done", "done", "error", "skipped"],
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

    def test_each_step_state_looks_different(self):
        look = """() => Object.fromEntries([...document.querySelectorAll('.stages span')].map(s => {
            const c = getComputedStyle(s);
            return [s.dataset.state, [c.borderTopColor, c.textDecorationLine].join(' ')]; }))"""
        self.show("failed")
        failed = self.page.evaluate(look)
        self.show("scan")
        running = self.page.evaluate(look)
        looks = {**failed, **running}
        self.assertEqual(sorted(looks), ["active", "done", "error", "pending", "skipped"])
        self.assertIn("line-through", looks["skipped"])
        self.assertEqual(len({looks[s] for s in ("active", "done", "error", "pending")}), 4, looks)


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
        done, total = status["bytes_done"], status["bytes_total"]
        history = self.sandbox.core.state()["activity"]["speed_history"]
        self.assertEqual(status["bytes_transferred"], done)
        self.assertAlmostEqual(
            sum(history) / done, 1, places=2, msg="the graph adds up to the bytes"
        )
        self.assertEqual(
            status["new_files"], status["files_done"], "a fresh card: every file is new"
        )
        self.assertAlmostEqual(done / status["files_done"] / MB, 25, delta=0.5)
        copy_seconds = len(history)
        self.assertAlmostEqual(
            status["eta_seconds"], (total - done) / (done / copy_seconds), delta=1
        )
        self.assertGreaterEqual(status["elapsed_seconds"], copy_seconds)
        self.assertLess(status["elapsed_seconds"], copy_seconds + 15, "plus a short scan")

    def test_it_shows_a_believable_install(self):
        self.show("readme_import")
        self.assertGreaterEqual(self.page.locator("#history > *").count(), 3)
        self.assertNotIn("Nothing uploaded yet", self.page.locator("#pipeline").inner_text())


class TestSpeedGraph(PageTestCase):
    """The graph fills its width for a run of any length, and shows the trend, not each poll."""

    def setUp(self):
        super().setUp()
        self.show("idle")

    def points(self, values):
        """The (x, y) pairs of the drawn line, control points included."""
        line = self.page.evaluate("v => sparkPaths(v, 600, 44).line", values)
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", line)]
        return list(zip(numbers[::2], numbers[1::2]))

    def height(self, values):
        heights = [y for _, y in self.points(values)]
        return max(heights) - min(heights)

    def test_a_short_run_starts_at_the_left_edge_and_grows_to_the_right(self):
        points = self.points([100] * 5)
        self.assertEqual(points[0][0], 0)
        self.assertLess(points[-1][0], 300)

    def test_ten_seconds_is_enough_to_fill_the_width(self):
        self.assertLess(self.points([100] * 9)[-1][0], 600)
        self.assertEqual(self.points([100] * 10)[-1][0], 600)

    def test_a_longer_run_fills_the_whole_width(self):
        for samples in (10, 30, 120, 180):
            with self.subTest(samples=samples):
                points = self.points([100] * samples)
                self.assertEqual((points[0][0], points[-1][0]), (0, 600))

    def test_a_very_long_run_shows_only_the_latest_three_minutes(self):
        points = self.points([10**9] * 100 + [100] * 180)
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
        swings = ([100] * 4 + [200] * 4) * 23
        self.assertLess(self.height(swings), 19 / 2)

    def test_a_half_minute_slowdown_still_shows_on_a_full_graph(self):
        self.assertGreater(self.height([200] * 75 + [20] * 30 + [200] * 75), 25)

    def test_the_curve_stays_inside_the_graph(self):
        for _, y in self.points([0, 500, 0, 900, 10, 700, 0] * 5):
            self.assertTrue(0 <= y <= 44, y)

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
        for state in SCENARIOS:
            for scheme in ("light", "dark"):
                self.show(state, color_scheme=scheme, **NATIVE_WINDOW)
                self.page.screenshot(path=str(folder / f"{state}-{scheme}.png"))
            self.show(state, **NARROW)
            self.page.screenshot(path=str(folder / f"{state}-narrow.png"))


if __name__ == "__main__":
    unittest.main()
