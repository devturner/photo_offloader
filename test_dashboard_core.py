#!/usr/bin/env python3
"""Tests for dashboard_core: the state the dashboard renders and the actions it can run."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import camera_offloader_v2 as catalog
import dashboard_core as core


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
        (self.install / "photo_offloader_service.sh").write_text("#!/bin/bash\n")
        (self.install / "google_photos_sync.py").write_text("")
        (self.install / "camera_offloader_trigger.sh").write_text("#!/bin/bash\n")

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
                alive = self.google_up if argv[-1].endswith("google_photos_sync.py") else self.importer_up
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
        self.assertTrue(self.spawned[-1][1].endswith("camera_offloader_trigger.sh"))

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
        self.copying()
        state = self.core.state()
        activity = state["activity"]
        self.assertEqual(activity["title"], "Copying to Mac archive")
        self.assertAlmostEqual(activity["percent"], 25.0)
        self.assertEqual(activity["detail"], "DSC_0142.NEF")
        self.assertEqual(state["headline"]["title"], "Importing from NIKON Z 8")
        self.assertEqual(state["headline"]["detail"], "Copying to Mac archive · DSC_0142.NEF")
        archive = self.pipeline("archive")
        self.assertEqual((archive["state"], archive["headline"], archive["detail"]),
                         ("active", "Copying", "DSC_0142.NEF"))

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
        self.record()
        state = self.core.state()
        last = state["last_transfer"]
        self.assertEqual(last["title"], "Imported from NIKON Z 8")
        self.assertEqual(last["summary"], "214 files · 420.0 MB")
        self.assertEqual(state["headline"]["tone"], "success")

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
        self.assertTrue(self.spawned[-1][1].endswith("camera_offloader_trigger.sh"))

    def test_usb_backup_requires_the_drive(self):
        result = self.core.act("usb_backup")
        self.assertFalse(result["ok"])
        self.assertEqual(self.spawned, [])
        self.usb.mkdir()
        self.core._cache.clear()
        self.assertTrue(self.core.act("usb_backup")["ok"])
        self.assertTrue(self.spawned[-1][1].endswith("photo_offloader_backup_launcher.py"))

    def test_catalog_update_and_google_sync_start_the_right_programs(self):
        self.core.act("update_catalog")
        self.assertEqual(self.spawned[-1][-1], "--update-catalog")
        self.core.act("google_sync")
        self.assertEqual(self.spawned[-1][-1], "--sync")

    def test_google_sync_reports_a_missing_helper(self):
        (self.install / "google_photos_sync.py").unlink()
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
    def test_helpers_match_the_old_dashboard(self):
        self.assertEqual(core.format_clock(3661), "01:01:01")
        self.assertEqual(core.format_clock(-5), "00:00:00")
        self.assertEqual(core.parse_display_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(core.parse_display_bytes("unknown"), 0.0)
        self.assertEqual(core.format_bytes(512), "512 B")
        self.assertEqual(core.format_duration(65), "1m 05s")


if __name__ == "__main__":
    unittest.main()
