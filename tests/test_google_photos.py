import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config
from photo_offloader import google_photos as sync
from tests import support


class TestGooglePhotosSync(unittest.TestCase):
    def setUp(self):
        # Safety net: anything a test does not point somewhere itself stays in a temp dir.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        support.isolate_config(self, Path(tmp.name))

    def test_filters_jpg_and_video_but_not_raw(self):
        for ext in ("*.jpg", "*.jpeg", "*.mov", "*.mp4"):
            self.assertIn(ext, sync.INCLUDE_EXTENSIONS)
        for ext in ("*.nef", "*.arw", "*.cr2", "*.dng"):
            self.assertNotIn(ext, sync.INCLUDE_EXTENSIONS)

    def test_parse_rclone_stats_line(self):
        parsed = sync.parse_rclone_stats_line(
            "2026/10/21 22:24:49 INFO  : 4.998 GiB / 17.703 GiB, "
            "28%, 8.387 MiB/s, ETA 25m51s (xfr#649/1520)"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["transferred"], "4.998 GiB")
        self.assertEqual(parsed["total"], "17.703 GiB")
        self.assertEqual(parsed["percent"], 28.0)
        self.assertEqual(parsed["speed"], "8.387 MiB/s")
        self.assertEqual(parsed["eta"], "25m51s")

    def test_parse_rclone_stats_line_reads_file_counts(self):
        parsed = sync.parse_rclone_stats_line(
            "2026/11/04 18:50:05 INFO  :   175.183 MiB / 267.950 MiB, 65%, 8.800 MiB/s, ETA 10s (xfr#8/18)")
        self.assertEqual((parsed["files_done"], parsed["files_total"]), (8, 18))
        done = sync.parse_rclone_stats_line(
            "2026/11/04 18:50:20 INFO  :   267.950 MiB / 267.950 MiB, 100%, 7.107 MiB/s, ETA 0s")
        self.assertEqual((done["files_done"], done["files_total"]), (None, None))

    def test_parse_rclone_stats_line_without_eta(self):
        self.assertIsNone(sync.parse_rclone_stats_line("rclone starting upload"))

    def test_google_photos_connection_preference_survives_install_directory_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            preferences = Path(tmp) / "PhotoOffloaderData" / "preferences.json"
            with patch.object(config, "PREFERENCES_PATH", preferences):
                sync.remember_google_photos_connection("gphotos")
            payload = json.loads(preferences.read_text(encoding="utf-8"))
            self.assertEqual(payload["google_photos"]["remote"], "gphotos:")
            self.assertTrue(payload["google_photos"]["configured"])
            self.assertIn("last_connection_check", payload["google_photos"])
            self.assertNotIn("token", json.dumps(payload).lower())

    def test_log_sync_event_appends_explicit_completion_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "sync.log"
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_LOG_PATH", log_path),
            ):
                sync.log_sync_event("Google Photos sync completed successfully.")

            contents = log_path.read_text(encoding="utf-8")
            self.assertIn("INFO  : Photo Offloader: Google Photos sync completed successfully.", contents)
            self.assertTrue(contents.endswith("\n"))

    def test_status_preserves_last_success_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "status.json"
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", status),
            ):
                sync.publish_status("complete", "ok")
                success = json.loads(status.read_text())["last_success"]
                sync.publish_status("failed", "error")
                payload = json.loads(status.read_text())

            self.assertEqual(payload["last_success"], success)
            self.assertEqual(payload["state"], "failed")

    def test_missing_rclone_reports_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "status.json"
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", status),
                patch.object(config, "GOOGLE_LOCK_PATH", root / ".lock"),
                patch.object(sync, "find_rclone", return_value=None),
            ):
                result = sync.run_sync()
                payload = json.loads(status.read_text())

            self.assertEqual(result, 2)
            self.assertEqual(payload["state"], "not_configured")

    # ------------------------------------------------------------------ fixture
    def sync_environment(self, root):
        """Patch every path the sync touches so nothing leaves the temp dir."""
        photos = root / "CameraImports"
        photos.mkdir()
        patches = [
            patch.object(config, "INSTALL_DIR", root),
            patch.object(config, "GOOGLE_STATUS_PATH", root / "status.json"),
            patch.object(config, "GOOGLE_LOCK_PATH", root / ".google-photos-sync.lock"),
            patch.object(config, "GOOGLE_LOG_PATH", root / "google_photos_sync.log"),
            patch.object(config, "LOCAL_DEST_ROOT", photos),
            patch.object(config, "PREFERENCES_PATH", root / "preferences.json"),
            patch.object(sync, "find_rclone", return_value="/usr/bin/rclone"),
            patch.object(config, "CATALOG_DB_PATH", root / "data" / "catalog.sqlite3"),
            patch.object(config, "LEGACY_CATALOG_DB_PATH", root / "legacy" / "catalog.sqlite3"),
            patch.object(catalog, "_CATALOG_INITIALIZED_PATH", None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return photos

    @staticmethod
    def fake_rclone(on_copy=None, copy_returncode=0):
        commands = []

        def run(command, *args, **kwargs):
            commands.append(list(command))
            verb = command[1]
            if verb == "listremotes":
                return subprocess.CompletedProcess(command, 0, stdout="gphotos:\n", stderr="")
            if verb == "copy" and on_copy is not None:
                on_copy(command)
            return subprocess.CompletedProcess(command, copy_returncode if verb == "copy" else 0,
                                               stdout="", stderr="")

        return run, commands

    # --------------------------------------- stale lock (review finding 3)
    def test_stale_lock_file_from_a_dead_process_does_not_block_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.sync_environment(root)
            # What a SIGKILL / power loss leaves behind: the file, but no live holder.
            (root / ".google-photos-sync.lock").write_text("99999")
            with patch.object(sync, "_sync", return_value=0) as run_sync:
                self.assertEqual(sync.run_sync(), 0)
            run_sync.assert_called_once()

    def test_live_lock_holder_still_blocks_a_second_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.sync_environment(root)
            holder = os.open(root / ".google-photos-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sync, "_sync", return_value=0) as run_sync:
                self.assertEqual(sync.run_sync(), 0)
            run_sync.assert_not_called()
            status = json.loads((root / "status.json").read_text())
            self.assertIn("already running", status["message"])

    def test_duplicate_launch_does_not_overwrite_the_running_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.sync_environment(root)
            sync.publish_status("running", "Uploading 40% (real progress state)")
            holder = os.open(root / ".google-photos-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sync.run_sync()
            status = json.loads((root / "status.json").read_text())
            self.assertEqual(status["message"], "Uploading 40% (real progress state)")

    # ----------------------------------- upload bookkeeping (review finding 4)
    def test_only_files_present_when_the_upload_started_are_marked_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "2026-10").mkdir()
            before = photos / "2026-10" / "before.jpg"
            before.write_bytes(b"uploaded-by-rclone")
            during = photos / "2026-10" / "imported-during-upload.jpg"

            def import_lands_mid_upload(_command):
                during.write_bytes(b"arrived after rclone listed the tree")

            run, commands = self.fake_rclone(on_copy=import_lands_mid_upload)
            with patch.object(sync.subprocess, "run", run):
                self.assertEqual(sync.run_sync(), 0)

            self.assertIsNotNone(catalog.google_photos_upload_record(before))
            self.assertIsNone(catalog.google_photos_upload_record(during),
                              "a file rclone never saw was recorded as uploaded")
            copy = next(c for c in commands if c[1] == "copy")
            self.assertIn("--files-from", copy)

    def test_files_already_uploaded_are_not_sent_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            done = photos / "done.jpg"
            done.write_bytes(b"already there")
            fresh = photos / "fresh.jpg"
            fresh.write_bytes(b"new")
            catalog.mark_google_photos_uploaded(done, sync.REMOTE_NAME, sync.ALBUM_NAME)

            listed = []

            def capture(command):
                listed.append(Path(command[command.index("--files-from") + 1]).read_text().split())

            run, _ = self.fake_rclone(on_copy=capture)
            with patch.object(sync.subprocess, "run", run):
                self.assertEqual(sync.run_sync(), 0)
            self.assertEqual(listed, [["fresh.jpg"]])

    def test_nothing_pending_skips_rclone_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            done = photos / "done.jpg"
            done.write_bytes(b"already there")
            catalog.mark_google_photos_uploaded(done, sync.REMOTE_NAME, sync.ALBUM_NAME)
            run, commands = self.fake_rclone()
            with patch.object(sync.subprocess, "run", run):
                self.assertEqual(sync.run_sync(), 0)
            self.assertFalse([c for c in commands if c[1] == "copy"])
            self.assertEqual(json.loads((root / "status.json").read_text())["state"], "complete")

    def test_failed_rclone_marks_nothing_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            photo = photos / "a.jpg"
            photo.write_bytes(b"a")
            run, _ = self.fake_rclone(copy_returncode=7)
            with patch.object(sync.subprocess, "run", run):
                self.assertEqual(sync.run_sync(), 7)
            self.assertIsNone(catalog.google_photos_upload_record(photo))

    # ------------------------------------------------- log growth (user report)
    def test_oversized_rclone_log_is_rotated_before_a_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "a.jpg").write_bytes(b"a")
            log = root / "google_photos_sync.log"
            log.write_text("old rclone output\n" * 500)
            run, _ = self.fake_rclone()
            with patch.object(sync, "SYNC_LOG_MAX_BYTES", 1000), patch.object(sync.subprocess, "run", run):
                self.assertEqual(sync.run_sync(), 0)
            self.assertTrue((root / "google_photos_sync.log.1").exists())
            self.assertLess(log.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
