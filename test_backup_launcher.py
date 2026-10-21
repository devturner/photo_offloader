#!/usr/bin/env python3
"""Tests for the cross-UI USB backup request lock."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).with_name("photo_offloader_backup_launcher.py")


class TestBackupLauncher(unittest.TestCase):
    def test_duplicate_request_is_ignored_while_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "backup.lock"
            importer = root / "importer.py"
            marker = root / "importer-ran"
            importer.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PHOTO_OFFLOADER_USB_REQUEST_LOCK"] = str(lock_path)
            env["PHOTO_OFFLOADER_IMPORTER"] = str(importer)

            with lock_path.open("a", encoding="utf-8") as held_lock:
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [sys.executable, str(LAUNCHER)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("request ignored", result.stdout)
            self.assertFalse(marker.exists())

    def test_request_runs_importer_when_lock_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            importer = root / "importer.py"
            marker = root / "importer-ran"
            importer.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PHOTO_OFFLOADER_USB_REQUEST_LOCK"] = str(root / "backup.lock")
            env["PHOTO_OFFLOADER_IMPORTER"] = str(importer)
            result = subprocess.run(
                [sys.executable, str(LAUNCHER)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
