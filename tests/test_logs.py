#!/usr/bin/env python3
import unittest
from unittest.mock import MagicMock, patch

from photo_offloader import logs
from tests import support

REAL_NOTIFY = logs.notify  # isolate_config replaces it during each test


class TestLogs(support.CardFixtureTestCase):
    @patch("subprocess.run")
    def test_notify_nonfatal(self, run):
        run.return_value = MagicMock(returncode=0, stderr="")
        REAL_NOTIFY("T", "B")
        run.return_value = MagicMock(returncode=1, stderr="bad")
        REAL_NOTIFY("T", "B")
        self.assertEqual(run.call_count, 2)

    def test_rotate_log_file_keeps_bounded_generations(self):
        log = self.root / "sync.log"
        for generation in range(5):
            log.write_text(f"generation-{generation}\n" + "x" * 2000)
            logs.rotate_log_file(log, max_bytes=1000, backups=2)
        self.assertFalse(log.exists() and log.stat().st_size > 1000)
        self.assertTrue((self.root / "sync.log.1").exists())
        self.assertTrue((self.root / "sync.log.2").exists())
        self.assertFalse((self.root / "sync.log.3").exists())
        self.assertIn("generation-4", (self.root / "sync.log.1").read_text())

    def test_rotate_log_file_leaves_small_files_alone(self):
        log = self.root / "small.log"
        log.write_text("tiny\n")
        logs.rotate_log_file(log, max_bytes=1000, backups=2)
        self.assertEqual(log.read_text(), "tiny\n")
        self.assertFalse((self.root / "small.log.1").exists())


if __name__ == "__main__":
    unittest.main()
