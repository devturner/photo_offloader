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

    def test_trigger_recognizes_any_mounted_volume_with_dcim(self):
        self.assertIn('for volume in "$VOLUMES_ROOT"/*; do', self.trigger)
        self.assertIn('[[ -d "$volume" && -d "$volume/DCIM" ]] || continue', self.trigger)
        self.assertIn('Recognized mounted volume with DCIM directory', self.trigger)
        self.assertNotIn('NIKON/Z_8', self.trigger)
        self.assertNotIn('NIKON001.DSC', self.trigger)
        self.assertNotIn('NIKON*|SONY*|EOS*', self.trigger)

    def test_background_import_uses_launchd_submit_not_nohup(self):
        start = self.trigger.index("start_background_import()")
        end = self.trigger.index("\n}\n", start)
        function = self.trigger[start:end]
        self.assertIn('"$LAUNCHCTL" submit -l "$IMPORT_WORKER_LABEL"', function)
        self.assertIn("could not submit import worker", function)
        self.assertNotIn("nohup /bin/bash", function)

    def test_installer_waits_for_worker_shutdown_before_replacing_files(self):
        self.assertIn('SHUTDOWN_WAIT_SECONDS="${PHOTO_OFFLOADER_SHUTDOWN_WAIT_SECONDS:-10}"', self.install)
        self.assertIn('launchctl kill SIGTERM "$DOMAIN/$IMPORT_LABEL"', self.install)
        self.assertIn('launchctl bootout "$DOMAIN/$IMPORT_LABEL"', self.install)
        self.assertIn("import_process_running", self.install)
        self.assertIn("shutdown could not be verified", self.install)
        shutdown_check = self.install.index('if [[ "$shutdown_verified" -ne 1 ]]')
        first_copy = self.install.index('cp "$SCRIPT_DIR/$SCRIPT_NAME" "$SCRIPT_PATH"')
        self.assertLess(shutdown_check, first_copy)

    def test_supervisor_removes_submitted_launchd_job_after_terminal_status(self):
        supervisor = (self.root / "photo_offloader_import_supervisor.sh").read_text(encoding="utf-8")
        self.assertIn('IMPORT_WORKER_LABEL="${PHOTO_OFFLOADER_IMPORT_WORKER_LABEL:-com.user.photooffloader.import}"', supervisor)
        self.assertIn('LAUNCHCTL="${PHOTO_OFFLOADER_LAUNCHCTL:-/bin/launchctl}"', supervisor)
        self.assertIn('"$LAUNCHCTL" remove "$IMPORT_WORKER_LABEL"', supervisor)
        self.assertIn('trap - EXIT HUP INT TERM', supervisor)
        self.assertLess(supervisor.index('write_status "success|$source_path|$supervisor_pid"'),
                        supervisor.index('"$LAUNCHCTL" remove "$IMPORT_WORKER_LABEL"'))
        self.assertLess(supervisor.index('write_status "failed|$source_path|$result"'),
                        supervisor.index('"$LAUNCHCTL" remove "$IMPORT_WORKER_LABEL"'))

    def test_uninstaller_verifies_no_installed_processes_before_removal(self):
        verify = self.script.index("# Verify both launchd registration and exact installed-script processes")
        remove = self.script.index('rm -rf "$INSTALL_DIR"')
        self.assertLess(verify, remove)
        self.assertIn('pids="$(find_pids "$target" || true)"', self.script)
        self.assertIn("Shutdown could not be verified", self.script)
        self.assertIn("Application files and LaunchAgent plists were NOT removed", self.script)


if __name__ == "__main__":
    unittest.main()
