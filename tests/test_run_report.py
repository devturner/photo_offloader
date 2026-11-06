"""What a finished import says went wrong: the summary, the saved report and the debug package."""

from __future__ import annotations

import csv
import errno
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config, run_report, transfer
from photo_offloader import importer as app
from photo_offloader.run_report import FileOutcome
from tests.support import CardFixtureTestCase, isolate_config

CARD = Path("/Volumes/CARD")
DCIM = CARD / "DCIM"
FOLDER = DCIM / "100NIKON"


def outcome(category, name, result="copied", reason="", folder=FOLDER, archive="", sha256=""):
    return FileOutcome(category, folder / name, result, archive, sha256, reason)


def failed(category, name, reason=run_report.READ_ERROR, folder=FOLDER):
    return outcome(category, name, run_report.FAILED, reason, folder)


def summarize(outcomes, unreadable=(), sizes=None):
    return run_report.summarize(outcomes, unreadable, sizes or {}, DCIM)


class TestFailureReason(unittest.TestCase):
    source = FOLDER / "DSC_0001.NEF"

    def reason(self, exc):
        return run_report.failure_reason(exc, self.source)

    def test_a_failure_reading_the_card_is_a_read_error(self):
        exc = OSError(errno.EIO, "Input/output error", str(self.source))
        self.assertEqual(self.reason(exc), run_report.READ_ERROR)

    def test_a_full_mac_is_not_blamed_on_the_card(self):
        exc = OSError(errno.ENOSPC, "No space left on device", "/tmp/.photo-copy-1")
        self.assertEqual(self.reason(exc), "The Mac ran out of space")

    def test_a_failure_writing_the_copy_is_not_a_read_error(self):
        exc = OSError(errno.EIO, "Input/output error", "/tmp/.photo-copy-1")
        self.assertEqual(self.reason(exc), "Couldn't write to the Mac: Input/output error")

    def test_a_file_that_changed_during_the_copy_says_so(self):
        exc = IOError(f"Source changed during copy: {self.source}")
        self.assertEqual(self.reason(exc), "Changed during the copy")

    def test_a_copy_that_did_not_match_says_so(self):
        exc = IOError(f"Verification failed for temporary copy: {self.source}")
        self.assertEqual(self.reason(exc), "The copy didn't match when read back")


class TestTypeRows(unittest.TestCase):
    def test_only_types_with_problems_get_a_row_in_a_fixed_order(self):
        summary = summarize(
            [
                failed("VIDEO", "MOV_1.MOV"),
                outcome("VIDEO", "MOV_2.MOV"),
                failed("JPG", "A.JPG"),
                failed("RAW", "B.NEF"),
                outcome("OTHER", "note.xml"),
            ]
        )
        self.assertEqual([row["type"] for row in summary["types"]], ["RAW", "JPG", "Movies"])

    def test_each_count_has_its_own_size(self):
        sizes = {str(FOLDER / "A.NEF"): 100, str(FOLDER / "B.NEF"): 30, str(FOLDER / "C.NEF"): 7}
        summary = summarize(
            [failed("RAW", "A.NEF"), outcome("RAW", "B.NEF"), outcome("RAW", "C.NEF", "skipped")],
            sizes=sizes,
        )
        row = summary["types"][0]
        self.assertEqual((row["lost"], row["lost_bytes"]), (1, 100))
        self.assertEqual((row["safe"], row["safe_bytes"]), (2, 37))
        self.assertEqual((row["total"], row["total_bytes"]), (3, 137))

    def test_a_file_whose_size_is_unknown_still_counts(self):
        row = summarize([failed("RAW", "A.NEF")])["types"][0]
        self.assertEqual((row["lost"], row["lost_bytes"]), (1, 0))

    def test_the_second_line_names_one_folder_and_one_reason_plainly(self):
        row = summarize([failed("RAW", "A.NEF"), failed("RAW", "B.NEF")])["types"][0]
        self.assertEqual(row["where"], "DCIM/100NIKON")
        self.assertEqual(row["why"], run_report.READ_ERROR)

    def test_the_second_line_counts_several_folders_and_reasons(self):
        other = DCIM / "102NIKON"
        row = summarize(
            [
                failed("RAW", "A.NEF"),
                failed("RAW", "B.NEF"),
                failed("RAW", "C.NEF", "Changed during the copy", folder=other),
            ]
        )["types"][0]
        self.assertEqual(row["where"], "2 in DCIM/100NIKON · 1 in DCIM/102NIKON")
        self.assertEqual(row["why"], f"{run_report.READ_ERROR} (2) · Changed during the copy (1)")

    def names_row(self, count):
        return summarize([failed("RAW", f"DSC_{n:04}.NEF") for n in range(count)])["types"][0]

    def test_a_row_names_all_its_files_up_to_30(self):
        row = self.names_row(30)
        self.assertEqual(len(row["names"]), 30)
        self.assertEqual(row["more"], 0)

    def test_past_30_a_row_names_the_first_20_and_says_how_many_more(self):
        row = self.names_row(31)
        self.assertEqual(len(row["names"]), 20)
        self.assertEqual(row["names"][0], "DSC_0000.NEF")
        self.assertEqual(row["more"], 11)

    def test_one_row_being_long_does_not_shorten_another(self):
        summary = summarize(
            [failed("RAW", f"DSC_{n:04}.NEF") for n in range(31)]
            + [failed("JPG", f"DSC_{n:04}.JPG") for n in range(30)]
        )
        self.assertEqual(len(summary["types"][1]["names"]), 30)

    def test_names_show_their_folder_only_when_a_row_has_several(self):
        other = DCIM / "102NIKON"
        row = summarize([failed("RAW", "A.NEF"), failed("RAW", "B.NEF", folder=other)])["types"][0]
        self.assertEqual(row["names"], ["100NIKON/A.NEF", "102NIKON/B.NEF"])


class TestPairsLine(unittest.TestCase):
    def line(self, outcomes, unreadable=()):
        return summarize(outcomes, unreadable)["pairs"]

    def test_a_raw_whose_jpg_was_saved_still_has_one_copy(self):
        line = self.line(
            [failed("RAW", "A.NEF"), failed("RAW", "B.NEF"), outcome("JPG", "A.JPG")]
            + [outcome("JPG", "B.JPG")]
        )
        self.assertEqual(line, "No photo lost both copies. 2 RAW still have their JPG.")

    def test_one_is_singular(self):
        line = self.line([failed("RAW", "A.NEF"), outcome("JPG", "A.JPG")])
        self.assertEqual(line, "No photo lost both copies. 1 RAW still has its JPG.")

    def test_a_pair_matches_by_name_whatever_the_case(self):
        line = self.line([failed("RAW", "a.nef"), outcome("JPG", "A.JPG")])
        self.assertIn("still has its JPG", line)

    def test_the_same_name_in_another_folder_is_not_a_pair(self):
        other = DCIM / "102NIKON"
        line = self.line([failed("RAW", "A.NEF"), outcome("JPG", "A.JPG", folder=other)])
        self.assertEqual(line, "No photo lost both copies. 1 file has no second copy.")

    def test_a_photo_that_lost_both_copies_is_counted_once(self):
        line = self.line([failed("RAW", "A.NEF"), failed("JPG", "A.JPG")])
        self.assertEqual(line, "1 photo lost both RAW and JPG.")

    def test_both_directions_of_one_copy_are_counted_together(self):
        line = self.line(
            [failed("RAW", "A.NEF"), failed("JPG", "A.JPG")]
            + [failed("RAW", "B.NEF"), outcome("JPG", "B.JPG")]
            + [outcome("RAW", "C.NEF"), failed("JPG", "C.JPG")]
        )
        self.assertEqual(line, "1 photo lost both RAW and JPG. 2 still have one copy.")

    def test_a_movie_has_no_second_copy(self):
        line = self.line([failed("VIDEO", "A.MOV"), failed("VIDEO", "B.MOV")])
        self.assertEqual(line, "No photo lost both copies. 2 movies have no second copy.")

    def test_a_photo_shot_alone_counts_like_a_movie(self):
        # Chosen by the user: "lost both" isn't true of it, and leaving it out hides a lost file.
        line = self.line([failed("VIDEO", "A.MOV"), failed("RAW", "B.NEF")])
        self.assertEqual(line, "No photo lost both copies. 2 files have no second copy.")

    def test_an_unreadable_folder_stops_it_claiming_nothing_lost_both(self):
        # That folder could hold anything, and this line could persuade someone to format the card.
        line = self.line([failed("RAW", "A.NEF"), outcome("JPG", "A.JPG")], [DCIM / "101NIKON"])
        self.assertEqual(
            line,
            "No photo lost both copies in the folders that could be read. 1 RAW still has its JPG.",
        )

    def test_a_count_of_lost_photos_says_what_it_leaves_out(self):
        line = self.line([failed("RAW", "A.NEF"), failed("JPG", "A.JPG")], [DCIM / "101NIKON"])
        self.assertEqual(
            line,
            "1 photo lost both RAW and JPG. "
            "This doesn't count DCIM/101NIKON, which couldn't be read.",
        )

    def test_several_unreadable_folders_are_counted(self):
        line = self.line(
            [failed("RAW", "A.NEF"), failed("JPG", "A.JPG")],
            [DCIM / "101NIKON", DCIM / "102NIKON"],
        )
        self.assertIn("This doesn't count the 2 folders that couldn't be read.", line)

    def test_a_clean_run_has_no_line(self):
        self.assertEqual(self.line([outcome("RAW", "A.NEF")]), "")


class TestTotals(unittest.TestCase):
    def test_safe_counts_copied_and_already_saved(self):
        summary = summarize(
            [outcome("RAW", "A.NEF"), outcome("RAW", "B.NEF", "skipped"), failed("RAW", "C.NEF")]
        )
        self.assertEqual(summary["safe"], 2)
        self.assertEqual(summary["not_saved"], 1)

    def test_an_unreadable_folder_is_a_problem_with_no_count_of_files(self):
        summary = summarize([failed("RAW", "A.NEF")], [DCIM / "101NIKON"])
        self.assertEqual(summary["problems"], 2)
        self.assertEqual(summary["not_saved"], 1)
        self.assertEqual(summary["unknown"], ["DCIM/101NIKON"])


ENOUGH = run_report.CARD_TROUBLE_MIN_FILES


class TestLikelyCause(unittest.TestCase):
    def test_a_handful_of_bad_photos_at_the_end_is_not_blamed_on_the_card(self):
        # Five seemed low to the user: the copy tries every file, so these are simply the last ones.
        self.assertGreaterEqual(ENOUGH, 20)

    def test_every_file_after_a_point_failing_looks_like_the_card(self):
        cause = summarize(
            [outcome("JPG", "A.JPG")] + [failed("JPG", f"B{n}.JPG") for n in range(ENOUGH)]
        )["cause"]
        self.assertIn("A.JPG", cause["detail"])
        self.assertIn("card or reader", cause["detail"])

    def test_every_file_failing_says_every_file(self):
        cause = summarize([failed("JPG", f"B{n}.JPG") for n in range(ENOUGH)])["cause"]
        self.assertIn("Every file failed", cause["detail"])

    def test_a_few_failures_are_not_enough_to_blame_the_card(self):
        cause = summarize(
            [outcome("JPG", "A.JPG")] + [failed("JPG", f"B{n}.JPG") for n in range(ENOUGH - 1)]
        )
        self.assertIsNone(cause["cause"])

    def test_a_success_after_a_failure_means_the_card_was_still_working(self):
        cause = summarize(
            [failed("JPG", f"B{n}.JPG") for n in range(ENOUGH)] + [outcome("JPG", "Z.JPG")]
        )
        self.assertIsNone(cause["cause"])

    def test_a_full_mac_is_not_blamed_on_the_card(self):
        cause = summarize(
            [outcome("JPG", "A.JPG")]
            + [failed("JPG", f"B{n}.JPG", "The Mac ran out of space") for n in range(ENOUGH)]
        )
        self.assertIsNone(cause["cause"])


class TestMessage(unittest.TestCase):
    def test_folders_and_files_that_could_not_be_read(self):
        message = run_report.dont_format_message(summarize([failed("RAW", "A.NEF")], [DCIM / "X"]))
        self.assertEqual(message, "Don't format this card: 1 folder and 1 file couldn't be read.")

    def test_it_does_not_say_read_when_the_mac_was_the_problem(self):
        message = run_report.dont_format_message(
            summarize([failed("RAW", "A.NEF", "The Mac ran out of space")])
        )
        self.assertEqual(message, "Don't format this card: 1 file couldn't be copied.")

    def test_an_unreadable_photo_counts_as_a_file(self):
        message = run_report.dont_format_message(summarize([], [DCIM / "bad.jpg"]))
        self.assertEqual(message, "Don't format this card: 1 file couldn't be read.")


class SavedReportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        isolate_config(self, self.root)
        self.outcomes = [
            outcome("RAW", "A.NEF", "copied", archive="/mac/A.NEF", sha256="abc"),
            failed("RAW", "B.NEF"),
            failed("JPG", "C.JPG"),
        ]

    def save(self, failed_run=True, **fields):
        facts = {
            "source": "NIKON Z 8",
            "started_at": 1_800_000_000.0,
            "outcome": "failed" if failed_run else "complete",
            "message": "Don't format this card",
            "dcim": DCIM,
            "files_not_reached": [],
            "outcomes": self.outcomes,
            "unreadable": [DCIM / "101NIKON"],
            "sizes": {str(FOLDER / "A.NEF"): 10},
            "usb_errors": [],
            "log_lines": ["line one", "line two"],
            "app_version": "2.5 (abc1234)",
            "volume_info": "Volume Name: NIKON Z 8",
            "problems": failed_run,
        }
        facts.update(fields)
        return run_report.save_report(run_report.RunFacts(**facts))

    def folder(self, **fields):
        return config.RUN_REPORTS_DIR / self.save(**fields)[0]

    def rows(self, path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))


class TestSavedReport(SavedReportTestCase):
    def test_every_card_file_has_a_row_with_its_type_first(self):
        rows = self.rows(self.folder() / "files.csv")
        self.assertEqual(list(rows[0])[0], "type")
        by_path = {row["card_path"]: row for row in rows}
        saved = by_path["DCIM/100NIKON/A.NEF"]
        self.assertEqual(saved["type"], "RAW")
        self.assertEqual(saved["result"], "copied")
        self.assertEqual(saved["archive_path"], "/mac/A.NEF")
        self.assertEqual(saved["bytes"], "10")
        self.assertEqual(saved["sha256"], "abc")
        self.assertEqual(by_path["DCIM/100NIKON/B.NEF"]["result"], "failed: Read error")
        self.assertEqual(by_path["DCIM/101NIKON"]["result"], "unreadable: contents unknown")

    def test_a_file_never_reached_is_listed_as_that(self):
        rows = self.rows(self.folder(files_not_reached=[("VIDEO", FOLDER / "Z.MOV")]) / "files.csv")
        row = next(row for row in rows if row["card_path"].endswith("Z.MOV"))
        self.assertEqual((row["type"], row["result"]), ("Movies", "not reached"))

    def test_a_usb_problem_is_listed_with_the_files(self):
        folder = self.folder(usb_errors=[("/mac/A.NEF", "USB file differs")])
        row = next(row for row in self.rows(folder / "files.csv") if row["type"] == "USB")
        self.assertEqual(row["result"], "failed: USB file differs")

    def test_a_file_name_cannot_become_a_spreadsheet_formula(self):
        # Names come from the card, and this list opens in a spreadsheet app.
        self.outcomes = [outcome("OTHER", "=cmd().txt", folder=CARD)]
        rows = self.rows(self.folder() / "files.csv")
        self.assertEqual(rows[0]["card_path"], "'=cmd().txt")

    def test_a_path_in_a_folder_is_not_mistaken_for_a_formula(self):
        rows = self.rows(self.folder() / "files.csv")
        self.assertFalse(any(row["card_path"].startswith("'") for row in rows))

    def test_the_run_keeps_its_own_log_lines(self):
        self.assertEqual((self.folder() / "run.log").read_text(), "line one\nline two\n")

    def test_the_summary_says_what_is_safe_and_what_to_do(self):
        text = (self.folder() / "summary.txt").read_text()
        self.assertIn("Don't format this card", text)
        self.assertIn("2.5 (abc1234)", text)
        self.assertIn("Safe on the Mac:", text)
        self.assertIn("DCIM/101NIKON", text)

    def test_the_summary_returned_is_what_the_history_row_keeps(self):
        _name, summary = self.save()
        self.assertEqual(summary["not_saved"], 2)
        json.dumps(summary)

    def test_a_clean_run_has_no_summary_to_keep(self):
        self.outcomes, unreadable = [outcome("RAW", "A.NEF")], []
        _name, summary = self.save(False, unreadable=unreadable)
        self.assertIsNone(summary)

    def test_only_the_newest_reports_are_kept(self):
        for second in range(run_report.REPORTS_KEPT + 3):
            self.save(started_at=1_800_000_000.0 + second)
        kept = sorted(p.name for p in config.RUN_REPORTS_DIR.iterdir())
        self.assertEqual(len(kept), run_report.REPORTS_KEPT)
        newest = run_report._folder_name(1_800_000_000.0 + run_report.REPORTS_KEPT + 2, True)
        self.assertIn(newest, kept)

    def test_clean_runs_never_push_out_a_report_with_problems(self):
        self.save(started_at=1_700_000_000.0)
        first = sorted(p.name for p in config.RUN_REPORTS_DIR.iterdir())[0]
        for second in range(run_report.REPORTS_KEPT + 3):
            self.save(False, started_at=1_800_000_000.0 + second)
        self.assertTrue((config.RUN_REPORTS_DIR / first).is_dir())

    def test_a_repeat_of_the_same_second_does_not_overwrite_a_report(self):
        first, _ = self.save()
        second, _ = self.save()
        self.assertNotEqual(first, second)


class TestFileList(SavedReportTestCase):
    def test_a_list_for_one_type_has_only_its_files_not_saved(self):
        path = run_report.write_type_list(self.folder(), "RAW")
        self.assertEqual([row["card_path"] for row in self.rows(path)], ["DCIM/100NIKON/B.NEF"])

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(ValueError):
            run_report.write_type_list(self.folder(), "../../secret")


class TestDebugPackage(SavedReportTestCase):
    def build(self, folder=None, **fields):
        folder = folder or self.folder()
        downloads = self.root / "Downloads"
        return run_report.build_package(folder, downloads, **fields)

    def test_it_holds_the_report_and_the_listed_extras(self):
        status = self.root / "status.json"
        status.write_text('{"state": "failed"}')
        path = self.build(extras={"status.json": status})
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            self.assertEqual(package.read("status.json"), b'{"state": "failed"}')
        self.assertLessEqual({"summary.txt", "files.csv", "run.log", "status.json"}, names)

    def test_a_missing_extra_is_left_out(self):
        path = self.build(extras={"status.json": self.root / "gone.json"})
        with zipfile.ZipFile(path) as package:
            self.assertNotIn("status.json", package.namelist())

    def test_a_long_log_is_cut_to_its_end(self):
        log = self.root / "trigger.log"
        log.write_text("".join(f"line {n}\n" for n in range(run_report.LOG_EXCERPT_LINES + 5)))
        path = self.build(log_excerpts={"trigger.log": log})
        with zipfile.ZipFile(path) as package:
            lines = package.read("trigger.log").decode().splitlines()
        self.assertEqual(len(lines), run_report.LOG_EXCERPT_LINES)
        self.assertEqual(lines[-1], f"line {run_report.LOG_EXCERPT_LINES + 4}")

    def test_the_name_says_when_and_which_card(self):
        with patch.object(run_report.time, "localtime", return_value=_at_1412()):
            name = self.build().name
        self.assertEqual(name, "PhotoOffloader-2026-11-06-1412-NIKON-Z-8.zip")

    def test_it_never_overwrites_an_earlier_package(self):
        folder = self.folder()
        first = self.build(folder)
        second = self.build(folder)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())

    def test_a_failed_build_leaves_no_partial_file(self):
        folder = self.folder()
        with (
            patch.object(run_report.zipfile.ZipFile, "write", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.build(folder)
        self.assertEqual(list((self.root / "Downloads").iterdir()), [])


def _at_1412():
    import time

    return time.struct_time((2026, 11, 6, 14, 12, 0, 4, 310, 0))


class TestImporterSavesAReport(CardFixtureTestCase):
    def import_card(self):
        with (
            patch("time.sleep"),
            patch("photo_offloader.importer.launch_google_photos_sync"),
            patch("photo_offloader.volumes.eject_volume", return_value=True),
        ):
            return app.main(["--source", str(self.card), "--no-usb"])

    def fail_copying(self, name):
        real = transfer.copy_one_to_destination

        def copy(source, *args, **kwargs):
            if source.name == name:
                raise OSError(errno.EIO, "Input/output error", str(source))
            return real(source, *args, **kwargs)

        return patch.object(transfer, "copy_one_to_destination", side_effect=copy)

    def history(self):
        return catalog.list_import_history()[0]

    def test_a_failed_file_is_named_with_the_run(self):
        self.media()
        with self.fail_copying("photo.nef"):
            self.assertEqual(self.import_card(), 1)
        problems = self.history()["details"]["problems"]
        self.assertEqual(problems["not_saved"], 1)
        self.assertEqual(problems["safe"], 2)
        self.assertEqual(problems["types"][0]["names"], ["photo.nef"])
        self.assertEqual(problems["types"][0]["why"], run_report.READ_ERROR)
        self.assertIn("1 RAW still has its JPG", problems["pairs"])

    def test_the_message_names_what_could_not_be_read(self):
        self.media()
        with self.fail_copying("photo.nef"):
            self.import_card()
        self.assertEqual(
            self.history()["message"], "Don't format this card: 1 file couldn't be read."
        )

    def test_the_report_folder_holds_the_runs_own_log_lines(self):
        self.media()
        with self.fail_copying("photo.nef"):
            self.import_card()
        folder = config.RUN_REPORTS_DIR / self.history()["details"]["report"]
        self.assertIn("Starting new photo offloader session", (folder / "run.log").read_text())
        self.assertTrue((folder / "files.csv").is_file())

    def test_a_clean_run_keeps_its_results_but_no_problem_summary(self):
        self.media()
        self.assertEqual(self.import_card(), 0)
        details = self.history()["details"]
        self.assertNotIn("problems", details)
        self.assertTrue((config.RUN_REPORTS_DIR / details["report"] / "files.csv").is_file())

    def test_a_failure_to_save_never_changes_the_result(self):
        self.media()
        with patch.object(run_report, "save_report", side_effect=OSError("disk full")):
            self.assertEqual(self.import_card(), 0)
        self.assertEqual(self.history()["outcome"], "complete")
        self.assertNotIn("report", self.history()["details"])

    def test_a_canceled_run_lists_what_it_never_reached(self):
        self.media()
        with patch.object(transfer, "copy_one_to_destination", side_effect=KeyboardInterrupt):
            self.assertEqual(self.import_card(), 130)
        folder = config.RUN_REPORTS_DIR / self.history()["details"]["report"]
        with (folder / "files.csv").open(encoding="utf-8-sig", newline="") as handle:
            results = {row["result"] for row in csv.DictReader(handle)}
        self.assertEqual(results, {"not reached"})


class TestCopyRecordsEachFile(CardFixtureTestCase):
    def test_a_copied_file_keeps_where_it_went_and_its_hash(self):
        source = self.media()["JPG"]
        stats = transfer.ImportStats()
        transfer.copy_category([source], self.dcim, [self.local], "JPG", stats)
        saved = stats.outcomes[0]
        self.assertEqual((saved.category, saved.source, saved.result), ("JPG", source, "copied"))
        self.assertTrue(Path(saved.archive_path).is_file())
        self.assertEqual(len(saved.sha256), 64)

    def test_a_file_found_already_saved_says_so(self):
        source = self.media()["JPG"]
        transfer.copy_category([source], self.dcim, [self.local], "JPG", transfer.ImportStats())
        stats = transfer.ImportStats()
        transfer.copy_category([source], self.dcim, [self.local], "JPG", stats)
        self.assertEqual(stats.outcomes[0].result, "skipped")
        self.assertEqual(len(stats.outcomes[0].sha256), 64)

    def test_a_failed_file_keeps_why(self):
        source = self.media()["JPG"]
        stats = transfer.ImportStats()
        with patch.object(
            transfer.fileops,
            "copy_file_with_progress",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            transfer.copy_category([source], self.dcim, [self.local], "JPG", stats)
        self.assertEqual(stats.outcomes[0].result, run_report.FAILED)
        self.assertEqual(stats.outcomes[0].reason, "The Mac ran out of space")

    def test_a_card_read_failing_partway_is_reported_against_the_card(self):
        source = self.media()["JPG"]
        real_open = Path.open

        class FailingCard(io.BytesIO):
            def read(self, *_args):
                raise OSError(errno.EIO, "Input/output error")

        def opened(path, *args, **kwargs):
            return FailingCard() if path == source else real_open(path, *args, **kwargs)

        stats = transfer.ImportStats()
        with patch.object(Path, "open", opened):
            transfer.copy_category([source], self.dcim, [self.local], "JPG", stats)
        self.assertEqual(stats.outcomes[0].result, run_report.FAILED)
        self.assertEqual(stats.outcomes[0].reason, run_report.READ_ERROR)


class TestSecondTry(CardFixtureTestCase):
    def copy_failing(self, times, names=None, reason=errno.EIO):
        """``copy_one_to_destination`` that fails each named file its first ``times`` tries."""
        real, tries = transfer.copy_one_to_destination, {}

        def copy(source, *args, **kwargs):
            tries[source.name] = tries.get(source.name, 0) + 1
            if (names is None or source.name in names) and tries[source.name] <= times:
                raise OSError(reason, "Input/output error", str(source))
            return real(source, *args, **kwargs)

        self.tries = tries
        return patch.object(transfer, "copy_one_to_destination", side_effect=copy)

    def first_pass(self):
        files = self.media()
        stats = transfer.ImportStats()
        for category, source in files.items():
            root = transfer.media_info.local_root_for_category(category)
            transfer.copy_category([source], self.dcim, [root], category, stats)
        return files, stats

    def test_a_read_error_that_clears_up_saves_the_file(self):
        with self.copy_failing(1, {"photo.nef"}):
            files, stats = self.first_pass()
            self.assertEqual(stats.failed, 1)
            recovered = transfer.retry_read_errors(self.dcim, stats)
        self.assertEqual(recovered, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.errors, [])
        saved = next(item for item in stats.outcomes if item.source == files["RAW"])
        self.assertEqual(saved.result, "copied")
        self.assertTrue(saved.retried)
        self.assertTrue(Path(saved.archive_path).is_file())
        self.assertEqual(stats.destination_copies, 3)

    def test_a_recovered_file_counts_as_new(self):
        with self.copy_failing(1, {"photo.nef"}):
            _files, stats = self.first_pass()
            transfer.retry_read_errors(self.dcim, stats)
        self.assertEqual(transfer.progress._PROGRESS["new_files"], 3)

    def test_a_file_that_fails_again_stays_failed(self):
        with self.copy_failing(2, {"photo.nef"}):
            _files, stats = self.first_pass()
            self.assertEqual(transfer.retry_read_errors(self.dcim, stats), 0)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(len(stats.errors), 1)

    def test_only_read_errors_are_tried_again(self):
        # A full disk doesn't clear up in five seconds, and trying again would only fill it further.
        with self.copy_failing(1, {"photo.nef"}, errno.ENOSPC):
            _files, stats = self.first_pass()
            self.assertEqual(stats.failed, 1)
            transfer.retry_read_errors(self.dcim, stats)
        self.assertEqual(self.tries["photo.nef"], 1)

    def test_a_clean_copy_does_not_wait(self):
        _files, stats = self.first_pass()
        with patch.object(transfer.time, "sleep") as sleep:
            self.assertEqual(transfer.retry_read_errors(self.dcim, stats), 0)
        sleep.assert_not_called()

    def test_it_waits_before_trying_again(self):
        with self.copy_failing(1, {"photo.nef"}):
            _files, stats = self.first_pass()
            with patch.object(transfer.time, "sleep") as sleep:
                transfer.retry_read_errors(self.dcim, stats)
        sleep.assert_called_once_with(config.READ_RETRY_PAUSE_SECONDS)

    def many_lost(self, count):
        return [
            FileOutcome(
                "JPG", self.dcim / f"lost{n}.jpg", run_report.FAILED, reason=run_report.READ_ERROR
            )
            for n in range(count)
        ]

    def test_a_dead_card_is_not_tried_file_by_file(self):
        # With no timeout on a read yet, each try on a dying card can take a long time.
        stats = transfer.ImportStats(failed=50, outcomes=self.many_lost(50))
        with patch.object(
            transfer, "copy_one_to_destination", side_effect=OSError(errno.EIO, "x")
        ) as copy:
            transfer.retry_read_errors(self.dcim, stats)
        self.assertEqual(copy.call_count, transfer.READ_RETRY_GIVE_UP_AFTER)
        self.assertEqual(stats.failed, 50)

    def test_a_success_starts_the_count_of_failures_again(self):
        stats = transfer.ImportStats(failed=30, outcomes=self.many_lost(30))
        calls = {"n": 0}

        def copy(source, dcim, base, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] % transfer.READ_RETRY_GIVE_UP_AFTER == 0:
                return transfer.DestinationResult(base, "skipped", source, "", "abc")
            raise OSError(errno.EIO, "x")

        with patch.object(transfer, "copy_one_to_destination", side_effect=copy):
            transfer.retry_read_errors(self.dcim, stats)
        self.assertEqual(calls["n"], 30)

    def test_a_card_that_has_gone_is_not_tried_at_all(self):
        stats = transfer.ImportStats(failed=3, outcomes=self.many_lost(3))
        with patch.object(transfer, "copy_one_to_destination") as copy:
            transfer.retry_read_errors(self.root / "gone" / "DCIM", stats)
        copy.assert_not_called()

    def test_the_list_says_which_files_needed_a_second_try(self):
        facts = run_report.RunFacts(
            source="card",
            started_at=1_800_000_000.0,
            outcome="complete",
            message="",
            dcim=self.dcim,
            problems=False,
            outcomes=[FileOutcome("JPG", self.dcim / "a.jpg", "copied", retried=True)],
        )
        folder = config.RUN_REPORTS_DIR / run_report.save_report(facts)[0]
        with (folder / "files.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(next(csv.DictReader(handle))["result"], "copied on the second try")

    def test_an_import_retries_and_finishes_clean(self):
        self.media()
        with (
            self.copy_failing(1, {"photo.nef"}),
            patch("time.sleep"),
            patch("photo_offloader.importer.launch_google_photos_sync"),
            patch("photo_offloader.volumes.eject_volume", return_value=True),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 0)
        row = catalog.list_import_history()[0]
        self.assertEqual(row["outcome"], "complete")
        self.assertEqual(row["failed"], 0)
        self.assertEqual(row["copied"], 3)
        self.assertEqual(row["details"]["second_tries"], 1)


if __name__ == "__main__":
    unittest.main()
