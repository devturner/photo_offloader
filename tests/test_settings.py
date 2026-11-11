"""settings.txt: the folders and backup drive a user can change without touching the code."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_offloader import settings

HOME = Path("/Users/someone")


class TestParse(unittest.TestCase):
    def test_an_empty_file_keeps_the_defaults(self):
        self.assertEqual(settings.parse("", HOME), settings.defaults(HOME))

    def test_defaults_are_the_documented_folders_and_a_drive_named_photos(self):
        values = settings.defaults(HOME)
        self.assertEqual(values.archive_folder, HOME / "Pictures" / "CameraImports")
        self.assertEqual(values.raw_archive_folder, HOME / "Pictures" / "CameraImportsRaw")
        self.assertEqual(values.backup_drive, "Photos")
        self.assertEqual(values.usb_volume, Path("/Volumes/Photos"))
        self.assertIsNone(values.problem)

    def test_reads_each_setting_with_a_home_relative_path(self):
        values = settings.parse(
            "# my settings\n"
            "\n"
            "archive_folder = ~/Archive/Photos\n"
            "raw_archive_folder=/Volumes/Big Disk/Raw files\n"
            "  backup_drive   =  Backup 2  \n",
            HOME,
        )
        self.assertIsNone(values.problem)
        self.assertEqual(values.archive_folder, HOME / "Archive" / "Photos")
        self.assertEqual(values.raw_archive_folder, Path("/Volumes/Big Disk/Raw files"))
        self.assertEqual(values.backup_drive, "Backup 2")
        self.assertEqual(values.usb_volume, Path("/Volumes/Backup 2"))

    def test_quotes_around_a_value_are_dropped_including_curly_ones(self):
        values = settings.parse('backup_drive = "Photos 2"\narchive_folder = “~/A B”\n', HOME)
        self.assertIsNone(values.problem)
        self.assertEqual(values.backup_drive, "Photos 2")
        self.assertEqual(values.archive_folder, HOME / "A B")

    def test_a_hash_inside_a_value_is_part_of_the_name(self):
        values = settings.parse("backup_drive = Disk #2\n", HOME)
        self.assertEqual(values.backup_drive, "Disk #2")

    def assertProblem(self, text, *expected):
        values = settings.parse(text, HOME)
        self.assertIsNotNone(values.problem, text)
        for part in expected:
            self.assertIn(part, values.problem)
        # A file with a problem is never half applied.
        self.assertEqual(values, settings.defaults(HOME)._replace(problem=values.problem))

    def test_a_line_without_an_equals_sign(self):
        self.assertProblem("backup_drive = Photos\nbackup_drive Photos 2\n", "Line 2")

    def test_an_unknown_setting_names_the_known_ones(self):
        self.assertProblem("backup_drve = Photos\n", "Line 1", "backup_drve", "backup_drive")

    def test_a_setting_given_twice(self):
        self.assertProblem("backup_drive = A\nbackup_drive = B\n", "Line 2", "twice")

    def test_an_empty_value(self):
        self.assertProblem("archive_folder =\n", "Line 1", "archive_folder")

    def test_a_folder_that_is_not_a_full_path(self):
        self.assertProblem("archive_folder = Pictures/CameraImports\n", "Line 1", "full path")

    def test_a_drive_given_as_a_path(self):
        self.assertProblem("backup_drive = /Volumes/Photos\n", "Line 1", "name")

    def test_the_same_folder_for_raw_and_other_files(self):
        self.assertProblem(
            "archive_folder = ~/Photos\nraw_archive_folder = ~/Photos/\n", "different"
        )

    def test_one_archive_folder_inside_the_other(self):
        self.assertProblem(
            "archive_folder = ~/Photos\nraw_archive_folder = ~/Photos/Raw\n", "inside"
        )


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.txt"

    def test_no_file_means_the_defaults(self):
        self.assertEqual(settings.load(self.path, HOME), settings.defaults(HOME))

    def test_an_unreadable_file_is_a_problem_not_the_defaults_in_silence(self):
        self.path.mkdir()  # reading a folder fails like a file without permission
        values = settings.load(self.path, HOME)
        self.assertIn("settings.txt", values.problem)

    def test_a_problem_in_the_file_names_the_line(self):
        self.path.write_text("# fine\nnonsense\n", encoding="utf-8")
        self.assertTrue(settings.load(self.path, HOME).problem.startswith("Line 2: "))


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data" / "settings.txt"

    def test_the_template_reads_back_as_the_values_it_was_made_from(self):
        current = settings.parse("archive_folder = ~/A\nbackup_drive = Backup\n", HOME)
        text = settings.template(current, HOME)
        self.assertEqual(settings.parse(text, HOME), current)
        self.assertIn("archive_folder = ~/A\n", text)  # shown as the user would write it

    def test_create_writes_the_current_values_once(self):
        self.assertTrue(settings.create_if_missing(self.path, settings.defaults(HOME), HOME))
        self.assertEqual(settings.load(self.path, HOME), settings.defaults(HOME))

    def test_create_never_replaces_a_file_someone_edited(self):
        self.path.parent.mkdir()
        self.path.write_text("backup_drive = Mine\n", encoding="utf-8")
        self.assertFalse(settings.create_if_missing(self.path, settings.defaults(HOME), HOME))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "backup_drive = Mine\n")


if __name__ == "__main__":
    unittest.main()
