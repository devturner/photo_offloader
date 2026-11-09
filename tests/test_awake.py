import os
import unittest
from unittest.mock import patch

from photo_offloader import awake


class TestKeepAwake(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_it_lasts_exactly_as_long_as_this_process(self, popen):
        # -w ends it even after a SIGKILL. The command line must not name the program, because
        # the trigger and dashboard use pgrep on the importer's path to mean an import is running.
        awake.keep_awake()
        self.assertEqual(
            popen.call_args.args[0], ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
        )

    @patch("subprocess.Popen", side_effect=OSError("no caffeinate"))
    def test_failing_to_start_never_stops_the_work(self, popen):
        with self.assertLogs("camera_offloader", level="WARNING"):
            awake.keep_awake()


if __name__ == "__main__":
    unittest.main()
