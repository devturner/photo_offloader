#!/usr/bin/env python3
import errno
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import camera_offloader_v2 as app
from camera_offloader_v2 import DestinationResult, ImportStats


class TestCameraOffloader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.card = self.root / "card"
        self.dcim = self.card / "DCIM"
        self.local = self.root / "local"
        self.usb = self.root / "usb"
        self.usb_volume = self.root / "usb-volume"
        self.log = self.root / "transfer.log"
        self.dcim.mkdir(parents=True)
        self.local.mkdir()
        self.usb.mkdir()
        self.usb_volume.mkdir()
        self.original = {k: getattr(app, k) for k in (
            "LOCAL_DEST_ROOT", "USB_DEST_ROOT", "USB_DEST_VOLUME",
            "LOG_FILE_PATH", "VERIFY_WITH_SHA256", "USE_FILE_BIRTH_TIME",
            "PRESERVE_CAMERA_SUBFOLDERS", "WAIT_SECONDS",
            "EJECT_CARD_AFTER_SUCCESS", "HAS_ALIVE_PROGRESS")}
        app.LOCAL_DEST_ROOT = self.local
        app.USB_DEST_ROOT = self.usb
        app.USB_DEST_VOLUME = self.usb_volume
        app.LOG_FILE_PATH = self.log
        app.VERIFY_WITH_SHA256 = False
        app.USE_FILE_BIRTH_TIME = False
        app.PRESERVE_CAMERA_SUBFOLDERS = True
        app.WAIT_SECONDS = 0
        app.EJECT_CARD_AFTER_SUCCESS = True
        app.HAS_ALIVE_PROGRESS = False
        app.logger.handlers.clear()

    def tearDown(self):
        for handler in app.logger.handlers[:]:
            handler.close()
        app.logger.handlers.clear()
        for key, value in self.original.items():
            setattr(app, key, value)
        self.tmp.cleanup()

    def media(self):
        folder = self.dcim / "100NIKON"
        folder.mkdir(parents=True, exist_ok=True)
        files = {
            "JPG": folder / "photo.jpg",
            "RAW": folder / "photo.nef",
            "VIDEO": folder / "clip.mp4",
        }
        files["JPG"].write_bytes(b"jpg")
        files["RAW"].write_bytes(b"raw")
        files["VIDEO"].write_bytes(b"video")
        return files

    # Configuration / logging / CLI
    def test_stats_aliases_and_success(self):
        stats = ImportStats(destination_copies=2, destination_skips=1, verified_copies=3)
        self.assertTrue(stats.successful)
        self.assertEqual((stats.copied, stats.skipped, stats.verified), (2, 1, 3))
        stats.errors.append(("x", "y"))
        self.assertFalse(stats.successful)

    def test_logging_writes_file(self):
        app.configure_logging(verbose=True)
        app.logger.info("hello")
        for handler in app.logger.handlers:
            handler.flush()
        self.assertIn("hello", self.log.read_text())

    def test_parse_args(self):
        args = app.parse_args(
            [
                "--source",
                str(self.card),
                "--no-usb",
                "--no-eject",
                "--sha256",
                "--verbose",
            ]
        )
        self.assertEqual(args.source, self.card)
        self.assertTrue(args.no_usb and args.no_eject and args.sha256 and args.verbose)

    # Notifications / OS integration
    @patch("camera_offloader_v2.subprocess.run")
    def test_notify_success_and_failure_are_nonfatal(self, run):
        run.return_value = MagicMock(returncode=0, stderr="")
        app.notify("T", "B")
        run.return_value = MagicMock(returncode=1, stderr="bad")
        app.notify("T", "B")
        self.assertEqual(run.call_count, 2)

    @patch("camera_offloader_v2.subprocess.run", side_effect=OSError("missing"))
    def test_notify_oserror_is_nonfatal(self, run):
        app.notify("T", "B")
        run.assert_called_once()

    # Discovery / scanning
    def test_resolve_dcim_accepts_volume_and_dcim(self):
        self.assertEqual(app.resolve_dcim_path(self.card), self.dcim.resolve())
        self.assertEqual(app.resolve_dcim_path(self.dcim), self.dcim.resolve())

    def test_resolve_dcim_rejects_missing(self):
        with self.assertRaises(RuntimeError):
            app.resolve_dcim_path(self.root / "missing")

    @patch("camera_offloader_v2.glob.glob", return_value=["/Volumes/NIKON/DCIM"])
    def test_find_card_single_match(self, glob):
        with (
            patch.object(Path, "is_dir", return_value=True),
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(app.find_card_dcim(), Path("/Volumes/NIKON/DCIM"))

    @patch("camera_offloader_v2.glob.glob", return_value=[])
    def test_find_card_no_match(self, glob):
        with patch("builtins.input", return_value=""), self.assertRaises(RuntimeError):
            app.find_card_dcim()

    def test_find_media_files_filters_and_sorts(self):
        self.media()
        (self.dcim / ".hidden.jpg").write_bytes(b"x")
        (self.dcim / "._meta.jpg").write_bytes(b"x")
        (self.dcim / "notes.txt").write_bytes(b"x")
        found = app.find_media_files(self.dcim)
        self.assertEqual([p.name for p in found["JPG"]], ["photo.jpg"])
        self.assertEqual([p.name for p in found["RAW"]], ["photo.nef"])
        self.assertEqual([p.name for p in found["VIDEO"]], ["clip.mp4"])

    @patch("camera_offloader_v2.find_media_files")
    def test_scan_retry(self, scan):
        empty = {"JPG": [], "RAW": [], "VIDEO": []}
        found = {"JPG": [self.dcim / "x.jpg"], "RAW": [], "VIDEO": []}
        scan.side_effect = [empty, found]
        app.WAIT_SECONDS = 30
        with (
            patch("camera_offloader_v2.time.monotonic", side_effect=[0, 1]),
            patch("camera_offloader_v2.time.sleep"),
        ):
            self.assertEqual(app.scan_with_retry(self.dcim), found)
        self.assertEqual(scan.call_count, 2)

    # Paths / verification
    def test_destination_path_and_month(self):
        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x")
        month = datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y-%m")
        result = app.build_destination_path(source, self.dcim, self.local)
        self.assertEqual(result, self.local / month / "100NIKON" / "IMG.JPG")

    def test_get_month_folder_prefers_exif_date(self):
        from PIL import Image

        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)

        image = Image.new("RGB", (1, 1), color="red")
        exif = Image.Exif()
        exif[36867] = "2024:01:02 03:04:05"
        image.save(source, format="JPEG", exif=exif)

        self.assertEqual(app.get_month_folder(source), "2024-01")

    def test_exif_datetime_normalizes_common_camera_formats(self):
        from PIL import Image

        source = self.dcim / "100NIKON" / "IMG2.JPG"
        source.parent.mkdir(parents=True)

        for raw_value in ["2024:01:02 03:04:05", "2024-01-02T03:04:05", "2024/01/02 03:04:05"]:
            image = Image.new("RGB", (1, 1), color="red")
            exif = Image.Exif()
            exif[36867] = raw_value
            image.save(source, format="JPEG", exif=exif)
            self.assertEqual(app.get_exif_datetime(source), datetime(2024, 1, 2, 3, 4, 5))

    def test_flatten_destination_path(self):
        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x")
        app.PRESERVE_CAMERA_SUBFOLDERS = False
        result = app.build_destination_path(source, self.dcim, self.local)
        self.assertEqual(result.name, "IMG.JPG")
        self.assertEqual(result.parent.name, app.get_month_folder(source))

    def test_files_match_size_and_sha256(self):
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"abc")
        b.write_bytes(b"xyz")
        self.assertTrue(app.files_match(a, b))  # size-only mode
        app.VERIFY_WITH_SHA256 = True
        self.assertFalse(app.files_match(a, b))
        b.write_bytes(b"abc")
        self.assertTrue(app.files_match(a, b))

    # Installation
    def test_install_hard_link_and_existing_file(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        self.assertTrue(app.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")
        self.assertFalse(app.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")

    @patch("camera_offloader_v2.os.link", side_effect=OSError(errno.EXDEV, "cross-device"))
    def test_install_cross_device_fallback(self, link):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        self.assertTrue(app.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")

    @patch("camera_offloader_v2.os.link", side_effect=OSError(errno.EXDEV, "cross-device"))
    @patch("camera_offloader_v2.shutil.copyfileobj", side_effect=OSError("write failed"))
    def test_install_fallback_cleans_partial_file(self, copy, link):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        with self.assertRaises(OSError):
            app.install_without_overwrite(temp, final)
        self.assertFalse(final.exists())

    # Copy / collision safety
    def test_copy_and_verify(self):
        source = self.media()["RAW"]
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.read_bytes(), b"raw")
        self.assertEqual(list(self.local.rglob(".photo-copy-*")), [])

    def test_matching_duplicate_is_skipped(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.path, destination)

    def test_different_duplicate_gets_conflict_name(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"different")
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(destination.read_bytes(), b"different")
        self.assertEqual(result.path.read_bytes(), b"raw")
        self.assertIn("__conflict-1", result.path.name)

    def test_conflict_counter_advances(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"older")
        destination.with_name("photo__conflict-1.nef").write_bytes(b"old1")
        destination.with_name("photo__conflict-2.nef").write_bytes(b"old2")
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.path.name, "photo__conflict-3.nef")

    @patch("camera_offloader_v2.shutil.copy2", side_effect=OSError("copy failed"))
    def test_copy_failure_leaves_no_temp_file(self, copy):
        source = self.media()["RAW"]
        with self.assertRaises(OSError):
            app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(list(self.local.rglob(".photo-copy-*")), [])

    @patch("camera_offloader_v2.files_match", side_effect=[False, True, True])
    @patch("camera_offloader_v2.install_without_overwrite", return_value=True)
    @patch("camera_offloader_v2.shutil.copy2")
    @patch("camera_offloader_v2.tempfile.mkstemp")
    @patch("camera_offloader_v2.os.close")
    def test_conflict_test_exercises_false_then_true_match_sequence(
        self,
        close,
        mkstemp,
        copy2,
        install,
        match,
    ):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"different")
        temp = self.local / ".photo-copy-test.tmp"
        temp.write_bytes(b"raw")
        mkstemp.return_value = (1, str(temp))
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(match.call_count, 3)

    # Categories / destinations
    @patch("camera_offloader_v2.copy_one_to_destination")
    def test_copy_category_tracks_copies_and_skips(self, copy):
        copy.side_effect = [
            DestinationResult(self.local, "copied"),
            DestinationResult(self.local, "skipped"),
        ]
        stats = ImportStats()
        app.copy_category([Path("a.jpg"), Path("b.jpg")], self.dcim, [self.local], "JPG", stats)
        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.destination_skips, 1)
        self.assertEqual(stats.verified_copies, 2)

    @patch("camera_offloader_v2.copy_one_to_destination", side_effect=OSError("I/O"))
    def test_copy_category_records_failure(self, copy):
        stats = ImportStats()
        app.copy_category([Path("a.jpg")], self.dcim, [self.local], "JPG", stats)
        self.assertEqual(stats.failed, 1)
        self.assertIn("I/O", stats.errors[0][1])

    @patch("camera_offloader_v2.copy_one_to_destination")
    def test_copy_category_attempts_other_destination_after_failure(self, copy):
        copy.side_effect = [OSError("local"), DestinationResult(self.usb, "copied")]
        stats = ImportStats()
        app.copy_category([Path("a.jpg")], self.dcim, [self.local, self.usb], "JPG", stats)
        self.assertEqual(copy.call_count, 2)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.destination_copies, 1)

    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_build_destinations_local_only(self, mounted):
        self.assertEqual(app.build_destinations(), [self.local])

    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    def test_build_destinations_includes_usb(self, mounted):
        self.assertEqual(app.build_destinations(), [self.local, self.usb])

    def test_validate_destinations(self):
        destination = self.root / "new"
        app.validate_destinations([destination])
        self.assertTrue(destination.is_dir())
        self.assertFalse((destination / ".camera-offloader-write-test").exists())

    # Ejection
    @patch("camera_offloader_v2.subprocess.run")
    def test_eject_success_and_failure(self, run):
        with patch.object(Path, "is_mount", return_value=True):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(app.eject_volume(self.card))
            run.return_value = MagicMock(returncode=1, stdout="", stderr="bad")
            self.assertFalse(app.eject_volume(self.card))

    @patch("camera_offloader_v2.subprocess.run", side_effect=OSError("missing"))
    def test_eject_oserror(self, run):
        with patch.object(Path, "is_mount", return_value=True):
            self.assertFalse(app.eject_volume(self.card))

    def test_eject_not_mounted(self):
        with patch.object(Path, "is_mount", return_value=False):
            self.assertFalse(app.eject_volume(self.card))

    # Main safety workflow
    def run_main(self, args):
        with patch("camera_offloader_v2.time.sleep"):
            return app.main(args)

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume", return_value=True)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_main_success_and_eject(self, mounted, eject, notify):
        self.media()
        result = self.run_main(["--source", str(self.card)])
        self.assertEqual(result, 0)
        eject.assert_called_once_with(self.card.resolve())
        notify.assert_called_once()

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.copy_one_to_destination", side_effect=OSError("copy failure"))
    def test_main_copy_failure_never_ejects(self, copy, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume", return_value=False)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_main_eject_failure_returns_2(self, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card)]), 2)
        eject.assert_called_once_with(self.card.resolve())
        self.assertIn("Eject Failed", notify.call_args.args[0])

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_main_no_eject(self, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card), "--no-eject"]), 0)
        eject.assert_not_called()

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    def test_main_no_usb_uses_only_local(self, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card), "--no-usb"]), 0)
        self.assertEqual([p for p in self.usb.rglob("*") if p.is_file()], [])

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume", return_value=True)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    def test_main_two_destinations(self, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card)]), 0)
        local = sorted(p.relative_to(self.local) for p in self.local.rglob("*") if p.is_file())
        usb = sorted(p.relative_to(self.usb) for p in self.usb.rglob("*") if p.is_file())
        self.assertEqual(local, usb)
        self.assertEqual(len(local), 3)

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", side_effect=[True, True, False])
    def test_main_usb_disconnect_never_ejects(self, mounted, eject, notify):
        self.media()
        self.assertEqual(self.run_main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_main_empty_card_fails_without_eject(self, mounted, eject, notify):
        self.assertEqual(self.run_main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_main_keyboard_interrupt_returns_130(self, mounted, eject, notify):
        with patch("camera_offloader_v2.scan_with_retry", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(["--source", str(self.card)]), 130)
        eject.assert_not_called()

    # Integration-style filesystem tests
    def test_end_to_end_two_destination_import(self):
        self.media()
        files = app.find_media_files(self.dcim)
        stats = ImportStats(found=sum(len(v) for v in files.values()))
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local, self.usb], category, stats)
        self.assertEqual(
            (stats.found, stats.destination_copies, stats.verified_copies, stats.failed),
            (3, 6, 6, 0),
        )
        local = sorted(p.relative_to(self.local) for p in self.local.rglob("*") if p.is_file())
        usb = sorted(p.relative_to(self.usb) for p in self.usb.rglob("*") if p.is_file())
        self.assertEqual(local, usb)
        for rel in local:
            self.assertEqual((self.local / rel).read_bytes(), (self.usb / rel).read_bytes())

    def test_second_import_skips_matching_files(self):
        self.media()
        files = app.find_media_files(self.dcim)
        first = ImportStats(found=3)
        second = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local], category, first)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local], category, second)
        self.assertEqual(first.destination_copies, 3)
        self.assertEqual(second.destination_copies, 0)
        self.assertEqual(second.destination_skips, 3)
        self.assertEqual(second.failed, 0)


if __name__ == "__main__":
    unittest.main()
