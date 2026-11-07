"""Shared test setup: keep every test inside its own temporary directory.

Photo Offloader is usually installed on the machine that runs the tests, so a
test that reaches a real path can rewrite the live status file, the catalog or
start a real Google Photos upload. ``isolate_config`` redirects *every* path in
``photo_offloader.config`` and refuses to run if a new path was added there
without a test location here.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import (
    card,
    catalog,
    catalog_refresh,
    config,
    logs,
    media_info,
    progress,
    run_report,
    transfer,
    volumes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths that describe the code, not user data; tests may read them as they are.
CODE_PATHS = {"APP_ROOT"}


def _test_locations(root: Path) -> dict:
    install, data = root / "install", root / "data"
    usb = root / "Volumes" / "Photos"
    return {
        "HOME": root / "home",
        "INSTALL_DIR": install,
        "DATA_DIR": data,
        "LOCAL_DEST_ROOT": root / "CameraImports",
        "LOCAL_RAW_DEST_ROOT": root / "CameraImportsRaw",
        "USB_DEST_VOLUME": usb,
        "USB_DEST_ROOT": usb / "CameraImports",
        "LOG_FILE_PATH": root / "transfer.log",
        "TRIGGER_LOG_PATH": root / "trigger.log",
        "SESSION_LOCK_PATH": root / "import.lock",
        "STATUS_FILE_PATH": install / "status.json",
        "CATALOG_DB_PATH": data / "catalog.sqlite3",
        "CARD_MANIFESTS_DIR": data / "card-manifests",
        "RUN_REPORTS_DIR": data / "run-reports",
        "VERIFY_REPORTS_DIR": data / "verify-reports",
        "VERIFY_LOCK_PATH": root / "verify.lock",
        "VERIFY_STATUS_PATH": data / "verify_status.json",
        "DELETED_FROM_MAC_PATH": data / "deleted_from_mac.json",
        "LEGACY_CATALOG_DB_PATH": install / "catalog.sqlite3",
        "BUILD_INFO_PATH": root / "build_info.json",
        "VERSION_PATH": root / "VERSION",
        # Never start the real installed Google Photos sync from a test.
        "GOOGLE_PHOTOS_SYNC_PATH": root / "not-installed" / "photo-offloader-google-sync",
        "GOOGLE_STATUS_PATH": install / "google_photos_status.json",
        "GOOGLE_LOG_PATH": install / "google_photos_sync.log",
        "GOOGLE_LAUNCHER_LOG_PATH": install / "google_photos_sync_launcher.log",
        "GOOGLE_LOCK_PATH": install / ".google-photos-sync.lock",
        "PREFERENCES_PATH": data / "preferences.json",
        "RCLONE_CONFIG_PATH": root / "rclone" / "rclone.conf",
    }


def isolate_config(testcase: unittest.TestCase, root: Path, **overrides) -> None:
    """Point every configured path into ``root`` (or ``overrides``) until the test ends."""
    values = {**_test_locations(root), **overrides}
    real_paths = {
        name
        for name, value in vars(config).items()
        if name.isupper() and isinstance(value, Path) and name not in CODE_PATHS
    }
    uncovered = real_paths - set(values)
    if uncovered:
        raise AssertionError(
            f"tests/support.py has no test location for config {sorted(uncovered)}"
        )
    for name, value in values.items():
        patcher = patch.object(config, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    # A test must never put a notification on the user's screen.
    patcher = patch.object(logs, "notify")
    patcher.start()
    testcase.addCleanup(patcher.stop)
    # A fake card is a plain folder with no volume ID, and a test must not run diskutil on it.
    patcher = patch.object(card, "card_volume_id", return_value="TEST-CARD-UUID")
    patcher.start()
    testcase.addCleanup(patcher.stop)
    patcher = patch.object(run_report, "volume_info", return_value="Test volume\n")
    patcher.start()
    testcase.addCleanup(patcher.stop)
    # The catalog schema is set up once per database path per process.
    patcher = patch.object(catalog, "_CATALOG_INITIALIZED_PATH", None)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class CardFixtureTestCase(unittest.TestCase):
    """A fake camera card and archive folders in a temp dir, with config isolated.

    The base for the importer module tests (test_card, test_transfer and so on).
    """

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
        isolate_config(
            self,
            self.root,
            LOCAL_DEST_ROOT=self.local,
            LOCAL_RAW_DEST_ROOT=self.root / "local-raw",
            USB_DEST_ROOT=self.usb,
            USB_DEST_VOLUME=self.usb_volume,
            LOG_FILE_PATH=self.log,
            SESSION_LOCK_PATH=self.lock,
            STATUS_FILE_PATH=self.root / "status.json",
            CATALOG_DB_PATH=self.root / "persistent-data" / "catalog.sqlite3",
            LEGACY_CATALOG_DB_PATH=self.root / "legacy-app-dir" / "catalog.sqlite3",
        )
        config.LOCAL_RAW_DEST_ROOT.mkdir()
        for name, value in (
            ("VERIFY_WITH_SHA256", False),
            ("USE_FILE_BIRTH_TIME", False),
            ("WAIT_SECONDS", 0),
            ("EJECT_CARD_AFTER_SUCCESS", True),
            ("MIN_FREE_SPACE_BUFFER_BYTES", 0),
            ("READ_RETRY_PAUSE_SECONDS", 0),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
            ("_PROGRESS_STARTED", None),
            (
                "_PROGRESS",
                {"state": "idle", "bytes_done": 0, "bytes_total": 0, "bytes_transferred": 0},
            ),
        ):
            patcher = patch.object(progress, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        media_info.find_exiftool.cache_clear()
        logs.logger.handlers.clear()

    def tearDown(self):
        for handler in logs.logger.handlers[:]:
            handler.close()
        media_info.find_exiftool.cache_clear()
        logs.logger.handlers.clear()
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

    def archive_photos(self, count, folder="Photos", suffix=".jpg"):
        target = config.LOCAL_DEST_ROOT / "2026-10" / "NIKON-Z-8" / folder
        target.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (target / f"refresh{index}{suffix}").write_bytes(b"photo %d" % index)
        return target

    def refresh_catalog(self, batch_calls=None):
        def fake_batch(paths, tags):
            if batch_calls is not None:
                batch_calls.append(len(paths))
            return {
                str(p.resolve()): {
                    "Make": "NIKON",
                    "Model": "Z 8",
                    "DateTimeOriginal": "2020:01:02 03:04:05",
                }
                for p in paths
            }

        with (
            patch.object(volumes, "usb_is_mounted", return_value=False),
            patch.object(media_info, "get_exiftool_metadata_batch", side_effect=fake_batch),
            patch.object(logs, "notify"),
        ):
            return catalog_refresh.update_file_catalog()

    def connections_during_refresh(self):
        opened = []
        real_connect = sqlite3.connect
        catalog.catalog_connection().close()

        def counting_connect(*args, **kwargs):
            opened.append(args)
            return real_connect(*args, **kwargs)

        with patch.object(catalog.sqlite3, "connect", counting_connect):
            self.refresh_catalog()
        return len(opened)

    def shift_mtime(self, path, seconds):
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1_000_000_000)))

    def fake_exiftool(self):
        """An exiftool stand-in that answers every file and logs each process start."""
        calls = self.root / "exiftool-calls.txt"
        script = self.root / "exiftool"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(calls)!r}, 'a').write('call\\n')\n"
            "files = [a for a in sys.argv[1:] if not a.startswith('-') and a != 'filename=UTF8']\n"
            "print(json.dumps([{'SourceFile': f, 'Make': 'NIKON CORPORATION', 'Model': 'NIKON Z 8',\n"
            "                   'DateTimeOriginal': '2026:09:14 10:00:00'} for f in files]))\n"
        )
        script.chmod(0o755)
        patcher = patch.object(media_info, "find_exiftool", return_value=str(script))
        patcher.start()
        self.addCleanup(patcher.stop)
        return lambda: len(calls.read_text().splitlines()) if calls.exists() else 0

    def card_files(self, count):
        folder = self.dcim / "100NCZ_8"
        folder.mkdir(parents=True, exist_ok=True)
        raws = [folder / f"DSC_{index:04d}.NEF" for index in range(count)]
        for index, path in enumerate(raws):
            path.write_bytes(b"raw%d" % index)
        clip = folder / "DSC_9999.MOV"
        clip.write_bytes(b"video")
        return {"JPG": [], "RAW": raws, "VIDEO": [clip], "OTHER": []}

    def archive_already_on_usb(self, count):
        archive = config.LOCAL_DEST_ROOT / "2026-11" / "NIKON-Z-8" / "Photos"
        backup = config.USB_DEST_ROOT / "2026-11" / "NIKON-Z-8" / "Photos"
        archive.mkdir(parents=True, exist_ok=True)
        backup.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (archive / f"p{index}.jpg").write_bytes(b"photo %d" % index)
            (backup / f"p{index}.jpg").write_bytes(b"photo %d" % index)

    def connections_during_usb_backup(self):
        opened = []
        real_connect = sqlite3.connect
        catalog.catalog_connection().close()  # one-time schema setup is not the point here

        def counting_connect(*args, **kwargs):
            opened.append(args)
            return real_connect(*args, **kwargs)

        with (
            patch.object(volumes, "usb_is_mounted", return_value=True),
            patch.object(catalog.sqlite3, "connect", counting_connect),
        ):
            stats = transfer.copy_local_archive_to_usb()
        self.assertEqual(stats.failed, 0, stats.errors)
        return len(opened)
