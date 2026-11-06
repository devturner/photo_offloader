"""What a USB backup says went wrong: per file, in its report, whether alone or after an import."""

from __future__ import annotations

import csv
import errno
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config, run_report, transfer, volumes
from photo_offloader import importer as app
from photo_offloader.dashboard import display
from photo_offloader.run_report import FileOutcome
from tests.support import CardFixtureTestCase


class UsbFixture(CardFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.archive = self.archive_photos(3)
        for name, value in (("usb_is_mounted", True), ("get_volume_identity", None)):
            patcher = patch.object(volumes, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def on_usb(self, source):
        return config.USB_DEST_ROOT / source.relative_to(config.LOCAL_DEST_ROOT)

    def conflict(self, name="refresh1.jpg"):
        """A different file with this name already on the drive."""
        destination = self.on_usb(self.archive / name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"something else")
        return destination

    def back_up(self):
        return transfer.copy_local_archive_to_usb()

    def result_of(self, stats, name):
        return next(item for item in stats.outcomes if item.source.name == name)


class TestEachFileIsRecorded(UsbFixture):
    def test_a_copied_file_says_where_it_went(self):
        stats = self.back_up()
        item = self.result_of(stats, "refresh0.jpg")
        self.assertEqual((item.category, item.result), ("JPG", "copied"))
        self.assertEqual(item.archive_path, str(self.on_usb(self.archive / "refresh0.jpg")))

    def test_a_file_already_there_says_so(self):
        self.back_up()
        stats = self.back_up()
        self.assertEqual(self.result_of(stats, "refresh0.jpg").result, "skipped")

    def test_a_different_file_on_the_drive_says_it_was_not_overwritten(self):
        self.conflict()
        item = self.result_of(self.back_up(), "refresh1.jpg")
        self.assertEqual(item.result, run_report.FAILED)
        self.assertIn("wasn't overwritten", item.reason)

    def fail_copies(self, error):
        return patch.object(transfer, "_copy_one_to_usb", side_effect=error)

    def test_a_full_drive_is_named(self):
        with self.fail_copies(OSError(errno.ENOSPC, "No space left")):
            item = self.result_of(self.back_up(), "refresh0.jpg")
        self.assertEqual(item.reason, "The USB drive ran out of space")

    def test_a_write_failure_blames_the_drive(self):
        with self.fail_copies(OSError(errno.EIO, "Input/output error", "/usb/x.jpg")):
            item = self.result_of(self.back_up(), "refresh0.jpg")
        self.assertEqual(item.reason, "Couldn't write to the drive")

    def test_a_failure_reading_the_mac_copy_is_not_blamed_on_the_drive(self):
        source = self.archive / "refresh0.jpg"
        with self.fail_copies(OSError(errno.EIO, "Input/output error", str(source))):
            item = self.result_of(self.back_up(), "refresh0.jpg")
        self.assertEqual(item.reason, run_report.MAC_READ_ERROR)

    def test_every_file_left_when_the_drive_goes_is_recorded(self):
        answers = iter([True, True, False])  # the start, its recheck, then the first file
        with patch.object(volumes, "usb_is_mounted", side_effect=lambda: next(answers, False)):
            stats = self.back_up()
        reasons = {item.reason for item in stats.outcomes}
        self.assertEqual(reasons, {run_report.DRIVE_GONE})
        self.assertEqual(len(stats.outcomes), 3)

    def test_sizes_are_kept_for_the_report(self):
        stats = self.back_up()
        self.assertEqual(stats.sizes[str(self.archive / "refresh0.jpg")], len(b"photo 0"))


def lost(
    name,
    reason="Couldn't write to the drive",
    folder=Path("/Pictures/CameraImports/2026-11/Z8/Photos"),
):
    return FileOutcome("JPG", folder / name, run_report.FAILED, reason=reason)


class TestSummary(UsbFixture):
    def summary(self, outcomes, sizes=None):
        return run_report.summarize_usb(outcomes, sizes or {}, [Path("/Pictures/CameraImports")])

    def test_it_counts_what_is_on_usb_and_what_is_not(self):
        summary = self.summary(
            [lost("a.jpg"), FileOutcome("JPG", Path("/Pictures/CameraImports/b.jpg"), "skipped")]
        )
        self.assertTrue(summary["usb"])
        self.assertEqual((summary["safe"], summary["not_saved"], summary["problems"]), (1, 1, 1))

    def test_folders_are_named_inside_the_archive(self):
        row = self.summary([lost("a.jpg")])["types"][0]
        self.assertEqual(row["where"], "2026-11/Z8/Photos")

    def test_it_says_the_files_are_safe_on_the_mac(self):
        self.assertEqual(
            self.summary([lost("a.jpg"), lost("b.jpg")])["pairs"],
            "All 2 are safe on the Mac, so the card isn't at risk. Back up again to retry them.",
        )

    def test_one_file_reads_properly(self):
        self.assertEqual(
            self.summary([lost("a.jpg")])["pairs"],
            "It's safe on the Mac, so the card isn't at risk. Back up again to retry it.",
        )

    def test_it_does_not_call_a_file_safe_if_the_mac_could_not_read_it(self):
        # This could be the only warning that a saved copy has gone bad.
        summary = self.summary([lost("a.jpg", run_report.MAC_READ_ERROR), lost("b.jpg")])
        self.assertNotIn("safe on the Mac", summary["pairs"])
        self.assertIn("1 file couldn't be read on the Mac", summary["pairs"])
        self.assertNotIn("safe on the Mac", run_report.usb_message(summary))

    def test_the_message_says_usb_so_the_report_marks_the_right_step(self):
        message = run_report.usb_message(self.summary([lost("a.jpg")]))
        self.assertEqual(message, "1 file couldn't be backed up to USB. It's safe on the Mac.")
        row = {"outcome": "failed", "usb": True, "message": message}
        states = {s["id"]: s["state"] for s in display.stages_after_import(row, "done")}
        self.assertEqual(states["usb"], "error")

    def test_a_drive_that_went_away_is_the_first_thing_said(self):
        cause = self.summary([lost("a.jpg", run_report.DRIVE_GONE)])["cause"]
        self.assertIn("disconnected", cause["lead"])

    def test_other_failures_give_no_cause(self):
        self.assertIsNone(self.summary([lost("a.jpg")])["cause"])


class TestBackupRun(UsbFixture):
    def run_backup(self):
        return app.main(["--backup-usb"])

    def row(self):
        return catalog.list_import_history()[0]

    def rows_of_csv(self, row):
        path = config.RUN_REPORTS_DIR / row["details"]["report"] / "files.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_a_failed_backup_keeps_its_problems_and_says_what_happened(self):
        self.conflict()
        self.assertEqual(self.run_backup(), 1)
        row = self.row()
        self.assertEqual(row["kind"], "usb_backup")
        problems = row["details"]["problems"]
        self.assertTrue(problems["usb"])
        self.assertEqual(problems["not_saved"], 1)
        self.assertEqual(
            row["message"], "1 file couldn't be backed up to USB. It's safe on the Mac."
        )

    def test_a_clean_backup_keeps_its_results_but_no_problems(self):
        self.assertEqual(self.run_backup(), 0)
        details = self.row()["details"]
        self.assertNotIn("problems", details)
        rows = self.rows_of_csv(self.row())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["result"], "copied")
        self.assertTrue(rows[0]["usb_path"].endswith(".jpg"))
        self.assertTrue(rows[0]["archive_path"].endswith(".jpg"))

    def test_a_backup_that_could_not_start_still_keeps_its_log(self):
        with patch.object(transfer, "_local_archive_files", side_effect=RuntimeError("broken")):
            self.assertEqual(self.run_backup(), 1)
        details = self.row()["details"]
        self.assertTrue((config.RUN_REPORTS_DIR / details["report"] / "run.log").is_file())
        self.assertNotIn("problems", details)

    def test_a_failure_to_save_the_report_changes_nothing(self):
        with patch.object(run_report, "save_report", side_effect=OSError("disk full")):
            self.assertEqual(self.run_backup(), 0)
        self.assertEqual(self.row()["outcome"], "complete")


class TestBackupAfterImport(UsbFixture):
    def import_card(self):
        self.media()
        with (
            patch("time.sleep"),
            patch("photo_offloader.importer.launch_google_photos_sync"),
            patch("photo_offloader.volumes.eject_volume", return_value=True),
        ):
            return app.main(["--source", str(self.card)])

    def test_its_problems_are_kept_with_the_import(self):
        self.conflict()
        self.assertEqual(self.import_card(), 1)
        row = catalog.list_import_history()[0]
        self.assertEqual(row["kind"], "import")
        self.assertTrue(row["details"]["problems"]["usb"])
        self.assertTrue(row["details"]["local_verified"])

    def test_the_report_marks_the_usb_step_not_the_copy(self):
        self.conflict()
        self.import_card()
        row = catalog.list_import_history()[0]
        steps = {s["id"]: s for s in display.run_report(row)["steps"]}
        self.assertEqual(steps["usb"]["state"], "error")
        self.assertIn("1 problem", steps["usb"]["result"])
        self.assertNotIn("problem", steps["copy"]["result"])

    def test_the_failed_files_are_in_the_runs_list_with_the_cards_files(self):
        self.conflict()
        self.import_card()
        details = catalog.list_import_history()[0]["details"]
        path = config.RUN_REPORTS_DIR / details["report"] / "files.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(any(row["card_path"].endswith("photo.jpg") for row in rows))
        failed = [row for row in rows if row["result"].startswith("failed")]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["usb_path"].endswith("refresh1.jpg"))
        self.assertEqual(failed[0]["card_path"], "")

    def test_a_clean_import_lists_only_the_cards_files(self):
        self.assertEqual(self.import_card(), 0)
        details = catalog.list_import_history()[0]["details"]
        path = config.RUN_REPORTS_DIR / details["report"] / "files.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)  # the card's three, not the whole archive on USB


class TestWording(UsbFixture):
    def backup_row(self, problems):
        return {
            "id": 1,
            "kind": "usb_backup",
            "outcome": "failed",
            "source": "",
            "files": 3130,
            "copied": 0,
            "skipped": 3127,
            "failed": 3,
            "bytes": 0,
            "usb": True,
            "message": "3 files couldn't be backed up to USB.",
            "duration_seconds": 64.0,
            "details": {"problems": problems},
        }

    def problems(self):
        return {"usb": True, "safe": 3127, "problems": 3, "safe_label": "safe on USB"}

    def test_the_row_says_what_is_on_usb(self):
        summary = display.describe_run(self.backup_row(self.problems()))["summary"]
        self.assertEqual(summary, "3,127 safe on USB · 3 problems")

    def test_its_report_shows_all_three_places(self):
        steps = display.run_report(self.backup_row(self.problems()))["steps"]
        self.assertEqual(
            [s["place"] for s in steps], ["Mac archive", "USB backup", "Google Photos"]
        )
        self.assertEqual(steps[1]["state"], "error")
        self.assertEqual(steps[1]["result"], "3,127 safe · 3 problems")
        self.assertEqual(steps[2]["result"], "Not part of this")


if __name__ == "__main__":
    import unittest

    unittest.main()
