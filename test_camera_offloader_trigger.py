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
        self.background_args = self.root / "background-import-args.txt"
        self.python.write_text(
            "#!/bin/bash\n"
            + "if [[ \"$1\" == \"$PHOTO_OFFLOADER_IMPORTER\" ]]; then "
            + f"printf '%s\\n' \"$@\" > {self.background_args}; sleep 0.2; fi\n"
            + "exit 0\\n"
        )
        self.importer.write_text("#!/bin/bash\nexit 0\n")
        self.dashboard = self.install / "photo_offloader_ui.py"
        self.dashboard.write_text("# dashboard test fixture\n")
        self.python.chmod(0o755)

        self.pgrep = self.install / "pgrep"
        self.pgrep.write_text("#!/bin/bash\nexit 1\n")
        self.pgrep.chmod(0o755)

        self.osascript = self.install / "osascript"
        self.command_capture = self.root / "osascript-command.txt"
        self.script_capture = self.root / "osascript-script.txt"
        self.osascript.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$2\" > {self.command_capture!s}\n"
            f"cat > {self.script_capture!s}\n"
            "exit 0\n"
        )
        self.osascript.chmod(0o755)

        self.log = self.root / "trigger.log"
        self.state = self.root / "state"
        self.lock = self.root / "trigger.lock"
        # Preserve terminal-mode expectations for legacy trigger tests; tests
        # for the new default explicitly remove this opt-in marker.
        (self.install / ".terminal-output-enabled").touch()
        self.script = Path(__file__).with_name("camera_offloader_trigger.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def run_trigger(self, **extra):
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root),
                "PHOTO_OFFLOADER_INSTALL_DIR": str(self.install),
                "PHOTO_OFFLOADER_PYTHON": str(self.python),
                "PHOTO_OFFLOADER_IMPORTER": str(self.importer),
                "PHOTO_OFFLOADER_DASHBOARD": str(self.dashboard),
                "PHOTO_OFFLOADER_TRIGGER_LOG": str(self.log),
                "PHOTO_OFFLOADER_TRIGGER_STATE": str(self.state),
                "PHOTO_OFFLOADER_TRIGGER_LOCK": str(self.lock),
                "PHOTO_OFFLOADER_TRIGGER_SETTLE_SECONDS": "0",
                "PHOTO_OFFLOADER_PGREP": str(self.pgrep),
                "PHOTO_OFFLOADER_OSASCRIPT": str(self.osascript),
                "PHOTO_OFFLOADER_VOLUMES_ROOT": str(self.volumes),
                "PHOTO_OFFLOADER_TERMINAL_OUTPUT_FILE": str(self.install / ".terminal-output-enabled"),
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

    def test_paused_service_does_not_start_an_import(self):
        pause_file = self.install / ".paused"
        pause_file.touch()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.command_capture.exists())
        self.assertFalse(self.state.exists())
        self.assertIn("Automatic import is paused", self.log.read_text())

    def test_import_start_requests_dashboard_open(self):
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Opened progress dashboard", self.log.read_text())

    def test_default_import_runs_without_opening_terminal(self):
        (self.install / ".terminal-output-enabled").unlink()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.script_capture.exists())
        self.assertIn("Terminal output disabled", self.log.read_text())
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))
        # Terminal output changes only where output is sent, not whether import runs.
        import time
        deadline = time.monotonic() + 2
        while not self.background_args.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.background_args.exists(), "background importer was not launched")
        args = self.background_args.read_text()
        self.assertIn("--source", args)
        self.assertIn(str(self.dcim), args)

    def test_terminal_can_be_enabled_by_dashboard_toggle(self):
        (self.install / ".terminal-output-enabled").touch()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        script = self.script_capture.read_text()
        self.assertIn('tell application "iTerm2"', script)
        self.assertIn("create window with default profile command", script)

    def test_terminal_app_can_be_overridden_when_enabled(self):
        (self.install / ".terminal-output-enabled").touch()
        result = self.run_trigger(PHOTO_OFFLOADER_TERMINAL_APP="Terminal")
        self.assertEqual(result.returncode, 0, result.stderr)
        script = self.script_capture.read_text()
        self.assertIn("tell application (item 2 of argv)", script)
        self.assertNotIn('tell application "iTerm2"', script)

    def test_photos_volume_alone_is_ignored(self):
        self.dcim.rmdir()
        self.card.rmdir()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertIn("No recognized camera DCIM currently mounted", self.log.read_text())

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
