#!/usr/bin/env python3
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from photo_offloader import catalog, config, media_info
from tests import support


class TestMediaInfo(support.CardFixtureTestCase):
    def test_raw_media_uses_separate_top_level_archive(self):
        self.assertEqual(media_info.local_root_for_category("RAW"), config.LOCAL_RAW_DEST_ROOT)
        self.assertEqual(media_info.local_root_for_category("JPG"), config.LOCAL_DEST_ROOT)
        self.assertEqual(media_info.local_root_for_category("VIDEO"), config.LOCAL_DEST_ROOT)

    def test_normalize_camera_model(self):
        self.assertEqual(media_info.normalize_camera_model("NIKON", "Z 8"), "NIKON-Z-8")
        self.assertEqual(media_info.normalize_camera_model("SONY", "ILCE-7M4"), "SONY-ILCE-7M4")
        self.assertEqual(media_info.normalize_camera_model("NIKON", "NIKON Z 8"), "NIKON-Z-8")
        self.assertEqual(
            media_info.normalize_camera_model("NIKON CORPORATION", "NIKON Z 8"), "NIKON-Z-8"
        )
        self.assertEqual(media_info.normalize_camera_model(None, None), "Unknown-Camera")

    @patch("shutil.which", return_value="/opt/homebrew/bin/exiftool")
    def test_find_exiftool_caches_discovery(self, which):
        self.assertEqual(media_info.find_exiftool(), "/opt/homebrew/bin/exiftool")
        self.assertEqual(media_info.find_exiftool(), "/opt/homebrew/bin/exiftool")
        which.assert_called_once_with("exiftool")

    def test_camera_metadata_is_cached_for_unchanged_file(self):
        path = self.root / "cached.NEF"
        path.write_bytes(b"raw image")
        with patch.object(
            media_info, "get_camera_model_from_exiftool", return_value="NIKON-Z-8"
        ) as read_metadata:
            self.assertEqual(media_info.get_camera_model(path), "NIKON-Z-8")
            self.assertEqual(media_info.get_camera_model(path), "NIKON-Z-8")
        read_metadata.assert_called_once_with(path)

    def test_catalog_invalidates_cached_metadata_when_file_changes(self):
        path = self.root / "changed.NEF"
        path.write_bytes(b"first")
        with patch.object(
            media_info, "get_camera_model_from_exiftool", return_value="NIKON-Z-8"
        ) as read_metadata:
            self.assertEqual(media_info.get_camera_model(path), "NIKON-Z-8")
            path.write_bytes(b"second content")
            self.assertEqual(media_info.get_camera_model(path), "NIKON-Z-8")
        self.assertEqual(read_metadata.call_count, 2)

    def test_exiftool_batch_metadata_is_keyed_by_resolved_source_path(self):
        media = self.local / "batch-test.NEF"
        media.write_bytes(b"raw bytes")
        payload = '[{"SourceFile": "' + str(media) + '", "Make": "Nikon", "Model": "Z 8"}]'
        with (
            patch.object(media_info, "find_exiftool", return_value="/usr/bin/exiftool"),
            patch.object(
                media_info.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout=payload, stderr=""),
            ) as run,
        ):
            result = media_info.get_exiftool_metadata_batch([media], ["-Make", "-Model"])
        self.assertEqual(result[str(media.resolve())]["Model"], "Z 8")
        self.assertEqual(run.call_count, 1)
        self.assertIn(str(media), run.call_args.args[0])

    def test_dst_shift_keeps_cached_capture_metadata(self):
        source = self.media()["RAW"]
        catalog.catalog_record(source, capture_month="2026-10", camera_model="NIKON-Z-8")
        self.shift_mtime(source, 3600)
        with patch.object(
            media_info, "get_exiftool_metadata", side_effect=AssertionError("must reuse metadata")
        ):
            self.assertEqual(media_info.get_camera_model(source), "NIKON-Z-8")
            self.assertEqual(media_info.get_month_folder(source), "2026-10")
        self.shift_mtime(source, 1)  # not a whole-hour shift: cache must miss
        self.assertIsNone(catalog.catalog_row(source))

    def test_fresh_card_metadata_is_read_in_one_exiftool_process(self):
        # User card 2026-11-04: two ExifTool processes per RAW cost ~340 s for
        # 1,188 files; one batched process takes ~19 s.
        exiftool_calls = self.fake_exiftool()
        files = self.card_files(25)
        media_info.prefetch_card_metadata(files)
        self.assertEqual(exiftool_calls(), 1)
        destinations = [
            media_info.build_destination_path(p, self.dcim, self.local) for p in files["RAW"]
        ]
        self.assertEqual(
            exiftool_calls(), 1, "per-file ExifTool calls still happened after the prefetch"
        )
        self.assertEqual(destinations[0].parent, self.local / "2026-09" / "NIKON-Z-8" / "Raws")

    def test_prefetch_matches_the_per_file_lookup(self):
        # Video months come from file time, not EXIF, in the per-file path; the
        # batch must produce exactly the same folders or files would move.
        self.fake_exiftool()
        files = self.card_files(2)
        everything = files["RAW"] + files["VIDEO"]
        per_file = [media_info.build_destination_path(p, self.dcim, self.local) for p in everything]
        config.CATALOG_DB_PATH.unlink()
        catalog._CATALOG_INITIALIZED_PATH = None
        media_info.prefetch_card_metadata(files)
        self.assertEqual(
            [media_info.build_destination_path(p, self.dcim, self.local) for p in everything],
            per_file,
        )

    def test_prefetch_skips_files_whose_metadata_is_cached(self):
        exiftool_calls = self.fake_exiftool()
        files = self.card_files(3)
        media_info.prefetch_card_metadata(files)
        media_info.prefetch_card_metadata(files)
        self.assertEqual(exiftool_calls(), 1)

    def test_get_camera_model_from_exif(self):
        fake_image = MagicMock()
        fake_image.__enter__.return_value = fake_image
        fake_image.getexif.return_value = {271: "NIKON", 272: "Z 8"}
        with patch("photo_offloader.media_info.Image.open", return_value=fake_image):
            self.assertEqual(media_info.get_camera_model(self.root / "IMG.JPG"), "NIKON-Z-8")

    @patch("shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("subprocess.run")
    def test_get_camera_model_from_nef_uses_exiftool(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"photo.NEF","Make":"NIKON CORPORATION","Model":"NIKON Z 8"}]',
            stderr="",
        )
        path = self.root / "photo.NEF"
        self.assertEqual(media_info.get_camera_model(path), "NIKON-Z-8")
        which.assert_called_once_with("exiftool")
        self.assertEqual(
            run.call_args.args[0],
            ["/opt/homebrew/bin/exiftool", "-j", "-Make", "-Model", str(path)],
        )

    @patch("shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("subprocess.run")
    def test_get_exif_datetime_from_nef_uses_exiftool(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"photo.NEF","DateTimeOriginal":"2026:10:21 13:49:15"}]',
            stderr="",
        )
        path = self.root / "photo.NEF"
        self.assertEqual(media_info.get_exif_datetime(path), datetime(2026, 10, 21, 13, 49, 15))
        which.assert_called_once_with("exiftool")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/opt/homebrew/bin/exiftool",
                "-j",
                "-DateTimeOriginal",
                "-CreateDate",
                "-DateTimeDigitized",
                "-ModifyDate",
                str(path),
            ],
        )

    def test_get_camera_model_missing_exif(self):
        fake_image = MagicMock()
        fake_image.__enter__.return_value = fake_image
        fake_image.getexif.return_value = {}
        with patch("photo_offloader.media_info.Image.open", return_value=fake_image):
            self.assertEqual(media_info.get_camera_model(self.root / "IMG.JPG"), "Unknown-Camera")

    @patch("shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("subprocess.run")
    def test_get_camera_model_from_mov_metadata(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"clip.MOV","Make":"Canon","Model":"Canon PowerShot S100"}]',
            stderr="",
        )
        path = self.root / "clip.MOV"
        self.assertEqual(media_info.get_camera_model(path), "Canon-PowerShot-S100")
        which.assert_called_once_with("exiftool")
        run.assert_called_once_with(
            [
                "/opt/homebrew/bin/exiftool",
                "-j",
                "-Make",
                "-Model",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("os.access", return_value=True)
    @patch.object(Path, "is_file", autospec=True)
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_get_camera_model_from_mov_finds_homebrew_exiftool_without_path(
        self, run, which, is_file, access
    ):
        is_file.side_effect = lambda candidate: str(candidate) == "/opt/homebrew/bin/exiftool"
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"clip.MOV","Make":"Canon","Model":"Canon PowerShot S100"}]',
            stderr="",
        )
        path = self.root / "clip.MOV"

        self.assertEqual(media_info.get_camera_model(path), "Canon-PowerShot-S100")
        which.assert_called_once_with("exiftool")
        run.assert_called_once_with(
            [
                "/opt/homebrew/bin/exiftool",
                "-j",
                "-Make",
                "-Model",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch.object(Path, "is_file", return_value=False)
    @patch("shutil.which", return_value=None)
    def test_get_camera_model_mov_without_exiftool(self, which, is_file):
        self.assertEqual(media_info.get_camera_model(self.root / "clip.MOV"), "Unknown-Camera")
        which.assert_called_once_with("exiftool")

    def test_media_type_folders(self):
        self.assertEqual(media_info.get_media_type_folder(Path("photo.jpg")), "Photos")
        self.assertEqual(media_info.get_media_type_folder(Path("photo.nef")), "Raws")
        self.assertEqual(media_info.get_media_type_folder(Path("clip.mov")), "Movies")

    def test_destination_path_uses_month_camera_model_and_media_type(self):
        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x")
        with (
            patch.object(media_info, "get_month_folder", return_value="2026-08"),
            patch.object(media_info, "get_camera_model", return_value="NIKON-Z-8"),
        ):
            result = media_info.build_destination_path(source, self.dcim, self.local)
        self.assertEqual(result, self.local / "2026-08" / "NIKON-Z-8" / "Photos" / "IMG.JPG")

    def test_unrecognized_files_skip_expensive_metadata_lookups(self):
        sidecar = self.dcim / "notes.xml"
        sidecar.write_bytes(b"<xml/>")
        with patch.object(
            media_info, "get_exiftool_metadata", side_effect=AssertionError("no exiftool")
        ):
            self.assertIsNone(media_info.get_exif_datetime(sidecar))
            self.assertEqual(media_info.get_camera_model(sidecar), "Unknown-Camera")


if __name__ == "__main__":
    unittest.main()
