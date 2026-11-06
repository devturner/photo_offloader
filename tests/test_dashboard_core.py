#!/usr/bin/env python3
"""Tests for photo_offloader.dashboard.core: the state the dashboard renders and the actions it can run."""

import dataclasses
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config, google_photos
from photo_offloader.dashboard import core, display
from tests import support


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.install = root / "install"
        self.data = root / "data"
        self.volumes = root / "Volumes"
        self.usb = self.volumes / "Photos"
        for folder in (self.install, self.data, self.volumes):
            folder.mkdir()
        for folder in ("bin", "scripts"):
            (self.install / folder).mkdir()
        (self.install / "scripts" / "service.sh").write_text("#!/bin/bash\n")
        (self.install / "bin" / "photo-offloader-google-sync").write_text("")
        (self.install / "scripts" / "trigger.sh").write_text("#!/bin/bash\n")

        support.isolate_config(
            self,
            root,
            CATALOG_DB_PATH=self.data / "catalog.sqlite3",
            LEGACY_CATALOG_DB_PATH=root / "legacy.sqlite3",
        )
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PHOTO_OFFLOADER_STATUS_FILE", None)
        os.environ.pop("PHOTO_OFFLOADER_PAUSE_FILE", None)

        self.paths = core.Paths(
            install_dir=self.install,
            data_dir=self.data,
            volumes_root=self.volumes,
            usb_volume=self.usb,
            local_archives=(root / "CameraImports", root / "CameraImportsRaw"),
            usb_archives=(self.usb / "CameraImports",),
            python=Path("/usr/bin/python3"),
        )
        self.now = 1_800_000_000.0
        self.service_up = True
        self.importer_up = False
        self.google_up = False
        self.spawned = []
        self.ran = []

        def run(argv, **_kwargs):
            self.ran.append(list(argv))
            if "print" in argv:
                return subprocess.CompletedProcess(argv, 0 if self.service_up else 113, "", "")
            if argv[0].endswith("pgrep"):
                alive = (
                    self.google_up
                    if argv[-1].endswith("photo-offloader-google-sync")
                    else self.importer_up
                )
                return subprocess.CompletedProcess(argv, 0, "4242\n" if alive else "", "")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        self.core = core.DashboardCore(
            self.paths,
            clock=lambda: self.now,
            run=run,
            popen=lambda argv, **kw: self.spawned.append(list(argv)),
        )

    # helpers ---------------------------------------------------------------------------
    def advance(self, seconds):
        self.now += seconds
        self.core._cache.clear()

    def write_status(self, **fields):
        stamp = datetime.fromtimestamp(self.now).isoformat(timespec="seconds")
        payload = {"updated_at": stamp, **fields}
        (self.install / "status.json").write_text(json.dumps(payload))

    def add_card(self, name="NIKON Z 8"):
        (self.volumes / name / "DCIM").mkdir(parents=True)

    def record(self, **fields):
        base = dict(
            kind="import",
            source="NIKON Z 8",
            outcome="complete",
            files=214,
            copied=214,
            bytes=420 * 1024 * 1024,
            started_at=self.now - 60,
            finished_at=self.now - 5,
        )
        base.update(fields)
        catalog.record_import_run(**base)

    def seed_catalog(self, local=0, usb=0):
        catalog.catalog_connection().close()
        with sqlite3.connect(self.data / "catalog.sqlite3") as db:
            for index in range(local):
                db.execute(
                    "INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode, indexed_at)"
                    " VALUES (?, ?, ?, 1, 1, ?, 1)",
                    (f"l{index}", str(self.paths.local_archives[0].resolve()), f"l{index}", index),
                )
            for index in range(usb):
                db.execute(
                    "INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode, indexed_at)"
                    " VALUES (?, ?, ?, 1, 1, ?, 1)",
                    (f"u{index}", str(self.paths.usb_archives[0].resolve()), f"u{index}", index),
                )

    def action_ids(self, step_id):
        return [action["id"] for action in self.pipeline(step_id)["actions"]]

    def pipeline(self, step_id):
        return next(s for s in self.core.state()["pipeline"] if s["id"] == step_id)


class TestHeadline(Fixture):
    def test_idle_with_nothing_mounted(self):
        head = self.core.state()["headline"]
        self.assertEqual((head["tone"], head["title"]), ("idle", "Ready"))

    def test_stopped_service_is_the_most_important_thing(self):
        self.service_up = False
        self.record(outcome="failed")
        self.write_status(state="copying", source="/Volumes/X")
        head = self.core.state()["headline"]
        self.assertEqual(head["tone"], "stopped")
        self.assertIn("stopped", head["title"])

    def test_a_mounted_card_is_named(self):
        self.add_card("NIKON Z 8")
        head = self.core.state()["headline"]
        self.assertEqual(head["title"], "NIKON Z 8 is mounted")

    def mark_card_handled(self, name="NIKON Z 8"):
        (self.install / ".last_camera_source").write_text(str(self.volumes / name / "DCIM") + "\n")

    def test_an_already_imported_card_says_so_instead_of_waiting(self):
        # User report 2026-11-04: a re-inserted card showed "Waiting to start the
        # import" although the trigger had already handled it and would never start.
        self.add_card("NIKON Z 8")
        self.mark_card_handled()
        head = self.core.state()["headline"]
        self.assertEqual(head["title"], "NIKON Z 8 was already imported")
        self.assertNotIn("Waiting", head["detail"])
        self.assertIn(
            {"id": "import_card", "label": "Import now"}, self.pipeline("card")["actions"]
        )

    def test_import_now_is_hidden_while_the_importer_is_busy(self):
        self.add_card("NIKON Z 8")
        self.mark_card_handled()
        self.importer_up = True
        self.write_status(
            state="copying",
            source=str(self.volumes / "NIKON Z 8" / "DCIM"),
            files_done=1,
            files_total=10,
        )
        self.assertEqual(self.pipeline("card")["actions"], [])

    def test_import_now_forgets_the_handled_card_and_rechecks(self):
        self.add_card("NIKON Z 8")
        self.mark_card_handled()
        result = self.core.act("import_card")
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.install / ".last_camera_source").exists())
        self.assertTrue(self.spawned[-1][1].endswith("scripts/trigger.sh"))

    def test_import_now_refuses_while_paused(self):
        self.add_card("NIKON Z 8")
        self.mark_card_handled()
        (self.install / ".paused").touch()
        result = self.core.act("import_card")
        self.assertFalse(result["ok"])
        self.assertTrue((self.install / ".last_camera_source").exists())
        self.assertEqual(self.spawned, [])

    def test_other_volumes_without_dcim_are_ignored(self):
        (self.volumes / "Macintosh HD").mkdir()
        self.assertFalse(self.core.state()["card"]["mounted"])

    def test_paused_imports_are_called_out(self):
        (self.install / ".paused").touch()
        head = self.core.state()["headline"]
        self.assertEqual((head["tone"], head["title"]), ("warning", "Automatic imports are paused"))


class TestActivity(Fixture):
    def copying(self, **extra):
        self.importer_up = True
        fields = dict(
            state="copying",
            phase="card-to-ssd",
            source="/Volumes/NIKON Z 8",
            bytes_done=100 * 1024**2,
            bytes_total=400 * 1024**2,
            bytes_transferred=100 * 1024**2,
            transfer_speed_bps=50 * 1024**2,
            eta_seconds=6,
            current_file="DSC_0142.NEF",
        )
        fields.update(extra)
        self.write_status(**fields)

    def test_copy_progress_is_reported(self):
        self.copying(stage="copy", category="RAW", files_done=40, files_total=160, new_files=3)
        state = self.core.state()
        activity = state["activity"]
        self.assertEqual(activity["title"], "Copying new files")
        self.assertAlmostEqual(activity["percent"], 25.0)
        self.assertEqual(activity["detail"], "RAW · DSC_0142.NEF")
        self.assertEqual(
            (activity["files_done"], activity["files_total"], activity["new_files"]), (40, 160, 3)
        )
        self.assertEqual(state["headline"]["title"], "Copying new files")
        self.assertEqual(state["headline"]["detail"], "NIKON Z 8 · RAW · DSC_0142.NEF")
        archive = self.pipeline("archive")
        self.assertEqual(
            (archive["state"], archive["headline"], archive["detail"]),
            ("active", "Copying", "RAW · DSC_0142.NEF"),
        )

    def checking(self, **extra):
        # Between copies the importer reports each file's copy decision as "scanning".
        fields = dict(
            state="scanning",
            phase="copy-decision",
            stage="copy",
            category="RAW",
            decision_files_checked=5,
            decision_files_total=1188,
        )
        fields.update(extra)
        self.copying(**fields)

    def test_activity_tells_what_it_is_actually_doing(self):
        # User feedback 2026-11-04: "Activity is too generic".
        expectations = [
            (dict(state="scanning", phase="card-to-ssd", stage="scan"), "Scanning card"),
            (dict(state="scanning", phase="card-to-ssd", stage="metadata"), "Reading photo info"),
            (
                dict(state="scanning", phase="copy-decision", stage="copy", new_files=0),
                "Checking for new files",
            ),
            (dict(state="scanning", phase="USB backup", stage="usb"), "Comparing with USB backup"),
            (dict(state="copying", phase="USB backup", stage="usb"), "Backing up to USB"),
            (
                dict(state="scanning", phase="catalog-update", stage="catalog"),
                "Updating file index",
            ),
        ]
        for fields, title in expectations:
            with self.subTest(title=title):
                self.copying(**fields)
                self.assertEqual(self.core.state()["activity"]["title"], title)

    def test_title_progress_and_graph_hold_steady_between_check_and_copy(self):
        # User report: the box, timers and graph flashed in and out because each file
        # alternates between a "checking" and a "copying" status.
        seen = []
        for step in range(6):
            self.advance(1)
            transferred = (100 + step * 10) * 1024**2
            if step % 2:
                self.checking(new_files=2, bytes_done=transferred, bytes_transferred=transferred)
            else:
                self.copying(
                    stage="copy",
                    category="RAW",
                    new_files=2,
                    bytes_done=transferred,
                    bytes_transferred=transferred,
                )
            seen.append(self.core.state()["activity"])
        self.assertEqual({a["title"] for a in seen}, {"Copying new files"})
        percents = [a["percent"] for a in seen]
        self.assertEqual(percents, sorted(percents), "progress must not jump between measures")
        self.assertEqual([len(a["speed_history"]) for a in seen], [0, 1, 2, 3, 4, 5])
        self.assertTrue(all(a["eta_seconds"] is not None for a in seen[1:]))
        self.assertTrue(all(a["speed_bps"] > 0 for a in seen[1:]))

    def test_while_scanning_the_card_step_is_active_not_the_archive(self):
        self.add_card("NIKON Z 8")
        self.copying(state="scanning", phase="card-to-ssd", stage="metadata")
        self.assertEqual(
            (self.pipeline("card")["state"], self.pipeline("card")["detail"]),
            ("active", "Reading photo info"),
        )
        self.assertNotEqual(self.pipeline("archive")["state"], "active")

    def test_speed_does_not_blank_out_while_unchanged_files_are_checked(self):
        transferred = 100 * 1024**2
        self.copying(stage="copy", new_files=1, bytes_transferred=transferred)
        self.core.state()
        self.advance(1)
        self.copying(stage="copy", new_files=1, bytes_transferred=transferred + 50 * 1024**2)
        self.core.state()
        for _ in range(5):  # five seconds of checks: nothing transferred
            self.advance(1)
            self.checking(new_files=1, bytes_transferred=transferred + 50 * 1024**2)
            activity = self.core.state()["activity"]
            self.assertGreater(activity["speed_bps"], 0)

    def test_import_stages_are_listed_with_progress(self):
        self.checking(usb_planned=False)
        stages = self.core.state()["activity"]["stages"]
        self.assertEqual(
            [(s["id"], s["state"]) for s in stages],
            [
                ("scan", "done"),
                ("metadata", "done"),
                ("copy", "active"),
                ("usb", "skipped"),
                ("upload", "skipped"),
            ],
        )
        self.copying(stage="usb", phase="USB backup", usb_planned=True)
        stages = self.core.state()["activity"]["stages"]
        self.assertEqual(
            [s["state"] for s in stages], ["done", "done", "done", "active", "skipped"]
        )

    def test_usb_backup_is_recognised_by_phase(self):
        self.importer_up = True
        self.write_status(state="copying", phase="usb-backup", bytes_done=1, bytes_total=4)
        activity = self.core.state()["activity"]
        self.assertEqual((activity["kind"], activity["title"]), ("usb_backup", "Backing up to USB"))
        self.assertEqual(self.pipeline("usb")["state"], "active")

    def test_catalog_update_is_indeterminate(self):
        self.importer_up = True
        self.write_status(state="scanning", phase="catalog-update", catalog_scanned=1234)
        activity = self.core.state()["activity"]
        self.assertTrue(activity["indeterminate"])
        self.assertIn("1,234", activity["detail"])

    def test_live_speed_is_measured_between_polls_and_graphed(self):
        self.copying()
        first = self.core.state()["activity"]
        self.assertEqual(first["speed_history"], [])  # nothing to measure against yet: no fake zero
        self.advance(2)
        self.copying(bytes_transferred=100 * 1024**2 + 40 * 1024**2)
        activity = self.core.state()["activity"]
        self.assertAlmostEqual(activity["speed_bps"], 20 * 1024**2, delta=1024)
        self.advance(2)
        self.copying(bytes_transferred=100 * 1024**2 + 100 * 1024**2)
        activity = self.core.state()["activity"]
        self.assertEqual(len(activity["speed_history"]), 2)
        self.assertAlmostEqual(activity["speed_history"][-1], 30 * 1024**2, delta=1024)
        self.assertIsNotNone(activity["eta_seconds"])

    def test_the_graph_keeps_three_minutes_and_the_speed_still_uses_the_last_ten_seconds(self):
        moved = 100 * 1024**2
        self.copying(bytes_transferred=moved)
        self.core.state()
        for second in range(400):
            self.advance(1)
            moved += (1 if second < 390 else 5) * 1024**2
            self.copying(bytes_transferred=moved)
            activity = self.core.state()["activity"]
        self.assertEqual(len(activity["speed_history"]), 180)
        self.assertAlmostEqual(activity["speed_bps"], 5 * 1024**2, delta=1024)

    def test_the_google_box_has_its_own_bar_only_while_an_import_uses_the_top_panel(self):
        google = {
            "configured": True,
            "state": "running",
            "progress": {
                "percent": 40.0,
                "finalizing": False,
                "transferred": "4 MiB",
                "total": "10 MiB",
            },
        }
        for import_running, percent in ((True, 40.0), (False, None)):
            with self.subTest(import_running=import_running):
                activity = (
                    {
                        "kind": "import",
                        "stage": "copy",
                        "detail": "",
                        "percent": 10.0,
                        "files_total": 2,
                        "new_files_expected": None,
                    }
                    if import_running
                    else None
                )
                steps = display.pipeline(None, activity, None, False, google, True, False, "")
                self.assertEqual(steps[-1]["percent"], percent)

    def test_the_card_box_says_how_many_files_are_new(self):
        for total, expected, text in (
            (2478, 1278, "1,278 new files of 2,478"),
            (2478, 1, "1 new file of 2,478"),
            (2478, 0, "No new files of 2,478"),
            (2478, None, "2,478 files"),
            (1, None, "1 file"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(display.card_contents(total, expected), text)

    def test_a_known_number_of_new_files_is_what_progress_measures(self):
        self.copying(
            bytes_done=600 * 1024**2,
            bytes_total=2000 * 1024**2,
            bytes_transferred=100 * 1024**2,
            expected_new_files=16,
            expected_new_bytes=400 * 1024**2,
            eta_seconds=999,
        )
        self.core.state()
        self.advance(1)
        self.copying(
            bytes_done=650 * 1024**2,
            bytes_total=2000 * 1024**2,
            bytes_transferred=150 * 1024**2,
            expected_new_files=16,
            expected_new_bytes=400 * 1024**2,
            eta_seconds=999,
        )
        activity = self.core.state()["activity"]
        self.assertAlmostEqual(activity["percent"], 37.5)
        self.assertEqual(activity["bytes_done"], 150 * 1024**2)
        self.assertEqual(activity["bytes_total"], 400 * 1024**2)
        self.assertAlmostEqual(activity["eta_seconds"], 5, places=3)  # 250 MB left at 50 MB/s
        self.assertEqual(activity["new_files_expected"], 16)

    def test_more_copied_than_expected_stops_at_full(self):
        self.copying(bytes_transferred=500 * 1024**2, expected_new_bytes=400 * 1024**2)
        activity = self.core.state()["activity"]
        self.assertEqual(activity["percent"], 100)
        self.assertIsNone(activity["eta_seconds"])

    def test_no_time_left_is_guessed_while_nothing_has_moved(self):
        self.copying(
            bytes_transferred=0,
            transfer_speed_bps=0,
            expected_new_bytes=400 * 1024**2,
            eta_seconds=999,
        )
        self.assertIsNone(self.core.state()["activity"]["eta_seconds"])

    def test_without_an_estimate_progress_measures_the_whole_card(self):
        for expected in (
            {},
            {"expected_new_files": None, "expected_new_bytes": None},
            {"expected_new_files": 0, "expected_new_bytes": 0},
        ):
            with self.subTest(expected=expected):
                self.copying(**expected)
                activity = self.core.state()["activity"]
                self.assertAlmostEqual(activity["percent"], 25.0)
                self.assertEqual(activity["bytes_total"], 400 * 1024**2)

    def test_a_usb_backup_never_uses_the_card_estimate(self):
        self.copying(
            phase="USB backup", stage="usb", expected_new_files=16, expected_new_bytes=1024**2
        )
        activity = self.core.state()["activity"]
        self.assertEqual(activity["bytes_total"], 400 * 1024**2)
        self.assertIsNone(activity["new_files_expected"])

    def test_camera_index_files_stay_out_of_the_new_file_count(self):
        self.copying(new_files=14, new_media_files=12, expected_new_files=12)
        self.assertEqual(self.core.state()["activity"]["new_files"], 12)

    def test_speed_history_resets_when_the_transfer_ends(self):
        self.copying()
        self.core.state()
        self.importer_up = False
        self.write_status(state="complete")
        self.core.state()
        self.assertEqual(len(self.core._samples), 0)

    def test_a_status_nobody_is_updating_is_reported_as_interrupted_not_as_copying_forever(self):
        self.copying()
        self.importer_up = False
        self.advance(120)  # the status file is now two minutes old and no importer is alive
        state = self.core.state()
        self.assertIsNone(state["activity"])
        self.assertTrue(state["interrupted"])
        self.assertEqual(state["headline"]["tone"], "warning")

    def test_an_interruption_can_be_dismissed(self):
        self.copying()
        self.importer_up = False
        self.advance(120)
        self.core.act("dismiss_last")
        state = self.core.state()
        self.assertFalse(state["interrupted"])
        self.assertIsNone(state["activity"])
        self.assertEqual(state["headline"]["title"], "Ready")

    def test_a_fresh_status_with_a_live_importer_is_not_stale(self):
        self.copying()
        self.advance(5)
        self.assertIsNotNone(self.core.state()["activity"])
        self.assertFalse(self.core.state()["interrupted"])


class TestLastTransferAndHistory(Fixture):
    def test_a_finished_import_is_summarised(self):
        self.record(details={"new": {"JPG": 7, "RAW": 7, "OTHER": 1}, "new_bytes": 280 * 1024**2})
        state = self.core.state()
        last = state["last_transfer"]
        self.assertEqual(last["title"], "Imported from NIKON Z 8")
        # User feedback 2026-11-04: say what this session backed up, then the card total.
        self.assertEqual(
            last["summary"],
            "15 new: 7 photos, 7 RAW, 1 other · 280.0 MB · 214 on card, all backed up",
        )
        self.assertEqual(state["headline"]["tone"], "success")

    def test_an_import_with_nothing_new_says_so(self):
        self.record(copied=0, skipped=214, details={"new": {}, "new_bytes": 0})
        self.assertEqual(
            self.core.history()[0]["summary"], "No new files · 214 on card, all backed up"
        )

    def test_usb_copies_of_the_session_are_counted(self):
        self.record(
            usb=True, details={"new": {"RAW": 2}, "new_bytes": 60 * 1024**2, "usb_copied": 2}
        )
        self.assertEqual(
            self.core.history()[0]["summary"],
            "2 new: 2 RAW · 60.0 MB · 214 on card, all backed up · 2 copied to USB",
        )

    def test_runs_recorded_before_session_details_still_read_sensibly(self):
        self.record(copied=12)
        self.assertEqual(self.core.history()[0]["summary"], "12 new · 214 on card, all backed up")

    def test_successful_results_fade_after_the_timeout(self):
        self.record()
        self.advance(core.LAST_TRANSFER_TTL_SECONDS + 60)
        state = self.core.state()
        self.assertIsNone(state["last_transfer"])
        self.assertEqual(state["headline"]["title"], "Ready")

    def test_failures_do_not_fade_but_can_be_dismissed(self):
        self.record(outcome="failed", failed=2, message="Local SSD import failed")
        self.advance(core.LAST_TRANSFER_TTL_SECONDS * 5)
        state = self.core.state()
        self.assertEqual(state["headline"]["tone"], "error")
        self.assertEqual(state["headline"]["title"], "Import from NIKON Z 8 failed")
        self.assertEqual(state["headline"]["detail"], "Local SSD import failed")
        self.assertIn("failed", state["last_transfer"]["summary"].lower())
        self.core.act("dismiss_last")
        self.assertIsNone(self.core.state()["last_transfer"])

    def test_dismissing_survives_a_restart_and_a_newer_run_reappears(self):
        self.record(outcome="failed", message="boom")
        self.core.act("dismiss_last")
        restarted = core.DashboardCore(
            self.paths,
            clock=lambda: self.now,
            run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""),
            popen=lambda *a, **k: None,
        )
        self.assertIsNone(restarted.state()["last_transfer"])
        self.record(outcome="failed", message="again")
        self.assertIsNotNone(restarted.state()["last_transfer"])

    def test_history_lives_between_restarts_and_lists_newest_first(self):
        self.record(source="A", started_at=1, finished_at=2)
        self.record(source="B", started_at=3, finished_at=4)
        restarted = core.DashboardCore(self.paths)
        titles = [row["title"] for row in restarted.history()]
        self.assertEqual(titles, ["Imported from B", "Imported from A"])

    def test_clear_history_removes_everything_and_the_last_transfer_card(self):
        self.record()
        self.record()
        result = self.core.act("clear_history")
        self.assertTrue(result["ok"])
        self.assertEqual(self.core.history(), [])
        self.assertIsNone(self.core.state()["last_transfer"])

    def test_usb_backup_and_canceled_runs_read_naturally(self):
        self.record(kind="usb_backup", source="", files=0, copied=12, skipped=30, bytes=0)
        self.record(outcome="canceled", message="Operation canceled")
        rows = self.core.history()
        self.assertEqual(rows[0]["title"], "Import from NIKON Z 8 canceled")
        self.assertEqual(rows[0]["tone"], "warning")
        self.assertEqual(rows[1]["summary"], "12 copied · 30 already there")


class TestLogs(Fixture):
    # User question 2026-11-04: the old UI had a way to view logs.
    def setUp(self):
        super().setUp()
        os.environ["PHOTO_OFFLOADER_IMPORT_LOG"] = str(self.install / "import.log")
        os.environ["PHOTO_OFFLOADER_TRIGGER_LOG"] = str(self.install / "trigger.log")

    def test_each_log_opens_in_console(self):
        for log_id, path in (
            ("import", self.install / "import.log"),
            ("trigger", self.install / "trigger.log"),
            ("google", self.install / "google_photos_sync.log"),
        ):
            with self.subTest(log=log_id):
                path.write_text("line\n")
                self.assertTrue(self.core.act("open_log", {"log": log_id})["ok"])
                self.assertEqual(self.spawned[-1], ["/usr/bin/open", "-a", "Console", str(path)])

    def test_only_known_logs_can_be_opened(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "", "catalog"):
            with self.subTest(log=bad):
                self.assertFalse(self.core.act("open_log", {"log": bad})["ok"])
        self.assertEqual(self.spawned, [])

    def test_a_log_that_does_not_exist_yet_says_so(self):
        result = self.core.act("open_log", {"log": "google"})
        self.assertFalse(result["ok"])
        self.assertIn("no", result["message"].lower())
        self.assertEqual(self.spawned, [])


class TestEject(Fixture):
    # User request 2026-11-04: eject the card and the USB drive from their cards.
    def test_a_mounted_card_offers_eject_and_ejects_it(self):
        self.add_card("NIKON Z 8")
        self.assertIn({"id": "eject_card", "label": "Eject"}, self.pipeline("card")["actions"])
        result = self.core.act("eject_card")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.ran[-1], ["/usr/sbin/diskutil", "eject", str(self.volumes / "NIKON Z 8")]
        )

    def test_the_card_cannot_be_ejected_while_it_is_being_imported(self):
        self.add_card("NIKON Z 8")
        self.importer_up = True
        self.write_status(state="copying", phase="copy", stage="copy", bytes_done=1, bytes_total=4)
        self.assertEqual(self.pipeline("card")["actions"], [])
        ran_before = list(self.ran)
        self.assertFalse(self.core.act("eject_card")["ok"])
        self.assertFalse(any("eject" in argv for argv in self.ran[len(ran_before) :]))

    def test_the_usb_drive_can_be_ejected_when_nothing_is_using_it(self):
        self.usb.mkdir()
        self.assertTrue(self.core.act("eject_usb")["ok"])
        self.assertEqual(self.ran[-1], ["/usr/sbin/diskutil", "eject", str(self.usb)])
        self.importer_up = True
        self.write_status(
            state="copying", phase="USB backup", stage="usb", bytes_done=1, bytes_total=4
        )
        self.core._cache.clear()
        self.assertFalse(self.core.act("eject_usb")["ok"])

    def test_nothing_to_eject_says_so(self):
        self.assertFalse(self.core.act("eject_card")["ok"])
        self.assertFalse(self.core.act("eject_usb")["ok"])


class TestRunFlow(Fixture):
    # User feedback 2026-11-04: the panel fell back to the "Imported" screen between
    # copying and uploading; it should stay on the transfer and end with a summary.
    def google_configured(self):
        (self.data / "preferences.json").write_text(
            json.dumps({"google_photos": {"configured": True}})
        )

    def google_status(self, state, seconds_ago):
        stamp = (
            datetime.fromtimestamp(self.now - seconds_ago)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": state, "updated_at": stamp})
        )

    def finished_import(self, seconds_ago=3):
        self.record(
            started_at=self.now - seconds_ago - 95,
            finished_at=self.now - seconds_ago,
            files=2378,
            copied=18,
            details={"new": {"JPG": 9, "RAW": 9}, "new_bytes": 400 * 1024**2},
        )

    def test_the_moment_before_the_upload_starts_stays_in_the_transfer_view(self):
        self.google_configured()
        self.google_status("complete", seconds_ago=600)  # last sync was before this import
        self.finished_import(seconds_ago=2)
        state = self.core.state()
        activity = state["activity"]
        self.assertEqual(
            (activity["kind"], activity["title"]), ("upload", "Starting upload to Google Photos")
        )
        self.assertEqual([s["state"] for s in activity["stages"]][-1], "active")
        self.assertEqual(state["headline"]["tone"], "active")

    def test_the_importers_last_second_stays_in_the_transfer_view(self):
        # Installed 2.5 on 2026-11-04 still showed "card is ready" between copy and upload:
        # status.json said "complete" before the run reached the history table.
        self.google_configured()
        self.add_card("NIKON Z 8")
        (self.install / ".last_camera_source").write_text(
            str(self.volumes / "NIKON Z 8" / "DCIM") + "\n"
        )
        self.record(started_at=self.now - 900, finished_at=self.now - 720)  # the previous import
        self.google_status("complete", seconds_ago=700)
        self.write_status(
            state="complete",
            phase="card-to-ssd",
            stage="copy",
            usb_planned=False,
            source=str(self.volumes / "NIKON Z 8"),
        )
        state = self.core.state()
        self.assertEqual(state["activity"]["title"], "Finishing import")
        self.assertEqual(
            [(s["id"], s["state"]) for s in state["activity"]["stages"]][2:],
            [("copy", "done"), ("usb", "skipped"), ("upload", "pending")],
        )
        self.assertNotIn("already imported", state["headline"]["title"])

    def test_a_finished_run_ends_with_a_summary_in_the_same_panel(self):
        self.google_configured()
        self.finished_import(seconds_ago=60)
        self.google_status("complete", seconds_ago=30)  # uploaded after the import
        state = self.core.state()
        self.assertIsNone(state["activity"])
        finished = state["finished"]
        self.assertEqual(finished["title"], "Imported from NIKON Z 8")
        self.assertEqual(
            [(s["id"], s["state"]) for s in finished["stages"]],
            [
                ("scan", "done"),
                ("metadata", "done"),
                ("copy", "done"),
                ("usb", "skipped"),
                ("upload", "done"),
            ],
        )
        self.assertEqual(
            (finished["percent"], finished["files_done"], finished["files_total"]),
            (100.0, 18, 2378),
        )
        self.assertEqual(finished["elapsed_seconds"], 95)
        self.assertEqual(finished["bytes_done"], 400 * 1024**2)

    def test_the_summary_fades_like_the_result_and_can_be_dismissed(self):
        self.finished_import(seconds_ago=60)
        self.assertIsNotNone(self.core.state()["finished"])
        self.core.act("dismiss_last")
        self.assertIsNone(self.core.state()["finished"])
        self.finished_import(seconds_ago=60)
        self.advance(core.LAST_TRANSFER_TTL_SECONDS + 1)
        self.assertIsNone(self.core.state()["finished"])

    def test_a_failed_upload_marks_its_step(self):
        self.google_configured()
        self.finished_import(seconds_ago=60)
        self.google_status("failed", seconds_ago=30)
        self.assertEqual(self.core.state()["finished"]["stages"][-1]["state"], "error")


class TestEventLog(Fixture):
    # User question 2026-11-04: is there a debug log of what the page showed?
    def events(self):
        log = self.install / "dashboard_events.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_each_change_of_what_the_dashboard_shows_is_logged_once(self):
        self.core.state()
        self.advance(1)
        self.core.state()
        self.assertEqual(len(self.events()), 1)
        self.add_card("NIKON Z 8")
        self.advance(5)
        self.core.state()
        lines = self.events()
        self.assertEqual(len(lines), 2)
        self.assertIn("NIKON Z 8 is mounted", lines[-1])
        self.assertIn("importer=", lines[-1])
        self.assertIn("google=", lines[-1])

    def test_the_dashboard_log_can_be_opened(self):
        self.core.state()
        self.assertTrue(self.core.act("open_log", {"log": "dashboard"})["ok"])
        self.assertEqual(self.spawned[-1][-1], str(self.install / "dashboard_events.log"))


class TestTheme(Fixture):
    # User feedback 2026-11-04: no way to switch light/dark from the dashboard.
    def test_theme_defaults_to_following_the_system(self):
        self.assertEqual(self.core.state()["settings"]["theme"], "system")

    def test_theme_choice_is_saved_and_survives_a_restart(self):
        self.assertTrue(self.core.act("set_theme", {"theme": "dark"})["ok"])
        restarted = core.DashboardCore(self.paths)
        self.assertEqual(restarted.state()["settings"]["theme"], "dark")

    def test_a_preference_that_cannot_be_saved_is_logged(self):
        with (
            patch("os.replace", side_effect=OSError("disk full")),
            self.assertLogs("camera_offloader", level="WARNING") as captured,
        ):
            self.core._save_dashboard_state(theme="dark")
        self.assertIn("disk full", captured.output[0])
        self.assertIn("dashboard_state.json", captured.output[0])

    def test_unknown_theme_is_refused(self):
        self.assertFalse(self.core.act("set_theme", {"theme": "neon"})["ok"])
        self.assertEqual(self.core.state()["settings"]["theme"], "system")


class TestCatalogConnection(Fixture):
    def test_archive_counts_closes_its_connection(self):
        catalog.catalog_connection().close()
        opened = []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        with patch.object(core.sqlite3, "connect", connect):
            self.assertEqual(self.core.archive_counts()["local"], 0)
        self.assertEqual(len(opened), 1)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            opened[0].execute("SELECT 1")


class TestPipeline(Fixture):
    def test_a_fresh_machine_shows_every_step_as_waiting(self):
        states = {step["id"]: step["state"] for step in self.core.state()["pipeline"]}
        self.assertEqual(states, {"card": "off", "archive": "ok", "usb": "off", "cloud": "off"})

    def test_archive_and_usb_counts_come_from_the_catalog(self):
        self.seed_catalog(local=3, usb=2)
        self.usb.mkdir()
        self.assertEqual(self.pipeline("archive")["detail"], "3 files indexed")
        self.assertEqual(self.pipeline("usb")["detail"], "2 files backed up")

    def test_usb_offers_a_backup_only_when_it_is_connected_and_nothing_is_running(self):
        self.assertEqual(self.pipeline("usb")["actions"], [])
        self.usb.mkdir()
        self.advance(10)
        self.assertEqual(self.action_ids("usb"), ["usb_backup", "eject_usb"])
        self.importer_up = True
        self.write_status(state="copying", phase="card-to-ssd", source="/Volumes/X")
        self.advance(10)
        self.assertEqual(self.pipeline("usb")["actions"], [])
        self.assertEqual(self.pipeline("cloud")["actions"], [])

    def test_catalog_maintenance_is_never_a_permanent_button(self):
        self.assertTrue(
            all(
                action["id"] != "update_catalog"
                for step in self.core.state()["pipeline"]
                for action in step["actions"]
            )
        )

    def test_google_photos_not_configured(self):
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["headline"]), ("off", "Not set up"))

    def test_not_set_up_names_the_first_missing_step(self):
        with patch.object(google_photos, "find_rclone", return_value=None):
            self.assertEqual(self.pipeline("cloud")["detail"], "Install rclone to set this up")
        self.advance(60)  # past the cached answer
        with patch.object(google_photos, "find_rclone", return_value="/usr/bin/rclone"):
            self.assertEqual(self.pipeline("cloud")["detail"], "Add an rclone remote named gphotos")

    def test_not_set_up_names_a_custom_remote(self):
        with (
            patch.object(google_photos, "find_rclone", return_value="/usr/bin/rclone"),
            patch.dict(os.environ, {"PHOTO_OFFLOADER_GPHOTOS_REMOTE": "photos-cloud:"}),
        ):
            self.assertEqual(
                self.pipeline("cloud")["detail"], "Add an rclone remote named photos-cloud"
            )

    def test_google_photos_configured_and_idle(self):
        (self.data / "preferences.json").write_text(
            json.dumps({"google_photos": {"configured": True}})
        )
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["actions"][0]["id"]), ("ok", "google_sync"))

    def rclone_config(self, text):
        config = Path(self.tmp.name) / "rclone.conf"
        config.write_text(text)
        self.core.paths = dataclasses.replace(self.paths, rclone_config=config)

    def test_a_configured_rclone_remote_counts_even_after_a_reinstall(self):
        # User report 2026-11-04: after uninstall/reinstall the dashboard asked to set up
        # Google Photos again although rclone still had the gphotos remote.
        self.rclone_config(
            '[other]\ntype = drive\n\n[gphotos]\ntype = google photos\ntoken = {"secret": 1}\n'
        )
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["actions"][0]["id"]), ("ok", "google_sync"))

    def test_another_rclone_remote_alone_is_not_google_photos(self):
        self.rclone_config("[gphotos-old]\ntype = drive\n[drive]\ntype = drive\n")
        self.assertEqual(self.pipeline("cloud")["headline"], "Not set up")

    def test_custom_remote_name_is_honored(self):
        self.rclone_config("[photos-cloud]\ntype = google photos\n")
        with patch.dict(os.environ, {"PHOTO_OFFLOADER_GPHOTOS_REMOTE": "photos-cloud:"}):
            self.assertEqual(self.pipeline("cloud")["state"], "ok")

    def test_default_paths_honor_rclone_config_override(self):
        with patch.dict(os.environ, {"RCLONE_CONFIG": "/tmp/custom-rclone.conf"}):
            self.assertEqual(core.Paths.default().rclone_config, Path("/tmp/custom-rclone.conf"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RCLONE_CONFIG", None)
            self.assertEqual(core.Paths.default().rclone_config, config.RCLONE_CONFIG_PATH)

    def test_google_photos_upload_shows_live_progress_from_the_rclone_log(self):
        self.google_up = True
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "running", "message": "Uploading"})
        )
        (self.install / "google_photos_sync.log").write_text(
            "2026/10/21 22:24:49 INFO  : 4.998 GiB / 17.703 GiB, 28%, 8.387 MiB/s, ETA 25m51s (xfr#649/1520)\n"
        )
        state = self.core.state()
        self.assertEqual(state["upload"]["percent"], 28.0)
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["headline"]), ("active", "Uploading"))
        self.assertEqual(step["detail"], "4.998 GiB of 17.703 GiB")

    def test_an_upload_is_the_headline_when_the_importer_is_idle(self):
        self.google_up = True
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "running"}))
        (self.install / "google_photos_sync.log").write_text(
            "2026/10/21 22:24:49 INFO  : 4.998 GiB / 17.703 GiB, 28%, 8.387 MiB/s, ETA 25m51s (xfr#649/1520)\n"
        )
        state = self.core.state()
        # The progress slots show the bytes; the subtitle says what is being uploaded.
        self.assertEqual(
            state["headline"],
            {
                "tone": "active",
                "title": "Uploading to Google Photos",
                "detail": "649 of 1,520 files",
            },
        )
        activity = state["activity"]
        self.assertEqual(
            (activity["kind"], activity["percent"], activity["eta_seconds"]),
            ("upload", 28.0, 1551.0),
        )
        self.assertAlmostEqual(activity["speed_bps"], 8.387 * 1024**2, delta=10)
        self.assertFalse(state["busy"], "an upload must not block backups or catalog updates")

    def start_upload(self, started_seconds_ago=30, line=None):
        self.google_up = True
        started = (
            datetime.fromtimestamp(self.now - started_seconds_ago)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "running", "updated_at": started})
        )
        (self.install / "google_photos_sync.log").write_text(
            line
            or (
                "2026/11/04 18:50:05 INFO  :   175.183 MiB / 267.950 MiB, 65%, 8.800 MiB/s, ETA 10s (xfr#8/18)\n"
            )
        )

    def test_upload_reports_files_and_elapsed_time(self):
        # User screenshot 2026-11-04: Files and Elapsed were blank during the upload.
        self.start_upload(started_seconds_ago=42)
        activity = self.core.state()["activity"]
        self.assertEqual((activity["files_done"], activity["files_total"]), (8, 18))
        self.assertEqual(activity["elapsed_seconds"], 42)

    def test_upload_speed_is_graphed(self):
        self.start_upload()
        self.core.state()
        for line in (
            "2026/11/04 18:50:10 INFO  :   204.131 MiB / 267.950 MiB, 76%, 8.059 MiB/s, ETA 7s (xfr#12/18)\n",
            "2026/11/04 18:50:15 INFO  :   237.655 MiB / 267.950 MiB, 89%, 7.590 MiB/s, ETA 3s (xfr#12/18)\n",
        ):
            self.advance(5)
            (self.install / "google_photos_sync.log").write_text(line)
            activity = self.core.state()["activity"]
        self.assertEqual(len(activity["speed_history"]), 2)

    def test_upload_right_after_an_import_continues_the_same_steps(self):
        # User feedback 2026-11-04: the upload should be a step of the import, not a new view.
        (self.data / "preferences.json").write_text(
            json.dumps({"google_photos": {"configured": True}})
        )
        self.record(usb=True, started_at=self.now - 200, finished_at=self.now - 60)
        self.start_upload()
        state = self.core.state()
        self.assertEqual(
            [(s["id"], s["state"]) for s in state["activity"]["stages"]],
            [
                ("scan", "done"),
                ("metadata", "done"),
                ("copy", "done"),
                ("usb", "done"),
                ("upload", "active"),
            ],
        )
        self.assertEqual(state["headline"]["detail"], "NIKON Z 8 · 8 of 18 files")

    def test_an_import_lists_the_upload_as_a_later_step_when_google_photos_is_set_up(self):
        self.importer_up = True
        self.write_status(
            state="copying",
            phase="copy",
            stage="copy",
            usb_planned=False,
            bytes_done=1,
            bytes_total=4,
        )
        self.assertEqual(self.core.state()["activity"]["stages"][-1]["state"], "skipped")
        (self.data / "preferences.json").write_text(
            json.dumps({"google_photos": {"configured": True}})
        )
        self.assertEqual(
            self.core.state()["activity"]["stages"][-1],
            {"id": "upload", "label": "Upload to Google Photos", "state": "pending"},
        )

    def test_an_importer_run_takes_precedence_over_an_upload_in_the_hero(self):
        self.google_up = self.importer_up = True
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "running"}))
        self.write_status(
            state="copying", phase="card-to-ssd", source="/Volumes/X", bytes_done=1, bytes_total=4
        )
        state = self.core.state()
        self.assertEqual(state["activity"]["kind"], "import")
        self.assertTrue(state["busy"])
        self.assertEqual(self.pipeline("cloud")["state"], "active")

    def test_an_upload_with_no_live_process_is_interrupted_not_uploading_forever(self):
        self.google_up = False  # sync crashed / was killed; its status still says "running"
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "running"}))
        state = self.core.state()
        self.assertIsNone(state["activity"])
        step = self.pipeline("cloud")
        self.assertEqual(
            (step["state"], step["headline"], step["actions"][0]["label"]),
            ("error", "Upload interrupted", "Resume"),
        )

    def test_a_failed_or_interrupted_upload_is_raised_to_the_headline(self):
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "failed", "message": "rclone exited with status 7"})
        )
        head = self.core.state()["headline"]
        self.assertEqual(
            (head["tone"], head["title"]), ("warning", "Google Photos upload needs attention")
        )
        self.assertEqual(head["detail"], "rclone exited with status 7")

    def test_google_photos_failure_offers_a_retry(self):
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "failed", "message": "rclone exited with status 7"})
        )
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["actions"][0]["label"]), ("error", "Retry"))


class TestActions(Fixture):
    def test_pause_and_resume(self):
        self.assertTrue(self.core.act("pause")["ok"])
        self.assertTrue((self.install / ".paused").exists())
        (self.install / ".last_camera_source").write_text("/Volumes/X/DCIM")
        self.assertTrue(self.core.act("resume")["ok"])
        self.assertFalse((self.install / ".paused").exists())
        self.assertFalse((self.install / ".last_camera_source").exists())
        self.assertEqual(self.spawned[-1][0], "/bin/bash")
        self.assertTrue(self.spawned[-1][1].endswith("scripts/trigger.sh"))

    def test_usb_backup_requires_the_drive(self):
        result = self.core.act("usb_backup")
        self.assertFalse(result["ok"])
        self.assertEqual(self.spawned, [])
        self.usb.mkdir()
        self.core._cache.clear()
        self.assertTrue(self.core.act("usb_backup")["ok"])
        self.assertTrue(self.spawned[-1][1].endswith("photo-offloader-usb-backup"))

    def test_catalog_update_and_google_sync_start_the_right_programs(self):
        self.core.act("update_catalog")
        self.assertEqual(self.spawned[-1][-1], "--update-catalog")
        self.core.act("google_sync")
        self.assertEqual(self.spawned[-1][-1], "--sync")

    def test_google_sync_reports_a_missing_helper(self):
        (self.install / "bin" / "photo-offloader-google-sync").unlink()
        self.assertFalse(self.core.act("google_sync")["ok"])

    def test_settings_toggle_on_and_off(self):
        first = self.core.act("toggle_auto_eject")
        self.assertTrue(first["enabled"])
        self.assertTrue(self.core.state()["settings"]["auto_eject"])
        second = self.core.act("toggle_auto_eject")
        self.assertFalse(second["enabled"])
        self.core.act("toggle_terminal_output")
        self.assertTrue((self.install / ".terminal-output-enabled").exists())

    def test_stop_keeps_the_dashboard_and_menu_bar_alive(self):
        # This is what the menu bar's "Stop" got wrong: it unloaded the menu bar itself,
        # leaving no way to start the service again.
        result = self.core.act("service_stop")
        self.assertTrue(result["ok"])
        self.assertIn("--keep-ui", self.spawned[-1])
        self.assertNotIn("--force", self.spawned[-1])

    def test_stop_asks_before_interrupting_a_transfer(self):
        self.importer_up = True
        result = self.core.act("service_stop")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_choice"])
        self.assertEqual(self.spawned, [])

    def test_stop_after_transfer_and_force_pass_the_right_flags(self):
        self.importer_up = True
        self.core.act("service_stop", {"mode": "wait"})
        self.assertEqual(self.spawned[-1][-2:], ["--keep-ui", "--wait"])
        self.core.act("service_stop", {"mode": "force"})
        self.assertEqual(self.spawned[-1][-2:], ["--keep-ui", "--force"])

    def test_stop_rejects_unknown_modes(self):
        self.assertFalse(self.core.act("service_stop", {"mode": "rm -rf"})["ok"])
        self.assertEqual(self.spawned, [])

    def test_start_runs_the_service_script(self):
        result = self.core.act("service_start")
        self.assertTrue(result["ok"])
        self.assertEqual(self.ran[-1][-1], "start")

    def test_unknown_actions_are_refused(self):
        self.assertFalse(self.core.act("format_disk")["ok"])


class TestFormatting(unittest.TestCase):
    def test_byte_helpers(self):
        self.assertEqual(display.parse_display_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(display.parse_display_bytes("2 KiB"), 2048.0)
        self.assertEqual(display.parse_display_bytes("unknown"), 0.0)
        self.assertEqual(display.format_bytes(512), "512 B")
        self.assertEqual(display.format_bytes(1024), "1.0 KB")


if __name__ == "__main__":
    unittest.main()
