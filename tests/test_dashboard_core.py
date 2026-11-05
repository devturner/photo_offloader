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

from photo_offloader import importer as catalog
from photo_offloader.dashboard import core


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

        for name, value in (("CATALOG_DB_PATH", self.data / "catalog.sqlite3"),
                            ("LEGACY_CATALOG_DB_PATH", root / "legacy.sqlite3"),
                            ("_CATALOG_INITIALIZED_PATH", None)):
            patcher = patch.object(catalog, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PHOTO_OFFLOADER_STATUS_FILE", None)
        os.environ.pop("PHOTO_OFFLOADER_PAUSE_FILE", None)

        self.paths = core.Paths(
            install_dir=self.install, data_dir=self.data, volumes_root=self.volumes,
            usb_volume=self.usb, local_archives=(root / "CameraImports", root / "CameraImportsRaw"),
            usb_archives=(self.usb / "CameraImports",), python=Path("/usr/bin/python3"))
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
                alive = self.google_up if argv[-1].endswith("photo-offloader-google-sync") else self.importer_up
                return subprocess.CompletedProcess(argv, 0, "4242\n" if alive else "", "")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        self.core = core.DashboardCore(self.paths, clock=lambda: self.now, run=run,
                                       popen=lambda argv, **kw: self.spawned.append(list(argv)))

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
        base = dict(kind="import", source="NIKON Z 8", outcome="complete", files=214,
                    copied=214, bytes=420 * 1024 * 1024, started_at=self.now - 60,
                    finished_at=self.now - 5)
        base.update(fields)
        catalog.record_import_run(**base)

    def seed_catalog(self, local=0, usb=0):
        catalog.catalog_connection().close()
        with sqlite3.connect(self.data / "catalog.sqlite3") as db:
            for index in range(local):
                db.execute("INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode, indexed_at)"
                           " VALUES (?, ?, ?, 1, 1, ?, 1)",
                           (f"l{index}", str(self.paths.local_archives[0].resolve()), f"l{index}", index))
            for index in range(usb):
                db.execute("INSERT INTO file_locations (path, root, relative_path, size, mtime_ns, inode, indexed_at)"
                           " VALUES (?, ?, ?, 1, 1, ?, 1)",
                           (f"u{index}", str(self.paths.usb_archives[0].resolve()), f"u{index}", index))

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
        self.assertEqual(self.pipeline("card")["action"], {"id": "import_card", "label": "Import now"})

    def test_import_now_is_hidden_while_the_importer_is_busy(self):
        self.add_card("NIKON Z 8")
        self.mark_card_handled()
        self.importer_up = True
        self.write_status(state="copying", source=str(self.volumes / "NIKON Z 8" / "DCIM"),
                          files_done=1, files_total=10)
        self.assertIsNone(self.pipeline("card")["action"])

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
        fields = dict(state="copying", phase="card-to-ssd", source="/Volumes/NIKON Z 8",
                      bytes_done=100 * 1024**2, bytes_total=400 * 1024**2,
                      bytes_transferred=100 * 1024**2, transfer_speed_bps=50 * 1024**2,
                      eta_seconds=6, current_file="DSC_0142.NEF")
        fields.update(extra)
        self.write_status(**fields)

    def test_copy_progress_is_reported(self):
        self.copying(stage="copy", category="RAW", files_done=40, files_total=160, new_files=3)
        state = self.core.state()
        activity = state["activity"]
        self.assertEqual(activity["title"], "Copying new files")
        self.assertAlmostEqual(activity["percent"], 25.0)
        self.assertEqual(activity["detail"], "RAW · DSC_0142.NEF")
        self.assertEqual((activity["files_done"], activity["files_total"], activity["new_files"]), (40, 160, 3))
        self.assertEqual(state["headline"]["title"], "Copying new files")
        self.assertEqual(state["headline"]["detail"], "NIKON Z 8 · RAW · DSC_0142.NEF")
        archive = self.pipeline("archive")
        self.assertEqual((archive["state"], archive["headline"], archive["detail"]),
                         ("active", "Copying", "RAW · DSC_0142.NEF"))

    def checking(self, **extra):
        # Between copies the importer reports each file's copy decision as "scanning".
        fields = dict(state="scanning", phase="copy-decision", stage="copy", category="RAW",
                      decision_files_checked=5, decision_files_total=1188)
        fields.update(extra)
        self.copying(**fields)

    def test_activity_tells_what_it_is_actually_doing(self):
        # User feedback 2026-11-04: "Activity is too generic".
        expectations = [
            (dict(state="scanning", phase="card-to-ssd", stage="scan"), "Scanning card"),
            (dict(state="scanning", phase="card-to-ssd", stage="metadata"), "Reading photo info"),
            (dict(state="scanning", phase="copy-decision", stage="copy", new_files=0), "Checking for new files"),
            (dict(state="scanning", phase="USB backup", stage="usb"), "Comparing with USB backup"),
            (dict(state="copying", phase="USB backup", stage="usb"), "Backing up to USB"),
            (dict(state="scanning", phase="catalog-update", stage="catalog"), "Updating file index"),
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
                self.copying(stage="copy", category="RAW", new_files=2,
                             bytes_done=transferred, bytes_transferred=transferred)
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
        self.assertEqual((self.pipeline("card")["state"], self.pipeline("card")["detail"]),
                         ("active", "Reading photo info"))
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
        self.assertEqual([(s["id"], s["state"]) for s in stages],
                         [("scan", "done"), ("metadata", "done"), ("copy", "active"), ("usb", "skipped")])
        self.copying(stage="usb", phase="USB backup", usb_planned=True)
        stages = self.core.state()["activity"]["stages"]
        self.assertEqual([s["state"] for s in stages], ["done", "done", "done", "active"])

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
        self.assertEqual(last["summary"], "15 new: 7 photos, 7 RAW, 1 other · 280.0 MB · 214 on card, all backed up")
        self.assertEqual(state["headline"]["tone"], "success")

    def test_an_import_with_nothing_new_says_so(self):
        self.record(copied=0, skipped=214, details={"new": {}, "new_bytes": 0})
        self.assertEqual(self.core.history()[0]["summary"], "No new files · 214 on card, all backed up")

    def test_usb_copies_of_the_session_are_counted(self):
        self.record(usb=True, details={"new": {"RAW": 2}, "new_bytes": 60 * 1024**2, "usb_copied": 2})
        self.assertEqual(self.core.history()[0]["summary"],
                         "2 new: 2 RAW · 60.0 MB · 214 on card, all backed up · 2 copied to USB")

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
        restarted = core.DashboardCore(self.paths, clock=lambda: self.now,
                                       run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""),
                                       popen=lambda *a, **k: None)
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
        for log_id, path in (("import", self.install / "import.log"), ("trigger", self.install / "trigger.log"),
                             ("google", self.install / "google_photos_sync.log")):
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


class TestTheme(Fixture):
    # User feedback 2026-11-04: no way to switch light/dark from the dashboard.
    def test_theme_defaults_to_following_the_system(self):
        self.assertEqual(self.core.state()["settings"]["theme"], "system")

    def test_theme_choice_is_saved_and_survives_a_restart(self):
        self.assertTrue(self.core.act("set_theme", {"theme": "dark"})["ok"])
        restarted = core.DashboardCore(self.paths)
        self.assertEqual(restarted.state()["settings"]["theme"], "dark")

    def test_unknown_theme_is_refused(self):
        self.assertFalse(self.core.act("set_theme", {"theme": "neon"})["ok"])
        self.assertEqual(self.core.state()["settings"]["theme"], "system")


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
        self.assertIsNone(self.pipeline("usb")["action"])
        self.usb.mkdir()
        self.advance(10)
        self.assertEqual(self.pipeline("usb")["action"]["id"], "usb_backup")
        self.importer_up = True
        self.write_status(state="copying", phase="card-to-ssd", source="/Volumes/X")
        self.advance(10)
        self.assertIsNone(self.pipeline("usb")["action"])
        self.assertIsNone(self.pipeline("cloud")["action"])

    def test_catalog_maintenance_is_never_a_permanent_button(self):
        self.assertTrue(all(step["action"] is None or step["action"]["id"] != "update_catalog"
                            for step in self.core.state()["pipeline"]))

    def test_google_photos_not_configured(self):
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["headline"]), ("off", "Not set up"))

    def test_google_photos_configured_and_idle(self):
        (self.data / "preferences.json").write_text(json.dumps({"google_photos": {"configured": True}}))
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["action"]["id"]), ("ok", "google_sync"))

    def rclone_config(self, text):
        config = Path(self.tmp.name) / "rclone.conf"
        config.write_text(text)
        self.core.paths = dataclasses.replace(self.paths, rclone_config=config)

    def test_a_configured_rclone_remote_counts_even_after_a_reinstall(self):
        # User report 2026-11-04: after uninstall/reinstall the dashboard asked to set up
        # Google Photos again although rclone still had the gphotos remote.
        self.rclone_config("[other]\ntype = drive\n\n[gphotos]\ntype = google photos\ntoken = {\"secret\": 1}\n")
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["action"]["id"]), ("ok", "google_sync"))

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
            self.assertEqual(core.Paths.default().rclone_config,
                             Path.home() / ".config" / "rclone" / "rclone.conf")

    def test_google_photos_upload_shows_live_progress_from_the_rclone_log(self):
        self.google_up = True
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "running", "message": "Uploading"}))
        (self.install / "google_photos_sync.log").write_text(
            "2026/10/21 22:24:49 INFO  : 4.998 GiB / 17.703 GiB, 28%, 8.387 MiB/s, ETA 25m51s (xfr#649/1520)\n")
        state = self.core.state()
        self.assertEqual(state["upload"]["percent"], 28.0)
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["headline"]), ("active", "Uploading"))
        self.assertEqual(step["detail"], "4.998 GiB of 17.703 GiB")

    def test_an_upload_is_the_headline_when_the_importer_is_idle(self):
        self.google_up = True
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "running"}))
        (self.install / "google_photos_sync.log").write_text(
            "2026/10/21 22:24:49 INFO  : 4.998 GiB / 17.703 GiB, 28%, 8.387 MiB/s, ETA 25m51s (xfr#649/1520)\n")
        state = self.core.state()
        self.assertEqual(state["headline"], {"tone": "active", "title": "Uploading to Google Photos",
                                             "detail": "4.998 GiB of 17.703 GiB"})
        activity = state["activity"]
        self.assertEqual((activity["kind"], activity["percent"], activity["eta_seconds"]), ("upload", 28.0, 1551.0))
        self.assertAlmostEqual(activity["speed_bps"], 8.387 * 1024**2, delta=10)
        self.assertFalse(state["busy"], "an upload must not block backups or catalog updates")

    def test_an_importer_run_takes_precedence_over_an_upload_in_the_hero(self):
        self.google_up = self.importer_up = True
        (self.install / "google_photos_status.json").write_text(json.dumps({"state": "running"}))
        self.write_status(state="copying", phase="card-to-ssd", source="/Volumes/X", bytes_done=1, bytes_total=4)
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
        self.assertEqual((step["state"], step["headline"], step["action"]["label"]),
                         ("error", "Upload interrupted", "Resume"))

    def test_a_failed_or_interrupted_upload_is_raised_to_the_headline(self):
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "failed", "message": "rclone exited with status 7"}))
        head = self.core.state()["headline"]
        self.assertEqual((head["tone"], head["title"]), ("warning", "Google Photos upload needs attention"))
        self.assertEqual(head["detail"], "rclone exited with status 7")

    def test_google_photos_failure_offers_a_retry(self):
        (self.install / "google_photos_status.json").write_text(
            json.dumps({"state": "failed", "message": "rclone exited with status 7"}))
        step = self.pipeline("cloud")
        self.assertEqual((step["state"], step["action"]["label"]), ("error", "Retry"))


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
        self.assertEqual(core.parse_display_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(core.parse_display_bytes("2 KiB"), 2048.0)
        self.assertEqual(core.parse_display_bytes("unknown"), 0.0)
        self.assertEqual(core.format_bytes(512), "512 B")
        self.assertEqual(core.format_bytes(1024), "1.0 KB")


if __name__ == "__main__":
    unittest.main()
