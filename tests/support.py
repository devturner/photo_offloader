"""Shared test setup: keep every test inside its own temporary directory.

Photo Offloader is usually installed on the machine that runs the tests, so a
test that reaches a real path can rewrite the live status file, the catalog or
start a real Google Photos upload. ``isolate_config`` redirects *every* path in
``photo_offloader.config`` and refuses to run if a new path was added there
without a test location here.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config

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
    real_paths = {name for name, value in vars(config).items()
                  if name.isupper() and isinstance(value, Path) and name not in CODE_PATHS}
    uncovered = real_paths - set(values)
    if uncovered:
        raise AssertionError(f"tests/support.py has no test location for config {sorted(uncovered)}")
    for name, value in values.items():
        patcher = patch.object(config, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    # The catalog schema is set up once per database path per process.
    patcher = patch.object(catalog, "_CATALOG_INITIALIZED_PATH", None)
    patcher.start()
    testcase.addCleanup(patcher.stop)
