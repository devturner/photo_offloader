#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestCameraOffloaderTrigger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.volumes = self.root / "Volumes"
        self.install = self.root / "install"
        self.volumes.mkdir()
        self.install.mkdir()

        self.photos = self.volumes / "Photos"
        self.photos.mkdir()
        self.card = self.volumes / "NIKON Z 8"
        self.dcim = self.card / "DCIM"
        self.dcim.mkdir(parents=True)

        self.python = self.install / "python3"
        self.importer = self.install / "camera_offloader_v2.py"
        self.python.write_text("#!/bin/bash\nexit 0\n")
        self.importer.write_text("#!/bin/bash\nexit 0\n")
        self.python.chmod(0o755)

        self.pgrep = self.install / "pgrep"
        self.pgrep.write_text("#!/bin/bash\nexit 1\n")
        self.pgrep.chmod(0o755)

        self.osascript = self.install / "osascript"
        self.command_capture = self.root / "osascript-command.txt"
        self.osascript.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$2\" > {self.command_capture!s}\n"
            "exit 0\n"
        )
        self.osascript.chmod(0o755)

        self.log = self.root / "trigger.log"
        self.state = self.root / "state"
        self.lock = self.root / "trigger.lock"
        self.script = Path(__file__).with_name("camera_offloader_trigger.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def run_trigger(self, **extra):
        env = os.environ.copy()
        env.update(
            {
                "PHOTO_OFFLOADER_INSTALL_DIR": str(self.install),
                "PHOTO_OFFLOADER_PYTHON": str(self.python),
                "PHOTO_OFFLOADER_IMPORTER": str(self.importer),
                "PHOTO_OFFLOADER_TRIGGER_LOG": str(self.log),
                "PHOTO_OFFLOADER_TRIGGER_STATE": str(self.state),
                "PHOTO_OFFLOADER_TRIGGER_LOCK": str(self.lock),
                "PHOTO_OFFLOADER_TRIGGER_SETTLE_SECONDS": "0",
                "PHOTO_OFFLOADER_PGREP": str(self.pgrep),
                "PHOTO_OFFLOADER_OSASCRIPT": str(self.osascript),
                "PHOTO_OFFLOADER_VOLUMES_ROOT": str(self.volumes),
            }
        )
        env.update(extra)
        return subprocess.run(
            ["/bin/bash", str(self.script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_photos_volume_alone_is_ignored(self):
        self.dcim.rmdir()
        self.card.rmdir()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertIn("no recognized camera DCIM", self.log.read_text())

    def test_camera_volume_with_spaces_is_launched_without_path_corruption(self):
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))
        command = self.command_capture.read_text().strip()
        # The trigger builds a shell-safe command with bash %q, so spaces
        # are escaped rather than wrapped in quotes.
        self.assertIn("NIKON\\ Z\\ 8/DCIM", command)
        self.assertIn("--source", command)
        self.assertNotIn("NIKON Z 8/DCIM", command)

    def test_repeated_volume_event_does_not_launch_same_mount_twice(self):
        self.assertEqual(self.run_trigger().returncode, 0)
        first_command = self.command_capture.read_text()
        self.command_capture.unlink()

        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertEqual(self.command_capture.exists(), False)
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))
        self.assertIn("already handled for this mount", self.log.read_text())
        self.assertNotEqual(first_command, "")

    def test_unmount_clears_state_and_allows_remount(self):
        self.assertEqual(self.run_trigger().returncode, 0)
        self.command_capture.unlink()
        self.dcim.rmdir()
        self.card.rmdir()

        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertFalse(self.state.exists())

        self.dcim.mkdir(parents=True)
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertTrue(self.command_capture.exists())

    def test_running_importer_is_not_launched_again(self):
        self.pgrep.write_text("#!/bin/bash\nexit 0\n")
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertIn("Importer already running", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
