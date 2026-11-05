#!/usr/bin/env python3
import pathlib
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import card, catalog, config, fileops, media_info, transfer, volumes
from photo_offloader.transfer import ImportStats
from photo_offloader.volumes import VolumeIdentity
from tests import support


class TestTransfer(support.CardFixtureTestCase):
    def test_usb_backup_mirrors_raw_archive_to_matching_root(self):
        jpg = self.local / "2026-10" / "100NIKON" / "Photos" / "photo.jpg"
        raw = config.LOCAL_RAW_DEST_ROOT / "2026-10" / "100NIKON" / "Raws" / "photo.nef"
        jpg.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        jpg.write_bytes(b"jpeg-content")
        raw.write_bytes(b"raw-content")
        identity = VolumeIdentity(self.usb_volume, "disk-test", "volume-test")

        with patch.object(volumes, "usb_is_mounted", return_value=True), \
                patch.object(volumes, "get_volume_identity", return_value=identity), \
                patch.object(volumes, "volume_identity_matches", return_value=True):
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
        with patch.object(fileops, "sha256_file", side_effect=AssertionError("must reuse cached hashes")):
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
        with patch.object(fileops, "sha256_file", side_effect=AssertionError("must reuse cached hashes")):
            result = transfer.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual(result.status, "skipped")

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

    def camera_index_file(self, content):
        source = self.dcim / "104NCZ_8" / "NC_FLLST.DAT"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        return source

    def test_camera_index_file_is_replaced_not_conflict_copied(self):
        # User log 2026-11-04: Nikon rewrites NC_FLLST.DAT on every shoot, so each
        # import left another NC_FLLST__conflict-N.DAT in Other.
        first = transfer.copy_one_to_destination(self.camera_index_file(b"index v1"), self.dcim, self.local)
        second = transfer.copy_one_to_destination(self.camera_index_file(b"index v2 longer"), self.dcim, self.local)
        self.assertEqual(second.status, "copied")
        self.assertEqual(second.path, first.path)
        self.assertEqual(first.path.read_bytes(), b"index v2 longer")
        self.assertEqual(sorted(p.name for p in first.path.parent.iterdir()), ["NC_FLLST.DAT"])

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
            transfer.copy_category(files[category], self.dcim, [self.local, self.usb], category, stats)
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
            transfer.copy_local_archive_to_usb()   # first run hashes and records everything
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
        with patch.object(volumes, "usb_is_mounted", return_value=True), \
                patch.object(fileops, "sha256_file", side_effect=AssertionError("re-read a file")):
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

        with patch.object(volumes, "usb_is_mounted", return_value=True), \
                patch.object(volumes, "get_volume_identity", return_value=None), \
                patch.object(pathlib.Path, "open", spy):
            stats = transfer.copy_local_archive_to_usb()
        self.assertEqual((stats.destination_copies, stats.failed), (1, 0))
        self.assertEqual(len(opened), 1, f"local file was read {len(opened)} times")


if __name__ == "__main__":
    unittest.main()
