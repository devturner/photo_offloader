#!/usr/bin/env python3
import plistlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from photo_offloader import volumes
from photo_offloader.volumes import DestinationSnapshot, VolumeIdentity
from tests import support


class TestVolumes(support.CardFixtureTestCase):
    def test_required_source_bytes(self):
        files = self.media()
        self.assertEqual(volumes.required_source_bytes({k: [v] for k, v in files.items()}), 11)

    def test_validate_destinations_checks_space(self):
        with patch("photo_offloader.volumes.available_bytes", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "Insufficient free space"):
                volumes.validate_destinations([self.local], required_bytes=11)

    def test_volume_identity_matching(self):
        expected = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        current = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        replaced = VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b")
        self.assertTrue(volumes.volume_identity_matches(expected, current))
        self.assertFalse(volumes.volume_identity_matches(expected, replaced))

    @patch("subprocess.run")
    def test_get_volume_identity(self, run):
        payload = plistlib.dumps(
            {
                "DeviceIdentifier": "disk4s1",
                "VolumeUUID": "uuid-a",
            }
        )
        run.return_value = MagicMock(returncode=0, stdout=payload)
        identity = volumes.get_volume_identity(self.usb_volume)
        self.assertEqual(identity.volume_uuid, "uuid-a")
        self.assertEqual(identity.device_identifier, "disk4s1")

    @patch("photo_offloader.volumes.usb_is_mounted", return_value=False)
    def test_snapshot_local_only(self, mounted):
        snapshot = volumes.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local,))
        self.assertFalse(snapshot.usb_expected)

    @patch("photo_offloader.volumes.usb_is_mounted", return_value=True)
    @patch("photo_offloader.volumes.get_volume_identity")
    def test_snapshot_includes_usb(self, identity, mounted):
        identity.return_value = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        snapshot = volumes.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local, self.usb))
        self.assertTrue(snapshot.usb_expected)
        self.assertEqual(snapshot.usb_identity.volume_uuid, "uuid-a")

    @patch("subprocess.run")
    def test_eject_success_failure(self, run):
        with patch.object(Path, "is_mount", return_value=True):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(volumes.eject_volume(self.card))
            run.return_value = MagicMock(returncode=1, stdout="", stderr="bad")
            self.assertFalse(volumes.eject_volume(self.card))

    def test_destination_snapshot_is_immutable_and_ordered(self):
        identity = VolumeIdentity(self.usb, "disk4s1", "uuid-a")
        snapshot = DestinationSnapshot((self.local, self.usb), True, identity)
        self.assertEqual(snapshot.destinations, (self.local, self.usb))
        self.assertTrue(snapshot.usb_expected)
        self.assertEqual(snapshot.usb_identity.device_identifier, "disk4s1")
        with self.assertRaises(AttributeError):
            snapshot.usb_expected = False

    def test_volume_identity_falls_back_to_device_identifier(self):
        mount = self.root / "volume"
        mount.mkdir()
        expected = VolumeIdentity(mount, "disk4s1", None)
        current = VolumeIdentity(mount, "disk4s1", None)
        replacement = VolumeIdentity(mount, "disk5s1", None)
        self.assertTrue(volumes.volume_identity_matches(expected, current))
        self.assertFalse(volumes.volume_identity_matches(expected, replacement))

    def test_volume_identity_rejects_a_missing_volume_or_another_mount_point(self):
        expected = VolumeIdentity(self.usb_volume, "disk4s1", "uuid-a")
        elsewhere = VolumeIdentity(self.root / "other", "disk4s1", "uuid-a")
        self.assertFalse(volumes.volume_identity_matches(expected, None))
        self.assertFalse(volumes.volume_identity_matches(expected, elsewhere))

    def test_volume_identity_without_ids_falls_back_to_the_mount_point(self):
        expected = VolumeIdentity(self.usb_volume, None, None)
        self.assertTrue(
            volumes.volume_identity_matches(expected, VolumeIdentity(self.usb_volume, None, None))
        )


if __name__ == "__main__":
    unittest.main()
