#!/usr/bin/env python3
import json
import logging
import logging.handlers
import os
import unittest
from unittest.mock import patch

from photo_offloader import card, catalog, config, fileops, logs, progress
from photo_offloader import importer as app
from photo_offloader.transfer import ImportStats
from photo_offloader.volumes import VolumeIdentity
from tests import support


class TestImporterCommandLine(support.CardFixtureTestCase):
    def test_fixture_never_reaches_the_installed_google_photos_helper(self):
        with patch.object(
            app.subprocess, "Popen", side_effect=AssertionError("started a real sync")
        ):
            app.launch_google_photos_sync()

    def test_logging(self):
        app.configure_logging(verbose=True)
        logs.logger.info("hello")
        for handler in logs.logger.handlers:
            handler.flush()
        self.assertIn("hello", self.log.read_text())

    def test_log_lines_written_during_a_progress_bar_are_not_prefixed(self):
        # User log 2026-11-04: "on 1181: [..] [DEBUG] Found exiftool". alive-progress
        # hooks logging stream handlers (FileHandler included) and prefixes the bar position.
        app.configure_logging(verbose=True)
        with progress.progress_bar(3, "RAW") as bar:
            bar()
            logs.logger.warning("inside the bar")
        for handler in logs.logger.handlers:
            handler.flush()
        line = next(text for text in self.log.read_text().splitlines() if "inside the bar" in text)
        self.assertTrue(line.startswith("["), f"log line was decorated: {line!r}")

    def test_parse_args(self):
        args = app.parse_args(
            ["--source", str(self.card), "--no-usb", "--no-eject", "--sha256", "--verbose"]
        )
        self.assertEqual(args.source, self.card)
        self.assertTrue(args.no_usb and args.no_eject and args.sha256 and args.verbose)
        self.assertFalse(args.backup_usb)
        self.assertTrue(app.parse_args(["--backup-usb"]).backup_usb)

    @patch("photo_offloader.progress.update_status")
    @patch(
        "photo_offloader.importer.load_build_info",
        return_value={
            "version": "2.4",
            "git_commit": "abc123def456",
            "git_branch": "feature/test",
            "git_tag": "v2.4",
            "git_dirty": "false",
            "build_time_utc": "2026-10-21T22:00:00Z",
            "source_path": "/installed/camera_offloader_v2.py",
        },
    )
    @patch(
        "photo_offloader.importer.import_session_lock",
        side_effect=app.SessionBusyError(
            "Another Photo Offloader import session is already running."
        ),
    )
    def test_duplicate_session_does_not_reset_active_progress(
        self, lock, build_info, update_status
    ):
        self.assertEqual(app.main(["--backup-usb"]), 0)
        update_status.assert_not_called()
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("version=2.4", log)
        self.assertIn("git_commit=abc123def456", log)
        self.assertIn("git_branch=feature/test", log)
        self.assertIn("run_id=", log)

    def test_load_build_info_reads_installed_provenance(self):
        config.BUILD_INFO_PATH.write_text(
            __import__("json").dumps(
                {
                    "version": "2.4",
                    "git_commit": "abc123def456",
                    "git_branch": "feature/test",
                    "git_tag": "v2.4",
                    "git_dirty": "false",
                    "build_time_utc": "2026-10-21T22:00:00Z",
                    "source_path": "/Applications/PhotoOffloader/camera_offloader_v2.py",
                }
            ),
            encoding="utf-8",
        )
        info = app.load_build_info()
        self.assertEqual(info["version"], "2.4")
        self.assertEqual(info["git_commit"], "abc123def456")
        self.assertEqual(info["git_branch"], "feature/test")
        self.assertEqual(info["git_tag"], "v2.4")
        self.assertEqual(info["git_dirty"], "false")
        self.assertEqual(info["source_path"], "/Applications/PhotoOffloader/camera_offloader_v2.py")

    def test_import_session_lock_acquires_and_releases(self):
        with app.import_session_lock():
            self.assertTrue(self.lock.exists())
            self.assertEqual(self.lock.read_text(), str(__import__("os").getpid()))

    def test_import_session_lock_rejects_second_lock(self):
        with app.import_session_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with app.import_session_lock():
                    pass

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=False)
    @patch("photo_offloader.logs.notify")
    def test_main_success_and_resolved_eject_path(self, notify, mounted, eject):
        self.media()
        with patch("time.sleep"):
            result = app.main(["--source", str(self.card)])
        self.assertEqual(result, 0)
        eject.assert_called_once_with(self.card.resolve())

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=False)
    @patch("photo_offloader.logs.notify")
    def test_import_reads_card_metadata_in_one_batch(self, notify, mounted, eject):
        exiftool_calls = self.fake_exiftool()
        self.card_files(12)
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 0)
        self.assertEqual(exiftool_calls(), 1)

    @patch("photo_offloader.volumes.eject_volume")
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=False)
    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.transfer.copy_one_to_destination", side_effect=OSError("copy failure"))
    def test_main_copy_failure_never_ejects(self, copy, notify, mounted, eject):
        self.media()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("photo_offloader.volumes.eject_volume", return_value=False)
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=False)
    @patch("photo_offloader.logs.notify")
    def test_main_eject_failure_returns_2(self, notify, mounted, eject):
        self.media()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 2)
        eject.assert_called_once_with(self.card.resolve())

    @patch("photo_offloader.volumes.eject_volume")
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=True)
    @patch("photo_offloader.logs.notify")
    def test_main_no_usb(self, notify, mounted, eject):
        self.media()
        with (
            patch("photo_offloader.volumes.get_volume_identity", return_value=None),
            patch("time.sleep"),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 0)
        self.assertEqual(list(self.usb.rglob("*")), [])

    @patch("photo_offloader.volumes.eject_volume")
    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=True)
    @patch("photo_offloader.volumes.get_volume_identity")
    def test_main_ejects_after_local_copy_and_reports_usb_replacement(
        self, identity, mounted, notify, eject
    ):
        self.media()
        identity.side_effect = [
            VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a"),
            VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b"),
        ]
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        # Local SSD is verified before ejection; USB replacement is detected
        # afterward, so the card is safely released and backup reports failure.
        eject.assert_called_once_with(self.card.resolve())

    @patch("photo_offloader.transfer.copy_local_archive_to_usb")
    @patch("photo_offloader.logs.notify")
    def test_standalone_usb_backup_does_not_require_card(self, notify, backup):
        backup.return_value = ImportStats(
            destination_copies=2, destination_skips=4, verified_copies=6
        )
        with patch("builtins.input", side_effect=AssertionError("should not prompt for a card")):
            self.assertEqual(app.main(["--backup-usb"]), 0)
        backup.assert_called_once_with()
        notify.assert_called_once()

    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.transfer.copy_local_archive_to_usb", return_value=ImportStats(failed=2))
    def test_a_standalone_usb_backup_reports_its_failures(self, _backup, notify):
        self.assertEqual(app._run_usb_backup(), 1)
        self.assertEqual(notify.call_args[0][0], "USB Backup Incomplete")

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_usb_failure_is_reported_without_starting_the_upload(self, notify, launch):
        result = app._report_outcome(
            ImportStats(found=3), ImportStats(failed=2), True, False, False
        )
        self.assertEqual(result, 1)
        launch.assert_not_called()
        self.assertEqual(notify.call_args[0][0], "USB Backup Incomplete")
        self.assertIn("copied to the Mac", notify.call_args[0][1])

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_finished_import_starts_the_upload(self, notify, launch):
        result = app._report_outcome(ImportStats(found=3), ImportStats(), False, False, False)
        self.assertEqual(result, 0)
        launch.assert_called_once_with()
        self.assertEqual(notify.call_args[0][0], "Photo Import Complete")

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.volumes.eject_volume")
    def test_full_import_copies_private_video_and_unrecognized_files(self, eject, notify, launch):
        self.media()
        sidecar = self.dcim / "100NIKON" / "CLIP0001.XML"
        sidecar.write_bytes(b"<xml/>")
        private_clip = self.card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
        private_clip.parent.mkdir(parents=True)
        private_clip.write_bytes(b"private-video")

        with patch("time.sleep"):
            code = app.main(["--source", str(self.card), "--no-usb", "--no-eject"])

        self.assertEqual(code, 0)
        imported = {p.name: p.parent.name for p in self.local.rglob("*") if p.is_file()}
        self.assertEqual(imported.get("C0001.MP4"), "Movies")
        self.assertEqual(imported.get("CLIP0001.XML"), "Other")
        self.assertIn("unrecognized", self.log.read_text().lower())
        history = card.load_card_manifest_payload(self.dcim)["history"]
        self.assertEqual(history["files_discovered"], 5)  # jpg, nef, mp4, xml, private mp4

    def test_importer_log_file_is_size_capped(self):
        app.configure_logging()
        handlers = [h for h in logs.logger.handlers if isinstance(h, logging.FileHandler)]
        self.assertTrue(handlers)
        self.assertIsInstance(handlers[0], logging.handlers.RotatingFileHandler)
        self.assertGreater(handlers[0].maxBytes, 0)
        self.assertGreaterEqual(handlers[0].backupCount, 1)

    def test_sigterm_cancels_the_import_and_publishes_a_canceled_status(self):
        # `scripts/service.sh stop --force` sends SIGTERM. The importer must
        # shut down like Ctrl-C: release its lock, report "canceled", exit 130.
        import json
        import subprocess
        import sys
        import textwrap

        self.media()
        status_path = self.root / "term-status.json"
        script = textwrap.dedent(f"""
            import os, signal, sys
            from pathlib import Path
            from unittest.mock import patch
            sys.path.insert(0, {str(support.REPO_ROOT)!r})
            from photo_offloader import card, config, logs
            from photo_offloader import importer as app
            config.STATUS_FILE_PATH = Path({str(status_path)!r})
            config.LOG_FILE_PATH = Path({str(self.log)!r})
            config.SESSION_LOCK_PATH = Path({str(self.lock)!r})
            config.CATALOG_DB_PATH = Path({str(self.root / "term-db" / "c.sqlite3")!r})
            config.LEGACY_CATALOG_DB_PATH = Path({str(self.root / "term-legacy.sqlite3")!r})
            config.LOCAL_DEST_ROOT = Path({str(self.local)!r})
            config.LOCAL_RAW_DEST_ROOT = Path({str(self.root / "local-raw")!r})
            def interrupted_scan(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGTERM)   # what `stop --force` does
                raise AssertionError("SIGTERM was not turned into a clean cancel")
            with patch.object(card, "scan_with_retry", interrupted_scan), \
                    patch.object(logs, "notify"):
                sys.exit(app.main(["--source", {str(self.card)!r}, "--no-usb", "--no-eject"]))
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertEqual(json.loads(status_path.read_text())["state"], "canceled")

    def unreadable_card_folder(self):
        if os.geteuid() == 0:
            self.skipTest("root can read every folder")
        folder = self.dcim / "101NIKON"
        folder.mkdir()
        (folder / "lost.jpg").write_bytes(b"x")
        folder.chmod(0)  # TemporaryDirectory restores this when it cleans up
        return folder

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_card_folder_that_cannot_be_read_fails_the_import(self, notify, launch, eject):
        # A failing card does this, and someone told "complete" might format it.
        self.media()
        self.unreadable_card_folder()
        with patch("time.sleep"):
            result = app.main(["--source", str(self.card), "--no-usb"])
        self.assertEqual(result, 1)
        status = json.loads(config.STATUS_FILE_PATH.read_text())
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["message"], "Don't format this card: 1 location couldn't be read.")
        self.assertIn("Don't format", notify.call_args.args[1])
        eject.assert_not_called()
        launch.assert_not_called()
        history = catalog.list_import_history()[0]
        self.assertEqual(history["outcome"], "failed")
        self.assertEqual(history["failed"], 1)
        scan = card.load_card_manifest_payload(self.dcim)["history"]
        self.assertEqual(scan["last_scan_status"], "incomplete")
        self.assertNotIn("last_successful_import", scan)

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.logs.notify")
    def test_what_can_be_read_is_still_copied_from_a_failing_card(self, notify, eject):
        self.media()
        self.unreadable_card_folder()
        with patch("time.sleep"):
            app.main(["--source", str(self.card), "--no-usb"])
        copied = [path.name for path in config.LOCAL_DEST_ROOT.rglob("*") if path.is_file()]
        self.assertIn("photo.jpg", copied)

    @patch("photo_offloader.logs.notify")
    def test_a_card_with_nothing_readable_says_so(self, notify):
        self.unreadable_card_folder()
        with patch("time.sleep"):
            result = app.main(["--source", str(self.card), "--no-usb"])
        self.assertEqual(result, 1)
        status = json.loads(config.STATUS_FILE_PATH.read_text())
        self.assertIn("1 location couldn't be read", status["message"])

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_successful_import_is_recorded_in_history(self, notify, launch):
        self.media()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 0)
        (run,) = catalog.list_import_history()
        self.assertEqual(
            (run["kind"], run["outcome"], run["source"]), ("import", "complete", "card")
        )
        self.assertEqual((run["files"], run["copied"], run["failed"]), (3, 3, 0))
        self.assertGreater(run["bytes"], 0)
        self.assertFalse(run["usb"])

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_history_records_what_this_session_copied_by_type(self, notify, launch):
        # User feedback 2026-11-04: Recent activity should say what this session
        # backed up, not just how many files are on the card.
        media = self.media()
        with patch("time.sleep"):
            app.main(["--source", str(self.card), "--no-usb", "--no-eject"])
        media["JPG"].with_name("second.jpg").write_bytes(b"new jpg")
        with patch("time.sleep"):
            app.main(["--source", str(self.card), "--no-usb", "--no-eject"])
        second, first = catalog.list_import_history()
        self.assertEqual(first["details"]["new"], {"JPG": 1, "RAW": 1, "VIDEO": 1})
        self.assertEqual(second["details"]["new"], {"JPG": 1})
        self.assertEqual(second["details"]["new_bytes"], len(b"new jpg"))
        self.assertEqual(second["files"], 4)
        self.assertIn("copy_seconds", second["details"])
        self.assertNotIn("usb_seconds", second["details"], "there was no USB backup to time")

    def test_a_usb_backup_with_nothing_to_copy_does_not_claim_the_cards_bytes(self):
        # The transfer counter still holds the card's copy, because the USB stage never reset it.
        self.addCleanup(app._RUN.clear)
        app._RUN.update(kind="import")
        identity = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        with (
            patch("photo_offloader.volumes.usb_is_mounted", return_value=True),
            patch("photo_offloader.volumes.get_volume_identity", return_value=identity),
            patch("photo_offloader.transfer.copy_local_archive_to_usb") as backup,
            patch.dict(progress._PROGRESS, {"bytes_transferred": 999}),
        ):
            backup.return_value = ImportStats()
            app._backup_after_import(identity)
        self.assertNotIn("usb_bytes", app._run_details())

    def test_a_stage_is_timed_even_when_it_fails_partway(self):
        self.addCleanup(app._RUN.clear)
        app._RUN.update(kind="import")
        with patch("time.monotonic", side_effect=[100.0, 112.34]), self.assertRaises(OSError):
            with app._timed("copy_seconds"):
                raise OSError("card pulled out")
        self.assertEqual(app._run_details()["copy_seconds"], 12.3)

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_status_reports_stage_and_session_counters(self, notify, launch):
        self.media()
        seen = []
        real = progress.update_status

        def spy(**updates):
            real(**updates)
            seen.append(dict(progress._PROGRESS))

        with patch("time.sleep"), patch.object(progress, "update_status", side_effect=spy):
            app.main(["--source", str(self.card), "--no-usb", "--no-eject"])
        stages = [s.get("stage") for s in seen]
        self.assertLess(stages.index("scan"), stages.index("copy"))
        last_copy = [s for s in seen if s.get("stage") == "copy"][-1]
        self.assertEqual(
            (last_copy["files_done"], last_copy["files_total"], last_copy["new_files"]), (3, 3, 3)
        )
        self.assertFalse(last_copy["usb_planned"])

    def _last_copy_status(self):
        seen = []
        real = progress.update_status

        def spy(**updates):
            real(**updates)
            seen.append(dict(progress._PROGRESS))

        with patch("time.sleep"), patch.object(progress, "update_status", side_effect=spy):
            app.main(["--source", str(self.card), "--no-usb", "--no-eject"])
        return [s for s in seen if s.get("stage") == "copy"][-1]

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_later_import_knows_how_many_files_are_new(self, notify, launch):
        media = self.media()
        first = self._last_copy_status()
        self.assertIsNone(first["expected_new_files"])  # nothing verified on this card yet
        self.assertEqual(first["new_media_files"], 3)
        extra = media["JPG"].parent / "shot-later.jpg"
        extra.write_bytes(b"a new photo")
        second = self._last_copy_status()
        self.assertEqual(second["expected_new_files"], 1)
        self.assertEqual(second["expected_new_bytes"], extra.stat().st_size)
        self.assertEqual(second["new_media_files"], 1)
        self.assertEqual(second["files_total"], 4)

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_rewritten_camera_index_file_is_not_a_new_photo(self, notify, launch):
        media = self.media()
        index = media["JPG"].parent / "NC_FLLST.DAT"
        index.write_bytes(b"first")
        self._last_copy_status()
        index.write_bytes(b"rewritten by the camera")
        status = self._last_copy_status()
        self.assertEqual(status["new_files"], 1)  # history still records what was copied
        self.assertEqual(status["new_media_files"], 0)
        self.assertEqual(status["expected_new_files"], 0)

    @patch("photo_offloader.logs.notify")
    def test_failed_import_is_recorded_with_its_reason(self, notify):
        self.media()
        with (
            patch("time.sleep"),
            patch.object(fileops, "copy_file_with_progress", side_effect=OSError("copy failure")),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 1)
        (run,) = catalog.list_import_history()
        self.assertEqual(run["outcome"], "failed")
        self.assertEqual(run["failed"], 3)
        self.assertIn("failure", run["message"].lower())

    @patch("photo_offloader.logs.notify")
    def test_canceled_import_is_recorded(self, notify):
        self.media()
        with (
            patch("time.sleep"),
            patch.object(card, "scan_with_retry", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 130)
        (run,) = catalog.list_import_history()
        self.assertEqual((run["outcome"], run["source"]), ("canceled", "card"))

    @patch("photo_offloader.logs.notify")
    def test_duplicate_launch_and_missing_card_do_not_create_history(self, notify):
        with patch.object(
            app,
            "import_session_lock",
            side_effect=app.SessionBusyError(
                "Another Photo Offloader import session is already running."
            ),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 0)
        self.assertEqual(catalog.list_import_history(), [])

    def status(self):
        return json.loads(config.STATUS_FILE_PATH.read_text())

    def imported_names(self, *roots):
        return {path.name for root in roots for path in root.rglob("*") if path.is_file()}

    @patch("photo_offloader.logs.notify")
    def test_a_card_with_no_media_stops_with_a_failed_status(self, notify):
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 1)
        self.assertEqual(self.status()["state"], "failed")
        self.assertIn("No supported media files", self.status()["message"])
        self.assertEqual(notify.call_args[0][0], "Photo Offloader Stopped")

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=True)
    @patch("photo_offloader.volumes.get_volume_identity")
    def test_an_import_with_the_drive_mounted_backs_up_to_usb(
        self, identity, mounted, eject, notify, launch
    ):
        identity.return_value = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        self.media()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 0)
        self.assertEqual(
            self.imported_names(self.usb), {"photo.jpg", "photo.nef", "clip.mp4"}, "USB backup"
        )
        launch.assert_called_once_with()
        (run,) = catalog.list_import_history()
        self.assertTrue(run["usb"])
        self.assertEqual(run["details"]["usb_copied"], 3)
        self.assertGreaterEqual(run["details"]["copy_seconds"], 0)
        self.assertGreaterEqual(run["details"]["usb_seconds"], 0)
        self.assertEqual(
            run["details"]["usb_bytes"], sum(path.stat().st_size for path in self.media().values())
        )

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.volumes.usb_is_mounted", return_value=True)
    @patch("photo_offloader.volumes.get_volume_identity")
    @patch("photo_offloader.transfer.copy_local_archive_to_usb", side_effect=OSError("usb died"))
    def test_a_usb_crash_does_not_invalidate_the_verified_import(
        self, _backup, identity, mounted, eject, notify, launch
    ):
        identity.return_value = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        self.media()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        self.assertEqual(
            self.imported_names(self.local, config.LOCAL_RAW_DEST_ROOT),
            {"photo.jpg", "photo.nef", "clip.mp4"},
        )
        eject.assert_called_once_with(self.card.resolve())  # the card is safe to release
        self.assertEqual(notify.call_args[0][0], "USB Backup Incomplete")
        self.assertIn("copied to the Mac", notify.call_args[0][1])
        self.assertIn("Local import succeeded", self.status()["message"])
        self.assertEqual(
            card.load_card_manifest_payload(self.dcim)["history"]["last_scan_status"], "complete"
        )
        (run,) = catalog.list_import_history()
        self.assertEqual((run["copied"], run["failed"]), (3, 0))  # counts the local import
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
