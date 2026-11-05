#!/usr/bin/env python3
import os
import unittest
from unittest.mock import patch

from photo_offloader import progress
from tests import support


class TestProgress(support.CardFixtureTestCase):
    def test_progress_status_writes_are_throttled_during_chunk_copies(self):
        writes = []
        real_replace = os.replace

        def counting_replace(src, dst):
            writes.append(dst)
            return real_replace(src, dst)

        progress.update_status(state="copying", bytes_total=10_000_000)
        with patch("os.replace", counting_replace):
            for _ in range(2000):
                progress.add_progress_bytes(1024, transferred=True)
        self.assertLessEqual(len(writes), 5, f"{len(writes)} status.json rewrites for 2000 chunks")
        self.assertEqual(progress._PROGRESS["bytes_done"], 2000 * 1024)  # counters stay exact


if __name__ == "__main__":
    unittest.main()
