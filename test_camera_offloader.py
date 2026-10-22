#!/usr/bin/env python3
import errno
import plistlib
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import camera_offloader_v2 as app
from camera_offloader_v2 import (
    ImportStats,
    VolumeIdentity,
)


class TestCameraOffloader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.card = self.root / "card"
        self.dcim = self.card / "DCIM"
        self.local = self.root / "local"
        self.usb = self.root / "usb"
        self.usb_volume = self.root / "usb-volume"
        self.log = self.root / "transfer.log"
        self.lock = self.root / "import.lock"
        self.dcim.mkdir(parents=True)
        self.local.mkdir()
        self.usb.mkdir()
        self.usb_volume.mkdir()
        self.original = {k: getattr(app, k) for k in (
            "LOCAL_DEST_ROOT", "LOCAL_RAW_DEST_ROOT", "USB_DEST_ROOT", "USB_DEST_VOLUME", "CATALOG_DB_PATH",
            "LEGACY_CATALOG_DB_PATH", "_CATALOG_INITIALIZED_PATH",
            "BUILD_INFO_PATH", "VERSION_PATH",
            "LOG_FILE_PATH", "SESSION_LOCK_PATH", "VERIFY_WITH_SHA256",
            "USE_FILE_BIRTH_TIME", "WAIT_SECONDS",
            "EJECT_CARD_AFTER_SUCCESS", "MIN_FREE_SPACE_BUFFER_BYTES", "STATUS_FILE_PATH",
            "_PROGRESS_STARTED", "_PROGRESS",
        )}
        app.LOCAL_DEST_ROOT = self.local
        app.LOCAL_RAW_DEST_ROOT = self.root / "local-raw"
        app.LOCAL_RAW_DEST_ROOT.mkdir()
        app.USB_DEST_ROOT = self.usb
        app.USB_DEST_VOLUME = self.usb_volume
        app.LOG_FILE_PATH = self.log
        app.SESSION_LOCK_PATH = self.lock
        app.VERIFY_WITH_SHA256 = False
        app.USE_FILE_BIRTH_TIME = False
        app.WAIT_SECONDS = 0
        app.EJECT_CARD_AFTER_SUCCESS = True
        app.MIN_FREE_SPACE_BUFFER_BYTES = 0
        app.STATUS_FILE_PATH = self.root / "status.json"
        app.CATALOG_DB_PATH = self.root / "persistent-data" / "catalog.sqlite3"
        app.LEGACY_CATALOG_DB_PATH = self.root / "legacy-app-dir" / "catalog.sqlite3"
        app._CATALOG_INITIALIZED_PATH = None
        app.BUILD_INFO_PATH = self.root / "build_info.json"
        app.VERSION_PATH = self.root / "VERSION"
        app._PROGRESS_STARTED = None
        app._PROGRESS = {"state": "idle", "bytes_done": 0, "bytes_total": 0, "bytes_transferred": 0}
        app.find_exiftool.cache_clear()
        app.logger.handlers.clear()

    def tearDown(self):
        for handler in app.logger.handlers[:]:
            handler.close()
        app.find_exiftool.cache_clear()
        app.logger.handlers.clear()
        for key, value in self.original.items():
            setattr(app, key, value)
        self.tmp.cleanup()

    def media(self):
        folder = self.dcim / "100NIKON"
        folder.mkdir(parents=True, exist_ok=True)
        files = {
            "JPG": folder / "photo.jpg",
            "RAW": folder / "photo.nef",
            "VIDEO": folder / "clip.mp4",
        }
        files["JPG"].write_bytes(b"jpg")
        files["RAW"].write_bytes(b"raw")
        files["VIDEO"].write_bytes(b"video")
        return files

    def test_raw_media_uses_separate_top_level_archive(self):
        self.assertEqual(app.local_root_for_category("RAW"), app.LOCAL_RAW_DEST_ROOT)
        self.assertEqual(app.local_root_for_category("JPG"), app.LOCAL_DEST_ROOT)
        self.assertEqual(app.local_root_for_category("VIDEO"), app.LOCAL_DEST_ROOT)
        self.assertEqual(app.usb_root_for_category("RAW"), app.USB_DEST_ROOT)
        self.assertEqual(app.usb_root_for_category("JPG"), app.USB_DEST_ROOT)

    def test_usb_backup_mirrors_raw_archive_to_matching_root(self):
        jpg = self.local / "2026-10" / "100NIKON" / "Photos" / "photo.jpg"
        raw = app.LOCAL_RAW_DEST_ROOT / "2026-10" / "100NIKON" / "Raws" / "photo.nef"
        jpg.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        jpg.write_bytes(b"jpeg-content")
        raw.write_bytes(b"raw-content")
        identity = VolumeIdentity(self.usb_volume, "disk-test", "volume-test")

        with patch.object(app, "usb_is_mounted", return_value=True), \
                patch.object(app, "get_volume_identity", return_value=identity), \
                patch.object(app, "volume_identity_matches", return_value=True):
            stats = app.copy_local_archive_to_usb(identity)

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.destination_copies, 2)
        self.assertEqual((self.usb / jpg.relative_to(self.local)).read_bytes(), b"jpeg-content")
        self.assertEqual(
            (self.usb / raw.relative_to(app.LOCAL_RAW_DEST_ROOT)).read_bytes(),
            b"raw-content",
        )

    def test_stats_aliases_and_success(self):
        stats = ImportStats(destination_copies=2, destination_skips=1, verified_copies=3)
        self.assertTrue(stats.successful)
        self.assertEqual((stats.copied, stats.skipped, stats.verified), (2, 1, 3))
        stats.errors.append(("x", "y"))
        self.assertFalse(stats.successful)

    def test_logging(self):
        app.configure_logging(verbose=True)
        app.logger.info("hello")
        for handler in app.logger.handlers:
            handler.flush()
        self.assertIn("hello", self.log.read_text())

    def test_parse_args(self):
        args = app.parse_args(["--source", str(self.card), "--no-usb", "--no-eject", "--sha256", "--verbose"])
        self.assertEqual(args.source, self.card)
        self.assertTrue(args.no_usb and args.no_eject and args.sha256 and args.verbose)
        self.assertFalse(args.backup_usb)
        self.assertTrue(app.parse_args(["--backup-usb"]).backup_usb)

    @patch("camera_offloader_v2.subprocess.run")
    def test_notify_nonfatal(self, run):
        run.return_value = MagicMock(returncode=0, stderr="")
        app.notify("T", "B")
        run.return_value = MagicMock(returncode=1, stderr="bad")
        app.notify("T", "B")
        self.assertEqual(run.call_count, 2)

    def test_resolve_dcim(self):
        self.assertEqual(app.resolve_dcim_path(self.card), self.dcim.resolve())
        self.assertEqual(app.resolve_dcim_path(self.dcim), self.dcim.resolve())

    def test_resolve_dcim_rejects_missing(self):
        with self.assertRaises(RuntimeError):
            app.resolve_dcim_path(self.root / "missing")

    @patch("camera_offloader_v2.glob.glob", return_value=[])
    def test_find_card_no_match(self, glob):
        with patch("builtins.input", return_value=""), self.assertRaises(RuntimeError):
            app.find_card_dcim()

    def test_scan_filters_hidden_and_unsupported(self):
        self.media()
        (self.dcim / ".hidden.jpg").write_bytes(b"x")
        (self.dcim / "._meta.jpg").write_bytes(b"x")
        (self.dcim / "notes.txt").write_bytes(b"x")
        found = app.find_media_files(self.dcim)
        self.assertEqual([p.name for p in found["JPG"]], ["photo.jpg"])
        self.assertEqual([p.name for p in found["RAW"]], ["photo.nef"])
        self.assertEqual([p.name for p in found["VIDEO"]], ["clip.mp4"])

    @patch("camera_offloader_v2.find_media_files")
    def test_scan_retry(self, scan):
        empty = {"JPG": [], "RAW": [], "VIDEO": []}
        found = {"JPG": [self.dcim / "x.jpg"], "RAW": [], "VIDEO": []}
        scan.side_effect = [empty, found]
        app.WAIT_SECONDS = 30
        with patch("camera_offloader_v2.time.monotonic", side_effect=[0, 1]), patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.scan_with_retry(self.dcim), found)

    def test_normalize_camera_model(self):
        self.assertEqual(app.normalize_camera_model("NIKON", "Z 8"), "NIKON-Z-8")
        self.assertEqual(app.normalize_camera_model("SONY", "ILCE-7M4"), "SONY-ILCE-7M4")
        self.assertEqual(app.normalize_camera_model("NIKON", "NIKON Z 8"), "NIKON-Z-8")
        self.assertEqual(app.normalize_camera_model("NIKON CORPORATION", "NIKON Z 8"), "NIKON-Z-8")
        self.assertEqual(app.normalize_camera_model(None, None), "Unknown-Camera")

    @patch("camera_offloader_v2.shutil.which", return_value="/opt/homebrew/bin/exiftool")
    def test_find_exiftool_caches_discovery(self, which):
        self.assertEqual(app.find_exiftool(), "/opt/homebrew/bin/exiftool")
        self.assertEqual(app.find_exiftool(), "/opt/homebrew/bin/exiftool")
        which.assert_called_once_with("exiftool")

    def test_camera_metadata_is_cached_for_unchanged_file(self):
        path = self.root / "cached.NEF"
        path.write_bytes(b"raw image")
        with patch.object(app, "get_camera_model_from_exiftool", return_value="NIKON-Z-8") as read_metadata:
            self.assertEqual(app.get_camera_model(path), "NIKON-Z-8")
            self.assertEqual(app.get_camera_model(path), "NIKON-Z-8")
        read_metadata.assert_called_once_with(path)

    def test_catalog_invalidates_cached_metadata_when_file_changes(self):
        path = self.root / "changed.NEF"
        path.write_bytes(b"first")
        with patch.object(app, "get_camera_model_from_exiftool", return_value="NIKON-Z-8") as read_metadata:
            self.assertEqual(app.get_camera_model(path), "NIKON-Z-8")
            path.write_bytes(b"second content")
            self.assertEqual(app.get_camera_model(path), "NIKON-Z-8")
        self.assertEqual(read_metadata.call_count, 2)

    @patch("camera_offloader_v2.catalog_connection")
    def test_catalog_db_closes_connection_after_transaction(self, connect):
        connection = MagicMock()
        connect.return_value = connection

        with app.catalog_db() as db:
            self.assertIs(db, connection)

        connection.__enter__.assert_called_once_with()
        connection.__exit__.assert_called_once()
        connection.close.assert_called_once_with()

    def test_catalog_migrates_from_legacy_install_directory(self):
        app.LEGACY_CATALOG_DB_PATH.parent.mkdir(parents=True)
        with sqlite3.connect(app.LEGACY_CATALOG_DB_PATH) as legacy:
            legacy.execute("CREATE TABLE legacy_marker (value TEXT)")
            legacy.execute("INSERT INTO legacy_marker VALUES ('preserve-me')")
        with app.catalog_connection() as db:
            self.assertEqual(db.execute("SELECT value FROM legacy_marker").fetchone()[0],
                             "preserve-me")
        self.assertTrue(app.CATALOG_DB_PATH.is_file())

    def test_update_file_catalog_indexes_local_archive_without_usb(self):
        media = self.local / "2026-10" / "NIKON-Z-8" / "Photos" / "test.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"photo bytes")
        with (
            patch.object(app, "usb_is_mounted", return_value=False),
            patch.object(
                app, "get_month_folder",
                side_effect=lambda p: (
                    app.catalog_record(p, capture_month="2026-10") or "2026-10"
                ),
            ),
            patch.object(
                app, "get_camera_model",
                side_effect=lambda p: (
                    app.catalog_record(p, camera_model="NIKON-Z-8") or "NIKON-Z-8"
                ),
            ),
            patch.object(app, "notify"),
        ):
            result = app.update_file_catalog()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["roots"], 2)  # CameraImports and CameraImportsRaw
        row = app.catalog_row(media)
        self.assertIsNotNone(row)
        self.assertEqual(row[5], "NIKON-Z-8")
        self.assertEqual(row[6], "Photos")

    def test_exiftool_batch_metadata_is_keyed_by_resolved_source_path(self):
        media = self.local / "batch-test.NEF"
        media.write_bytes(b"raw bytes")
        payload = '[{"SourceFile": "' + str(media) + '", "Make": "Nikon", "Model": "Z 8"}]'
        with patch.object(app, "find_exiftool", return_value="/usr/bin/exiftool"), patch.object(
            app.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout=payload, stderr=""),
        ) as run:
            result = app.get_exiftool_metadata_batch([media], ["-Make", "-Model"])
        self.assertEqual(result[str(media.resolve())]["Model"], "Z 8")
        self.assertEqual(run.call_count, 1)
        self.assertIn(str(media), run.call_args.args[0])

    def test_card_manifest_round_trip_and_fingerprint_invalidation(self):
        source = self.dcim / "100NIKON" / "IMG_0001.NEF"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"raw-data")
        digest = app.sha256_file(source)
        manifest = {}
        app.update_card_manifest_entry(manifest, source, self.dcim, digest)
        self.assertTrue(app.save_card_manifest(self.dcim, manifest))
        loaded = app.load_card_manifest(self.dcim)
        self.assertEqual(app.card_manifest_entry(loaded, source, self.dcim), digest)

        source.write_bytes(b"changed raw data")
        self.assertIsNone(app.card_manifest_entry(loaded, source, self.dcim))

    def test_copy_decision_reuses_card_and_catalog_hashes(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        digest = app.sha256_file(source)
        manifest = {}
        app.update_card_manifest_entry(manifest, source, self.dcim, digest)
        app.catalog_record(destination, sha256=digest)
        with patch.object(app, "sha256_file", side_effect=AssertionError("must reuse cached hashes")):
            result = app.copy_one_to_destination(source, self.dcim, self.local, manifest)
        self.assertEqual(result.status, "skipped")

    def test_google_photos_upload_record_tracks_current_fingerprint(self):
        path = self.local / "uploaded.jpg"
        path.write_bytes(b"photo")
        app.mark_google_photos_uploaded(path, "gphotos", "Photo Offloader")
        record = app.google_photos_upload_record(path)
        self.assertIsNotNone(record)
        self.assertEqual(record["remote"], "gphotos")
        self.assertEqual(record["album"], "Photo Offloader")
        path.write_bytes(b"changed photo")
        self.assertIsNone(app.google_photos_upload_record(path))

    def test_catalog_records_file_locations(self):
        path = self.root / "catalog-photo.jpg"
        path.write_bytes(b"photo")
        app.catalog_record(path, sha256=app.sha256_file(path), capture_month="2026-10",
                           camera_model="NIKON-Z-8", media_folder="Photos")
        row = app.catalog_row(path)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], app.sha256_file(path))
        self.assertEqual(row[4:7], ("2026-10", "NIKON-Z-8", "Photos"))

    def test_get_camera_model_from_exif(self):
        fake_image = MagicMock()
        fake_image.__enter__.return_value = fake_image
        fake_image.getexif.return_value = {271: "NIKON", 272: "Z 8"}
        with patch("camera_offloader_v2.Image.open", return_value=fake_image):
            self.assertEqual(app.get_camera_model(self.root / "IMG.JPG"), "NIKON-Z-8")

    @patch("camera_offloader_v2.shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("camera_offloader_v2.subprocess.run")
    def test_get_camera_model_from_nef_uses_exiftool(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"photo.NEF","Make":"NIKON CORPORATION","Model":"NIKON Z 8"}]',
            stderr="",
        )
        path = self.root / "photo.NEF"
        self.assertEqual(app.get_camera_model(path), "NIKON-Z-8")
        which.assert_called_once_with("exiftool")
        self.assertEqual(run.call_args.args[0], [
            "/opt/homebrew/bin/exiftool", "-j", "-Make", "-Model", str(path)
        ])

    @patch("camera_offloader_v2.shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("camera_offloader_v2.subprocess.run")
    def test_get_exif_datetime_from_nef_uses_exiftool(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"photo.NEF","DateTimeOriginal":"2026:10:21 13:49:15"}]',
            stderr="",
        )
        path = self.root / "photo.NEF"
        self.assertEqual(app.get_exif_datetime(path), app.datetime(2026, 10, 21, 13, 49, 15))
        which.assert_called_once_with("exiftool")
        self.assertEqual(run.call_args.args[0], [
            "/opt/homebrew/bin/exiftool", "-j", "-DateTimeOriginal",
            "-CreateDate", "-DateTimeDigitized", "-ModifyDate", str(path)
        ])

    def test_get_camera_model_missing_exif(self):
        fake_image = MagicMock()
        fake_image.__enter__.return_value = fake_image
        fake_image.getexif.return_value = {}
        with patch("camera_offloader_v2.Image.open", return_value=fake_image):
            self.assertEqual(app.get_camera_model(self.root / "IMG.JPG"), "Unknown-Camera")

    @patch("camera_offloader_v2.shutil.which", return_value="/opt/homebrew/bin/exiftool")
    @patch("camera_offloader_v2.subprocess.run")
    def test_get_camera_model_from_mov_metadata(self, run, which):
        run.return_value = MagicMock(
            returncode=0,
            stdout='[{"SourceFile":"clip.MOV","Make":"Canon","Model":"Canon PowerShot S100"}]',
            stderr="",
        )
        path = self.root / "clip.MOV"
        self.assertEqual(app.get_camera_model(path), "Canon-PowerShot-S100")
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

    @patch("camera_offloader_v2.os.access", return_value=True)
    @patch.object(Path, "is_file", autospec=True)
    @patch("camera_offloader_v2.shutil.which", return_value=None)
    @patch("camera_offloader_v2.subprocess.run")
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

        self.assertEqual(app.get_camera_model(path), "Canon-PowerShot-S100")
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
    @patch("camera_offloader_v2.shutil.which", return_value=None)
    def test_get_camera_model_mov_without_exiftool(self, which, is_file):
        self.assertEqual(app.get_camera_model(self.root / "clip.MOV"), "Unknown-Camera")
        which.assert_called_once_with("exiftool")

    def test_media_type_folders(self):
        self.assertEqual(app.get_media_type_folder(Path("photo.jpg")), "Photos")
        self.assertEqual(app.get_media_type_folder(Path("photo.nef")), "Raws")
        self.assertEqual(app.get_media_type_folder(Path("clip.mov")), "Movies")

    def test_destination_path_uses_month_camera_model_and_media_type(self):
        source = self.dcim / "100NIKON" / "IMG.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x")
        with (
            patch.object(app, "get_month_folder", return_value="2026-08"),
            patch.object(app, "get_camera_model", return_value="NIKON-Z-8"),
        ):
            result = app.build_destination_path(source, self.dcim, self.local)
        self.assertEqual(result, self.local / "2026-08" / "NIKON-Z-8" / "Photos" / "IMG.JPG")

    def test_files_match_requires_sha_for_existing_duplicate(self):
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"abc")
        b.write_bytes(b"xyz")
        self.assertTrue(app.files_match(a, b))
        self.assertFalse(app.files_match(a, b, force_sha256=True))
        b.write_bytes(b"abc")
        self.assertTrue(app.files_match(a, b, force_sha256=True))

    @patch("camera_offloader_v2.update_status")
    @patch("camera_offloader_v2.load_build_info", return_value={
        "version": "2.4", "git_commit": "abc123def456",
        "git_branch": "feature/test", "git_tag": "v2.4",
        "git_dirty": "false", "build_time_utc": "2026-10-21T22:00:00Z",
        "source_path": "/installed/camera_offloader_v2.py",
    })
    @patch("camera_offloader_v2.import_session_lock",
           side_effect=RuntimeError("Another Photo Offloader import session is already running."))
    def test_duplicate_session_does_not_reset_active_progress(
        self, lock, build_info, update_status
    ):
        self.assertEqual(app.main(["--backup-usb"]), 0)
        update_status.assert_not_called()
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("version=2.4", log)
        self.assertIn("git_commit=abc123def456", log)
        self.assertIn("git_branch=feature/test", log)
        self.assertIn("run_id=", log)

    def test_load_build_info_reads_installed_provenance(self):
        app.BUILD_INFO_PATH.write_text(
            __import__("json").dumps({
                "version": "2.4",
                "git_commit": "abc123def456",
                "git_branch": "feature/test",
                "git_tag": "v2.4",
                "git_dirty": "false",
                "build_time_utc": "2026-10-21T22:00:00Z",
                "source_path": "/Applications/PhotoOffloader/camera_offloader_v2.py",
            }),
            encoding="utf-8",
        )
        info = app.load_build_info()
        self.assertEqual(info["version"], "2.4")
        self.assertEqual(info["git_commit"], "abc123def456")
        self.assertEqual(info["git_branch"], "feature/test")
        self.assertEqual(info["git_tag"], "v2.4")
        self.assertEqual(info["git_dirty"], "false")
        self.assertEqual(info["source_path"], "/Applications/PhotoOffloader/camera_offloader_v2.py")

    def test_import_session_lock_acquires_and_releases(self):
        with app.import_session_lock():
            self.assertTrue(self.lock.exists())
            self.assertEqual(self.lock.read_text(), str(__import__("os").getpid()))

    def test_import_session_lock_rejects_second_lock(self):
        with app.import_session_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with app.import_session_lock():
                    pass

    def test_source_fingerprint(self):
        source = self.root / "source"
        source.write_bytes(b"abc")
        fp = app.file_fingerprint(source)
        self.assertEqual(fp.size, 3)
        self.assertGreaterEqual(fp.mtime_ns, 0)

    def test_install_cross_device(self):
        temp, final = self.root / "tmp", self.root / "final"
        temp.write_bytes(b"data")
        with patch("camera_offloader_v2.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            self.assertTrue(app.install_without_overwrite(temp, final))
        self.assertEqual(final.read_bytes(), b"data")

    def test_copy_and_verify(self):
        source = self.media()["RAW"]
        source.chmod(0o700)
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertEqual(result.path.read_bytes(), b"raw")
        self.assertEqual(stat.S_IMODE(result.path.stat().st_mode), 0o600)


    def test_matching_duplicate_is_content_verified(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "skipped")

    def test_same_size_different_content_gets_conflict(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"xxx")  # same size as b"raw"
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.status, "copied")
        self.assertIn("__conflict-1", result.path.name)
        self.assertEqual(destination.read_bytes(), b"xxx")
        self.assertEqual(result.path.read_bytes(), b"raw")

    def test_conflict_counter_advances(self):
        source = self.media()["RAW"]
        destination = app.build_destination_path(source, self.dcim, self.local)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old")
        destination.with_name("photo__conflict-1.nef").write_bytes(b"old")
        destination.with_name("photo__conflict-2.nef").write_bytes(b"old")
        result = app.copy_one_to_destination(source, self.dcim, self.local)
        self.assertEqual(result.path.name, "photo__conflict-3.nef")

    def test_source_mutation_is_rejected(self):
        source = self.media()["RAW"]
        original_copy = app.copy_file_with_progress

        def mutate(src, dst):
            original_copy(src, dst)
            Path(src).write_bytes(b"RAW")  # changes size

        with patch("camera_offloader_v2.copy_file_with_progress", side_effect=mutate):
            with self.assertRaisesRegex(IOError, "Source changed"):
                app.copy_one_to_destination(source, self.dcim, self.local)

    def test_required_source_bytes(self):
        files = self.media()
        self.assertEqual(app.required_source_bytes({k: [v] for k, v in files.items()}), 11)

    def test_validate_destinations_checks_space(self):
        with patch("camera_offloader_v2.available_bytes", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "Insufficient free space"):
                app.validate_destinations([self.local], required_bytes=11)

    def test_volume_identity_matching(self):
        expected = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        current = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        replaced = VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b")
        self.assertTrue(app.volume_identity_matches(expected, current))
        self.assertFalse(app.volume_identity_matches(expected, replaced))

    @patch("camera_offloader_v2.subprocess.run")
    def test_get_volume_identity(self, run):
        payload = plistlib.dumps({
            "DeviceIdentifier": "disk4s1",
            "VolumeUUID": "uuid-a",
        })
        run.return_value = MagicMock(returncode=0, stdout=payload)
        identity = app.get_volume_identity(self.usb_volume)
        self.assertEqual(identity.volume_uuid, "uuid-a")
        self.assertEqual(identity.device_identifier, "disk4s1")

    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    def test_snapshot_local_only(self, mounted):
        snapshot = app.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local,))
        self.assertFalse(snapshot.usb_expected)

    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.get_volume_identity")
    def test_snapshot_includes_usb(self, identity, mounted):
        identity.return_value = VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a")
        snapshot = app.snapshot_destinations()
        self.assertEqual(snapshot.destinations, (self.local, self.usb))
        self.assertTrue(snapshot.usb_expected)
        self.assertEqual(snapshot.usb_identity.volume_uuid, "uuid-a")

    @patch("camera_offloader_v2.subprocess.run")
    def test_eject_success_failure(self, run):
        with patch.object(Path, "is_mount", return_value=True):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(app.eject_volume(self.card))
            run.return_value = MagicMock(returncode=1, stdout="", stderr="bad")
            self.assertFalse(app.eject_volume(self.card))

    @patch("camera_offloader_v2.eject_volume", return_value=True)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    def test_main_success_and_resolved_eject_path(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            result = app.main(["--source", str(self.card)])
        self.assertEqual(result, 0)
        eject.assert_called_once_with(self.card.resolve())

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.copy_one_to_destination", side_effect=OSError("copy failure"))
    def test_main_copy_failure_never_ejects(self, copy, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        eject.assert_not_called()

    @patch("camera_offloader_v2.eject_volume", return_value=False)
    @patch("camera_offloader_v2.usb_is_mounted", return_value=False)
    @patch("camera_offloader_v2.notify")
    def test_main_eject_failure_returns_2(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 2)
        eject.assert_called_once_with(self.card.resolve())

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.notify")
    def test_main_no_usb(self, notify, mounted, eject):
        self.media()
        with patch("camera_offloader_v2.get_volume_identity", return_value=None), patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card), "--no-usb", "--no-eject"]), 0)
        self.assertEqual(list(self.usb.rglob("*")), [])

    @patch("camera_offloader_v2.eject_volume")
    @patch("camera_offloader_v2.notify")
    @patch("camera_offloader_v2.usb_is_mounted", return_value=True)
    @patch("camera_offloader_v2.get_volume_identity")
    def test_main_ejects_after_local_copy_and_reports_usb_replacement(self, identity, mounted, notify, eject):
        self.media()
        identity.side_effect = [
            VolumeIdentity(self.usb_volume.resolve(), "disk4s1", "uuid-a"),
            VolumeIdentity(self.usb_volume.resolve(), "disk5s1", "uuid-b"),
        ]
        with patch("camera_offloader_v2.time.sleep"):
            self.assertEqual(app.main(["--source", str(self.card)]), 1)
        # Local SSD is verified before ejection; USB replacement is detected
        # afterward, so the card is safely released and backup reports failure.
        eject.assert_called_once_with(self.card.resolve())

    def test_second_import_skips(self):
        self.media()
        files = app.find_media_files(self.dcim)
        first = ImportStats(found=3)
        second = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local], category, first)
            app.copy_category(files[category], self.dcim, [self.local], category, second)
        self.assertEqual(first.destination_copies, 3)
        self.assertEqual(second.destination_skips, 3)

    def test_end_to_end_two_destinations(self):
        self.media()
        files = app.find_media_files(self.dcim)
        stats = ImportStats(found=3)
        for category in ("JPG", "RAW", "VIDEO"):
            app.copy_category(files[category], self.dcim, [self.local, self.usb], category, stats)
        self.assertEqual((stats.destination_copies, stats.verified_copies, stats.failed), (6, 6, 0))

    @patch("camera_offloader_v2.copy_local_archive_to_usb")
    @patch("camera_offloader_v2.notify")
    def test_standalone_usb_backup_does_not_require_card(self, notify, backup):
        backup.return_value = ImportStats(destination_copies=2, destination_skips=4,
                                          verified_copies=6)
        with patch("builtins.input", side_effect=AssertionError("should not prompt for a card")):
            self.assertEqual(app.main(["--backup-usb"]), 0)
        backup.assert_called_once_with()
        notify.assert_called_once()

    def test_usb_backup_copies_missing_files_and_skips_matching_files(self):
        archive = self.local / "2026-10" / "NIKON-Z-8" / "Photos"
        archive.mkdir(parents=True)
        (archive / "already.jpg").write_bytes(b"same content")
        (archive / "missing.jpg").write_bytes(b"new content")
        existing = self.usb / "2026-10" / "NIKON-Z-8" / "Photos"
        existing.mkdir(parents=True)
        (existing / "already.jpg").write_bytes(b"same content")

        with patch.object(app, "usb_is_mounted", return_value=True):
            stats = app.copy_local_archive_to_usb()

        self.assertEqual(stats.destination_copies, 1)
        self.assertEqual(stats.destination_skips, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual((existing / "missing.jpg").read_bytes(), b"new content")

    def test_usb_backup_does_not_overwrite_different_existing_file(self):
        archive = self.local / "2026-10" / "Photos"
        archive.mkdir(parents=True)
        (archive / "photo.jpg").write_bytes(b"local version")
        existing = self.usb / "2026-10" / "Photos"
        existing.mkdir(parents=True)
        (existing / "photo.jpg").write_bytes(b"USB version")

        with patch.object(app, "usb_is_mounted", return_value=True):
            stats = app.copy_local_archive_to_usb()

        self.assertEqual(stats.destination_copies, 0)
        self.assertEqual(stats.failed, 1)
        self.assertEqual((existing / "photo.jpg").read_bytes(), b"USB version")


if __name__ == "__main__":
    unittest.main()
