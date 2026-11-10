#!/usr/bin/env python3
"""What the dashboard does about a check of the saved copies."""

import json
import unittest
from unittest.mock import patch

from photo_offloader import catalog, config, verify_report
from tests.test_dashboard_core import Fixture


class Check(Fixture):
    def write_progress(self, **fields):
        values = {
            "where": "USB drive",
            "usb": True,
            "files_done": 3490,
            "files_total": 6300,
            "bytes_done": 76.4e9,
            "bytes_total": 138.8e9,
            "seconds_left": 900,
        }
        config.VERIFY_STATUS_PATH.write_text(json.dumps({**values, **fields}))

    def checking(self, **fields):
        self.verify_up = True
        self.write_progress(**fields)

    def record_check(self, **fields):
        row = {
            "kind": "verify",
            "outcome": "complete",
            "started_at": self.now - 1090,
            "finished_at": self.now - 10,
            "files": 6300,
            "usb": True,
            "details": {"whole": True},
        }
        catalog.record_import_run(**{**row, **fields})

    def failed_check(self, rows):
        name = verify_report.save_report(rows, self.now)
        self.record_check(
            outcome="failed",
            failed=len(rows),
            message=verify_report.message_for(rows),
            details={"whole": True, "check": verify_report.details_for(rows, name)},
        )
        return catalog.list_import_history(limit=1)[0]["id"]


def missing_row(relative="2026-10/a/Raws/one.nef"):
    return {
        "where": verify_report.MAC,
        "problem": verify_report.MISSING,
        "other_copy": verify_report.OTHER_GOOD,
        "path": f"/Mac/{relative}",
        "relative_path": relative,
        "bytes": 10,
        "expected_sha256": "aa",
        "actual_sha256": "",
    }


def damaged_row(relative="2026-10/a/Raws/two.nef"):
    return {**missing_row(relative), "problem": verify_report.DAMAGED}


class TestStarting(Check):
    def test_it_starts_the_installed_program(self):
        result = self.core.act("verify")
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.spawned[-1],
            [
                str(self.paths.python),
                str(self.install / config.VERIFY),
            ],
        )

    def test_a_second_start_is_refused(self):
        self.checking()
        self.assertFalse(self.core.act("verify")["ok"])
        self.assertEqual(self.spawned, [])

    def test_it_waits_for_a_running_import(self):
        self.importer_up = True
        self.assertFalse(self.core.act("verify")["ok"])
        self.assertEqual(self.spawned, [])


class TestRunning(Check):
    def test_nothing_shows_when_no_check_is_running(self):
        self.assertIsNone(self.core.state()["verify"])

    def test_a_progress_file_left_behind_shows_nothing(self):
        self.write_progress()
        self.assertIsNone(self.core.state()["verify"])

    def test_a_running_check_says_how_far_it_has_got(self):
        self.checking()
        shown = self.core.state()["verify"]
        self.assertEqual(shown["percent"], 55)
        self.assertEqual(
            shown["text"], "55% · USB drive · 3,490 of 6,300 copies · about 15 min left"
        )

    def test_it_says_something_before_the_first_figures_arrive(self):
        self.verify_up = True
        shown = self.core.state()["verify"]
        self.assertEqual((shown["percent"], shown["text"]), (0, "Getting ready"))

    def test_a_check_is_not_an_import(self):
        """The panel stays free, and the other actions stay available."""
        self.checking()
        state = self.core.state()
        self.assertFalse(state["busy"])
        self.assertIsNone(state["activity"])

    def test_stop_ends_only_the_check(self):
        self.checking()
        self.assertTrue(self.core.act("stop_verify")["ok"])
        self.assertEqual(
            self.ran[-1], ["/usr/bin/pkill", "-TERM", "-f", str(self.install / config.VERIFY)]
        )

    def test_stop_with_nothing_running_says_so(self):
        self.assertFalse(self.core.act("stop_verify")["ok"])


class TestTheDrive(Check):
    def setUp(self):
        super().setUp()
        self.usb.mkdir()
        (self.usb / "CameraImports").mkdir()

    def usb_step(self):
        return next(s for s in self.core.state()["pipeline"] if s["id"] == "usb")

    def test_the_box_says_the_check_is_using_it_and_hides_eject(self):
        self.checking()
        step = self.usb_step()
        self.assertEqual(step["detail"], "In use by the check")
        self.assertNotIn("eject_usb", [action["id"] for action in step["actions"]])

    def test_eject_is_refused_while_the_check_uses_it(self):
        self.checking()
        result = self.core.act("eject_usb")
        self.assertFalse(result["ok"])
        self.assertIn("check", result["message"])

    def test_a_check_of_the_mac_alone_leaves_the_drive_alone(self):
        self.checking(usb=False)
        self.assertIn("eject_usb", [action["id"] for action in self.usb_step()["actions"]])


class TestWarning(Check):
    def setUp(self):
        super().setUp()
        self.drive_id = "drive-1"
        self.core._volume_id = lambda volume: self.drive_id
        sizes = patch.object(catalog, "bytes_under", return_value=60e9)
        sizes.start()
        self.addCleanup(sizes.stop)

    def record_speeds(self, drive_id="drive-1", **speeds):
        self.record_check(details={"whole": True, "drive_id": drive_id, "speeds": speeds})

    def test_it_uses_the_speed_this_drive_read_at_last_time(self):
        self.usb.mkdir()
        self.record_speeds(**{verify_report.MAC: 1e9, verify_report.USB: 100e6})
        warning = self.core.act("verify_warning")
        # 60 s for the Mac and 600 s for the drive.
        self.assertEqual(warning["time"], "About 11 minutes, going by this drive's last check.")

    def test_another_drives_speed_is_only_a_guess_for_this_one(self):
        self.usb.mkdir()
        self.record_speeds(**{verify_report.MAC: 1e9, verify_report.USB: 100e6})
        self.drive_id = "drive-2"
        self.assertIn("hasn't been checked before", self.core.act("verify_warning")["time"])

    def test_the_macs_speed_counts_without_the_drive(self):
        self.record_speeds(**{verify_report.MAC: 2e9})
        self.assertTrue(
            self.core.act("verify_warning")["time"].startswith(
                "About a minute, going by your last check."
            )
        )


class TestMarkingDeleted(Check):
    def test_marking_everything_turns_the_run_into_a_passed_check(self):
        run_id = self.failed_check([missing_row(), missing_row("2026-10/a/Raws/b.nef")])

        result = self.core.act("mark_deleted", {"run_id": run_id})

        self.assertTrue(result["ok"])
        self.assertEqual(
            verify_report.deleted_files(), {"2026-10/a/Raws/one.nef", "2026-10/a/Raws/b.nef"}
        )
        row = catalog.list_import_history(limit=1)[0]
        self.assertEqual(row["outcome"], "complete")
        self.assertIn("2 files deleted from the Mac on purpose", row["message"])
        self.assertIsNone(row["details"]["check"])

    def test_it_deletes_nothing(self):
        run_id = self.failed_check([missing_row()])
        self.core.act("mark_deleted", {"run_id": run_id})
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.ran, [])

    def test_other_problems_keep_the_run_failed(self):
        run_id = self.failed_check([missing_row(), damaged_row()])

        self.core.act("mark_deleted", {"run_id": run_id})

        row = catalog.list_import_history(limit=1)[0]
        self.assertEqual(row["outcome"], "failed")
        self.assertIn("1 problem: 1 damaged", row["message"])
        self.assertIsNone(row["details"]["check"]["deleted"])
        self.assertEqual(len(row["details"]["check"]["others"]), 1)

    def test_a_check_that_did_not_finish_never_becomes_a_pass(self):
        name = verify_report.save_report([missing_row()], self.now)
        self.record_check(
            outcome="failed",
            details={"check": verify_report.details_for([missing_row()], name)},
        )
        run_id = catalog.list_import_history(limit=1)[0]["id"]

        self.core.act("mark_deleted", {"run_id": run_id})

        self.assertEqual(catalog.list_import_history(limit=1)[0]["outcome"], "failed")

    def test_a_report_that_is_gone_says_so(self):
        run_id = self.failed_check([missing_row()])
        for path in config.VERIFY_REPORTS_DIR.iterdir():
            path.unlink()
        self.assertFalse(self.core.act("mark_deleted", {"run_id": run_id})["ok"])

    def test_an_unknown_run_is_refused(self):
        self.assertFalse(self.core.act("mark_deleted", {"run_id": 999})["ok"])

    def test_open_full_list_opens_that_runs_spreadsheet(self):
        run_id = self.failed_check([missing_row()])
        self.assertTrue(self.core.act("open_verify_report", {"run_id": run_id})["ok"])
        self.assertEqual(self.spawned[-1][0], "/usr/bin/open")
        self.assertTrue(self.spawned[-1][1].endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
