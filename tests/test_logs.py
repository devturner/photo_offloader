#!/usr/bin/env python3
import unittest
from unittest.mock import MagicMock, patch

from photo_offloader import logs
from tests import support

REAL_NOTIFY = logs.notify  # isolate_config replaces it during each test


class TestLogs(support.CardFixtureTestCase):
    @patch.object(logs, "_notification_needed", return_value=True)
    @patch("subprocess.run")
    def test_notify_nonfatal(self, run, _needed):
        run.return_value = MagicMock(returncode=0, stderr="")
        REAL_NOTIFY("T", "B")
        run.return_value = MagicMock(returncode=1, stderr="bad")
        REAL_NOTIFY("T", "B")
        self.assertEqual(run.call_count, 2)

    def test_timed_adds_up_sections_and_counts_a_failure_too(self):
        logs.reset_timings()
        with patch("time.perf_counter", side_effect=[10.0, 11.5, 20.0, 20.5]):
            with logs.timed("catalog"):
                pass
            with self.assertRaises(ValueError), logs.timed("catalog"):
                raise ValueError
        self.assertEqual(logs.timing_summary(5.0), "5.0 s: catalog 2.0 s, other 3.0 s")

    def test_timed_works_as_a_decorator_and_keeps_the_result(self):
        logs.reset_timings()

        @logs.timed("status")
        def write(value):
            return value

        self.assertEqual(write(7), 7)
        self.assertEqual(write(8), 8)
        self.assertIn("status ", logs.timing_summary(1.0))

    def test_sections_are_listed_slowest_first_and_other_never_goes_negative(self):
        logs.reset_timings()
        with patch("time.perf_counter", side_effect=[0.0, 1.0, 0.0, 3.0]):
            with logs.timed("small"):
                pass
            with logs.timed("big"):
                pass
        self.assertEqual(logs.timing_summary(3.5), "3.5 s: big 3.0 s, small 1.0 s, other 0.0 s")

    def test_reset_starts_again(self):
        with logs.timed("catalog"):
            pass
        logs.reset_timings()
        self.assertEqual(logs.timing_summary(1.0), "1.0 s: other 1.0 s")

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
