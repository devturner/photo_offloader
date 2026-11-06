"""Tests for photo_offloader.jsonfile: tolerant reads and atomic writes of the state files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import jsonfile


class TestReadObject(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"

    def test_returns_the_stored_object(self):
        self.path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(jsonfile.read_object(self.path), {"a": 1})

    def test_missing_corrupt_or_non_object_files_read_as_empty(self):
        self.assertEqual(jsonfile.read_object(self.path), {})
        for content in ("{not json", "[1, 2]", ""):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(jsonfile.read_object(self.path), {}, content)


class TestWriteJson(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_creates_the_parent_folder_and_leaves_no_temporary_file(self):
        target = self.root / "new" / "state.json"
        jsonfile.write_json(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual([item.name for item in target.parent.iterdir()], ["state.json"])

    def test_passes_formatting_options_to_json(self):
        target = self.root / "state.json"
        jsonfile.write_json(target, {"b": "é", "a": 1}, indent=2, sort_keys=True, ensure_ascii=False)
        self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "a": 1,\n  "b": "é"\n}\n')

    def test_a_failed_replace_keeps_the_previous_file(self):
        target = self.root / "state.json"
        jsonfile.write_json(target, {"version": 1})
        with patch("photo_offloader.jsonfile.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                jsonfile.write_json(target, {"version": 2})
        self.assertEqual(jsonfile.read_object(target), {"version": 1})


if __name__ == "__main__":
    unittest.main()
