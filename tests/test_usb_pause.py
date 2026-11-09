"""A USB backup gives way to a camera card, and leaves word so it can carry on."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config, importer, progress, transfer, volumes
from photo_offloader.transfer import ImportStats
from tests import support


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        support.isolate_config(self, self.root)
        config.INSTALL_DIR.mkdir(parents=True)
        config.DATA_DIR.mkdir(parents=True)
        config.USB_DEST_ROOT.mkdir(parents=True)
        config.LOCAL_DEST_ROOT.mkdir(parents=True)
        identity = volumes.VolumeIdentity(config.USB_DEST_VOLUME, "disk9s1", "DRIVE-1")
        for name, value in (("get_volume_identity", identity), ("usb_is_mounted", True)):
            patcher = patch.object(volumes, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestCopyLoop(Fixture):
    def add_files(self, count):
        for index in range(count):
            (config.LOCAL_DEST_ROOT / f"f{index}.JPG").write_bytes(b"x" * (index + 1))

    def test_it_stops_between_files_when_a_card_asks(self):
        self.add_files(3)
        real = transfer._copy_one_to_usb

        def copy_then_card_arrives(*args, **kwargs):
            copied = real(*args, **kwargs)
            config.USB_YIELD_PATH.touch()
            return copied

        with patch.object(transfer, "_copy_one_to_usb", copy_then_card_arrives):
            stats = transfer.copy_local_archive_to_usb(may_pause=True)
        self.assertTrue(stats.paused)
        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.to_copy, 3)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(len(list(config.USB_DEST_ROOT.iterdir())), 1)

    def test_it_reports_files_as_it_goes_for_the_dashboard(self):
        self.add_files(2)
        seen = []
        real = progress.update_status

        def note(**fields):
            real(**fields)
            seen.append(
                (progress._PROGRESS.get("files_done"), progress._PROGRESS.get("files_total"))
            )

        with patch.object(progress, "update_status", note):
            transfer.copy_local_archive_to_usb()
        self.assertIn((1, 2), seen)

    def test_a_backup_after_an_import_does_not_give_way(self):
        self.add_files(2)
        config.USB_YIELD_PATH.touch()
        stats = transfer.copy_local_archive_to_usb()
        self.assertFalse(stats.paused)
        self.assertEqual(stats.destination_copies, 2)


class TestRun(Fixture):
    def run_backup(self, stats):
        with patch.object(transfer, "copy_local_archive_to_usb", return_value=stats):
            return importer.main(["--backup-usb"])

    def test_a_pause_leaves_word_and_is_not_a_finished_run(self):
        config.USB_YIELD_PATH.touch()
        code = self.run_backup(ImportStats(paused=True, destination_copies=140, to_copy=475))
        self.assertEqual(code, 0)
        self.assertEqual(config.USB_RESUME_PATH.read_text().split(), ["DRIVE-1", "140", "475"])
        self.assertEqual(catalog.list_import_history(), [])

    def test_the_request_is_cleared_once_it_has_been_answered(self):
        def card_asks_during_the_run(**_kwargs):
            config.USB_YIELD_PATH.touch()
            return ImportStats(paused=True)

        with patch.object(transfer, "copy_local_archive_to_usb", card_asks_during_the_run):
            importer.main(["--backup-usb"])
        self.assertFalse(config.USB_YIELD_PATH.exists())

    def test_a_new_backup_starts_clean_and_a_finished_one_leaves_nothing(self):
        config.USB_YIELD_PATH.touch()
        config.USB_RESUME_PATH.write_text("DRIVE-1\n1\n2\n")
        seen = []

        def backup(**_kwargs):
            seen.append(config.USB_YIELD_PATH.exists())
            return ImportStats()

        with patch.object(transfer, "copy_local_archive_to_usb", backup):
            self.assertEqual(importer.main(["--backup-usb"]), 0)
        self.assertEqual(seen, [False], "an old request must not pause this backup at once")
        self.assertFalse(config.USB_RESUME_PATH.exists())
        self.assertEqual(len(catalog.list_import_history()), 1)


if __name__ == "__main__":
    unittest.main()
