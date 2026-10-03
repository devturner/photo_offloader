#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import camera_offloader_v2 as app
from camera_offloader_v2 import DestinationResult, DestinationSnapshot, FileFingerprint, ImportStats, VolumeIdentity


class TestHardeningRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        self.usb = self.root / "usb"
        self.source = self.root / "source.jpg"
        self.local.mkdir()
        self.usb.mkdir()
        self.source.write_bytes(b"photo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_destination_result_defaults_are_stable(self):
        result = DestinationResult(self.local, "copied")
        self.assertEqual(result.destination, self.local)
        self.assertEqual(result.status, "copied")
        self.assertIsNone(result.path)
        self.assertEqual(result.message, "")

    def test_destination_snapshot_is_immutable_and_ordered(self):
        identity = VolumeIdentity(self.usb, "disk4s1", "uuid-a")
        snapshot = DestinationSnapshot((self.local, self.usb), True, identity)
        self.assertEqual(snapshot.destinations, (self.local, self.usb))
        self.assertTrue(snapshot.usb_expected)
        self.assertEqual(snapshot.usb_identity.device_identifier, "disk4s1")
        with self.assertRaises(AttributeError):
            snapshot.usb_expected = False

    def test_file_fingerprint_captures_stat_identity(self):
        fingerprint = app.file_fingerprint(self.source)
        stat = self.source.stat()
        self.assertEqual(fingerprint, FileFingerprint(stat.st_size, stat.st_mtime_ns, stat.st_ino))

    def test_copy_category_records_partial_destination_failure(self):
        stats = ImportStats(found=1)
        copied_path = self.local / "photo.jpg"
        copied_path.write_bytes(b"photo")
        results = [
            DestinationResult(self.local, "copied", copied_path, "copied and verified"),
        ]

        with patch("camera_offloader_v2.copy_one_to_destination", side_effect=[results[0], OSError("backup unavailable")]):
            app.copy_category([self.source], self.root, [self.local, self.usb], "JPG", stats)

        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.verified_copies, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("backup unavailable", stats.errors[0][1])

    def test_volume_identity_falls_back_to_device_identifier(self):
        mount = self.root / "volume"
        mount.mkdir()
        expected = VolumeIdentity(mount, "disk4s1", None)
        current = VolumeIdentity(mount, "disk4s1", None)
        replacement = VolumeIdentity(mount, "disk5s1", None)
        self.assertTrue(app.volume_identity_matches(expected, current))
        self.assertFalse(app.volume_identity_matches(expected, replacement))


if __name__ == "__main__":
    unittest.main()
