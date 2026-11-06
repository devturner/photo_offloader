"""The dashboard's side of a run's report: the warning on the card, the wording and the actions."""

from __future__ import annotations

import dataclasses
import zipfile
from pathlib import Path

from photo_offloader import catalog, config, run_report
from photo_offloader.run_report import FileOutcome
from tests.test_dashboard_core import Fixture

DCIM = Path("/Volumes/NIKON Z 8/DCIM")
FOLDER = DCIM / "100NIKON"


def outcomes():
    return [
        FileOutcome("RAW", FOLDER / "A.NEF", "copied", "/mac/A.NEF", "abc"),
        FileOutcome("RAW", FOLDER / "B.NEF", run_report.FAILED, reason=run_report.READ_ERROR),
        FileOutcome("JPG", FOLDER / "B.JPG", "skipped", "/mac/B.JPG", "def"),
    ]


class ReportFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.downloads = Path(self.tmp.name) / "Downloads"
        self.paths = dataclasses.replace(self.paths, downloads=self.downloads)
        self.core = self.core_with(self.paths)

    def core_with(self, paths):
        real = self.core
        return type(real)(paths, clock=real._clock, run=real._run, popen=real._popen)

    def failed_run(self, **fields):
        """A failed import with a saved report, as the importer leaves it."""
        folder, summary = run_report.save_report(
            run_report.RunFacts(
                source="NIKON Z 8",
                started_at=self.now - 60,
                outcome="failed",
                message="Don't format this card: 1 file couldn't be read.",
                dcim=DCIM,
                problems=True,
                outcomes=outcomes(),
                log_lines=["a log line"],
            )
        )
        self.record(
            outcome="failed",
            failed=1,
            message="Don't format this card: 1 file couldn't be read.",
            details={"report": folder, "problems": summary},
            **fields,
        )
        return self.latest_id()

    def latest_id(self):
        return catalog.list_import_history(limit=1)[0]["id"]

    def card_step(self):
        return next(s for s in self.core.state()["pipeline"] if s["id"] == "card")


class TestCardWarning(ReportFixture):
    def test_the_card_keeps_saying_dont_format_after_a_failed_import(self):
        self.add_card()
        self.failed_run()
        step = self.card_step()
        self.assertEqual(step["state"], "error")
        self.assertEqual(step["detail"], "Don't format: files not saved")

    def test_eject_asks_first(self):
        self.add_card()
        self.failed_run()
        eject = next(a for a in self.card_step()["actions"] if a["id"] == "eject_card")
        self.assertIn("weren't copied", eject["confirm"])

    def test_it_offers_to_try_again(self):
        self.add_card()
        self.failed_run()
        labels = {a["id"]: a["label"] for a in self.card_step()["actions"]}
        self.assertEqual(labels["import_card"], "Try again")

    def test_a_successful_retry_clears_it(self):
        self.add_card()
        self.failed_run()
        self.record(outcome="complete")
        step = self.card_step()
        self.assertEqual(step["state"], "ok")
        self.assertNotIn("confirm", step["actions"][-1])

    def test_a_failed_retry_keeps_it(self):
        self.add_card()
        self.failed_run()
        self.failed_run()
        self.assertEqual(self.card_step()["state"], "error")

    def test_a_usb_backup_since_does_not_clear_it(self):
        self.add_card()
        self.failed_run()
        self.record(
            kind="usb_backup", outcome="complete"
        )  # the same source, to prove it's the kind
        self.assertEqual(self.card_step()["state"], "error")

    def test_another_card_is_not_warned_about(self):
        self.add_card("OTHER")
        self.failed_run()
        self.assertEqual(self.card_step()["state"], "ok")

    def test_a_usb_failure_leaves_the_card_safe(self):
        # Its photos are verified on the Mac, so formatting this card loses nothing.
        self.add_card()
        self.record(outcome="failed", message="USB failed", details={"local_verified": True})
        self.assertEqual(self.card_step()["state"], "ok")

    def test_a_run_that_stopped_before_copying_still_warns(self):
        self.add_card()
        self.record(outcome="failed", message="Insufficient free space")
        self.assertEqual(self.card_step()["state"], "error")

    def test_an_empty_card_is_not_warned_about(self):
        self.add_card()
        self.record(outcome="failed", files=0, message="No supported media files")
        self.assertEqual(self.card_step()["state"], "ok")

    def test_with_no_card_there_is_no_warning(self):
        self.failed_run()
        self.assertEqual(self.card_step()["state"], "off")


class TestRunWording(ReportFixture):
    def run_row(self):
        return self.core.history()[0]

    def test_the_row_says_what_is_safe_and_how_many_problems(self):
        self.failed_run()
        self.assertEqual(self.run_row()["summary"], "2 safe on the Mac · 1 problem")

    def test_the_panel_says_the_same(self):
        self.failed_run()
        copy = next(s for s in self.run_row()["report"]["steps"] if s["id"] == "copy")
        self.assertEqual(copy["result"], "2 safe · 1 problem")

    def test_steps_that_never_ran_say_not_started(self):
        self.failed_run()
        steps = {s["id"]: s for s in self.run_row()["report"]["steps"]}
        self.assertEqual(steps["usb"]["result"], "Not started")
        self.assertEqual(steps["upload"]["result"], "Not started")

    def test_a_run_from_before_reports_still_reads_as_it_did(self):
        self.record(outcome="failed", failed=2, message="Local SSD import failed")
        self.assertIn("2 failed", self.run_row()["summary"])

    def test_a_row_says_whether_its_report_is_still_there(self):
        self.failed_run()
        self.assertTrue(self.run_row()["report_saved"])
        self.record(outcome="complete", details={"report": "gone-problems"})
        self.assertFalse(self.run_row()["report_saved"])


class TestReportActions(ReportFixture):
    def test_the_full_list_opens_the_runs_file(self):
        run = self.failed_run()
        result = self.core.act("open_file_list", {"run_id": run})
        self.assertTrue(result["ok"], result)
        argv = self.spawned[-1]
        self.assertEqual(argv[0], "/usr/bin/open")
        self.assertEqual(Path(argv[1]).name, "files.csv")

    def test_one_types_list_holds_only_that_type(self):
        run = self.failed_run()
        self.assertTrue(self.core.act("open_file_list", {"run_id": run, "type": "RAW"})["ok"])
        self.assertEqual(Path(self.spawned[-1][1]).name, "files-RAW.csv")

    def test_a_made_up_type_is_refused(self):
        run = self.failed_run()
        result = self.core.act("open_file_list", {"run_id": run, "type": "../../secret"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.spawned, [])

    def test_a_run_whose_report_is_gone_says_so(self):
        self.record(outcome="failed", details={"report": "gone-problems"})
        result = self.core.act("open_file_list", {"run_id": self.latest_id()})
        self.assertFalse(result["ok"])
        self.assertIn("no longer", result["message"])

    def test_a_report_name_cannot_lead_out_of_the_reports_folder(self):
        config.RUN_REPORTS_DIR.mkdir(parents=True)  # otherwise ../ fails for the wrong reason
        secret = config.RUN_REPORTS_DIR.parent / "elsewhere"
        secret.mkdir()
        (secret / "files.csv").write_text("secret")
        self.record(outcome="failed", details={"report": "../elsewhere"})
        result = self.core.act("open_file_list", {"run_id": self.latest_id()})
        self.assertFalse(result["ok"])

    def test_a_run_id_that_is_not_a_number_is_refused(self):
        result = self.core.act("open_file_list", {"run_id": "1; rm"})
        self.assertFalse(result["ok"])

    def test_the_preview_shows_the_summary_and_the_start_of_the_list(self):
        run = self.failed_run()
        result = self.core.act("report_preview", {"run_id": run})
        self.assertIn("Safe on the Mac", result["summary"])
        self.assertIn("DCIM/100NIKON/B.NEF", result["files"])

    def save(self, run):
        return self.core.act("save_debug_package", {"run_id": run})

    def test_a_package_goes_to_downloads_and_says_its_name_and_size(self):
        result = self.save(self.failed_run())
        self.assertTrue(result["ok"], result)
        package = self.downloads / result["name"]
        self.assertTrue(package.is_file())
        self.assertEqual(result["bytes"], package.stat().st_size)

    def test_a_package_never_holds_the_token_or_google_credentials(self):
        (self.install / "dashboard.json").write_text('{"token": "SECRET-TOKEN"}')
        config.RCLONE_CONFIG_PATH.parent.mkdir(parents=True)
        config.RCLONE_CONFIG_PATH.write_text("token = SECRET-GOOGLE")
        (self.install / "status.json").write_text('{"state": "failed"}')
        result = self.save(self.failed_run())
        with zipfile.ZipFile(self.downloads / result["name"]) as package:
            names = set(package.namelist())
            contents = b"".join(package.read(name) for name in names)
        self.assertIn("status.json", names)
        self.assertNotIn(b"SECRET", contents)
        self.assertFalse(names & {"dashboard.json", "rclone.conf"})

    def test_a_package_holds_the_trigger_and_upload_logs(self):
        self.paths.trigger_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.paths.trigger_log_file.write_text("card seen\n")
        self.paths.google_log_file.write_text("uploaded\n")
        result = self.save(self.failed_run())
        with zipfile.ZipFile(self.downloads / result["name"]) as package:
            self.assertEqual(package.read("trigger.log"), b"card seen\n")
            self.assertEqual(package.read("google_photos_sync.log"), b"uploaded\n")

    def test_show_in_finder_shows_the_package_just_saved(self):
        result = self.save(self.failed_run())
        self.assertTrue(self.core.act("reveal_debug_package", {})["ok"])
        self.assertEqual(
            self.spawned[-1], ["/usr/bin/open", "-R", str(self.downloads / result["name"])]
        )

    def test_show_in_finder_with_nothing_saved_says_so(self):
        self.assertFalse(self.core.act("reveal_debug_package", {})["ok"])
