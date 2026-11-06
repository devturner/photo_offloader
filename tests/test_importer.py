#!/usr/bin/env python3
import json
import logging
import logging.handlers
import os
import unittest
from pathlib import Path
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
    def test_a_usb_failure_is_reported_and_the_upload_still_starts(self, notify, launch):
        # Each stage stands alone: the photos are verified on the Mac, so a USB problem must
        # not also keep them from Google Photos until someone notices.
        result = app._report_outcome(
            ImportStats(found=3), ImportStats(failed=2), True, False, False
        )
        self.assertEqual(result, 1)
        launch.assert_called_once_with()
        self.assertEqual(notify.call_args[0][0], "USB Backup Incomplete")
        self.assertIn("copied to the Mac", notify.call_args[0][1])

    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_card_that_would_not_eject_still_gets_its_upload(self, notify, launch):
        result = app._report_outcome(ImportStats(found=3), ImportStats(), True, True, False)
        self.assertEqual(result, 2)
        launch.assert_called_once_with()

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_usb_failure_records_that_the_mac_copy_was_verified(self, notify, launch, eject):
        self.media()
        with (
            patch("time.sleep"),
            patch("photo_offloader.volumes.usb_is_mounted", return_value=True),
            patch("photo_offloader.volumes.get_volume_identity", return_value=None),
            patch.object(app, "_backup_after_import", return_value=ImportStats(failed=1)),
        ):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        launch.assert_called_once_with()
        history = catalog.list_import_history()[0]
        self.assertEqual(history["outcome"], "failed")
        self.assertTrue(history["details"]["local_verified"])

    @patch("photo_offloader.volumes.eject_volume", return_value=True)
    @patch("photo_offloader.importer.launch_google_photos_sync")
    @patch("photo_offloader.logs.notify")
    def test_a_failed_copy_is_not_recorded_as_verified(self, notify, launch, eject):
        self.media()
        with (
            patch("time.sleep"),
            patch("photo_offloader.fileops.copy_file_with_progress", side_effect=OSError("bad")),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 1)
        launch.assert_not_called()
        self.assertNotIn("local_verified", catalog.list_import_history()[0]["details"])

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

    def test_an_import_leaves_the_card_exactly_as_it_was(self):
        self.media()
        self.card.joinpath("PRIVATE").mkdir()
        self.card.joinpath("PRIVATE", "clip.mp4").write_bytes(b"movie")

        def card_state():
            return {
                str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in [self.card, *self.card.rglob("*")]
            }

        before = card_state()
        with patch("time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 0)
        self.assertEqual(card_state(), before)
        self.assertTrue(
            any(entry.get("sha256") for entry in card.load_card_manifest(self.dcim).values())
        )

    def import_counting_end_checks(self):
        with (
            patch("time.sleep"),
            patch.object(fileops, "ends_match", wraps=fileops.ends_match) as ends,
        ):
            code = app.main(["--source", str(self.card), "--no-usb", "--no-eject"])
        return code, ends.call_count

    def test_an_unchanged_card_is_not_read_again_and_new_photos_are_still_saved(self):
        media = self.media()
        self.assertEqual(self.import_counting_end_checks()[0], 0)
        self.assertEqual(self.import_counting_end_checks(), (0, 0))
        new_photo = media["JPG"].with_name("new.jpg")
        new_photo.write_bytes(b"a new photo")
        self.assertEqual(self.import_counting_end_checks(), (0, 0))
        self.assertIn("new.jpg", self.imported_names(self.local))

    def test_a_deleted_photo_means_one_fully_checked_import_then_back_to_fast(self):
        media = self.media()
        self.import_counting_end_checks()
        media["JPG"].unlink()
        code, checks = self.import_counting_end_checks()
        self.assertEqual(code, 0)
        self.assertGreater(checks, 0)
        self.assertEqual(self.import_counting_end_checks(), (0, 0))

    def format_keeping_the_id_with_one_new_photo_matching_an_old_one(self, media):
        """Everything old is gone, and one new photo has an old one's path, size and time."""
        for kind, path in media.items():
            if kind != "RAW":
                path.unlink()
        before = media["RAW"].stat()
        content = bytes(reversed(media["RAW"].read_bytes()))
        media["RAW"].write_bytes(content)
        os.utime(media["RAW"], ns=(before.st_atime_ns, before.st_mtime_ns))
        return content

    def assert_saved_in_raw_archive(self, content):
        saved = [
            path.read_bytes() for path in config.LOCAL_RAW_DEST_ROOT.rglob("*") if path.is_file()
        ]
        self.assertIn(content, saved)

    def test_a_format_that_keeps_the_card_id_cannot_lose_a_new_photo(self):
        media = self.media()
        self.import_counting_end_checks()
        content = self.format_keeping_the_id_with_one_new_photo_matching_an_old_one(media)
        self.assertEqual(self.import_counting_end_checks()[0], 0)
        self.assert_saved_in_raw_archive(content)

    def test_an_interrupted_import_after_such_a_format_cannot_lose_it_either(self):
        media = self.media()
        self.import_counting_end_checks()
        content = self.format_keeping_the_id_with_one_new_photo_matching_an_old_one(media)
        with patch.object(app.transfer, "copy_category", side_effect=OSError("card pulled")):
            self.assertNotEqual(self.import_counting_end_checks()[0], 0)
        self.assertEqual(self.import_counting_end_checks()[0], 0)
        self.assert_saved_in_raw_archive(content)

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
        # This process is already isolated, so its own values are safe. Taking all of them means a
        # new config path can't reach the real install from here.
        isolated = {
            name: str(value)
            for name, value in vars(config).items()
            if name.isupper() and isinstance(value, Path) and name not in support.CODE_PATHS
        }
        script = textwrap.dedent(f"""
            import os, signal, sys
            from pathlib import Path
            from unittest.mock import patch
            sys.path.insert(0, {str(support.REPO_ROOT)!r})
            from photo_offloader import card, config, logs
            from photo_offloader import importer as app
            for name, value in {isolated!r}.items():
                setattr(config, name, Path(value))
            config.STATUS_FILE_PATH = Path({str(status_path)!r})
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
        self.assertEqual(len(list(config.RUN_REPORTS_DIR.iterdir())), 1)  # here, not the real one

    def card_with_sizes(self, jpg=600, raw=600):
        folder = self.dcim / "100NIKON"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "A.JPG").write_bytes(b"j" * jpg)
        (folder / "A.NEF").write_bytes(b"r" * raw)
        return card.find_media_files(self.dcim)

    def test_the_space_check_adds_up_archives_on_the_same_disk(self):
        # Reproduced: 1,200 bytes needed with 1,000 free passed, because each archive folder was
        # checked alone. The copy then ran out of space partway through.
        files = self.card_with_sizes()
        with (
            patch("photo_offloader.volumes.available_bytes", return_value=1000),
            self.assertRaisesRegex(RuntimeError, "Insufficient free space"),
        ):
            app._check_local_space(files, self.dcim)

    def test_the_space_check_treats_archives_on_different_disks_separately(self):
        files = self.card_with_sizes()
        real_stat = os.stat

        class OnItsOwnDisk:
            def __init__(self, info):
                self.st_mode = info.st_mode
                self.st_dev = 999

        def stat_with_raw_elsewhere(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            return OnItsOwnDisk(info) if str(path) == str(config.LOCAL_RAW_DEST_ROOT) else info

        with (
            patch("photo_offloader.volumes.available_bytes", return_value=1000),
            patch("photo_offloader.volumes.os.stat", stat_with_raw_elsewhere),
        ):
            app._check_local_space(files, self.dcim)

    def test_the_space_check_only_counts_files_that_look_new(self):
        # Reproduced: 10 new bytes with 500 free was refused, because a 1,210-byte card counted
        # files already imported.
        files = self.card_with_sizes()
        manifest = {}
        for path in files["JPG"] + files["RAW"]:
            card.update_card_manifest_entry(manifest, path, self.dcim, "verified-hash")
        card.save_card_manifest(self.dcim, manifest)
        (self.dcim / "100NIKON" / "B.JPG").write_bytes(b"n" * 10)
        files = card.find_media_files(self.dcim)
        with patch("photo_offloader.volumes.available_bytes", return_value=500):
            app._check_local_space(files, self.dcim)

    def test_new_files_alone_can_still_be_too_big_for_the_disk(self):
        files = self.card_with_sizes()
        with (
            patch("photo_offloader.volumes.available_bytes", return_value=500),
            self.assertRaisesRegex(RuntimeError, "Insufficient free space"),
        ):
            app._check_local_space(files, self.dcim)

    def test_a_changed_file_counts_as_new_even_with_a_saved_hash(self):
        files = self.card_with_sizes()
        manifest = {}
        for path in files["JPG"] + files["RAW"]:
            card.update_card_manifest_entry(manifest, path, self.dcim, "verified-hash")
        card.save_card_manifest(self.dcim, manifest)
        (self.dcim / "100NIKON" / "A.JPG").write_bytes(b"j" * 900)
        files = card.find_media_files(self.dcim)
        with (
            patch("photo_offloader.volumes.available_bytes", return_value=500),
            self.assertRaisesRegex(RuntimeError, "Insufficient free space"),
        ):
            app._check_local_space(files, self.dcim)

    def test_the_space_check_works_before_the_archive_folders_exist(self):
        files = self.card_with_sizes()
        config.LOCAL_RAW_DEST_ROOT.rmdir()
        app._check_local_space(files, self.dcim)
        self.assertTrue(config.LOCAL_RAW_DEST_ROOT.is_dir())

    def test_a_full_disk_stops_the_import_before_anything_is_copied(self):
        self.card_with_sizes()
        with (
            patch("time.sleep"),
            patch("photo_offloader.logs.notify"),
            patch("photo_offloader.volumes.available_bytes", return_value=500),
        ):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb"]), 1)
        self.assertEqual([p for p in self.local.rglob("*") if p.is_file()], [])

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
        self.assertEqual(status["message"], "Don't format this card: 1 folder couldn't be read.")
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
        self.assertIn("1 folder couldn't be read", status["message"])

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
    def test_camera_index_files_are_not_copied_or_counted(self, notify, launch):
        # User log 2026-11-06: "Local copies: 2" on every insert, with no new photos.
        media = self.media()
        index = media["JPG"].parent / "NC_FLLST.DAT"
        index.write_bytes(b"first")
        self._last_copy_status()
        index.write_bytes(b"rewritten by the camera")
        status = self._last_copy_status()
        self.assertEqual(status["new_files"], 0)
        self.assertEqual(status["new_media_files"], 0)
        self.assertEqual(status["expected_new_files"], 0)
        self.assertNotIn("NC_FLLST.DAT", self.imported_names(self.local))

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
        self.assertIn("3 files couldn't be copied", run["message"])

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
        launch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
