#!/usr/bin/env python3
import errno
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import camera_offloader_v2 as app
from camera_offloader_v2 import (
    ImportStats,
    VolumeIdentity,
)


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
        self.lock = self.root / "import.lock"
        self.dcim.mkdir(parents=True)
        self.local.mkdir()
        self.usb.mkdir()
        self.usb_volume.mkdir()
        self.original = {k: getattr(app, k) for k in (
            "LOCAL_DEST_ROOT", "USB_DEST_ROOT", "USB_DEST_VOLUME",
            "LOG_FILE_PATH", "SESSION_LOCK_PATH", "VERIFY_WITH_SHA256",
            "USE_FILE_BIRTH_TIME", "PRESERVE_CAMERA_SUBFOLDERS", "WAIT_SECONDS",
            "EJECT_CARD_AFTER_SUCCESS", "MIN_FREE_SPACE_BUFFER_BYTES",
        )}
        app.LOCAL_DEST_ROOT = self.local
        app.USB_DEST_ROOT = self.usb
        app.USB_DEST_VOLUME = self.usb_volume
        app.LOG_FILE_PATH = self.log
        app.SESSION_LOCK_PATH = self.lock
        app.VERIFY_WITH_SHA256 = False
        app.USE_FILE_BIRTH_TIME = False
        app.PRESERVE_CAMERA_SUBFOLDERS = True
        app.WAIT_SECONDS = 0
        app.EJECT_CARD_AFTER_SUCCESS = True
        app.MIN_FREE_SPACE_BUFFER_BYTES = 0
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

    def test_stats_aliases_and_success(self):
        stats = ImportStats(destination_copies=2, destination_skips=1, verified_copies=3)
        self.assertTrue(stats.successful)
        self.assertEqual((stats.copied, stats.skipped, stats.verified), (2, 1, 3))
        stats.errors.append(("x", "y"))
        self.assertFalse(stats.successful)

    def test_logging(self):
        app.configure_logging(verbose=True)
        app.logger.info("hello")
        for handler in app.logger.handlers:
            handler.flush()
        self.assertIn("hello", self.log.read_text())

    def test_parse_args(self):
        args = app.parse_args(["--source", str(self.card), "--no-usb", "--no-eject", "--sha256", "--verbose"])
        self.assertEqual(args.source, self.card)
        self.assertTrue(args.no_usb and args.no_eject and args.sha256 and args.verbose)

    @patch("camera_offloader_v2.subprocess.run")
    def test_notify_nonfatal(self, run):
        run.return_value = MagicMock(returncode=0, stderr="")
        app.notify("T", "B")
        run.return_value = MagicMock(returncode=1, stderr="bad")
        app.notify("T", "B")
        self.assertEqual(run.call_count, 2)

    def test_resolve_dcim(self):
        self.assertEqual(app.resolve_dcim_path(self.card), self.dcim.resolve())
        self.assertEqual(app.resolve_dcim_path(self.dcim), self.dcim.resolve())

    def test_resolve_dcim_rejects_missing(self):
        with self.assertRaises(RuntimeError):
            app.resolve_dcim_path(self.root / "missing")

    @patch("camera_offloader_v2.glob.glob", return_value=[])
    def test_find_card_no_match(self, glob):
        with patch("builtins.input", return_value=""), self.assertRaises(RuntimeError):
            app.find_card_dcim()

    def test_scan_filters_hidden_and_unsupported(self):
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
        with patch("camera_offloader_v2.time.monotonic", side_effect=[0, 1]), patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.scan_with_retry(self.dcim), found)

    def test_destination_path(self):
        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x")
        month = app.get_month_folder(source)
        result = app.build_destination_path(source, self.dcim, self.local)
        self.assertEqual(result, self.local / month / "100NIKON" / "IMG.JPG")

    def test_files_match_requires_sha_for_existing_duplicate(self):
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"abc")
        b.write_bytes(b"xyz")
        self.assertTrue(app.files_match(a, b))
        self.assertFalse(app.files_match(a, b, force_sha256=True))
        b.write_bytes(b"abc")
        self.assertTrue(app.files_match(a, b, force_sha256=True))

    def test_import_session_lock_acquires_and_releases(self):
        with app.import_session_lock():
            self.assertTrue(self.lock.exists())
            self.assertEqual(self.lock.read_text(), str(__import__("os").getpid()))

    def test_import_session_lock_rejects_second_lock(self):
        with app.import_session_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with app.import_session_lock():
                    pass

    def test_source_fingerprint(self):
        source = self.root / "source"
        source.write_bytes(b"abc")
        fp = app.file_fingerprint(source)
        self.assertEqual(fp.size, 3)
        self.assertGreaterEqual(fp.mtime_ns, 0)

    def test_install_cross_device(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        with patch("camera_offloader_v2.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            self.assertTrue(app.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")

    def test_copy_and_verify(self):
        source = self.media()["RAW"]
        source.chmod(0o700)
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.read_bytes(), b"raw")
        self.assertEqual(stat.S_IMODE(result.path.stat().st_mode), 0o600)


    def test_matching_duplicate_is_content_verified(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")

    def test_same_size_different_content_gets_conflict(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"xxx")  # same size as b"raw"
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertIn("__conflict-1", result.path.name)
        self.assertEqual(destination.read_bytes(), b"xxx")
        self.assertEqual(result.path.read_bytes(), b"raw")

    def test_conflict_counter_advances(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old")
        destination.with_name("photo__conflict-1.nef").write_bytes(b"old")
        destination.with_name("photo__conflict-2.nef").write_bytes(b"old")
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.path.name, "photo__conflict-3.nef")

    def test_source_mutation_is_rejected(self):
        source = self.media()["RAW"]
        original_copy = app.shutil.copy2

        def mutate(src, dst):
            original_copy(src, dst)
            Path(src).write_bytes(b"RAW")  # changes size

        with patch("camera_offloader_v2.shutil.copy2", side_effect=mutate):
            with self.assertRaisesRegex(IOError, "Source changed"):
                app.copy_one_to_destination(source, self.dcim, self.local)

    def test_required_source_bytes(self):
        files = self.media()
        self.assertEqual(app.required_source_bytes({k: [v] for k, v in files.items()}), 11)

    def test_validate_destinations_checks_space(self):
        with patch("camera_offloader_v2.available_bytes", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "Insufficient free space"):
                app.validate_destinations([self.local], required_bytes=11)

    def test_volume_identity_matching(self):
        expected = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        current = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        replaced = VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b")
        self.assertTrue(app.volume_identity_matches(expected, current))
        self.assertFalse(app.volume_identity_matches(expected, replaced))

    @patch("camera_offloader_v2.subprocess.run")
    def test_get_volume_identity(self, run):
        payload = plistlib.dumps({
            "DeviceIdentifier": "disk4s1",
            "VolumeUUID": "uuid-a",
        })
        run.return_value = MagicMock(returncode=0, stdout=payload)
        identity = app.get_volume_identity(self.usb_volume)
        self.assertEqual(identity.volume_uuid, "uuid-a")
        self.assertEqual(identity.device_identifier, "disk4s1")

    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_snapshot_local_only(self, mounted):
        snapshot = app.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local,))
        self.assertFalse(snapshot.usb_expected)

    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.get_volume_identity")
    def test_snapshot_includes_usb(self, identity, mounted):
        identity.return_value = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        snapshot = app.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local, self.usb))
        self.assertTrue(snapshot.usb_expected)
        self.assertEqual(snapshot.usb_identity.volume_uuid, "uuid-a")

    @patch("camera_offloader_v2.subprocess.run")
    def test_eject_success_failure(self, run):
        with patch.object(Path, "is_mount", return_value=True):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(app.eject_volume(self.card))
            run.return_value = MagicMock(returncode=1, stdout="", stderr="bad")
            self.assertFalse(app.eject_volume(self.card))

    @patch("camera_offloader_v2.eject_volume", return_value=True)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    def test_main_success_and_resolved_eject_path(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            result = app.main(["--source", str(self.card)])
        self.assertEqual(result, 0)
        eject.assert_called_once_with(self.card.resolve())

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.copy_one_to_destination", side_effect=OSError("copy failure"))
    def test_main_copy_failure_never_ejects(self, copy, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("camera_offloader_v2.eject_volume", return_value=False)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    def test_main_eject_failure_returns_2(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 2)
        eject.assert_called_once_with(self.card.resolve())

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.notify")
    def test_main_no_usb(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.get_volume_identity", return_value=None), patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 0)
        self.assertEqual(list(self.usb.rglob("*")), [])

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.get_volume_identity")
    def test_main_usb_replacement_never_ejects(self, identity, mounted, notify, eject):
        self.media()
        identity.side_effect = [
            VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a"),
            VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b"),
        ]
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-eject"]), 1)
        eject.assert_not_called()

    def test_second_import_skips(self):
        self.media()
        files = app.find_media_files(self.dcim)
        first = ImportStats(found=3)
        second = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local], category, first)
            app.copy_category(files[category], self.dcim, [self.local], category, second)
        self.assertEqual(first.destination_copies, 3)
        self.assertEqual(second.destination_skips, 3)

    def test_end_to_end_two_destinations(self):
        self.media()
        files = app.find_media_files(self.dcim)
        stats = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local, self.usb], category, stats)
        self.assertEqual((stats.destination_copies, stats.verified_copies, stats.failed), (6, 6, 0))


if __name__ == "__main__":
    unittest.main()
