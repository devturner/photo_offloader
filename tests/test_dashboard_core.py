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

from photo_offloader import catalog, config, google_photos, settings
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
        self.rclone_up = False  # an rclone our sync started, perhaps left by a killed sync
        self.verify_up = False
        self.spawned = []
        self.ran = []

        def run(argv, **_kwargs):
            self.ran.append(list(argv))
            if "print" in argv:
                return subprocess.CompletedProcess(argv, 0 if self.service_up else 113, "", "")
            if argv[0].endswith("pgrep"):
                alive = {
                    "photo-offloader-google-sync": self.google_up,
                    ".upload-list-": self.rclone_up,
                    "photo-offloader-verify": self.verify_up,
                }.get(argv[-1].rsplit("/", 1)[-1], self.importer_up)
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

    def seed_files(self, local, usb, usb_indexed_at=1.0):
        """Catalog rows by {relative path: size}, so the Mac and the drive can share names."""
        catalog.catalog_connection().close()
        rows = [
            (f"m/{name}", str(self.paths.local_archives[0].resolve()), name, size, 1.0)
            for name, size in local.items()
        ] + [
            (f"u/{name}", str(self.paths.usb_archives[0].resolve()), name, size, usb_indexed_at)
            for name, size in usb.items()
        ]
        with sqlite3.connect(self.data / "catalog.sqlite3") as db:
            db.executemany(
                "INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode,"
                " indexed_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
                rows,
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

    def usb_failed_run(self, **details):
        return {
            "kind": "import",
            "outcome": "failed",
            "usb": True,
            "message": "Local import succeeded; USB backup has 1 failure(s)",
            "finished_at": 1000.0,
            "details": details,
        }

    def test_an_upload_after_a_usb_failure_shows_on_the_import_it_followed(self):
        google = {"configured": True, "state": "running", "updated_at": 1001.0}
        run = self.usb_failed_run(local_verified=True)
        self.assertEqual(self.core._upload_state_after(run, google, 1002.0), "active")
        stages = {
            stage["id"]: stage["state"] for stage in display.stages_after_import(run, "active")
        }
        self.assertEqual(stages["upload"], "active")

    def test_the_panel_shows_an_upload_starting_after_a_usb_failure(self):
        google = {"configured": True, "state": "idle", "updated_at": None, "progress": None}
        run = self.usb_failed_run(local_verified=True)
        run["source"] = "NIKON Z 8"
        self.assertIsNotNone(self.core._upload_activity(google, run, 1002.0))

    def test_an_import_not_verified_on_the_mac_shows_no_upload(self):
        google = {"configured": True, "state": "running", "updated_at": 1001.0}
        run = self.usb_failed_run()
        self.assertEqual(self.core._upload_state_after(run, google, 1002.0), "skipped")
        stages = {
            stage["id"]: stage["state"] for stage in display.stages_after_import(run, "active")
        }
        self.assertEqual(stages["upload"], "skipped")

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

    def set_card_aside(self, name="NIKON Z 8"):
        (self.install / ".last_camera_source.parked").write_text(
            f"{self.volumes / name / 'DCIM'}\t\n"
        )

    def test_a_card_set_aside_for_a_second_card_counts_as_handled(self):
        self.add_card()
        self.set_card_aside()
        self.assertTrue(self.core.card_handled(self.volumes / "NIKON Z 8"))

    def test_a_mounted_card_nobody_has_handled_is_not_handled(self):
        self.add_card()
        self.set_card_aside("ANOTHER CARD")
        self.assertFalse(self.core.card_handled(self.volumes / "NIKON Z 8"))

    def test_import_now_also_forgets_a_card_set_aside(self):
        self.add_card()
        self.set_card_aside()
        self.core.act("import_card")
        self.assertFalse((self.install / ".last_camera_source.parked").exists())

    def test_resume_also_forgets_a_card_set_aside(self):
        self.add_card()
        self.set_card_aside()
        self.core.act("resume")
        self.assertFalse((self.install / ".last_camera_source.parked").exists())

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

    def test_the_mac_archive_box_agrees_with_the_panel_about_checking_or_copying(self):
        # User feedback 2026-11-06: the box said Copying while the panel said it was still checking.
        self.checking(new_files=0)
        self.assertEqual(self.core.state()["activity"]["title"], "Checking for new files")
        self.assertEqual(self.pipeline("archive")["headline"], "Checking")
        # Once a file has been copied, both hold still on Copying between files.
        self.checking(new_files=3)
        self.assertEqual(self.core.state()["activity"]["title"], "Copying new files")
        self.assertEqual(self.pipeline("archive")["headline"], "Copying")

    def test_a_running_import_with_no_progress_says_so(self):
        self.write_status(state="copying", phase="copy", current_file="DSC_0001.NEF")
        with patch.object(self.core, "importer_running", return_value=True):
            self.advance(core.STALLED_STATUS_SECONDS - 1)
            self.assertIn("DSC_0001", self.core.state()["activity"]["detail"])
            self.advance(1)
            self.assertIn("No progress for 5 min", self.core.state()["activity"]["detail"])

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

    def test_the_graph_starts_when_bytes_start_moving(self):
        for _ in range(4):  # checking files before the first copy: nothing has moved
            self.copying()
            self.assertEqual(self.core.state()["activity"]["speed_history"], [])
            self.advance(1)
        self.copying(bytes_transferred=150 * 1024**2)
        history = self.core.state()["activity"]["speed_history"]
        self.assertEqual(history, [50 * 1024**2])

    def test_a_stall_after_the_start_stays_on_the_graph(self):
        moved = 100 * 1024**2
        self.copying(bytes_transferred=moved)
        self.core.state()
        for speed in (10, 10, 0, 0, 10):
            self.advance(1)
            moved += speed * 1024**2
            self.copying(bytes_transferred=moved)
            history = self.core.state()["activity"]["speed_history"]
        self.assertEqual([round(s / 1024**2) for s in history], [10, 10, 0, 0, 10])

    def test_the_graph_keeps_ninety_seconds_to_show_and_the_speed_still_uses_the_last_ten_seconds(
        self,
    ):
        moved = 100 * 1024**2
        self.copying(bytes_transferred=moved)
        self.core.state()
        for second in range(400):
            self.advance(1)
            moved += (1 if second < 390 else 5) * 1024**2
            self.copying(bytes_transferred=moved)
            activity = self.core.state()["activity"]
        self.assertEqual(len(activity["speed_history"]), core.SPEED_SAMPLES)
        self.assertGreater(core.SPEED_SAMPLES, 90, "extra samples let the smoothing settle first")
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
                        "title": "Copying new files",
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


class TestSettingsFile(Fixture):
    """Edit settings… opens settings.txt; the dashboard follows it and says when it's broken."""

    def settings_file(self):
        return self.data / "settings.txt"

    def test_edit_settings_creates_the_file_with_the_current_values_and_opens_it(self):
        result = self.core.act("edit_settings")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            settings.load(self.settings_file(), config.HOME), settings.defaults(config.HOME)
        )
        self.assertEqual(self.spawned, [["/usr/bin/open", "-t", str(self.settings_file())]])

    def test_edit_settings_never_rewrites_a_file_someone_edited(self):
        self.settings_file().write_text("backup_drive = Mine\n", encoding="utf-8")
        self.assertTrue(self.core.act("edit_settings")["ok"])
        self.assertEqual(self.settings_file().read_text(encoding="utf-8"), "backup_drive = Mine\n")
        self.assertEqual(self.spawned, [["/usr/bin/open", "-t", str(self.settings_file())]])

    def test_the_state_says_what_is_wrong_with_the_file(self):
        self.assertIsNone(self.core.state()["settings_problem"])
        self.settings_file().write_text("backup_drive = /Volumes/X\n", encoding="utf-8")
        self.advance(5)
        self.assertIn("Line 1", self.core.state()["settings_problem"])

    def test_a_dashboard_on_the_installed_paths_follows_an_edit(self):
        installed = core.DashboardCore(popen=lambda argv, **kw: None)
        self.settings_file().write_text(
            "backup_drive = Backup 2\narchive_folder = ~/Elsewhere\n", encoding="utf-8"
        )
        with patch.object(core.Paths, "default", side_effect=AssertionError("rebuilt")):
            installed.follow_settings()
        self.assertEqual(installed.paths.usb_volume, Path("/Volumes/Backup 2"))
        self.assertEqual(installed.paths.usb_archives, (Path("/Volumes/Backup 2/CameraImports"),))
        self.assertEqual(installed.paths.local_archives[0], config.HOME / "Elsewhere")
        self.assertEqual(config.USB_DEST_VOLUME, Path("/Volumes/Backup 2"))

    def test_a_dashboard_on_given_paths_keeps_them(self):
        self.settings_file().write_text("backup_drive = Backup 2\n", encoding="utf-8")
        self.core.follow_settings()
        self.assertEqual(self.core.paths, self.paths)


class TestCards(Fixture):
    """The cards row: each card seen, and whether it is safe to format."""

    SD = {"kind": "SD", "size_bytes": 63_864_569_856, "maker": "SanDisk", "serial": "0x1"}
    CF = {"kind": "CFexpress", "size_bytes": 256_060_514_304}

    def card_manifest(self, card_id, files, identity, last_seen="2026-11-10T16:41:19Z"):
        folder = self.data / "card-manifests"
        folder.mkdir(exist_ok=True)
        history = {"identity": identity, "volume_name": "NIKON Z 8  ", "last_seen": last_seen}
        history["first_seen"] = "2026-10-21T10:00:00Z"
        payload = {
            "files": {name: {"sha256": digest} for name, digest in files.items()},
            "history": history,
        }
        (folder / f"{card_id}.json").write_text(json.dumps(payload))

    def saved(self, mac=(), usb=()):
        catalog.catalog_connection().close()
        rows = [(f"m/{d}", self.paths.local_archives[0], d) for d in mac]
        rows += [(f"u/{d}", self.paths.usb_archives[0], d) for d in usb]
        with sqlite3.connect(self.data / "catalog.sqlite3") as db:
            db.executemany(
                "INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode,"
                " sha256, indexed_at) VALUES (?, ?, ?, 1, 1, 1, ?, 1)",
                [(path, str(root.resolve()), f"x/{digest}", digest) for path, root, digest in rows],
            )

    def cards(self):
        self.core._cache.clear()
        return self.core.state()["cards"]

    def test_no_cards_seen_gives_an_empty_row(self):
        self.assertEqual(self.cards(), [])

    def test_a_safe_card_says_so_with_the_date(self):
        self.card_manifest("A", {"a": "h1", "b": "h2"}, self.CF)
        self.saved(mac=("h1", "h2"), usb=("h1", "h2"))
        (self.data / "card_safety.json").write_text(
            json.dumps({"A": datetime(2026, 11, 17, 21, 14).timestamp()})
        )
        (card,) = self.cards()
        self.assertEqual(card["title"], "NIKON Z 8 · 256 GB CFexpress")
        self.assertEqual(
            (card["tone"], card["status"]), ("success", "Safe to format · since Nov 17")
        )
        self.assertEqual(card["report"]["headline"], "Safe to format since Nov 17, 9:14 PM")
        self.assertIn("All 2 files on this card", card["report"]["text"])
        self.assertEqual(card["report"]["seen"], "First seen Oct 21 · last inserted Nov 10")

    def test_a_card_missing_from_the_drive_says_how_many(self):
        self.card_manifest("A", {"a": "h1", "b": "h2", "c": "h3"}, self.SD)
        self.saved(mac=("h1", "h2", "h3"), usb=("h1",))
        (card,) = self.cards()
        self.assertEqual(card["title"], "NIKON Z 8 · 64 GB SanDisk SD")
        self.assertEqual((card["tone"], card["status"]), ("warning", "Don't format · 2 not on USB"))
        self.assertEqual(card["report"]["headline"], "Don't format this card yet")
        self.assertIn("2 files aren't on the USB drive", card["report"]["text"])
        self.assertEqual(card["report"]["files"], ["b", "c"])

    def test_files_missing_from_the_mac_come_first(self):
        self.card_manifest("A", {"a": "h1"}, self.SD)
        self.assertEqual(self.cards()[0]["status"], "Don't format · 1 not on the Mac")

    def test_a_renamed_card_keeps_its_name_after_a_format_when_it_has_a_serial(self):
        self.card_manifest("A", {"a": "h1"}, self.SD)
        self.assertTrue(self.core.act("rename_card", {"id": "A", "name": "Red SanDisk"})["ok"])
        self.assertEqual(self.cards()[0]["title"], "Red SanDisk")
        (self.data / "card-manifests" / "A.json").unlink()
        self.card_manifest("B", {}, self.SD)  # the same card, formatted
        self.assertEqual(self.cards()[0]["title"], "Red SanDisk")
        self.core.act("rename_card", {"id": "B", "name": "  "})
        self.assertEqual(self.cards()[0]["title"], "NIKON Z 8 · 64 GB SanDisk SD")

    def test_i_formatted_it_hides_a_card_until_it_is_seen_again(self):
        self.card_manifest("A", {"a": "h1"}, self.CF)
        self.assertTrue(self.core.act("forget_card", {"id": "A"})["ok"])
        self.assertEqual(self.cards(), [])
        self.advance(60)
        self.card_manifest(
            "A",
            {"a": "h1"},
            self.CF,
            last_seen=datetime.utcfromtimestamp(self.now).isoformat() + "Z",
        )
        self.assertEqual(len(self.cards()), 1)

    def test_an_unknown_card_cannot_be_renamed_or_forgotten(self):
        self.assertFalse(self.core.act("rename_card", {"id": "../x", "name": "a"})["ok"])
        self.assertFalse(self.core.act("forget_card", {"id": "nope"})["ok"])

    def test_every_inserted_card_is_marked(self):
        # The user's Mac, 2026-11-11: two cards from one camera, mounted as "NIKON Z 8  " and
        # "NIKON Z 8   1".
        self.card_manifest("A", {"a": "h1"}, self.CF)
        self.card_manifest("B", {"b": "h2"}, self.SD)
        self.add_card("NIKON Z 8  ")
        self.add_card("NIKON Z 8   1")
        ids = {"NIKON Z 8  ": "B", "NIKON Z 8   1": "A"}
        with patch.object(self.core, "_volume_id", side_effect=lambda volume: ids[volume.name]):
            self.assertEqual([c["mounted"] for c in self.cards()], [True, True])

    def test_an_inserted_card_imported_before_this_version_is_described_by_macos(self):
        self.card_manifest("A", {"a": "h1"}, {})
        manifest = self.data / "card-manifests" / "A.json"
        payload = json.loads(manifest.read_text())
        del payload["history"]["volume_name"]
        manifest.write_text(json.dumps(payload))
        self.add_card("NIKON Z 8   1")
        live = {**self.CF, "volume_name": "NIKON Z 8  "}
        with (
            patch.object(self.core, "_volume_id", return_value="A"),
            patch.object(core.card_identity, "read", return_value=live) as read,
        ):
            self.assertEqual(self.cards()[0]["title"], "NIKON Z 8 · 256 GB CFexpress")
            self.now += 5  # the next polls, past every short-lived cache
            self.core.state()
        self.assertEqual(read.call_count, 1, "asked once, then kept")

    def test_the_end_of_an_import_says_whether_that_card_is_safe_to_format(self):
        self.card_manifest("A", {"a": "h1"}, self.SD)
        self.saved(mac=("h1",))
        self.record(details={"card_id": "A", "new": {"RAW": 1}})
        state = self.core.state()
        detail = state["finished"]["detail"]
        self.assertTrue(detail.startswith("Don't format the card yet · 1 new: 1 RAW"), detail)
        self.assertNotIn("all backed up", detail)
        self.assertEqual(state["headline"]["detail"], detail)  # what the panel shows
        self.saved(usb=("h1",))
        self.advance(1)
        self.assertTrue(self.core.state()["finished"]["detail"].startswith("Safe to format · "))

    def test_an_import_from_an_unknown_card_says_nothing_about_formatting(self):
        self.record(details={"card_id": "GONE"})
        self.assertNotIn("format", self.core.state()["finished"]["detail"])

    def test_the_mounted_card_is_marked_and_its_box_says_whether_to_format(self):
        self.card_manifest("A", {"a": "h1"}, self.CF)
        self.card_manifest("B", {"b": "h2"}, self.SD)
        self.saved(mac=("h1", "h2"), usb=("h1",))
        self.add_card()
        with patch.object(self.core, "_volume_id", return_value="B"):
            cards = self.cards()
            box = self.pipeline("card")
        self.assertEqual([c["mounted"] for c in cards], [False, True])
        self.assertEqual(box["detail"], "Don't format · 1 not on USB")
        self.assertEqual(box["state"], "warn")


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

    REAL_RUN = {
        "new": {"JPG": 18, "RAW": 18, "OTHER": 2},
        "new_bytes": 840 * 1024**2,
        "copy_seconds": 10.0,
        "upload": {
            "outcome": "complete",
            "files": 18,
            "bytes": 450 * 1024**2,
            "seconds": 45.0,
            "album": "Photo Offloader",
        },
    }

    def report(self, details, **record):
        """The report of one run, as Recent activity gets it, with Google Photos done since."""
        self.google_configured()
        self.google_status("complete", seconds_ago=50)
        self.record(
            files=2574,
            copied=38,
            skipped=2536,
            bytes=60 * 1024**3,
            started_at=self.now - 112,
            finished_at=self.now - 100,
            details=details,
            **record,
        )
        return self.core.history()[0]["report"]

    def step(self, report, step_id):
        return next(step for step in report["steps"] if step["id"] == step_id)

    def test_a_report_has_a_row_for_each_step_of_the_run(self):
        report = self.report(self.REAL_RUN)
        self.assertEqual(
            [(s["id"], s["state"], s["result"], s["bytes"], s["seconds"]) for s in report["steps"]],
            [
                ("scan", "done", "2,574 on card · 2,536 already saved", 60 * 1024**3, 2.0),
                ("copy", "done", "38 new: 18 photos, 18 RAW, 2 other", 840 * 1024**2, 10.0),
                ("usb", "skipped", "Skipped", None, None),
                ("upload", "done", "18 files to Photo Offloader", 450 * 1024**2, 45.0),
            ],
        )
        self.assertEqual((report["total_seconds"], report["total_note"]), (57.0, "Start to finish"))

    def test_speed_is_over_the_time_of_that_step_alone(self):
        report = self.report(self.REAL_RUN)
        self.assertAlmostEqual(self.step(report, "copy")["speed_bps"], 84 * 1024**2)
        self.assertAlmostEqual(self.step(report, "upload")["speed_bps"], 10 * 1024**2)
        self.assertIsNone(self.step(report, "scan")["speed_bps"])

    def copy_labels(self, new, **record):
        report = self.report({**self.REAL_RUN, "new": new}, **record)
        segment = next(seg for seg in report["segments"] if seg["id"] == "copy")
        return self.step(report, "copy")["label"], segment["label"]

    def test_a_run_that_copied_nothing_says_it_checked(self):
        # User feedback 2026-11-06: "Copy 20s" on a card with no new files.
        self.assertEqual(self.copy_labels({}), ("Check for new files", "Check"))

    def test_a_run_that_copied_files_says_it_copied(self):
        self.assertEqual(self.copy_labels({"JPG": 1}), ("Copy to Mac", "Copy"))

    def test_a_failed_copy_still_says_copy_when_nothing_got_through(self):
        self.assertEqual(self.copy_labels({}, outcome="failed", failed=3), ("Copy to Mac", "Copy"))

    def test_a_report_lists_where_the_time_went(self):
        self.assertEqual(
            self.report(self.REAL_RUN)["segments"],
            [
                {"id": "scan", "label": "Scan", "seconds": 2.0},
                {"id": "copy", "label": "Copy", "seconds": 10.0},
                {"id": "upload", "label": "Upload", "seconds": 45.0},
            ],
        )

    def test_a_usb_backup_is_timed_and_measured(self):
        report = self.report(
            {**self.REAL_RUN, "usb_copied": 38, "usb_seconds": 4.0, "usb_bytes": 400}, usb=True
        )
        usb = self.step(report, "usb")
        self.assertEqual(
            (usb["state"], usb["result"], usb["files"], usb["bytes"], usb["seconds"]),
            ("done", "38 files copied", 38, 400, 4.0),
        )
        self.assertEqual(self.step(report, "scan")["seconds"], 0.0, "never below zero")

    def test_a_run_from_before_times_were_saved_still_has_a_report(self):
        report = self.report({"new": {"JPG": 1}, "new_bytes": 5})
        self.assertIsNone(self.step(report, "copy")["seconds"])
        self.assertIsNone(self.step(report, "copy")["speed_bps"])
        self.assertIsNone(self.step(report, "scan")["seconds"], "the copy's share isn't known")
        self.assertEqual(self.step(report, "upload")["result"], "Not recorded")
        self.assertEqual(report["segments"], [{"id": "import", "label": "Import", "seconds": 12.0}])
        self.assertEqual((report["total_seconds"], report["total_note"]), (12.0, "Import only"))

    def test_a_failed_upload_says_it_stopped(self):
        upload = {**self.REAL_RUN["upload"], "outcome": "failed", "seconds": 7.0}
        step = self.step(self.report({**self.REAL_RUN, "upload": upload}), "upload")
        self.assertEqual(
            (step["state"], step["result"], step["seconds"]), ("error", "Stopped", 7.0)
        )
        self.assertIsNone(step["bytes"], "what it set out to send isn't what got there")

    def upload_step(self, outcome, files):
        upload = {"outcome": outcome, "files": files, "bytes": 300, "seconds": 7.0}
        return self.step(self.report({**self.REAL_RUN, "upload": upload}), "upload")

    def test_a_paused_upload_says_how_far_it_got(self):
        step = self.upload_step("paused", 3)
        self.assertEqual(
            (step["state"], step["result"], step["files"], step["bytes"]),
            ("skipped", "Paused after 3 files", 3, 300),
        )

    def test_a_stopped_upload_says_how_far_it_got(self):
        step = self.upload_step("stopped", 1)
        self.assertEqual(
            (step["state"], step["result"], step["files"]), ("error", "Stopped after 1 file", 1)
        )
        self.assertEqual(self.upload_step("stopped", 0)["result"], "Stopped")

    def test_an_import_with_nothing_for_google_photos_says_so(self):
        report = self.report({"new": {"RAW": 3}, "new_bytes": 5, "copy_seconds": 1.0})
        self.assertEqual(self.step(report, "upload")["result"], "Nothing to send")

    def test_a_failed_copy_does_not_claim_later_steps(self):
        report = self.report(
            {"new": {"JPG": 1}, "new_bytes": 5, "copy_seconds": 1.0},
            outcome="failed",
            failed=2,
            message="Mac disk full",
        )
        self.assertEqual(self.step(report, "copy")["state"], "error")
        self.assertEqual(self.step(report, "copy")["result"], "1 new: 1 photo · 2 failed")
        self.assertEqual(self.step(report, "usb")["result"], "Skipped")
        self.assertEqual(self.step(report, "upload")["result"], "Skipped")
        self.assertEqual(report["total_note"], "Start to finish")

    def test_the_newest_import_reports_an_upload_still_running(self):
        self.record(
            finished_at=self.now - 10, started_at=self.now - 20, details={"new": {"JPG": 1}}
        )
        self.google_up = True
        self.google_status("running", seconds_ago=5)
        report = self.core.history()[0]["report"]
        self.assertEqual(self.step(report, "upload")["result"], "Uploading now")
        self.assertEqual(report["total_note"], "Import only")

    def test_an_older_import_does_not_report_todays_upload_as_its_own(self):
        self.record(finished_at=self.now - 500, details={"new": {"JPG": 1}})
        self.record(finished_at=self.now - 10, details={"new": {"JPG": 1}})
        self.google_up = True
        self.google_status("running", seconds_ago=5)
        self.assertEqual(
            self.step(self.core.history()[1]["report"], "upload")["result"], "Not recorded"
        )

    def test_a_usb_backup_run_has_just_its_own_step(self):
        self.record(kind="usb_backup", copied=3, skipped=1, usb=True, bytes=9, details={})
        report = self.core.history()[0]["report"]
        self.assertEqual([s["id"] for s in report["steps"]], ["usb"])
        self.assertEqual(report["steps"][0]["seconds"], 55.0)
        self.assertEqual(
            report["segments"], [{"id": "usb", "label": "USB backup", "seconds": 55.0}]
        )

    def test_a_catalog_update_has_a_report_too(self):
        self.record(kind="catalog", details={})
        report = self.core.history()[0]["report"]
        self.assertEqual(
            [(s["label"], s["seconds"]) for s in report["steps"]], [("Update file catalog", 55.0)]
        )

    def test_the_report_style_defaults_to_basic_and_is_kept(self):
        self.assertEqual(self.core.state()["settings"]["report_style"], "basic")
        self.assertTrue(self.core.act("set_report_style", {"report_style": "advanced"})["ok"])
        self.assertEqual(self.core.state()["settings"]["report_style"], "advanced")

    def test_a_saved_report_style_that_is_not_known_falls_back_to_the_default(self):
        self.core._save_dashboard_state(report_style="fancy")
        self.assertEqual(self.core.state()["settings"]["report_style"], "basic")

    def test_an_unknown_report_style_is_refused(self):
        self.assertFalse(self.core.act("set_report_style", {"report_style": "fancy"})["ok"])
        self.assertEqual(self.core.state()["settings"]["report_style"], "basic")

    def test_a_finished_run_ends_with_a_summary_in_the_same_panel(self):
        self.google_configured()
        self.finished_import(seconds_ago=60)
        self.google_status("complete", seconds_ago=30)  # uploaded after the import
        state = self.core.state()
        self.assertIsNone(state["activity"])
        finished = state["finished"]
        self.assertEqual(finished["title"], "Imported from NIKON Z 8")
        self.assertEqual(
            [(s["id"], s["state"]) for s in finished["steps"]],
            [("scan", "done"), ("copy", "done"), ("usb", "skipped"), ("upload", "done")],
        )
        self.assertEqual(finished["total_seconds"], 95)

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
        self.assertEqual(self.core.state()["finished"]["steps"][-1]["state"], "error")


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

    def mix(self, step_id):
        step = self.pipeline(step_id)
        return [(m["label"], m["count"], m["more"]) for m in step["mix"]], step["mix_stale"]

    def test_archive_and_usb_counts_come_from_the_catalog(self):
        self.seed_files(local={"a": 1, "b": 1, "c": 1}, usb={"a": 1, "b": 1, "c": 1})
        self.usb.mkdir()
        self.assertEqual(self.pipeline("archive")["headline"], "3 files")
        self.assertEqual(self.pipeline("usb")["headline"], "3 files")
        self.assertEqual(self.pipeline("usb")["state"], "ok")

    def test_each_card_counts_jpg_raw_and_video(self):
        # Chosen by the user 2026-11-10 (option B): the total, then a column for each type.
        files = {"a.JPG": 1, "b.jpeg": 1, "c.NEF": 1, "d.mov": 1, "NC_FLLST.DAT": 1}
        self.seed_files(local=files, usb=files)
        self.usb.mkdir()
        self.assertEqual(self.pipeline("archive")["headline"], "5 files", "other files count too")
        self.assertEqual(
            self.mix("archive"),
            ([("JPG", 2, False), ("RAW", 1, False), ("Video", 1, False)], False),
        )
        self.assertEqual(self.mix("usb")[0], self.mix("archive")[0])
        self.assertEqual(self.pipeline("archive")["detail"], "")

    def test_the_drive_marks_a_type_it_holds_more_of_than_the_mac(self):
        # RAW files deleted from the Mac to save space are still on the drive.
        self.seed_files(local={"a.jpg": 1, "b.nef": 1}, usb={"a.jpg": 1, "b.nef": 1, "c.nef": 1})
        self.usb.mkdir()
        self.assertEqual(
            self.mix("usb")[0], [("JPG", 1, False), ("RAW", 2, True), ("Video", 0, False)]
        )

    def test_a_connected_drive_says_how_far_behind_it_is(self):
        self.seed_files(local={"a": 1, "b": 1024, "c": 2048}, usb={"a": 1})
        self.usb.mkdir()
        step = self.pipeline("usb")
        self.assertEqual(step["state"], "warn")
        self.assertEqual(step["headline"], "2 files behind")
        self.assertEqual(step["detail"], "", "the columns take its place")
        self.assertEqual(self.action_ids("usb"), ["usb_backup", "eject_usb"])

    def test_one_file_behind_reads_naturally(self):
        self.seed_files(local={"a": 1, "b": 2048}, usb={"a": 1})
        self.usb.mkdir()
        self.assertEqual(self.pipeline("usb")["headline"], "1 file behind")

    def test_extra_files_on_the_drive_do_not_hide_what_is_missing(self):
        self.seed_files(local={"a": 1, "b": 1}, usb={"a": 1, "x": 1, "y": 1})
        self.usb.mkdir()
        self.assertEqual(self.pipeline("usb")["headline"], "1 file behind")

    def backed_up_then_imported(self, imports):
        self.record(
            kind="usb_backup", usb=True, started_at=self.now - 900, finished_at=self.now - 890
        )
        for index in range(imports):
            self.record(
                usb=False, started_at=self.now - 500 + index, finished_at=self.now - 400 + index
            )

    def test_an_unplugged_drive_counts_the_imports_it_missed(self):
        self.seed_files(local={"a.jpg": 1, "b.jpg": 2048}, usb={"a.jpg": 1})
        self.backed_up_then_imported(2)
        step = self.pipeline("usb")
        self.assertEqual(
            (step["state"], step["headline"], step["detail"]),
            ("warn", "Not connected", "2 missed imports"),
        )
        self.assertEqual(
            self.mix("usb"), ([("JPG", 1, False), ("RAW", 0, False), ("Video", 0, False)], True)
        )
        self.assertEqual(step["actions"], [])

    def test_imports_before_the_last_backup_are_not_missed(self):
        self.seed_files(local={"a.jpg": 1}, usb={"a.jpg": 1})
        self.record(usb=False, started_at=self.now - 3000, finished_at=self.now - 2900)
        self.backed_up_then_imported(0)
        step = self.pipeline("usb")
        self.assertEqual((step["state"], step["detail"]), ("off", ""))

    def test_one_missed_import_reads_naturally(self):
        self.seed_files(local={"a.jpg": 1, "b.jpg": 1}, usb={"a.jpg": 1})
        self.backed_up_then_imported(1)
        self.assertEqual(self.pipeline("usb")["detail"], "1 missed import")

    def test_an_import_that_backed_up_as_it_went_is_not_missed(self):
        self.seed_files(local={"a.jpg": 1}, usb={"a.jpg": 1})
        self.backed_up_then_imported(0)
        self.record(usb=True, started_at=self.now - 100, finished_at=self.now - 90)
        step = self.pipeline("usb")
        self.assertEqual((step["state"], step["detail"]), ("off", ""))
        self.assertTrue(step["mix_stale"])

    def test_an_import_whose_usb_copy_failed_is_missed(self):
        self.seed_files(local={"a.jpg": 1, "b.jpg": 1}, usb={"a.jpg": 1})
        self.backed_up_then_imported(0)
        self.record(
            usb=True,
            outcome="failed",
            message="USB backup failed: the drive was removed",
            details={"local_verified": True},
        )
        self.assertEqual(self.pipeline("usb")["detail"], "1 missed import")

    def test_someone_who_has_never_used_a_drive_is_not_warned(self):
        self.seed_files(local={"a": 1}, usb={})
        step = self.pipeline("usb")
        self.assertEqual(step["state"], "off")
        self.assertEqual(step["detail"], "Plug in the backup drive")

    def test_a_first_backup_to_a_connected_drive_shows_everything_as_waiting(self):
        self.seed_files(local={"a": 1, "b": 1}, usb={})
        self.usb.mkdir()
        self.assertEqual(self.pipeline("usb")["headline"], "2 files behind")

    def paused_backup(self, importing=True):
        self.usb.mkdir(exist_ok=True)
        self.paths.usb_resume_file.write_text("DRIVE-1\n140\n475\n")
        if importing:
            self.importer_up = True
            self.write_status(state="copying", phase="card-to-ssd", source="/Volumes/X")

    def test_a_backup_paused_for_a_card_says_so_in_the_box_and_the_panel(self):
        self.paused_backup()
        step = self.pipeline("usb")
        self.assertEqual(
            (step["headline"], step["detail"]), ("Paused for card", "140 of 475 files copied")
        )
        headline = self.core.state()["headline"]
        self.assertEqual(headline["title"], "Importing your card first")
        self.assertEqual(
            headline["detail"],
            "USB backup paused at 140 of 475 files. It carries on by itself after this import.",
        )

    def test_a_paused_backup_with_no_import_running_says_it_will_carry_on(self):
        self.paused_backup(importing=False)
        self.assertEqual(self.pipeline("usb")["detail"], "Carries on in a moment")

    def test_a_paused_backup_with_the_drive_unplugged_shows_the_drive_as_unplugged(self):
        self.paused_backup(importing=False)
        self.usb.rmdir()
        self.advance(10)
        self.assertEqual(self.pipeline("usb")["headline"], "Not connected")

    def test_with_the_option_on_a_drive_waits_for_the_import(self):
        self.usb.mkdir()
        self.core.act("toggle_auto_usb_backup")
        self.importer_up = True
        self.write_status(state="copying", phase="card-to-ssd", source="/Volumes/X")
        step = self.pipeline("usb")
        self.assertEqual(
            (step["headline"], step["detail"]), ("Waiting", "Backs up after this import")
        )

    def test_the_automatic_backup_option_is_off_until_switched_on(self):
        self.assertFalse(self.core.state()["settings"]["auto_usb_backup"])
        self.core.act("toggle_auto_usb_backup")
        self.assertTrue(self.core.state()["settings"]["auto_usb_backup"])
        self.assertTrue(self.paths.auto_usb_backup_file.exists())

    def backing_up(self):
        self.usb.mkdir(exist_ok=True)
        self.importer_up = True
        self.write_status(
            state="copying",
            phase="USB backup",
            stage="usb",
            source="/Users/a",
            bytes_done=197 * 1024 * 1024,
            bytes_total=340 * 1024 * 1024,
            files_done=7,
            files_total=12,
            elapsed_seconds=20,
            eta_seconds=6,
        )

    def test_a_lone_backup_has_the_ordinary_panel(self):
        self.backing_up()
        self.assertNotIn("lanes", self.core.state()["activity"])

    def test_a_backup_and_an_upload_together_get_a_row_each(self):
        self.backing_up()
        progress = {
            "transferred": "41 MB",
            "total": "90 MB",
            "percent": 46,
            "speed": "7.0 MB/s",
            "eta": "7s",
            "files_done": 3,
            "files_total": 8,
            "finalizing": False,
        }
        google = {
            "state": "running",
            "progress": progress,
            "configured": True,
            "started_at": self.now - 10,
            "updated_at": self.now,
            "message": "",
        }
        with patch.object(self.core, "_google", return_value=google):
            state = self.core.state()
        self.assertEqual(state["headline"]["title"], "Backing up and uploading")
        backup, upload = state["activity"]["lanes"]
        self.assertEqual((backup["label"], upload["label"]), ("USB backup", "Google Photos"))
        self.assertEqual(backup["left"], "7 of 12 files · 197.0 MB of 340.0 MB")
        self.assertTrue(backup["right"].startswith("About 6 s left"))
        self.assertEqual(upload["left"], "3 of 8 files · 41 MB of 90 MB")
        self.assertEqual(upload["right"], "About 7 s left · 7.0 MB/s")
        self.assertEqual(upload["percent"], 46)

    def test_the_backup_row_shows_its_speed_once_it_has_moved(self):
        self.usb.mkdir(exist_ok=True)
        self.importer_up = True
        progress = {
            "transferred": "41 MB",
            "total": "90 MB",
            "percent": 46,
            "speed": "7.0 MB/s",
            "eta": "7s",
            "files_done": 3,
            "files_total": 8,
            "finalizing": False,
            "logged_at": self.now,
        }
        google = {
            "state": "running",
            "progress": progress,
            "configured": True,
            "started_at": self.now - 10,
            "updated_at": self.now,
            "message": "",
        }
        with patch.object(self.core, "_google", return_value=google):
            for moved in (10, 34, 58, 82):
                self.write_status(
                    state="copying",
                    phase="USB backup",
                    stage="usb",
                    bytes_done=moved * 1024 * 1024,
                    bytes_total=340 * 1024 * 1024,
                    bytes_transferred=moved * 1024 * 1024,
                    files_done=1,
                    files_total=12,
                    elapsed_seconds=20,
                )
                state = self.core.state()
                self.advance(1)
        self.assertRegex(state["activity"]["lanes"][0]["right"], r"\d MB/s$")

    def test_an_upload_that_finishes_first_stays_on_show_as_done(self):
        self.backing_up()
        google = {
            "state": "complete",
            "progress": None,
            "configured": True,
            "started_at": self.now - 15,
            "updated_at": self.now - 5,
            "message": "",
        }
        with patch.object(self.core, "_google", return_value=google):
            state = self.core.state()
        self.assertEqual(state["activity"]["lanes"][1]["left"], "Done")
        self.assertEqual(state["activity"]["lanes"][1]["percent"], 100)

    def test_an_upload_finished_before_the_backup_began_is_not_shown(self):
        self.backing_up()
        google = {
            "state": "complete",
            "progress": None,
            "configured": True,
            "started_at": self.now - 900,
            "updated_at": self.now - 800,
            "message": "",
        }
        with patch.object(self.core, "_google", return_value=google):
            self.assertNotIn("lanes", self.core.state()["activity"])

    def test_an_upload_that_began_after_the_backup_ended_has_the_ordinary_panel(self):
        self.record(
            kind="usb_backup",
            source="USB backup",
            started_at=self.now - 90,
            finished_at=self.now - 60,
        )
        progress = {
            "transferred": "41 MB",
            "total": "90 MB",
            "percent": 46,
            "speed": "7.0 MB/s",
            "eta": "7s",
            "files_done": 3,
            "files_total": 8,
            "finalizing": False,
            "logged_at": self.now,
        }
        google = {
            "state": "running",
            "progress": progress,
            "configured": True,
            "started_at": self.now - 10,
            "updated_at": self.now,
            "message": "",
        }
        with patch.object(self.core, "_google", return_value=google):
            self.assertNotIn("lanes", self.core.state()["activity"])

    def test_a_backup_that_finishes_first_stays_on_show_as_done(self):
        self.record(
            kind="usb_backup",
            source="USB backup",
            started_at=self.now - 30,
            finished_at=self.now - 4,
        )
        progress = {
            "transferred": "41 MB",
            "total": "90 MB",
            "percent": 46,
            "speed": "7.0 MB/s",
            "eta": "7s",
            "files_done": 3,
            "files_total": 8,
            "finalizing": False,
            "logged_at": self.now,
        }
        google = {
            "state": "running",
            "progress": progress,
            "configured": True,
            "started_at": self.now - 10,
            "updated_at": self.now,
            "message": "",
            "logged_at": None,
        }
        with patch.object(self.core, "_google", return_value=google):
            state = self.core.state()
        self.assertEqual(state["headline"]["title"], "Backing up and uploading")
        self.assertEqual(state["activity"]["lanes"][0]["left"], "Done")
        self.assertEqual(state["activity"]["lanes"][1]["label"], "Google Photos")

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

    def stats_line(self, stats, seconds_ago=0):
        """One rclone log line, dated by the test's clock the way the real log is."""
        stamp = datetime.fromtimestamp(self.now - seconds_ago).strftime("%Y/%m/%d %H:%M:%S")
        return f"{stamp} INFO  :   {stats}\n"

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
            or self.stats_line("175.183 MiB / 267.950 MiB, 65%, 8.800 MiB/s, ETA 10s (xfr#8/18)")
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
        for stats in (
            "204.131 MiB / 267.950 MiB, 76%, 8.059 MiB/s, ETA 7s (xfr#12/18)",
            "237.655 MiB / 267.950 MiB, 89%, 7.590 MiB/s, ETA 3s (xfr#12/18)",
        ):
            self.advance(5)
            (self.install / "google_photos_sync.log").write_text(self.stats_line(stats))
            activity = self.core.state()["activity"]
        self.assertEqual(len(activity["speed_history"]), 3)

    def append_to_upload_log(self, stats):
        with (self.install / "google_photos_sync.log").open("a") as log:
            log.write(self.stats_line(stats))

    def poll_upload(self, seconds):
        """One poll a second, as the page does; the activity seen at each."""
        seen = []
        for _ in range(seconds):
            self.advance(1)
            seen.append(self.core.state()["activity"])
        return seen

    def start_upload_after_an_earlier_one(self):
        """The real log of the 21:29:53 upload on 2026-11-05: the last stats line is the earlier
        run's, and the new run's first line (0 B / 0 B) isn't one the parser can use."""
        earlier = self.stats_line(
            "158.592 MiB / 158.592 MiB, 100%, 7.323 MiB/s, ETA 0s", seconds_ago=1500
        )
        self.start_upload(started_seconds_ago=0, line=earlier)

    def test_while_rclone_gets_ready_the_last_upload_is_not_shown_as_this_one(self):
        self.start_upload_after_an_earlier_one()
        for activity in self.poll_upload(10):
            self.assertEqual(activity["speed_history"], [])
            self.assertIsNone(activity["percent"])
            self.assertTrue(activity["indeterminate"])
            self.assertEqual(activity["bytes_done"], 0)
            self.assertEqual(activity["detail"], "Preparing upload")
        self.append_to_upload_log("0 B / 0 B, -, 0 B/s, ETA -")
        self.assertEqual(self.poll_upload(5)[-1]["speed_history"], [])

    def test_the_upload_graph_starts_with_the_first_bytes_and_is_not_redrawn_after(self):
        self.start_upload_after_an_earlier_one()
        self.poll_upload(16)
        self.append_to_upload_log("47.688 MiB / 260.254 MiB, 18%, 11.745 MiB/s, ETA 18s (xfr#0/19)")
        lengths = [len(a["speed_history"]) for a in self.poll_upload(5)]
        self.append_to_upload_log("80.635 MiB / 260.254 MiB, 31%, 8.879 MiB/s, ETA 20s (xfr#4/19)")
        lengths += [len(a["speed_history"]) for a in self.poll_upload(5)]
        self.assertEqual(lengths, list(range(1, 11)))

    def test_polls_between_rclones_reports_are_not_counted_as_a_stop(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.core.state()
        self.poll_upload(5)
        self.append_to_upload_log("80 MiB / 260 MiB, 30%, 8 MiB/s, ETA 22s (xfr#4/19)")
        history = self.poll_upload(4)[-1]["speed_history"]
        self.assertEqual(
            [round(s / 1024**2) for s in history],
            [10] * 6 + [6] * 4,
            "each rate holds until rclone's next report: 30 MiB moved in 5 s",
        )

    def test_a_stalled_upload_falls_to_zero_and_keeps_what_came_before(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.core.state()
        for _ in range(4):  # rclone keeps reporting, and the figure doesn't move
            self.poll_upload(5)
            self.append_to_upload_log("50 MiB / 260 MiB, 19%, 6 MiB/s, ETA 35s (xfr#0/19)")
        history = self.poll_upload(1)[-1]["speed_history"]
        self.assertEqual(history[0], 10 * 1024**2)
        self.assertEqual(history[-1], 0)
        self.assertEqual(len(history), 22)
        self.append_to_upload_log("90 MiB / 260 MiB, 34%, 7 MiB/s, ETA 24s (xfr#4/19)")
        resumed = self.poll_upload(1)[-1]["speed_history"]
        self.assertEqual(len(resumed), 23)
        self.assertGreater(resumed[-1], 0)

    def test_a_late_report_is_not_a_stall(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.core.state()
        history = self.poll_upload(5)[-1]["speed_history"]
        self.assertEqual(history[-1], 10 * 1024**2)

    def test_an_upload_gone_quiet_falls_to_zero(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.core.state()
        history = self.poll_upload(15)[-1]["speed_history"]
        self.assertEqual(history[9], 10 * 1024**2)
        self.assertEqual(history[-1], 0)

    def test_an_upload_counter_that_goes_back_does_not_wipe_the_graph(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.core.state()
        self.poll_upload(5)
        self.append_to_upload_log("40 MiB / 260 MiB, 15%, 8 MiB/s, ETA 27s (xfr#0/19)")  # a retry
        history = self.poll_upload(1)[-1]["speed_history"]
        self.assertEqual((len(history), history[0], history[-1]), (7, 10 * 1024**2, 0))

    def test_a_new_upload_starts_a_new_graph(self):
        self.start_upload_after_an_earlier_one()
        self.advance(1)
        self.append_to_upload_log("50 MiB / 260 MiB, 19%, 10 MiB/s, ETA 21s (xfr#0/19)")
        self.assertEqual(len(self.poll_upload(3)[-1]["speed_history"]), 3)
        self.google_up = False
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "complete"}))
        self.poll_upload(1)
        self.advance(60)
        self.start_upload_after_an_earlier_one()
        self.assertEqual(self.poll_upload(2)[-1]["speed_history"], [])

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
        self.assertFalse(self.core.act("service_stop_exit", {"mode": "rm -rf"})["ok"])
        self.assertEqual(self.spawned, [])

    def test_stop_and_exit_also_closes_the_dashboard_and_menu_bar(self):
        result = self.core.act("service_stop_exit")
        self.assertTrue(result["ok"])
        self.assertEqual(self.spawned[-1][-1], "stop")
        self.assertNotIn("--keep-ui", self.spawned[-1])

    def test_stop_and_exit_asks_before_interrupting_a_transfer(self):
        self.importer_up = True
        result = self.core.act("service_stop_exit")
        self.assertTrue(result["needs_choice"])
        self.assertEqual(self.spawned, [])
        self.core.act("service_stop_exit", {"mode": "wait"})
        self.assertEqual(self.spawned[-1][-2:], ["stop", "--wait"])
        self.core.act("service_stop_exit", {"mode": "force"})
        self.assertEqual(self.spawned[-1][-2:], ["stop", "--force"])

    def test_start_runs_the_service_script(self):
        result = self.core.act("service_start")
        self.assertTrue(result["ok"])
        self.assertEqual(self.ran[-1][-1], "start")

    def test_unknown_actions_are_refused(self):
        self.assertFalse(self.core.act("format_disk")["ok"])


class TestUploadControl(Fixture):
    def google(self, state, **fields):
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": state, "message": fields.pop("message", ""), **fields})
        )

    def paused_flag(self):
        return self.install / ".google-photos-paused"

    def owe_retry(self, attempts, seconds_ago):
        (self.install / ".google-photos-retry").write_text(
            f"{attempts} {int(self.now - seconds_ago)}\n"
        )

    def actions(self, step):
        return [(a["id"], a["label"]) for a in step["actions"]]

    def stopped(self, pattern):
        return ["/usr/bin/pkill", "-TERM", "-f", pattern] in self.ran

    def test_an_rclone_left_by_a_killed_sync_is_still_uploading(self):
        self.rclone_up = True
        self.google("running")
        self.assertEqual(self.pipeline("cloud")["state"], "active")

    def test_a_running_upload_can_be_paused_from_its_card(self):
        self.google_up = True
        self.google("running")
        self.assertEqual(self.actions(self.pipeline("cloud")), [("google_pause", "Pause")])
        self.assertEqual(self.core.state()["google"], {"state": "running", "waiting": None})

    def test_pausing_sets_the_flag_and_stops_the_sync(self):
        self.google_up = True
        self.assertTrue(self.core.act("google_pause")["ok"])
        self.assertTrue(self.paused_flag().exists())
        self.assertTrue(self.stopped(str(self.install / config.GOOGLE_SYNC)))
        # The sync stops rclone itself, and says paused. Stopping rclone too could beat it to
        # it, and the sync would take rclone's exit for a failed upload.
        self.assertFalse(self.stopped(str(self.install / ".upload-list-")))

    def test_pausing_stops_an_rclone_that_no_sync_is_looking_after(self):
        self.rclone_up = True
        self.core.act("google_pause")
        self.assertTrue(self.stopped(str(self.install / ".upload-list-")))

    def test_paused_uploads_say_how_many_files_wait_and_offer_resume(self):
        self.paused_flag().touch()
        self.google("paused", waiting=312)
        step = self.pipeline("cloud")
        self.assertEqual(
            (step["state"], step["headline"], step["detail"]),
            ("off", "Paused", "312 files waiting"),
        )
        self.assertEqual(self.actions(step), [("google_resume", "Resume")])
        state = self.core.state()
        self.assertEqual(state["google"], {"state": "paused", "waiting": 312})
        self.assertNotIn("Google", state["headline"]["title"], "pausing is not a problem")

    def test_paused_shows_at_once_even_before_the_sync_has_stopped(self):
        self.google_up = True
        self.paused_flag().touch()
        self.google("running")
        self.assertEqual(self.pipeline("cloud")["headline"], "Paused")

    def test_resuming_clears_the_flag_and_starts_the_sync(self):
        self.paused_flag().touch()
        self.assertTrue(self.core.act("google_resume")["ok"])
        self.assertFalse(self.paused_flag().exists())
        self.assertIn(
            [str(self.paths.python), str(self.install / config.GOOGLE_SYNC), "--sync"],
            self.spawned,
        )

    def test_a_failed_upload_due_a_retry_says_when(self):
        self.google("failed", message="Couldn't reach Google")
        for attempts, seconds_ago, when in (
            (1, 20, "Retrying in 1 min"),
            (3, 60, "Retrying in 14 min"),
            (1, 90, "Retrying soon"),
        ):
            with self.subTest(attempts=attempts, seconds_ago=seconds_ago):
                self.owe_retry(attempts, seconds_ago)
                self.core._cache.clear()
                step = self.pipeline("cloud")
                self.assertEqual(
                    (step["state"], step["headline"], step["detail"]),
                    ("warn", when, "Couldn't reach Google"),
                )
                self.assertEqual(
                    self.actions(step), [("google_sync", "Retry now"), ("google_pause", "Pause")]
                )

    def test_an_interrupted_upload_due_a_retry_says_when(self):
        self.google("interrupted", message="The upload was stopped before it finished.")
        self.owe_retry(1, 20)
        self.assertEqual(self.pipeline("cloud")["headline"], "Retrying in 1 min")
        self.assertNotIn("Google", self.core.state()["headline"]["title"])

    def test_a_sync_waiting_behind_the_upload_is_mentioned(self):
        self.google_up = True
        self.google("running")
        (self.install / "google_photos_sync.log").write_text(
            "2026/10/21 22:24:49 INFO  : 4 MiB / 17 MiB, 28%, 8 MiB/s, ETA 2s (xfr#6/15)\n"
        )
        queue = self.install / ".google-photos-sync.queue.lock"
        queue.write_text("999999")  # a sync that has gone
        self.assertEqual(self.pipeline("cloud")["detail"], "4 MiB of 17 MiB")
        queue.write_text(str(os.getpid()))  # the waiting sync's process ID
        self.core._cache.clear()
        self.assertEqual(self.pipeline("cloud")["detail"], "More files queued")

    def test_the_page_can_tell_an_upload_from_an_import(self):
        self.google_up = True
        self.google("running")
        state = self.core.state()
        self.assertFalse(state["busy"])
        self.assertEqual(state["google"]["state"], "running")

    def test_stop_and_exit_can_pause_the_upload_first(self):
        self.google_up = True
        self.assertTrue(self.core.act("service_stop_exit", {"pause_upload": True})["ok"])
        self.assertTrue(self.paused_flag().exists())
        self.assertTrue(self.stopped(str(self.install / config.GOOGLE_SYNC)))
        self.assertIn(["/bin/bash", str(self.install / config.SERVICE), "stop"], self.spawned)

    def test_stop_and_exit_leaves_the_upload_alone_unless_asked(self):
        self.google_up = True
        self.core.act("service_stop_exit", {})
        self.assertFalse(self.paused_flag().exists())

    def test_opening_photo_offloader_carries_on_an_owed_upload(self):
        self.core.resume_owed_upload()
        self.assertEqual(self.spawned, [])
        self.owe_retry(5, 10)
        self.core.resume_owed_upload()
        self.assertEqual(
            self.spawned,
            [[str(self.paths.python), str(self.install / config.GOOGLE_SYNC), "--retry"]],
        )


class TestFormatting(unittest.TestCase):
    def test_byte_helpers(self):
        self.assertEqual(display.parse_display_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(display.parse_display_bytes("2 KiB"), 2048.0)
        self.assertEqual(display.parse_display_bytes("unknown"), 0.0)
        self.assertEqual(display.format_bytes(512), "512 B")
        self.assertEqual(display.format_bytes(1024), "1.0 KB")


if __name__ == "__main__":
    unittest.main()
