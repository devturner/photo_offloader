"""The importer notes which card it read, and says when a card has become safe to format."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from photo_offloader import card_identity, card_status, catalog, config, jsonfile
from photo_offloader import importer as app
from photo_offloader.volumes import VolumeIdentity
from tests import support

SD = {"kind": "SD", "size_bytes": 63_864_569_856, "maker": "SanDisk", "serial": "0x1"}


@patch("photo_offloader.importer.launch_google_photos_sync")
@patch("photo_offloader.volumes.eject_volume", return_value=True)
@patch("photo_offloader.logs.notify")
class TestCardSafetyAfterRuns(support.CardFixtureTestCase):
    def import_card(self, usb, *extra, identity=SD):
        drive = VolumeIdentity(self.usb_volume.resolve(), "disk9s1", "usb-uuid")
        with (
            patch("time.sleep"),
            patch("photo_offloader.volumes.usb_is_mounted", return_value=usb),
            patch("photo_offloader.volumes.get_volume_identity", return_value=drive),
            patch.object(card_identity, "read", return_value=dict(identity)),
        ):
            return app.main(["--source", str(self.card), "--sha256", *extra])

    def back_up(self):
        identity = VolumeIdentity(self.usb_volume.resolve(), "disk9s1", "usb-uuid")
        with (
            patch("photo_offloader.volumes.usb_is_mounted", return_value=True),
            patch("photo_offloader.volumes.get_volume_identity", return_value=identity),
        ):
            return app.main(["--backup-usb", "--sha256"])

    def safe_notes(self, notify):
        return [call.args[1] for call in notify.call_args_list if call.args[0] == "Safe to Format"]

    def test_an_import_notes_the_cards_name_kind_and_size(self, notify, eject, launch):
        self.media()
        self.import_card(False)
        manifest = json.loads((config.CARD_MANIFESTS_DIR / "TEST-CARD-UUID.json").read_text())
        self.assertEqual(manifest["history"]["identity"], SD)
        self.assertEqual(manifest["history"]["volume_name"], "card")
        self.assertIn("first_seen", manifest["history"])

    def test_without_the_drive_a_card_is_not_safe_until_a_later_backup(self, notify, eject, launch):
        self.media()
        self.assertEqual(self.import_card(False), 0)
        self.assertEqual(self.safe_notes(notify), [])
        self.assertEqual(card_status.safe_since(), {})
        self.assertEqual(self.back_up(), 0)
        self.assertIn("TEST-CARD-UUID", card_status.safe_since())
        self.assertEqual(
            self.safe_notes(notify),
            ["card · 64 GB SanDisk SD: all 3 files are on the Mac and the USB drive."],
        )
        self.back_up()
        self.assertEqual(len(self.safe_notes(notify)), 1, "said once, not after every backup")

    def test_an_import_with_the_drive_makes_the_card_safe_at_once(self, notify, eject, launch):
        self.media()
        self.assertEqual(self.import_card(True), 0)
        self.assertTrue(card_status.all_cards()[0].safe)
        self.assertEqual(len(self.safe_notes(notify)), 1)

    def test_the_same_card_formatted_replaces_its_old_entry(self, notify, eject, launch):
        jsonfile.write_json(
            config.CARD_MANIFESTS_DIR / "OLD-UUID.json",
            {"schema": 1, "files": {}, "history": {"identity": SD, "volume_name": "card"}},
        )
        self.media()
        self.import_card(False)
        self.assertEqual([c.card_id for c in card_status.all_cards()], ["TEST-CARD-UUID"])

    def test_the_cards_own_name_is_kept_not_its_mount_folders(self, notify, eject, launch):
        self.media()
        self.import_card(False, identity={**SD, "volume_name": "NIKON Z 8  "})
        history = json.loads((config.CARD_MANIFESTS_DIR / "TEST-CARD-UUID.json").read_text())[
            "history"
        ]
        self.assertEqual(history["volume_name"], "NIKON Z 8  ")
        self.assertEqual(history["identity"], SD)
        self.assertEqual(catalog.list_import_history()[0]["source"], "NIKON Z 8  ")

    def test_a_reader_that_says_nothing_does_not_stop_the_import(self, notify, eject, launch):
        self.media()
        with patch.object(card_status, "forget_formatted", side_effect=OSError("read-only")):
            self.assertEqual(self.import_card(False), 0)


if __name__ == "__main__":
    unittest.main()
