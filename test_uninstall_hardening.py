#!/usr/bin/env python3
import unittest
from pathlib import Path


class TestUninstallHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent
        cls.script = (cls.root / "uninstall.sh").read_text(encoding="utf-8")
        cls.trigger = (cls.root / "camera_offloader_trigger.sh").read_text(encoding="utf-8")
        cls.install = (cls.root / "install.sh").read_text(encoding="utf-8")

    def test_verbose_dry_run_and_help_options_are_available(self):
        self.assertIn("--verbose", self.script)
        self.assertIn("--dry-run", self.script)
        self.assertIn("--help", self.script)
        self.assertIn("DRY RUN ONLY", self.script)

    def test_uninstall_handles_all_known_launch_agents(self):
        for label in (
            "com.user.photooffloader",
            "com.user.photooffloader.menubar",
            "com.user.photooffloader.import",
        ):
            self.assertIn(label, self.script)
        self.assertIn("launchctl bootout", self.script)
        self.assertIn("Cleanup incomplete", self.script)

    def test_uninstall_stops_installed_processes_before_removing_files(self):
        stop_call = self.script.index('stop_matching_processes "$INSTALL_DIR/camera_offloader_trigger.sh"')
        remove_call = self.script.index('rm -rf "$INSTALL_DIR"')
        self.assertLess(stop_call, remove_call)
        self.assertIn("photo_offloader_import_supervisor.sh", self.script)
        self.assertIn("camera_offloader_v2.py", self.script)
        self.assertIn("photo_offloader_ui.py", self.script)
        self.assertIn("photo_offloader_menubar.py", self.script)

    def test_uninstall_preserves_media_and_catalog(self):
        self.assertNotIn('rm -rf "$DATA_DIR"', self.script)
        self.assertNotIn('rm -rf "$HOME/Pictures/CameraImports"', self.script)
        self.assertIn("Persistent media catalog preserved", self.script)
        self.assertIn("Imported media under ~/Pictures/CameraImports", self.script)

    def test_background_import_uses_launchd_submit_not_nohup(self):
        start = self.trigger.index("start_background_import()")
        end = self.trigger.index("\n}\n", start)
        function = self.trigger[start:end]
        self.assertIn('"$LAUNCHCTL" submit -l "$IMPORT_WORKER_LABEL"', function)
        self.assertIn("could not submit import worker", function)
        self.assertNotIn("nohup /bin/bash", function)

    def test_installer_stops_a_stale_submitted_worker(self):
        self.assertIn('launchctl kill SIGTERM "gui/$(id -u)/com.user.photooffloader.import"', self.install)


if __name__ == "__main__":
    unittest.main()
