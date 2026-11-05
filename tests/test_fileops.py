#!/usr/bin/env python3
import errno
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import fileops
from photo_offloader.fileops import FileFingerprint
from tests import support


class TestFileops(support.CardFixtureTestCase):
    def test_files_match_requires_sha_for_existing_duplicate(self):
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"abc")
        b.write_bytes(b"xyz")
        self.assertTrue(fileops.files_match(a, b))
        self.assertFalse(fileops.files_match(a, b, force_sha256=True))
        b.write_bytes(b"abc")
        self.assertTrue(fileops.files_match(a, b, force_sha256=True))

    def test_source_fingerprint(self):
        source = self.root / "source"
        source.write_bytes(b"abc")
        fp = fileops.file_fingerprint(source)
        self.assertEqual(fp.size, 3)
        self.assertGreaterEqual(fp.mtime_ns, 0)

    def test_install_cross_device(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        with patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            self.assertTrue(fileops.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")

    def test_camera_index_file_name_match_is_case_insensitive(self):
        self.assertTrue(fileops.is_camera_index_file(Path("nc_fllst.dat")))
        self.assertFalse(fileops.is_camera_index_file(Path("NOTES.DAT")))

    def test_destination_verification_bypasses_the_page_cache_when_supported(self):
        if not hasattr(fileops.fcntl, "F_NOCACHE"):
            self.skipTest("F_NOCACHE is macOS-only")
        target = self.root / "verify.bin"
        target.write_bytes(b"data")
        with patch.object(fileops.fcntl, "fcntl") as fcntl_call:
            fileops.sha256_file(target, nocache=True)
        self.assertTrue(any(c.args[1] == fileops.fcntl.F_NOCACHE for c in fcntl_call.call_args_list))

    def test_file_fingerprint_captures_stat_identity(self):
        source = self.root / "source.jpg"
        source.write_bytes(b"photo")
        fingerprint = fileops.file_fingerprint(source)
        stat = source.stat()
        self.assertEqual(fingerprint, FileFingerprint(stat.st_size, stat.st_mtime_ns, stat.st_ino))


if __name__ == "__main__":
    unittest.main()
