"""config takes the archive folders and backup drive from settings.txt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_offloader import config, settings
from tests import support


class TestSettingsInConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        support.isolate_config(self, self.root)
        config.SETTINGS_PATH.parent.mkdir(parents=True)

    def test_reload_applies_the_settings_file(self):
        config.SETTINGS_PATH.write_text(
            "archive_folder = ~/Archive\nraw_archive_folder = /Volumes/Big/Raw\n"
            "backup_drive = Backup 2\n",
            encoding="utf-8",
        )
        config.reload_settings()
        self.assertEqual(config.LOCAL_DEST_ROOT, config.HOME / "Archive")
        self.assertEqual(config.LOCAL_RAW_DEST_ROOT, Path("/Volumes/Big/Raw"))
        self.assertEqual(config.USB_DEST_VOLUME, Path("/Volumes/Backup 2"))
        self.assertEqual(config.USB_DEST_ROOT, Path("/Volumes/Backup 2/CameraImports"))
        self.assertIsNone(config.SETTINGS_PROBLEM)
        config.check_settings()  # no problem, no error

    def test_a_file_with_a_problem_is_reported_and_refused(self):
        config.SETTINGS_PATH.write_text("backup_drive = /Volumes/X\n", encoding="utf-8")
        config.reload_settings()
        self.assertIn("Line 1", config.SETTINGS_PROBLEM)
        self.assertEqual(config.USB_DEST_VOLUME, Path("/Volumes/Photos"))
        with self.assertRaisesRegex(settings.SettingsProblem, "Edit settings"):
            config.check_settings()

    def test_reload_reports_whether_anything_changed(self):
        self.assertTrue(config.reload_settings())  # the isolated paths become the defaults
        self.assertFalse(config.reload_settings())
        config.SETTINGS_PATH.write_text("backup_drive = Other\n", encoding="utf-8")
        self.assertTrue(config.reload_settings())


if __name__ == "__main__":
    unittest.main()
