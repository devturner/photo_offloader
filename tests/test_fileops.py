#!/usr/bin/env python3
import ctypes
import errno
import mmap
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import fileops
from photo_offloader.fileops import FileFingerprint
from tests import support


def pages_in_ram(path: Path) -> int:
    """How many of the file's pages macOS holds in memory, found without reading them."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_longlong,
    ]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    size = path.stat().st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
        pages = ctypes.create_string_buffer((size + mmap.PAGESIZE - 1) // mmap.PAGESIZE)
        if libc.mincore(address, size, pages) != 0:
            raise OSError(ctypes.get_errno(), "mincore failed")
        libc.munmap(address, size)
    finally:
        os.close(fd)
    return sum(byte & 1 for byte in pages.raw)


class TestFileops(support.CardFixtureTestCase):
    def test_a_partial_copy_that_cannot_be_removed_is_logged_and_the_error_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_path, final_path = Path(tmp) / "temp", Path(tmp) / "final"
            temp_path.write_bytes(b"photo")
            with (
                patch(
                    "photo_offloader.fileops.os.link",
                    side_effect=OSError(errno.EXDEV, "cross-device"),
                ),
                patch("photo_offloader.fileops.os.replace", side_effect=OSError("disk full")),
                patch.object(Path, "unlink", side_effect=OSError("still busy")),
                self.assertLogs("camera_offloader", level="WARNING") as captured,
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                fileops.install_without_overwrite(temp_path, final_path)
        self.assertIn("still busy", captured.output[0])

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

    def test_install_never_overwrites_an_existing_file(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"new")
        final.write_bytes(b"old")
        self.assertFalse(fileops.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"old")

    def test_install_on_a_drive_without_hard_links_never_overwrites_an_existing_file(self):
        # exFAT, the usual format for a USB backup drive, takes the fallback path.
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"new")
        final.write_bytes(b"old")
        with patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            self.assertFalse(fileops.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"old")

    def test_install_without_hard_links_moves_the_verified_file_into_place(self):
        # A second copy would be a file nobody verified, and would sit half-written under the
        # final name for as long as it took. exFAT has no hard links, but renames over a file.
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"verified")
        inode = temp.stat().st_ino
        with patch("os.link", side_effect=OSError(errno.ENOTSUP, "not supported")):
            self.assertTrue(fileops.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"verified")
        self.assertEqual(final.stat().st_ino, inode)
        self.assertFalse(temp.exists())

    def test_a_cancel_during_an_install_without_hard_links_leaves_no_file(self):
        # SIGTERM arrives as KeyboardInterrupt, which "except Exception" misses. A file left
        # under the final name made every later backup fail with "USB file differs".
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"verified")
        with (
            patch("os.link", side_effect=OSError(errno.ENOTSUP, "not supported")),
            patch("photo_offloader.fileops.os.replace", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            fileops.install_without_overwrite(temp, final)
        self.assertFalse(final.exists())
        self.assertEqual(temp.read_bytes(), b"verified")

    def test_a_sigterm_just_after_the_name_is_claimed_cannot_leave_an_empty_file(self):
        # An empty file under the final name makes every later backup report "USB file differs",
        # and a signal here lands before anything can record that the file was created.
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"verified")
        real_open = os.open

        def open_then_sigterm(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == final:
                os.kill(os.getpid(), signal.SIGTERM)
            return fd

        def interrupt(_signum, _frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, interrupt)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with (
            patch("os.link", side_effect=OSError(errno.ENOTSUP, "not supported")),
            patch("photo_offloader.fileops.os.open", open_then_sigterm),
            self.assertRaises(KeyboardInterrupt),
        ):
            fileops.install_without_overwrite(temp, final)
        self.assertEqual(final.read_bytes(), b"verified")

    def test_a_failed_close_of_the_claimed_name_leaves_no_empty_file(self):
        # A failing or disconnecting drive can refuse the close after the file was created.
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"verified")
        real_close = os.close

        def close_then_fail(fd):
            real_close(fd)
            raise OSError(errno.EIO, "disk error")

        with (
            patch("os.link", side_effect=OSError(errno.ENOTSUP, "not supported")),
            patch("photo_offloader.fileops.os.close", close_then_fail),
            self.assertRaisesRegex(OSError, "disk error"),
        ):
            fileops.install_without_overwrite(temp, final)
        self.assertFalse(final.exists())
        self.assertEqual(temp.read_bytes(), b"verified")

    def test_install_raises_an_unexpected_link_error_and_installs_nothing(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"new")
        with (
            patch("os.link", side_effect=OSError(errno.EIO, "disk error")),
            self.assertRaisesRegex(OSError, "disk error"),
        ):
            fileops.install_without_overwrite(temp, final)
        self.assertFalse(final.exists())

    def test_files_do_not_match_when_sizes_differ_or_one_is_missing(self):
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"abc")
        self.assertFalse(fileops.files_match(a, b))
        b.write_bytes(b"abcd")
        self.assertFalse(fileops.files_match(a, b))

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
        self.assertTrue(
            any(c.args[1] == fileops.fcntl.F_NOCACHE for c in fcntl_call.call_args_list)
        )

    def test_a_copy_leaves_nothing_in_ram_so_verifying_it_reads_the_disk(self):
        # F_NOCACHE on the verifying read does not evict pages the copy just wrote: with them in
        # RAM, a 50 MB copy "verified" at 13.5 GB/s against 1.4 GB/s from the disk.
        if not hasattr(fileops.fcntl, "F_NOCACHE"):
            self.skipTest("F_NOCACHE is macOS-only")
        source, copy = self.root / "source", self.root / "copy"
        source.write_bytes(os.urandom(5 * 1024 * 1024 + 123))  # not a whole number of pages
        fileops.copy_file_with_progress(source, copy)
        self.assertEqual(pages_in_ram(copy), 0)

    def fcntl_commands_during(self, action):
        commands = []
        real_fcntl = fileops.fcntl.fcntl

        def recording(fd, command, *args):
            commands.append(command)
            return real_fcntl(fd, command, *args)

        with patch.object(fileops.fcntl, "fcntl", recording):
            action()
        return commands

    def test_a_copy_is_flushed_out_of_the_drives_own_cache(self):
        # On macOS fsync only reaches the drive's cache: 0.02 ms against 5 ms for F_FULLFSYNC.
        if not hasattr(fileops.fcntl, "F_FULLFSYNC"):
            self.skipTest("F_FULLFSYNC is macOS-only")
        source, copy = self.root / "source", self.root / "copy"
        source.write_bytes(b"photo")
        commands = self.fcntl_commands_during(lambda: fileops.copy_file_with_progress(source, copy))
        self.assertIn(fileops.fcntl.F_FULLFSYNC, commands)

    def test_flush_uses_fsync_on_a_drive_without_a_full_flush(self):
        target = self.root / "target"
        target.write_bytes(b"photo")
        fd = os.open(target, os.O_RDWR)
        self.addCleanup(os.close, fd)
        with (
            patch.object(fileops.fcntl, "fcntl", side_effect=OSError(errno.ENOTSUP, "no")),
            patch.object(fileops.os, "fsync") as fsync,
        ):
            fileops.flush_to_disk(fd)
        fsync.assert_called_once_with(fd)

    def test_a_flush_that_fails_is_not_hidden(self):
        target = self.root / "target"
        target.write_bytes(b"photo")
        fd = os.open(target, os.O_RDWR)
        self.addCleanup(os.close, fd)
        with (
            patch.object(fileops.fcntl, "fcntl", side_effect=OSError(errno.ENOTSUP, "no")),
            patch.object(fileops.os, "fsync", side_effect=OSError(errno.EIO, "disk error")),
            self.assertRaisesRegex(OSError, "disk error"),
        ):
            fileops.flush_to_disk(fd)

    def test_a_copy_that_cannot_be_flushed_fails(self):
        source, copy = self.root / "source", self.root / "copy"
        source.write_bytes(b"photo")
        with (
            patch.object(fileops, "flush_to_disk", side_effect=OSError(errno.EIO, "disk error")),
            self.assertRaisesRegex(OSError, "disk error"),
        ):
            fileops.copy_file_with_progress(source, copy)

    def assert_install_flushes_the_folder(self, replace):
        temp, final = self.root / "tmp", self.root / "folder" / "final"
        final.parent.mkdir()
        temp.write_bytes(b"new")
        if replace:
            final.write_bytes(b"old")
        flushed = []
        with patch.object(fileops, "flush_to_disk", lambda fd: flushed.append(os.fstat(fd).st_ino)):
            self.assertTrue(fileops.install_file(temp, final, replace=replace))
        self.assertIn(final.parent.stat().st_ino, flushed)

    def test_installing_a_file_flushes_its_folder_so_the_name_survives_a_power_cut(self):
        self.assert_install_flushes_the_folder(replace=False)

    def test_replacing_an_index_file_flushes_its_folder(self):
        self.assert_install_flushes_the_folder(replace=True)

    def test_a_file_that_was_not_installed_does_not_flush_its_folder(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"new")
        final.write_bytes(b"old")
        with patch.object(fileops, "flush_to_disk") as flush:
            self.assertFalse(fileops.install_file(temp, final))
        flush.assert_not_called()

    def test_file_fingerprint_captures_stat_identity(self):
        source = self.root / "source.jpg"
        source.write_bytes(b"photo")
        fingerprint = fileops.file_fingerprint(source)
        stat = source.stat()
        self.assertEqual(fingerprint, FileFingerprint(stat.st_size, stat.st_mtime_ns, stat.st_ino))


class TestEndsMatch(unittest.TestCase):
    END = fileops.ENDS_CHECK_BYTES

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.first = Path(tmp.name) / "first"
        self.second = Path(tmp.name) / "second"

    def compare(self, first, second):
        self.first.write_bytes(first)
        self.second.write_bytes(second)
        return fileops.ends_match(self.first, self.second)

    def test_identical_files_match(self):
        for size in (0, 10, self.END, 2 * self.END, 2 * self.END + 1, 3 * self.END):
            with self.subTest(size=size):
                self.assertTrue(self.compare(b"x" * size, b"x" * size))

    def test_a_difference_in_either_end_is_found(self):
        original = bytearray(b"x" * (3 * self.END))
        for position in (0, self.END - 1, 2 * self.END, 3 * self.END - 1):
            changed = bytearray(original)
            changed[position] = ord("y")
            with self.subTest(position=position):
                self.assertFalse(self.compare(bytes(original), bytes(changed)))

    def test_a_different_size_never_matches(self):
        # Both ends read the same, so only the size tells these apart.
        self.assertFalse(self.compare(b"x" * (3 * self.END), b"x" * (3 * self.END + 1)))

    def test_the_middle_is_not_read(self):
        original = bytearray(b"x" * (3 * self.END))
        changed = bytearray(original)
        changed[self.END] = ord("y")
        self.assertTrue(self.compare(bytes(original), bytes(changed)))

    def test_an_unreadable_file_raises(self):
        self.first.write_bytes(b"x")
        with self.assertRaises(OSError):
            fileops.ends_match(self.first, self.second)


class TestForgetCachedPages(support.CardFixtureTestCase):
    def test_a_file_in_ram_is_no_longer_in_ram(self):
        path = self.root / "photo.bin"
        path.write_bytes(os.urandom(1024 * 1024))
        path.read_bytes()
        self.assertGreater(pages_in_ram(path), 0)

        self.assertTrue(fileops.forget_cached_pages(path))

        self.assertEqual(pages_in_ram(path), 0)

    def test_the_file_is_unchanged(self):
        path = self.root / "photo.bin"
        content = os.urandom(100_000)
        path.write_bytes(content)
        before = path.stat().st_mtime_ns

        fileops.forget_cached_pages(path)

        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_an_empty_file_has_nothing_to_forget(self):
        path = self.root / "empty.bin"
        path.write_bytes(b"")
        self.assertTrue(fileops.forget_cached_pages(path))

    def test_a_missing_file_raises_like_any_other_read(self):
        with self.assertRaises(FileNotFoundError):
            fileops.forget_cached_pages(self.root / "gone.bin")


if __name__ == "__main__":
    unittest.main()
