#!/usr/bin/env python3
"""Serialize USB-backup launch requests before they reach the importer."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
IMPORTER_SCRIPT = Path(os.environ.get(
    "PHOTO_OFFLOADER_IMPORTER", str(INSTALL_DIR / "camera_offloader_v2.py")
))
LOCK_PATH = Path(os.environ.get(
    "PHOTO_OFFLOADER_USB_REQUEST_LOCK", str(INSTALL_DIR / ".usb-backup-request.lock")
))


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("USB backup request ignored: another USB backup launch is already active.")
            return 0

        if not IMPORTER_SCRIPT.is_file():
            print(f"Photo Offloader importer not found: {IMPORTER_SCRIPT}", file=sys.stderr)
            return 1

        # Hold this separate request lock for the entire child process lifetime.
        # Both the menu-bar app and dashboard use this launcher, so rapid repeated
        # clicks cannot start multiple importer processes.
        return subprocess.run(
            [sys.executable, str(IMPORTER_SCRIPT), "--backup-usb"],
            check=False,
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
