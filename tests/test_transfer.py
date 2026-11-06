#!/usr/bin/env python3
import errno
import json
import os
import pathlib
import sqlite3
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import (
    card,
    catalog,
    config,
    fileops,
    logs,
    media_info,
    progress,
    transfer,
    volumes,
)
from photo_offloader.transfer import DestinationResult, ImportStats
from photo_offloader.volumes import VolumeIdentity
from tests import support


def file_appears_during_the_copy(content):
    """Another process creates the destination just before the verified file is installed."""
    real_install = fileops.install_file

    def install(temp, final, replace=False):
        Path(final).write_bytes(content)
        return real_install(temp, final, replace=replace)

    return patch("photo_offloader.fileops.install_file", side_effect=install)


class TestTransfer(support.CardFixtureTestCase):
    def test_usb_backup_mirrors_raw_archive_to_matching_root(self):
        jpg = self.local / "2026-10" / "100NIKON" / "Photos" / "photo.jpg"
        raw = config.LOCAL_RAW_DEST_ROOT / "2026-10" / "100NIKON" / "Raws" / "photo.nef"
        jpg.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        jpg.write_bytes(b"jpeg-content")
        raw.write_bytes(b"raw-content")
        identity = VolumeIdentity(self.usb_volume, "disk-test", "volume-test")

        with (
            patch.object(volumes, "usb_is_mounted", return_value=True),
            patch.object(volumes, "get_volume_identity", return_value=identity),
            patch.object(volumes, "volume_identity_matches", return_value=True),
        ):
            stats = transfer.copy_local_archive_to_usb(identity)

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.destination_copies, 2)
        self.assertEqual((self.usb / jpg.relative_to(self.local)).read_bytes(), b"jpeg-content")
        self.assertEqual(
            (self.usb / raw.relative_to(config.LOCAL_RAW_DEST_ROOT)).read_bytes(),
            b"raw-content",
        )

    def test_stats_aliases_and_success(self):
        stats = ImportStats(destination_copies=2, destination_skips=1, verified_copies=3)
        self.assertTrue(stats.successful)
        self.assertEqual((stats.copied, stats.skipped, stats.verified), (2, 1, 3))
        stats.errors.append(("x", "y"))
        self.assertFalse(stats.successful)

    def test_copy_decision_reuses_card_and_catalog_hashes(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        digest = fileops.sha256_file(source)
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        catalog.catalog_record(destination, sha256=digest)
        with patch.object(
            fileops, "sha256_file", side_effect=AssertionError("must reuse cached hashes")
        ):
            result = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual(result.status, "skipped")

    def test_dst_shifted_card_timestamps_keep_cached_hashes(self):
        # User log 2026-11-04: after DST ended, exFAT/FAT local-time stamps all read
        # one hour later; 2346 files counted as "changed" and the card was re-hashed.
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        digest = fileops.sha256_file(source)
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        catalog.catalog_record(destination, sha256=digest)
        self.shift_mtime(source, 3600)
        with patch.object(
            fileops, "sha256_file", side_effect=AssertionError("must reuse cached hashes")
        ):
            result = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual(result.status, "skipped")

    def saved_photo(self):
        """A card file with a verified copy in the archive; returns (source, copy, its hash)."""
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        digest = fileops.sha256_file(source)
        catalog.catalog_record(destination, sha256=digest)
        return source, destination, digest

    def replace_keeping_details(self, path):
        """New content with the same path, size and time, as a new photo after a format could be."""
        before = path.stat()
        content = bytes(reversed(path.read_bytes()))
        path.write_bytes(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return content

    def assert_new_photo_was_saved(self, destination, result, content):
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.name, f"{destination.stem}__conflict-1{destination.suffix}")
        self.assertEqual(result.path.read_bytes(), content)
        self.assertNotEqual(destination.read_bytes(), content)

    def test_a_new_photo_is_saved_when_the_manifest_has_an_old_photos_hash(self):
        source, destination, digest = self.saved_photo()
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        content = self.replace_keeping_details(source)
        result = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assert_new_photo_was_saved(destination, result, content)
        # The wrong hash is gone, so the next insert finds the copy instead of adding another.
        again = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual((again.status, again.path), ("skipped", result.path))

    def test_a_new_photo_is_saved_when_the_catalog_has_an_old_photos_hash(self):
        source, destination, digest = self.saved_photo()
        catalog.catalog_record(source, sha256=digest)
        content = self.replace_keeping_details(source)
        result = transfer.copy_one_to_destination(source, self.dcim, self.local, {})
        self.assert_new_photo_was_saved(destination, result, content)
        self.assertEqual(catalog.catalog_row(source)[3], fileops.sha256_file(source))

    def test_an_old_hash_in_the_catalog_is_replaced_when_the_conflict_copy_already_exists(self):
        source, destination, digest = self.saved_photo()
        catalog.catalog_record(source, sha256=digest)
        content = self.replace_keeping_details(source)
        copy = destination.with_name(f"{destination.stem}__conflict-1{destination.suffix}")
        copy.write_bytes(content)
        result = transfer.copy_one_to_destination(source, self.dcim, self.local, {})
        self.assertEqual((result.status, result.path), ("skipped", copy))
        self.assertEqual(catalog.catalog_row(source)[3], fileops.sha256_file(source))

    def test_an_intact_card_skips_the_end_check_for_a_hash_from_its_manifest(self):
        source, _, digest = self.saved_photo()
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        with patch.object(fileops, "ends_match", side_effect=AssertionError("must not read")):
            result = transfer.copy_one_to_destination(
                source, self.dcim, self.local, manifest, trust_manifest=True
            )
        self.assertEqual(result.status, "skipped")

    def test_a_hash_from_the_catalog_is_always_checked_even_on_an_intact_card(self):
        # The catalog isn't per card, so an intact manifest says nothing about its rows.
        source, destination, digest = self.saved_photo()
        catalog.catalog_record(source, sha256=digest)
        content = self.replace_keeping_details(source)
        result = transfer.copy_one_to_destination(
            source, self.dcim, self.local, {}, trust_manifest=True
        )
        self.assert_new_photo_was_saved(destination, result, content)

    def test_a_skipped_file_is_not_reported_as_copying(self):
        # Dashboard log 2026-11-06: the panel flipped to Copying on a card with nothing to copy.
        source, _, digest = self.saved_photo()
        result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(progress._PROGRESS["state"], "scanning")

    def test_a_damaged_archive_copy_is_not_taken_for_the_card_file(self):
        source, destination, digest = self.saved_photo()
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        self.replace_keeping_details(destination)
        result = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.read_bytes(), source.read_bytes())
        self.assertEqual(catalog.catalog_row(destination)[3], fileops.sha256_file(destination))

    def test_copy_and_verify(self):
        source = self.media()["RAW"]
        source.chmod(0o700)
        result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.read_bytes(), b"raw")
        self.assertEqual(stat.S_IMODE(result.path.stat().st_mode), 0o600)

    def test_matching_duplicate_is_content_verified(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")

    def test_same_size_different_content_gets_conflict(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"xxx")  # same size as b"raw"
        result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertIn("__conflict-1", result.path.name)
        self.assertEqual(destination.read_bytes(), b"xxx")
        self.assertEqual(result.path.read_bytes(), b"raw")

    def test_a_category_opens_the_catalog_once_however_many_files_it_has(self):
        # Measured on a real 2,722-file card: reopening it for each lookup took about 5 s of 7 s.
        catalog.catalog_connection().close()
        files = [self.dcim / "100NIKON" / f"photo{number}.jpg" for number in range(5)]
        files[0].parent.mkdir(parents=True)
        for number, path in enumerate(files):
            path.write_bytes(b"photo %d" % number)
        real_connect = sqlite3.connect
        with patch("sqlite3.connect", side_effect=real_connect) as connect:
            transfer.copy_category(files, self.dcim, [self.local], "JPG", ImportStats())
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(len(list(self.local.rglob("photo*.jpg"))), 5)

    def test_a_category_logs_where_its_time_went(self):
        source = self.media()["JPG"]
        with self.assertLogs("camera_offloader", "INFO") as logged:
            transfer.copy_category([source], self.dcim, [self.local], "JPG", ImportStats())
        line = next(message for message in logged.output if "Time for JPG" in message)
        self.assertRegex(line, r"Time for JPG, 1 file\(s\): [\d.]+ s: ")
        for part in ("catalog ", "status ", "other "):
            self.assertIn(part, line)

    def test_checking_saved_files_does_not_rewrite_the_status_for_each_file(self):
        # Measured on a real 2,722-file card: 4 writes per file took 8.8 s of a 25.7 s check.
        files = [self.dcim / "100NIKON" / f"photo{number}.jpg" for number in range(20)]
        files[0].parent.mkdir(parents=True)
        for number, path in enumerate(files):
            path.write_bytes(b"photo %d" % number)
        transfer.copy_category(files, self.dcim, [self.local], "JPG", ImportStats())
        real_write = progress.jsonfile.write_json
        with (
            patch.object(progress.jsonfile, "write_json", side_effect=real_write) as write,
            patch.object(progress.time, "monotonic", return_value=progress.time.monotonic()),
        ):
            transfer.copy_category(files, self.dcim, [self.local], "JPG", ImportStats())
        status_writes = [c for c in write.call_args_list if c.args[0] == config.STATUS_FILE_PATH]
        self.assertEqual(len(status_writes), 2)  # as it starts and as it ends
        status = json.loads(config.STATUS_FILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(status["decision_files_checked"], 20)

    def test_each_category_times_only_its_own_work(self):
        source = self.media()["JPG"]
        with logs.timed("left over from the last category"):
            pass
        with self.assertLogs("camera_offloader", "INFO") as logged:
            transfer.copy_category([source], self.dcim, [self.local], "JPG", ImportStats())
        self.assertNotIn("left over", "".join(logged.output))

    def test_conflict_counter_advances(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old")
        destination.with_name("photo__conflict-1.nef").write_bytes(b"old")
        destination.with_name("photo__conflict-2.nef").write_bytes(b"old")
        result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.path.name, "photo__conflict-3.nef")

    def test_source_mutation_is_rejected(self):
        source = self.media()["RAW"]
        original_copy = fileops.copy_file_with_progress

        def mutate(src, dst):
            original_copy(src, dst)
            Path(src).write_bytes(b"RAW")  # changes size

        with patch("photo_offloader.fileops.copy_file_with_progress", side_effect=mutate):
            with self.assertRaisesRegex(IOError, "Source changed"):
                transfer.copy_one_to_destination(source, self.dcim, self.local)

    def test_a_corrupt_temporary_copy_is_never_installed(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        good_hash = fileops.sha256_file(source)

        def corrupt_copy(_src, dst):
            Path(dst).write_bytes(b"corrupted on the way to the disk")
            return good_hash

        with patch("photo_offloader.fileops.copy_file_with_progress", side_effect=corrupt_copy):
            with self.assertRaisesRegex(IOError, "Verification failed for temporary copy"):
                transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])  # temporary file cleaned up

    def test_a_corrupt_final_copy_is_removed_and_reported(self):
        # A fallback install (exFAT has no hard links) is a fresh write that needs its own check.
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)

        def corrupt_install(_temp, final, replace=False):
            Path(final).write_bytes(b"corrupted on the way to the disk")
            return True

        with patch("photo_offloader.fileops.install_file", side_effect=corrupt_install):
            with self.assertRaisesRegex(IOError, "Final verification failed"):
                transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertFalse(destination.exists())

    def test_second_import_skips(self):
        self.media()
        files = card.find_media_files(self.dcim)
        first = ImportStats(found=3)
        second = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            transfer.copy_category(files[category], self.dcim, [self.local], category, first)
            transfer.copy_category(files[category], self.dcim, [self.local], category, second)
        self.assertEqual(first.destination_copies, 3)
        self.assertEqual(second.destination_skips, 3)

    def test_end_to_end_two_destinations(self):
        self.media()
        files = card.find_media_files(self.dcim)
        stats = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            transfer.copy_category(
                files[category], self.dcim, [self.local, self.usb], category, stats
            )
        self.assertEqual((stats.destination_copies, stats.verified_copies, stats.failed), (6, 6, 0))

    def test_usb_backup_copies_missing_files_and_skips_matching_files(self):
        archive = self.local / "2026-10" / "NIKON-Z-8" / "Photos"
        archive.mkdir(parents=True)
        (archive / "already.jpg").write_bytes(b"same content")
        (archive / "missing.jpg").write_bytes(b"new content")
        existing = self.usb / "2026-10" / "NIKON-Z-8" / "Photos"
        existing.mkdir(parents=True)
        (existing / "already.jpg").write_bytes(b"same content")

        with patch.object(volumes, "usb_is_mounted", return_value=True):
            stats = transfer.copy_local_archive_to_usb()

        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.destination_skips, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual((existing / "missing.jpg").read_bytes(), b"new content")

    def test_usb_comparison_reads_the_catalog_in_bulk_not_per_file(self):
        # Measured 2026-11-04: three catalog connections per archive file, ~5 s for
        # 2,784 files and ~90 s at 50,000; one bulk read takes 0.06 s.
        self.archive_already_on_usb(5)
        with patch.object(volumes, "usb_is_mounted", return_value=True):
            transfer.copy_local_archive_to_usb()  # first run hashes and records everything
        small = self.connections_during_usb_backup()
        self.archive_already_on_usb(60)
        with patch.object(volumes, "usb_is_mounted", return_value=True):
            transfer.copy_local_archive_to_usb()
        large = self.connections_during_usb_backup()
        self.assertEqual(small, large, "catalog connections must not grow with the archive")
        self.assertLessEqual(large, 2)

    def test_repeat_usb_backup_reuses_recorded_hashes(self):
        self.archive_already_on_usb(4)
        with patch.object(volumes, "usb_is_mounted", return_value=True):
            first = transfer.copy_local_archive_to_usb()
        self.assertEqual(first.destination_skips, 4)
        with (
            patch.object(volumes, "usb_is_mounted", return_value=True),
            patch.object(fileops, "sha256_file", side_effect=AssertionError("re-read a file")),
        ):
            second = transfer.copy_local_archive_to_usb()
        self.assertEqual((second.destination_skips, second.failed), (4, 0))

    def test_usb_backup_does_not_overwrite_different_existing_file(self):
        archive = self.local / "2026-10" / "Photos"
        archive.mkdir(parents=True)
        (archive / "photo.jpg").write_bytes(b"local version")
        existing = self.usb / "2026-10" / "Photos"
        existing.mkdir(parents=True)
        (existing / "photo.jpg").write_bytes(b"USB version")

        with patch.object(volumes, "usb_is_mounted", return_value=True):
            stats = transfer.copy_local_archive_to_usb()

        self.assertEqual(stats.destination_copies, 0)
        self.assertEqual(stats.failed, 1)
        self.assertEqual((existing / "photo.jpg").read_bytes(), b"USB version")

    def test_usb_backup_refreshes_a_changed_camera_index_file(self):
        # Otherwise every USB backup after the second import fails on NC_FLLST.DAT.
        archive = self.local / "2026-11" / "Other"
        archive.mkdir(parents=True)
        (archive / "NC_FLLST.DAT").write_bytes(b"index v2")
        existing = self.usb / "2026-11" / "Other"
        existing.mkdir(parents=True)
        (existing / "NC_FLLST.DAT").write_bytes(b"index v1")

        with patch.object(volumes, "usb_is_mounted", return_value=True):
            stats = transfer.copy_local_archive_to_usb()

        self.assertEqual(stats.failed, 0, stats.errors)
        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual((existing / "NC_FLLST.DAT").read_bytes(), b"index v2")
        self.assertEqual(sorted(p.name for p in existing.iterdir()), ["NC_FLLST.DAT"])

    def test_card_to_ssd_copy_reads_the_source_only_once(self):
        self.media()
        source = self.dcim / "100NIKON" / "photo.jpg"
        opened = []
        real_open = pathlib.Path.open

        def spy(path_self, *args, **kwargs):
            if path_self == source:
                opened.append(args[0] if args else kwargs.get("mode", "r"))
            return real_open(path_self, *args, **kwargs)

        with patch.object(pathlib.Path, "open", spy):
            result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(len(opened), 1, f"card file was read {len(opened)} times")

    def test_usb_backup_reads_each_local_source_only_once(self):
        archive = self.local / "2026-10" / "NIKON-Z-8" / "Photos"
        archive.mkdir(parents=True)
        source = archive / "photo.jpg"
        source.write_bytes(b"local-photo")
        opened = []
        real_open = pathlib.Path.open

        def spy(path_self, *args, **kwargs):
            if path_self == source:
                opened.append(1)
            return real_open(path_self, *args, **kwargs)

        with (
            patch.object(volumes, "usb_is_mounted", return_value=True),
            patch.object(volumes, "get_volume_identity", return_value=None),
            patch.object(pathlib.Path, "open", spy),
        ):
            stats = transfer.copy_local_archive_to_usb()
        self.assertEqual((stats.destination_copies, stats.failed), (1, 0))
        self.assertEqual(len(opened), 1, f"local file was read {len(opened)} times")

    def test_destination_result_defaults_are_stable(self):
        result = DestinationResult(self.local, "copied")
        self.assertEqual(result.destination, self.local)
        self.assertEqual(result.status, "copied")
        self.assertIsNone(result.path)
        self.assertEqual(result.message, "")

    def test_copy_category_records_partial_destination_failure(self):
        source = self.root / "source.jpg"
        source.write_bytes(b"photo")
        stats = ImportStats(found=1)
        copied_path = self.local / "photo.jpg"
        copied_path.write_bytes(b"photo")
        results = [
            DestinationResult(self.local, "copied", copied_path, "copied and verified"),
        ]

        with patch(
            "photo_offloader.transfer.copy_one_to_destination",
            side_effect=[results[0], OSError("backup unavailable")],
        ):
            transfer.copy_category([source], self.root, [self.local, self.usb], "JPG", stats)

        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.verified_copies, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("backup unavailable", stats.errors[0][1])

    def test_copy_category_counts_a_result_that_did_not_copy(self):
        source = self.root / "source.jpg"
        source.write_bytes(b"photo")
        stats = ImportStats(found=1)
        with patch(
            "photo_offloader.transfer.copy_one_to_destination",
            return_value=DestinationResult(self.local, "failed"),
        ):
            transfer.copy_category([source], self.root, [self.local], "JPG", stats)
        self.assertEqual((stats.failed, stats.verified_copies), (1, 0))
        self.assertEqual(stats.errors, [(str(source), "copy failed")])

    def test_a_matching_file_that_appears_during_the_copy_counts_as_skipped(self):
        source = self.media()["RAW"]
        with file_appears_during_the_copy(source.read_bytes()):
            result = transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.path.read_bytes(), b"raw")

    def test_a_different_file_that_appears_during_the_copy_is_kept_and_reported(self):
        source = self.media()["RAW"]
        destination = media_info.build_destination_path(source, self.dcim, self.local)
        with file_appears_during_the_copy(b"someone else's file"):
            with self.assertRaisesRegex(FileExistsError, "does not match source"):
                transfer.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(destination.read_bytes(), b"someone else's file")
        self.assertEqual(list(destination.parent.iterdir()), [destination])  # no temporary file


class TestUsbBackupFailures(support.CardFixtureTestCase):
    """What a backup does when the drive or a file misbehaves part way through."""

    def setUp(self):
        super().setUp()
        self.identity = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        self.drive = self.identity  # the drive mounted right now; None once it's unplugged
        for name, fake in (
            ("usb_is_mounted", lambda: self.drive is not None),
            ("get_volume_identity", lambda _path: self.drive),
        ):
            patcher = patch.object(volumes, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def archived(self, name="photo.jpg", content=b"local version"):
        source = self.local / "2026-10" / "Photos" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        return source

    def on_usb(self, source):
        return self.usb / source.relative_to(self.local)

    def backup(self):
        return transfer.copy_local_archive_to_usb(self.identity)

    def assertNoTemporaryFiles(self):
        self.assertEqual(list(self.usb.rglob(".photo-backup-*")), [])

    def test_a_source_that_changes_during_the_copy_is_not_installed(self):
        source = self.archived()
        real_copy = fileops.copy_file_with_progress

        def changes(src, dst):
            digest = real_copy(src, dst)
            Path(src).write_bytes(b"edited while the backup was reading it")
            return digest

        with patch("photo_offloader.fileops.copy_file_with_progress", side_effect=changes):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (1, 0))
        self.assertIn("changed during backup", stats.errors[0][1])
        self.assertFalse(self.on_usb(source).exists())
        self.assertNoTemporaryFiles()

    def test_a_corrupt_temporary_copy_is_never_installed(self):
        source = self.archived()
        good_hash = fileops.sha256_file(source)

        def corrupt(_src, dst):
            Path(dst).write_bytes(b"corrupted on the way to the drive")
            return good_hash

        with patch("photo_offloader.fileops.copy_file_with_progress", side_effect=corrupt):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (1, 0))
        self.assertIn("verification failed for USB temporary copy", stats.errors[0][1])
        self.assertFalse(self.on_usb(source).exists())
        self.assertNoTemporaryFiles()

    def test_a_corrupt_final_copy_is_removed_and_reported(self):
        # A fallback install (exFAT has no hard links) is a fresh write that needs its own check.
        source = self.archived()

        def corrupt(_temp, final, replace=False):
            Path(final).write_bytes(b"corrupted on the way to the drive")
            return True

        with patch("photo_offloader.fileops.install_file", side_effect=corrupt):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (1, 0))
        self.assertIn("verification failed for USB copy", stats.errors[0][1])
        self.assertFalse(self.on_usb(source).exists())

    def test_a_different_file_that_appears_on_usb_is_kept_and_reported(self):
        source = self.archived()
        with file_appears_during_the_copy(b"someone else's file"):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (1, 0))
        self.assertIn("appeared and differs", stats.errors[0][1])
        self.assertEqual(self.on_usb(source).read_bytes(), b"someone else's file")
        self.assertNoTemporaryFiles()

    def test_a_matching_file_that_appears_on_usb_counts_as_skipped(self):
        source = self.archived()
        with file_appears_during_the_copy(source.read_bytes()):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (0, 0))
        self.assertEqual((stats.destination_skips, stats.verified_copies), (1, 1))

    def test_a_matching_file_that_appears_on_a_drive_without_hard_links_counts_as_skipped(self):
        source = self.archived()
        with (
            file_appears_during_the_copy(source.read_bytes()),
            patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")),
        ):
            stats = self.backup()
        self.assertEqual(stats.failed, 0, stats.errors)
        self.assertEqual(stats.destination_skips, 1)

    def test_one_failed_file_does_not_stop_the_others(self):
        bad, good = self.archived("bad.jpg", b"bad"), self.archived("good.jpg", b"good")
        real_copy = fileops.copy_file_with_progress

        def fails_on_one(src, dst):
            if src == bad:
                raise OSError("read error")
            return real_copy(src, dst)

        with patch("photo_offloader.fileops.copy_file_with_progress", side_effect=fails_on_one):
            stats = self.backup()
        self.assertEqual((stats.failed, stats.destination_copies), (1, 1))
        self.assertEqual(stats.errors[0][0], str(bad))
        self.assertEqual(self.on_usb(good).read_bytes(), b"good")
        self.assertFalse(self.on_usb(bad).exists())
        self.assertEqual(progress._PROGRESS["state"], "failed")

    def test_a_backup_refuses_to_start_when_the_drive_is_not_mounted(self):
        source = self.archived()
        self.drive = None
        with self.assertRaisesRegex(RuntimeError, "not mounted"):
            self.backup()
        self.assertFalse(self.on_usb(source).exists())

    def test_a_backup_refuses_to_start_on_a_different_drive(self):
        source = self.archived()
        self.drive = VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b")
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.backup()
        self.assertFalse(self.on_usb(source).exists())

    def after_the_first_copy(self, change_drive):
        real_copy = transfer._copy_one_to_usb

        def copy(*args, **kwargs):
            copied = real_copy(*args, **kwargs)
            change_drive()
            return copied

        return patch.object(transfer, "_copy_one_to_usb", side_effect=copy)

    def test_unplugging_the_drive_stops_the_backup_and_reports_it(self):
        self.archived("a.jpg", b"a")
        self.archived("b.jpg", b"b")

        def unplug():
            self.drive = None

        with self.after_the_first_copy(unplug) as copy:
            stats = self.backup()
        self.assertEqual(copy.call_count, 1, "must not keep copying to a missing drive")
        self.assertEqual(stats.destination_copies, 1)
        self.assertGreater(stats.failed, 0)
        self.assertIn("disconnected during the backup", stats.errors[0][1])
        self.assertEqual(progress._PROGRESS["state"], "failed")

    def test_swapping_the_drive_during_the_backup_is_reported(self):
        self.archived()

        def swap():
            self.drive = VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b")

        with self.after_the_first_copy(swap):
            stats = self.backup()
        self.assertEqual(stats.failed, 1)
        self.assertIn("disconnected or replaced", stats.errors[0][1])
        self.assertEqual(progress._PROGRESS["state"], "failed")


class TestConflictCopies(support.CardFixtureTestCase):
    def two_photos_with_one_name(self):
        for folder, body in (("100NIKON", b"first"), ("101NIKON", b"second")):
            target = self.dcim / folder
            target.mkdir(parents=True)
            photo = target / "DSC_0001.JPG"
            photo.write_bytes(body)
            os.utime(photo, (1.7e9, 1.7e9))
        return card.find_media_files(self.dcim)

    def archived(self):
        return sorted(path.name for path in self.local.rglob("*.JPG"))

    def import_once(self, files):
        stats = transfer.ImportStats()
        transfer.copy_category(files["JPG"], self.dcim, [self.local], "JPG", stats)
        return stats

    def test_importing_a_card_again_does_not_add_another_conflict_copy(self):
        # Reproduced 2026-11-05: three imports made conflict-1, conflict-2 and conflict-3. Only the
        # original name was compared, and each copy also went to USB and Google Photos.
        files = self.two_photos_with_one_name()
        for _ in range(3):
            self.import_once(files)
        self.assertEqual(self.archived(), ["DSC_0001.JPG", "DSC_0001__conflict-1.JPG"])

    def test_a_repeat_import_counts_the_conflict_copy_as_already_saved(self):
        files = self.two_photos_with_one_name()
        self.import_once(files)
        stats = self.import_once(files)
        self.assertEqual((stats.copied, stats.skipped, stats.failed), (0, 2, 0))

    def test_a_third_different_file_still_gets_its_own_conflict_copy(self):
        files = self.two_photos_with_one_name()
        self.import_once(files)
        third = self.dcim / "102NIKON"
        third.mkdir()
        (third / "DSC_0001.JPG").write_bytes(b"third")
        os.utime(third / "DSC_0001.JPG", (1.7e9, 1.7e9))
        self.import_once(card.find_media_files(self.dcim))
        self.assertEqual(
            self.archived(),
            ["DSC_0001.JPG", "DSC_0001__conflict-1.JPG", "DSC_0001__conflict-2.JPG"],
        )

    def test_a_gap_in_the_conflict_numbers_does_not_hide_a_matching_copy(self):
        files = self.two_photos_with_one_name()
        self.import_once(files)
        kept = next(self.local.rglob("DSC_0001__conflict-1.JPG"))
        kept.rename(kept.with_name("DSC_0001__conflict-2.JPG"))
        self.import_once(files)
        self.assertEqual(self.archived(), ["DSC_0001.JPG", "DSC_0001__conflict-2.JPG"])

    def test_a_name_with_glob_characters_finds_its_conflict_copy(self):
        for folder, body in (("100NIKON", b"first"), ("101NIKON", b"second")):
            target = self.dcim / folder
            target.mkdir(parents=True)
            photo = target / "IMG[1].JPG"
            photo.write_bytes(body)
            os.utime(photo, (1.7e9, 1.7e9))
        files = card.find_media_files(self.dcim)
        self.import_once(files)
        self.import_once(files)
        self.assertEqual(self.archived(), ["IMG[1].JPG", "IMG[1]__conflict-1.JPG"])

    def test_a_later_conflict_copy_is_found_when_the_first_does_not_match(self):
        files = self.two_photos_with_one_name()
        third = self.dcim / "102NIKON"
        third.mkdir()
        (third / "DSC_0001.JPG").write_bytes(b"third")
        os.utime(third / "DSC_0001.JPG", (1.7e9, 1.7e9))
        files = card.find_media_files(self.dcim)
        self.import_once(files)
        self.import_once(files)
        self.assertEqual(
            self.archived(),
            ["DSC_0001.JPG", "DSC_0001__conflict-1.JPG", "DSC_0001__conflict-2.JPG"],
        )

    def test_a_name_that_only_starts_like_a_conflict_copy_is_not_one(self):
        files = self.two_photos_with_one_name()
        self.import_once(files)
        kept = next(self.local.rglob("DSC_0001__conflict-1.JPG"))
        kept.rename(kept.with_name("DSC_0001__conflict-1.JPG.bak"))
        self.import_once(files)
        self.assertIn("DSC_0001__conflict-1.JPG", self.archived())

    def test_a_similar_name_is_not_taken_for_a_conflict_copy(self):
        files = self.two_photos_with_one_name()
        self.import_once(files)
        folder = next(self.local.rglob("DSC_0001.JPG")).parent
        kept = folder / "DSC_0001__conflict-1.JPG"
        (folder / "DSC_0001__conflict-note.JPG").write_bytes(kept.read_bytes())
        kept.unlink()
        self.import_once(files)
        self.assertIn("DSC_0001__conflict-1.JPG", self.archived())


if __name__ == "__main__":
    unittest.main()
