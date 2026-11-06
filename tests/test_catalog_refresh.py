#!/usr/bin/env python3
import unittest
from unittest.mock import patch

from photo_offloader import catalog, catalog_refresh, logs, media_info, volumes
from tests import support


class TestCatalogRefresh(support.CardFixtureTestCase):
    def test_update_file_catalog_indexes_local_archive_without_usb(self):
        media = self.local / "2026-10" / "NIKON-Z-8" / "Photos" / "test.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"photo bytes")
        with (
            patch.object(volumes, "usb_is_mounted", return_value=False),
            # Provide deterministic batch metadata so this test covers the
            # fast catalog path without depending on a locally installed ExifTool.
            patch.object(
                media_info,
                "get_exiftool_metadata_batch",
                side_effect=lambda paths, tags: {
                    str(path.resolve()): {"Make": "NIKON", "Model": "Z 8"} for path in paths
                },
            ),
            patch.object(
                media_info,
                "get_month_folder",
                side_effect=lambda p: (
                    catalog.catalog_record(p, capture_month="2026-10") or "2026-10"
                ),
            ),
            patch.object(
                media_info,
                "get_camera_model",
                side_effect=lambda p: (
                    catalog.catalog_record(p, camera_model="NIKON-Z-8") or "NIKON-Z-8"
                ),
            ),
            patch.object(logs, "notify"),
        ):
            result = catalog_refresh.update_file_catalog()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["roots"], 2)  # CameraImports and CameraImportsRaw
        row = catalog.catalog_row(media)
        self.assertIsNotNone(row)
        self.assertEqual(row[5], "NIKON-Z-8")
        self.assertEqual(row[6], "Photos")

    def test_catalog_refresh_uses_the_catalog_in_bulk_not_per_file(self):
        self.archive_photos(5)
        small = self.connections_during_refresh()
        self.archive_photos(50)
        large = self.connections_during_refresh()
        self.assertEqual(small, large, "catalog connections must not grow with the archive")

    def test_repeat_catalog_refresh_asks_exiftool_for_nothing(self):
        self.archive_photos(6)
        self.assertEqual(self.refresh_catalog()["scanned"], 6)
        calls = []
        self.assertEqual(self.refresh_catalog(batch_calls=calls)["scanned"], 6)
        self.assertEqual(sum(calls), 0)

    def test_catalog_refresh_dates_video_like_an_import_does(self):
        # Video months come from file time on import (get_exif_datetime skips video),
        # so the refresh must cache the same month rather than an EXIF date.
        clip = self.archive_photos(1, folder="Movies", suffix=".mov") / "refresh0.mov"
        self.refresh_catalog()
        self.assertEqual(catalog.catalog_row(clip)[4], media_info._month_from_file_time(clip))
        photo = self.archive_photos(1) / "refresh0.jpg"
        self.refresh_catalog()
        self.assertEqual(catalog.catalog_row(photo)[4], "2020-01")


if __name__ == "__main__":
    unittest.main()
