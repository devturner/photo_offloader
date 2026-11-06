import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
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
            "2026/11/04 18:50:05 INFO  :   175.183 MiB / 267.950 MiB, 65%, 8.800 MiB/s, ETA 10s (xfr#8/18)"
        )
        self.assertEqual((parsed["files_done"], parsed["files_total"]), (8, 18))
        done = sync.parse_rclone_stats_line(
            "2026/11/04 18:50:20 INFO  :   267.950 MiB / 267.950 MiB, 100%, 7.107 MiB/s, ETA 0s"
        )
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
            self.assertIn(
                "INFO  : Photo Offloader: Google Photos sync completed successfully.", contents
            )
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

    def upload_saved_after(self, sent, exit_code):
        """Run a sync that sends ``sent`` and ends with ``exit_code``; what it saved on the run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in sent:
                (root / name).write_bytes(b"x" * 1000)
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", root / "status.json"),
                patch.object(config, "GOOGLE_LOG_PATH", root / "sync.log"),
                patch.object(config, "LOCAL_DEST_ROOT", root),
                patch.object(sync, "find_rclone", return_value="rclone"),
                patch.object(sync, "list_upload_candidates", return_value=sent),
                patch.object(sync, "_send_pending", return_value=exit_code),
                patch.object(sync.catalog, "record_upload_on_import") as record,
            ):
                self.assertEqual(sync._sync(), exit_code)
        return record.call_args

    def send(self, album_known, upload_code=0, album_code=None):
        """Run the sending step; returns its exit code, and the album-check mock."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", root / "status.json"),
                patch.object(config, "GOOGLE_LOG_PATH", root / "sync.log"),
                patch.object(sync, "_check_remote", return_value=None),
                patch.object(sync, "remember_google_photos_connection"),
                patch.object(sync, "_record_upload", return_value=0),
                patch.object(sync, "_upload_snapshot", return_value=upload_code) as upload,
                patch.object(sync, "_ensure_album", return_value=album_code) as album,
                patch.object(
                    sync.catalog, "google_photos_album_used", return_value=album_known
                ) as used,
            ):
                code = sync._send_pending("rclone", ["a.jpg"])
                used.assert_called_once_with(sync.REMOTE_NAME, sync.ALBUM_NAME)
                self.log_after = (root / "sync.log").read_text()
                self.status_after = (
                    json.loads((root / "status.json").read_text() or "{}")
                    if (root / "status.json").exists()
                    else {}
                )
        return code, album, upload

    def test_the_log_times_each_step_of_starting_an_upload(self):
        self.send(album_known=False)
        for step in ("remote check", "album check", "rclone copy"):
            self.assertRegex(self.log_after, rf"Upload step: {step} took \d+\.\ds")

    def test_the_log_says_when_the_album_check_was_skipped(self):
        self.send(album_known=True)
        self.assertIn("Upload step: album check skipped", self.log_after)
        self.assertNotRegex(self.log_after, r"album check took")

    def test_a_step_log_line_is_not_read_as_upload_progress(self):
        self.send(album_known=False)
        for line in self.log_after.splitlines():
            self.assertIsNone(sync.parse_rclone_stats_line(line), line)

    def test_finding_the_files_is_timed_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", root / "status.json"),
                patch.object(config, "GOOGLE_LOG_PATH", root / "sync.log"),
                patch.object(config, "LOCAL_DEST_ROOT", root),
                patch.object(sync, "find_rclone", return_value="rclone"),
                patch.object(sync, "list_upload_candidates", return_value=[]),
            ):
                sync._sync()
            self.assertRegex((root / "sync.log").read_text(), r"finding files took \d+\.\ds")

    def test_an_album_already_uploaded_to_is_not_checked_with_google(self):
        code, album, upload = self.send(album_known=True)
        self.assertEqual(code, 0)
        album.assert_not_called()
        upload.assert_called_once()

    def test_a_new_album_is_created_first(self):
        code, album, upload = self.send(album_known=False)
        self.assertEqual(code, 0)
        album.assert_called_once()
        upload.assert_called_once()

    def test_a_new_album_that_cannot_be_created_stops_before_the_upload(self):
        code, album, upload = self.send(album_known=False, album_code=7)
        self.assertEqual(code, 7)
        upload.assert_not_called()

    def test_a_remote_that_cannot_be_used_stops_before_the_album_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(config, "GOOGLE_LOG_PATH", Path(tmp) / "sync.log"),
                patch.object(sync, "_check_remote", return_value=2),
                patch.object(sync.catalog, "google_photos_album_used", return_value=False),
                patch.object(sync, "_ensure_album") as album,
            ):
                self.assertEqual(sync._send_pending("rclone", ["a.jpg"]), 2)
        album.assert_not_called()

    def test_a_failed_upload_to_a_known_album_checks_it_to_say_why(self):
        # Keeps the friendly message for a refused album, and remakes one deleted in Google Photos.
        code, album, _ = self.send(album_known=True, upload_code=3, album_code=7)
        self.assertEqual(code, 7)
        album.assert_called_once()

    def test_a_failed_upload_with_a_good_album_reports_rclones_failure(self):
        code, album, _ = self.send(album_known=True, upload_code=3)
        self.assertEqual(code, 3)
        album.assert_called_once()
        self.assertEqual(self.status_after["state"], "failed")

    def test_rclone_reports_as_often_as_the_dashboards_graph_samples(self):
        # User feedback 2026-11-05: at one report every 5 s the graph's line was visibly stepped.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(config, "INSTALL_DIR", root),
                patch.object(config, "GOOGLE_STATUS_PATH", root / "status.json"),
                patch.object(sync.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                sync._upload_snapshot("rclone", "gphotos:album/x", ["a.jpg"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--stats") + 1], "1s")

    def test_a_finished_upload_is_saved_with_the_import_it_followed(self):
        call = self.upload_saved_after(["a.jpg", "b.jpg"], 0)
        upload = call.args[0]
        self.assertEqual(
            (upload["outcome"], upload["files"], upload["bytes"], upload["album"]),
            ("complete", 2, 2000, sync.ALBUM_NAME),
        )
        self.assertGreaterEqual(upload["seconds"], 0)
        self.assertEqual(call.kwargs["within_seconds"], sync.UPLOAD_FOLLOWS_IMPORT_SECONDS)

    def test_a_failed_upload_is_saved_as_failed(self):
        self.assertEqual(self.upload_saved_after(["a.jpg"], 3).args[0]["outcome"], "failed")

    def test_a_file_gone_before_it_is_measured_does_not_stop_the_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "LOCAL_DEST_ROOT", Path(tmp)):
                self.assertEqual(sync._pending_bytes(["gone.jpg"]), 0)

    def test_a_sync_with_nothing_to_send_saves_nothing(self):
        self.assertIsNone(self.upload_saved_after([], 0))

    def test_running_is_published_before_contacting_rclone_and_keeps_its_start_time(self):
        # User report 2026-11-04: the dashboard fell back to the import result while the
        # sync was still talking to rclone, because "running" was only published later.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "a.jpg").write_bytes(b"a")
            seen = []
            run, _ = self.fake_rclone()

            def watching_run(argv, *args, **kwargs):
                seen.append(json.loads(config.GOOGLE_STATUS_PATH.read_text()))
                return run(argv, *args, **kwargs)

            with patch.object(sync.subprocess, "run", watching_run):
                self.assertEqual(sync.run_sync(), 0)
            self.assertEqual(seen[0]["state"], "running")
            self.assertEqual(
                {entry["started_at"] for entry in seen if entry["state"] == "running"},
                {seen[0]["started_at"]},
            )

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
            return subprocess.CompletedProcess(
                command, copy_returncode if verb == "copy" else 0, stdout="", stderr=""
            )

        return run, commands

    def test_a_file_that_cannot_be_read_is_logged_when_listing_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "good.jpg").write_bytes(b"good")
            bad = photos / "bad.jpg"
            bad.write_bytes(b"bad")
            real_stat = Path.stat

            def stat(path, *args, **kwargs):
                # is_file() swallows errors, so fail only the sync's own later stat of this file.
                if path == bad and sys._getframe(1).f_code.co_name == "list_upload_candidates":
                    raise OSError("I/O error")
                return real_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", stat):
                pending = sync.list_upload_candidates()

            self.assertEqual(pending, ["good.jpg"])
            log = (root / "google_photos_sync.log").read_text(encoding="utf-8")
            self.assertIn("bad.jpg", log)
            self.assertIn("I/O error", log)

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
            self.assertIsNone(
                catalog.google_photos_upload_record(during),
                "a file rclone never saw was recorded as uploaded",
            )
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
            with (
                patch.object(sync, "SYNC_LOG_MAX_BYTES", 1000),
                patch.object(sync.subprocess, "run", run),
            ):
                self.assertEqual(sync.run_sync(), 0)
            self.assertTrue((root / "google_photos_sync.log.1").exists())
            self.assertLess(log.stat().st_size, 1000)

    def test_failed_sign_in_during_a_sync_names_the_fix(self):
        # A Testing-status Google app stops working a week after sign-in; the dashboard
        # shows this message, so it has to say what to do, not repeat rclone's error.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "a.jpg").write_bytes(b"a")
            run, _ = self.fake_rclone()

            def expired(command, *args, **kwargs):
                if command[1] == "mkdir":
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr=EXPIRED_TOKEN)
                return run(command, *args, **kwargs)

            with patch.object(sync.subprocess, "run", expired):
                self.assertEqual(sync.run_sync(), 1)
            payload = json.loads(config.GOOGLE_STATUS_PATH.read_text())
            self.assertEqual(payload["state"], "failed")
            self.assertIn("rclone config reconnect gphotos:", payload["message"])
            self.assertNotIn("invalid_grant", payload["message"])

    def test_other_album_failures_keep_rclones_own_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = self.sync_environment(root)
            (photos / "a.jpg").write_bytes(b"a")
            run, _ = self.fake_rclone()

            def broken(command, *args, **kwargs):
                if command[1] == "mkdir":
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="quota exceeded"
                    )
                return run(command, *args, **kwargs)

            with patch.object(sync.subprocess, "run", broken):
                self.assertEqual(sync.run_sync(), 1)
            self.assertIn(
                "quota exceeded", json.loads(config.GOOGLE_STATUS_PATH.read_text())["message"]
            )


# What rclone really prints when Google refuses the saved sign-in.
EXPIRED_TOKEN = (
    "2026/11/05 16:00:15 ERROR : error listing: couldn't list albums: Get \"https://photoslibrary."
    "googleapis.com/v1/albums?pageSize=50\": couldn't fetch token: invalid_grant: maybe token expired? "
    '- try refreshing with "rclone config reconnect gphotos:"\n'
)


class TestSetupCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        support.isolate_config(self, self.root)
        self.commands = []
        self.remotes = "gphotos:\n"
        self.albums = "Holiday/\nPhoto Offloader/\n"
        self.albums_error = ""
        for item in (
            patch.object(sync, "find_rclone", return_value="/usr/bin/rclone"),
            patch.object(sync.subprocess, "run", self.fake_run),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_run(self, command, *args, **kwargs):
        self.commands.append(list(command))
        if command[1] == "listremotes":
            return subprocess.CompletedProcess(command, 0, stdout=self.remotes, stderr="")
        if self.albums_error:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=self.albums_error)
        return subprocess.CompletedProcess(command, 0, stdout=self.albums, stderr="")

    def check(self):
        """Returns (exit status, printed report, the steps)."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = sync.main(["--check"])
        return status, output.getvalue(), sync.check_setup()

    def test_everything_set_up(self):
        status, report, steps = self.check()
        self.assertEqual(status, 0)
        self.assertEqual([step.passed for step in steps], [True, True, True, True])
        self.assertIn("Sync now", report)

    def test_missing_rclone_stops_at_the_first_step_and_says_how_to_install(self):
        sync.find_rclone.return_value = None
        status, report, steps = self.check()
        self.assertEqual(status, 1)
        self.assertEqual([step.passed for step in steps], [False, None, None, None])
        self.assertIn("brew install rclone", report)
        self.assertIn(sync.SETUP_GUIDE_URL, report)
        self.assertEqual(self.commands, [])

    def test_missing_remote_says_to_run_rclone_config(self):
        self.remotes = "other:\ngphotos-old:\n"
        status, report, steps = self.check()
        self.assertEqual(status, 1)
        self.assertEqual([step.passed for step in steps], [True, False, None, None])
        self.assertIn("rclone config", report)
        self.assertIn(sync.SETUP_GUIDE_URL, report)

    def test_expired_sign_in_says_to_reconnect_and_warns_about_testing_status(self):
        self.albums_error = EXPIRED_TOKEN
        status, report, steps = self.check()
        self.assertEqual(status, 1)
        self.assertEqual([step.passed for step in steps], [True, True, False, None])
        self.assertIn("rclone config reconnect gphotos:", report)
        self.assertIn("In production", report)

    def test_each_google_refusal_gets_its_own_next_step(self):
        cases = {
            "couldn't fetch token: invalid_client: if you're using your own client id/secret": "client ID",
            "empty token found - please reconnect": "rclone config reconnect",
            "Token has been expired or revoked": "rclone config reconnect",
            "Error 403: Photos Library API has not been used in project 123 before or it is disabled": "Photos Library API",
            "Error 403: Request had insufficient authentication scopes": "scopes",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.albums_error = error
                status, report, _ = self.check()
                self.assertEqual(status, 1)
                self.assertIn(expected, report)

    def test_a_network_failure_is_not_reported_as_a_sign_in_problem(self):
        self.albums_error = (
            'ERROR : Get "https://photoslibrary.googleapis.com": dial tcp: no such host\n'
        )
        status, report, _ = self.check()
        self.assertEqual(status, 1)
        self.assertNotIn("reconnect", report)
        self.assertIn("no such host", report)

    def test_rclone_hanging_or_failing_to_start_is_a_failed_step_not_a_crash(self):
        for error in (subprocess.TimeoutExpired("rclone", 60), OSError("no permission")):
            with self.subTest(error=error), patch.object(sync.subprocess, "run", side_effect=error):
                status, _, steps = self.check()
                self.assertEqual(status, 1)
                self.assertEqual([step.passed for step in steps], [True, False, None, None])

    def test_album_not_created_yet_passes_because_the_first_sync_creates_it(self):
        # A new setup has no album; that is not something for the user to fix.
        self.albums = "Holiday/\n"
        status, report, steps = self.check()
        self.assertEqual(status, 0)
        self.assertTrue(steps[-1].passed)
        self.assertIn("first sync", report)

    def test_check_only_reads(self):
        # It runs on a working install, possibly during an upload: it must not create the
        # album or disturb the status the dashboard shows.
        config.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        before = sorted(path for path in self.root.rglob("*"))
        self.check()
        self.assertEqual({command[1] for command in self.commands}, {"listremotes", "lsf"})
        self.assertEqual(sorted(path for path in self.root.rglob("*")), before)

    def test_custom_remote_and_album_are_honored(self):
        self.remotes = "photos-cloud:\n"
        self.albums = "Trips/\n"
        with (
            patch.object(sync, "REMOTE_NAME", "photos-cloud"),
            patch.object(sync, "ALBUM_NAME", "Trips"),
        ):
            status, report, _ = self.check()
        self.assertEqual(status, 0)
        self.assertIn("photos-cloud:album", self.commands[-1])
        self.assertNotIn("first sync", report)

    def test_unknown_arguments_still_print_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(sync.main(["--nope"]), 2)
        self.assertIn("--check", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
