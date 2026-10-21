#!/usr/bin/env python3
"""Serialize USB-backup launch requests before they reach the importer."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
IMPORTER_SCRIPT = Path(os.environ.get(
    "PHOTO_OFFLOADER_IMPORTER", str(INSTALL_DIR / "camera_offloader_v2.py")
))
LOCK_PATH = Path(os.environ.get(
    "PHOTO_OFFLOADER_USB_REQUEST_LOCK", str(INSTALL_DIR / ".usb-backup-request.lock")
))
DUPLICATE_WINDOW_SECONDS = 15.0


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("USB backup request ignored: another USB backup launch is already active.")
            return 0

        now = time.time()
        lock.seek(0)
        previous_text = lock.read().strip()
        try:
            previous_request = float(previous_text) if previous_text else 0.0
        except ValueError:
            previous_request = 0.0
        if now - previous_request < DUPLICATE_WINDOW_SECONDS:
            print("USB backup request ignored: duplicate request within 15 seconds.")
            return 0

        if not IMPORTER_SCRIPT.is_file():
            print(f"Photo Offloader importer not found: {IMPORTER_SCRIPT}", file=sys.stderr)
            return 1

        # Persist a short debounce window as well as holding the lock for the
        # child lifetime. This suppresses repeated clicks even if a fast backup
        # finishes before the next click arrives.
        lock.seek(0)
        lock.truncate()
        lock.write(str(now))
        lock.flush()
        os.fsync(lock.fileno())

        return subprocess.run(
            [sys.executable, str(IMPORTER_SCRIPT), "--backup-usb"],
            check=False,
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
