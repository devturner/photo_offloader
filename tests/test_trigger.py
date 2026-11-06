#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


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
        (self.install / "bin").mkdir()
        (self.install / "scripts").mkdir()
        self.importer = self.install / "bin" / "photo-offloader-import"
        self.background_args = self.root / "background-import-args.txt"
        self.background_path = self.root / "background-import-path.txt"
        self.python.write_text(
            "#!/bin/bash\n"
            + 'if [[ "$1" == "$PHOTO_OFFLOADER_IMPORTER" ]]; then '
            + f"printf '%s\\n' \"$@\" > {self.background_args}; "
            + f"printf '%s\\n' \"$PATH\" > {self.background_path}; sleep 0.2; fi\n"
            + "exit 0\n"
        )
        self.importer.write_text("#!/bin/bash\nexit 0\n")
        self.supervisor = self.install / "scripts" / "import_supervisor.sh"
        self.supervisor.write_text((REPO / "scripts" / "import_supervisor.sh").read_text())
        self.supervisor.chmod(0o755)
        self.dashboard = self.install / "bin" / "photo-offloader-dashboard"
        self.dashboard.write_text("# dashboard test fixture\n")
        self.python.chmod(0o755)

        self.launchctl = self.install / "launchctl"
        self.launchctl.write_text(
            "#!/bin/bash\n"
            + 'if [[ "$1" == "print" ]]; then [[ "${PHOTO_OFFLOADER_TEST_WORKER_REGISTERED:-0}" == "1" ]]; exit $?; fi\n'
            + 'if [[ "$1" == "submit" ]]; then shift 4; "$@" >> "$PHOTO_OFFLOADER_IMPORT_LOG" 2>&1 </dev/null & exit 0; fi\n'
            + "exit 1\n"
        )
        self.launchctl.chmod(0o755)

        self.pgrep = self.install / "pgrep"
        self.pgrep.write_text("#!/bin/bash\nexit 1\n")
        self.pgrep.chmod(0o755)

        self.diskutil = self.install / "diskutil"
        self.eject_capture = self.root / "eject-args.txt"
        self.diskutil.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "info" ]]; then\n'
            '  volume="${@: -1}"\n'
            '  uuid="$(cat "$volume/.test-volume-uuid" 2>/dev/null || true)"\n'
            '  [[ -n "$uuid" ]] || exit 1\n'
            "  printf '<plist><dict><key>VolumeUUID</key><string>%s</string></dict></plist>\\n' \"$uuid\"\n"
            "  exit 0\n"
            "fi\n"
            f"printf '%s\\n' \"$@\" >> {self.eject_capture!s}\n"
            "exit ${PHOTO_OFFLOADER_TEST_EJECT_STATUS:-0}\n"
        )
        self.diskutil.chmod(0o755)

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
        self.script = REPO / "scripts" / "trigger.sh"

    def worker_pids(self):
        """PIDs of any stub worker/supervisor still running out of this test's temp dir."""
        result = subprocess.run(["pgrep", "-f", str(self.root)], capture_output=True, text=True)
        return [int(pid) for pid in result.stdout.split()] if result.returncode == 0 else []

    def wait_for_workers(self, timeout=10.0):
        # The launchctl stub backgrounds the supervisor, so a test can finish while
        # it is still writing status/log files. Deleting the temp dir underneath it
        # fails intermittently with "Directory not empty" (seen on slower CI runners).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.worker_pids():
            time.sleep(0.05)
        for pid in self.worker_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        return not self.worker_pids()

    def tearDown(self):
        self.wait_for_workers()
        for _attempt in range(5):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)

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
                "PHOTO_OFFLOADER_LAUNCHCTL": str(self.launchctl),
                "PHOTO_OFFLOADER_IMPORT_LOG": str(self.root / ".camera_transfer.log"),
                "PHOTO_OFFLOADER_DISKUTIL": str(self.diskutil),
                "PHOTO_OFFLOADER_EJECT_STATE": str(self.install / ".eject-state"),
                "PHOTO_OFFLOADER_IMPORT_STATUS": str(self.install / ".import-status"),
                "PHOTO_OFFLOADER_OSASCRIPT": str(self.osascript),
                "PHOTO_OFFLOADER_VOLUMES_ROOT": str(self.volumes),
                "PHOTO_OFFLOADER_TERMINAL_OUTPUT_FILE": str(
                    self.install / ".terminal-output-enabled"
                ),
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
        self.assertIn("Requested progress dashboard", self.log.read_text())

    def test_default_import_runs_without_opening_terminal(self):
        (self.install / ".terminal-output-enabled").unlink()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.script_capture.exists())
        self.assertIn("Terminal output disabled", self.log.read_text())
        self.assertIn("com.user.photooffloader.import", self.log.read_text())
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))
        # Terminal output changes only where output is sent, not whether import runs.
        import time

        # The stub writes its PATH last, so once that has content both files are complete.
        self.assertTrue(
            self.wait_for(
                lambda: self.background_path.exists() and self.background_path.read_text()
            ),
            "background importer was not launched",
        )
        args = self.background_args.read_text()
        self.assertIn("--source", args)
        self.assertIn(str(self.dcim), args)
        self.assertIn(
            "--no-eject", args, "auto-eject disabled must be enforced inside the importer"
        )
        self.assertIn("/opt/homebrew/bin", self.background_path.read_text())
        self.assertIn("Submitted importer supervisor to launchd", self.log.read_text())
        import_log = self.root / ".camera_transfer.log"
        deadline = time.monotonic() + 2
        while (
            not import_log.exists() or "Background supervisor started" not in import_log.read_text()
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(import_log.exists(), "background importer log was not created")
        self.assertIn(
            "Background supervisor started",
            import_log.read_text(),
            "supervisor must leave a diagnostic before starting the importer",
        )
        self.assertIn(
            "Background supervisor started",
            self.log.read_text(),
            "supervisor start must also be recorded in the trigger log",
        )
        deadline = time.monotonic() + 2
        status_file = self.install / ".import-status"
        while (
            not status_file.exists() or not status_file.read_text().startswith("success|")
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(status_file.exists(), "supervisor must persist import completion status")
        self.assertTrue(status_file.read_text().startswith("success|"), status_file.read_text())

    def test_stale_background_state_without_success_is_suppressed_until_unmount(self):
        (self.install / ".terminal-output-enabled").unlink()
        self.state.write_text(str(self.dcim) + "\n")
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.state.exists(),
            "an already-handled mount must not be reset without completion status",
        )
        self.assertFalse(
            self.background_args.exists(),
            "missing completion status must not trigger a relaunch loop",
        )
        self.assertIn("suppressing relaunch until unmount", self.log.read_text())

    def test_registered_worker_does_not_clear_handled_state_or_relaunch(self):
        self.state.write_text(str(self.dcim) + "\n")
        result = self.run_trigger(PHOTO_OFFLOADER_TEST_WORKER_REGISTERED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.state.exists(), "registered worker must not cause handled-state to be deleted"
        )
        self.assertFalse(
            self.command_capture.exists(), "registered worker must suppress a duplicate launch"
        )
        self.assertIn("preserving handled state", self.log.read_text())

    def test_failed_background_import_is_not_retried_until_unmount(self):
        (self.install / ".terminal-output-enabled").unlink()
        self.state.write_text(str(self.dcim) + "\n")
        (self.install / ".import-status").write_text(f"failed|{self.dcim}|1\n")

        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            self.background_args.exists(), "a recorded failure must suppress immediate relaunch"
        )
        self.assertTrue(self.state.exists(), "failed mount state must remain until unmount")
        self.assertIn(
            "suppressing automatic retry until the card is unmounted", self.log.read_text()
        )

    def test_failed_mount_can_retry_after_unmount_and_remount(self):
        (self.install / ".terminal-output-enabled").unlink()
        self.state.write_text(str(self.dcim) + "\n")
        (self.install / ".import-status").write_text(f"failed|{self.dcim}|1\n")
        self.dcim.rmdir()
        self.card.rmdir()

        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists(), "unmount should clear handled state")

        self.dcim.mkdir(parents=True)
        (self.install / ".import-status").unlink()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        import time

        deadline = time.monotonic() + 2
        while not self.background_args.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(
            self.background_args.exists(), "remounted card should be eligible for a fresh import"
        )

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

    def test_successful_import_state_survives_transient_missing_dcim(self):
        # A completed card must not be re-imported if launchd observes a brief
        # /Volumes visibility gap while the card remains part of this session.
        self.state.write_text(str(self.dcim) + "\n")
        (self.install / ".import-status").write_text(f"success|{self.dcim}|123\n")
        self.dcim.rmdir()
        self.card.rmdir()

        result = self.run_trigger()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.state.exists(), "successful handled-state must survive transient DCIM absence"
        )
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))
        self.assertIn(
            "preserving successful handled-state through mount visibility gap", self.log.read_text()
        )

    # ------------------------------------------------------------------ helpers
    def wait_for(self, predicate, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def set_card_uuid(self, uuid):
        (self.card / ".test-volume-uuid").write_text(uuid + "\n")

    def import_status_text(self):
        status = self.install / ".import-status"
        return status.read_text() if status.exists() else ""

    def insert_card_in_background_mode(self, uuid):
        """Insert a card and let the stub worker finish with a success record."""
        self.dcim.mkdir(parents=True, exist_ok=True)
        self.set_card_uuid(uuid)
        self.background_args.unlink(missing_ok=True)
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertTrue(self.wait_for(self.background_args.exists), "import was not launched")
        self.assertTrue(self.wait_for(lambda: self.import_status_text().startswith("success|")))

    # ------------------------------------------- card identity (review finding 1)
    def test_second_card_with_same_volume_name_is_imported(self):
        # Cards from the same camera body share a volume name ("NIKON Z 8",
        # "EOS_DIGITAL"). A different physical card must never be mistaken for
        # the card that was already imported.
        (self.install / ".terminal-output-enabled").unlink()
        self.insert_card_in_background_mode("AAAA-AAAA")

        shutil.rmtree(self.card)  # card A removed
        self.assertEqual(self.run_trigger().returncode, 0)

        self.background_args.unlink(missing_ok=True)
        self.dcim.mkdir(parents=True)  # card B, same volume name
        self.set_card_uuid("BBBB-BBBB")
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertTrue(
            self.wait_for(self.background_args.exists),
            "a different card with the same volume name was never imported",
        )
        self.assertTrue(self.wait_for_workers(), "card B's import worker did not finish")
        self.assertIn("different card", self.log.read_text().lower())

    def test_same_card_after_visibility_gap_is_not_imported_again(self):
        # Preserves the existing mount-churn protection: the *same* card
        # (same volume UUID) reappearing must not trigger a second import.
        (self.install / ".terminal-output-enabled").unlink()
        self.insert_card_in_background_mode("AAAA-AAAA")

        uuid_file = self.card / ".test-volume-uuid"
        shutil.rmtree(self.card)
        self.assertEqual(self.run_trigger().returncode, 0)

        self.background_args.unlink(missing_ok=True)
        self.dcim.mkdir(parents=True)
        uuid_file.write_text("AAAA-AAAA\n")
        self.assertEqual(self.run_trigger().returncode, 0)
        time.sleep(0.5)
        self.assertFalse(self.background_args.exists(), "same card must not be re-imported")

    def remove_card_and_backdate_absence(self, seconds_ago):
        """Unmount the card, let the trigger notice, then pretend that was `seconds_ago`."""
        uuid = (self.card / ".test-volume-uuid").read_text()
        shutil.rmtree(self.card)
        self.assertEqual(self.run_trigger().returncode, 0)
        absent = Path(str(self.state) + ".absent")
        self.assertTrue(absent.exists(), "trigger did not record when the card went away")
        absent.write_text(f"{int(time.time()) - seconds_ago}\n")
        self.background_args.unlink(missing_ok=True)
        self.dcim.mkdir(parents=True)
        (self.card / ".test-volume-uuid").write_text(uuid)

    def test_same_card_reinserted_after_real_removal_is_imported_again(self):
        # User report 2026-11-04: eject, shoot more, re-insert -> "waiting to
        # start import" forever. A removal longer than the mount-churn grace
        # period starts a new session; the importer only copies new files.
        (self.install / ".terminal-output-enabled").unlink()
        self.insert_card_in_background_mode("AAAA-AAAA")
        self.remove_card_and_backdate_absence(seconds_ago=120)

        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertTrue(
            self.wait_for(self.background_args.exists),
            "re-inserted card with new photos was never imported",
        )
        self.assertTrue(self.wait_for_workers())
        self.assertIn("re-inserted", self.log.read_text())

    def test_brief_absence_of_same_card_is_still_treated_as_mount_churn(self):
        (self.install / ".terminal-output-enabled").unlink()
        self.insert_card_in_background_mode("AAAA-AAAA")
        self.remove_card_and_backdate_absence(seconds_ago=2)

        self.assertEqual(self.run_trigger().returncode, 0)
        time.sleep(0.5)
        self.assertFalse(self.background_args.exists(), "a brief mount gap must not re-import")
        self.assertFalse(
            Path(str(self.state) + ".absent").exists(),
            "absence marker must be cleared once the card is seen again",
        )

    def test_remount_grace_period_is_configurable(self):
        (self.install / ".terminal-output-enabled").unlink()
        self.insert_card_in_background_mode("AAAA-AAAA")
        self.remove_card_and_backdate_absence(seconds_ago=120)

        self.assertEqual(
            self.run_trigger(PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS="600").returncode, 0
        )
        time.sleep(0.5)
        self.assertFalse(self.background_args.exists())

    # ---------------------------------------------------- log growth (user report)
    def test_trigger_log_is_size_capped(self):
        self.log.write_text("x" * 5000 + "\n")
        result = self.run_trigger(PHOTO_OFFLOADER_LOG_MAX_BYTES="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        rotated = Path(str(self.log) + ".1")
        self.assertTrue(rotated.exists(), "oversized trigger log was not rotated")
        self.assertLess(self.log.stat().st_size, 1000)

    def test_trigger_log_keeps_a_bounded_number_of_generations(self):
        for _ in range(6):
            self.log.write_text("y" * 3000 + "\n")
            self.run_trigger(PHOTO_OFFLOADER_LOG_MAX_BYTES="1000")
        generations = sorted(p.name for p in self.root.glob("trigger.log*"))
        self.assertLessEqual(len(generations), 3, generations)

    def test_idle_polling_does_not_grow_the_log(self):
        # StartInterval=30 means this runs ~2,880 times a day with no card.
        shutil.rmtree(self.card)
        self.run_trigger()
        size = self.log.stat().st_size if self.log.exists() else 0
        for _ in range(3):
            self.assertEqual(self.run_trigger().returncode, 0)
        after = self.log.stat().st_size if self.log.exists() else 0
        self.assertEqual(after, size, "idle polls must not append to the trigger log")

    def test_photos_volume_alone_is_ignored(self):
        self.dcim.rmdir()
        self.card.rmdir()
        result = self.run_trigger()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        # Nothing was launched, and an idle poll stays silent (see the log-growth tests).
        self.assertFalse(self.command_capture.exists())
        self.assertFalse(self.background_args.exists())
        log_text = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("Recognized camera source", log_text)

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

    def test_auto_eject_is_disabled_by_default_for_already_handled_card(self):
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertFalse(self.eject_capture.exists())
        self.assertIn("auto-eject is disabled", self.log.read_text())
        self.assertIn("leaving card mounted for Finder access", self.log.read_text())

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

    # ---- a lock left behind must not stop imports for good
    def background_mode(self):
        (self.install / ".terminal-output-enabled").unlink(missing_ok=True)

    def leave_lock(self, pid=None, age_seconds=0):
        self.lock.mkdir()
        if pid is not None:
            (self.lock / "pid").write_text(f"{pid}\n")
        stamp = time.time() - age_seconds
        os.utime(self.lock, (stamp, stamp))

    def dead_pid(self):
        process = subprocess.Popen(["/usr/bin/true"])
        process.wait()
        return process.pid

    def live_pid(self):
        process = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process.pid

    def test_a_lock_left_by_a_trigger_that_died_is_replaced(self):
        # A power cut or SIGKILL during a run left the lock, and every later run gave up.
        self.background_mode()
        self.leave_lock(pid=self.dead_pid())
        self.assertEqual(self.run_trigger().returncode, 0)
        self.wait_for_workers()
        self.assertTrue(self.background_args.exists())
        self.assertIn("stale trigger lock", self.log.read_text())
        self.assertFalse(self.lock.exists())

    def test_a_lock_held_by_a_running_trigger_is_respected(self):
        self.background_mode()
        pid = self.live_pid()
        self.leave_lock(pid=pid)
        self.assertEqual(self.run_trigger().returncode, 0)
        self.assertFalse(self.background_args.exists())
        self.assertEqual((self.lock / "pid").read_text().strip(), str(pid))

    def test_a_lock_far_too_old_to_be_real_is_replaced_even_if_its_pid_is_alive(self):
        # After a restart, the recorded number can belong to an unrelated process.
        self.background_mode()
        self.leave_lock(pid=self.live_pid(), age_seconds=3600)
        self.run_trigger()
        self.wait_for_workers()
        self.assertTrue(self.background_args.exists())

    def test_an_old_lock_from_before_it_recorded_a_pid_is_replaced(self):
        self.background_mode()
        self.leave_lock(age_seconds=3600)
        self.run_trigger()
        self.wait_for_workers()
        self.assertTrue(self.background_args.exists())

    def test_a_new_lock_without_a_pid_yet_is_respected(self):
        self.background_mode()
        self.leave_lock()
        self.run_trigger()
        self.assertFalse(self.background_args.exists())
        self.assertTrue(self.lock.is_dir())

    def test_a_running_trigger_records_its_pid_and_removes_the_lock_at_the_end(self):
        self.background_mode()
        seen = self.root / "pid-while-running"
        self.pgrep.write_text(f'#!/bin/bash\ncat "{self.lock}/pid" > "{seen}"\nexit 1\n')
        self.run_trigger()
        self.wait_for_workers()
        self.assertRegex(seen.read_text(), r"^\d+$")
        self.assertFalse(self.lock.exists())

    # ---- a broken install must say so
    def test_a_missing_python_warns_once_instead_of_only_logging(self):
        # A Homebrew cleanup can remove the Python the install was built from. The menu bar uses
        # the same Python, so nothing else can tell anyone that cards have stopped importing.
        missing = str(self.root / "gone" / "python3")
        self.assertEqual(self.run_trigger(PHOTO_OFFLOADER_PYTHON=missing).returncode, 0)
        self.assertIn("install.sh", self.command_capture.read_text())
        self.command_capture.unlink()
        self.run_trigger(PHOTO_OFFLOADER_PYTHON=missing)
        self.assertFalse(self.command_capture.exists())

    def test_a_repaired_install_warns_again_if_it_breaks_again(self):
        missing = str(self.root / "gone" / "python3")
        self.run_trigger(PHOTO_OFFLOADER_PYTHON=missing)
        self.command_capture.unlink()
        self.background_mode()
        self.run_trigger()
        self.wait_for_workers()
        self.run_trigger(PHOTO_OFFLOADER_PYTHON=missing)
        self.assertTrue(self.command_capture.exists())

    def test_a_working_install_does_not_warn(self):
        self.background_mode()
        self.run_trigger()
        self.wait_for_workers()
        self.assertFalse(self.command_capture.exists())

    # ---- two cards mounted together
    def imported_card(self):
        """The card the last run started importing, or None. Clears the record for the next run."""
        self.wait_for_workers()
        if not self.background_args.exists():
            return None
        lines = self.background_args.read_text().splitlines()
        self.background_args.unlink()
        return next(line for line in lines if line.endswith("/DCIM"))

    def second_card(self, name="NIKON Z 8 1"):
        dcim = self.volumes / name / "DCIM"
        dcim.mkdir(parents=True)
        return dcim

    def test_a_second_card_is_imported_while_the_first_stays_mounted(self):
        # Auto-eject is off by default, so the first card stays. The trigger only ever looked at
        # the first DCIM it found, so the second was never imported.
        self.background_mode()
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(self.dcim))
        second = self.second_card()
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(second))

    def test_two_cards_are_each_imported_once_without_taking_turns_for_ever(self):
        self.background_mode()
        second = self.second_card()
        seen = []
        for _ in range(5):
            self.run_trigger()
            seen.append(self.imported_card())
        self.assertEqual(seen, [str(self.dcim), str(second), None, None, None])

    def test_a_second_card_waits_while_the_first_is_importing(self):
        self.background_mode()
        self.run_trigger()
        self.imported_card()
        self.second_card()
        self.pgrep.write_text("#!/bin/bash\nexit 0\n")
        self.run_trigger()
        self.assertIsNone(self.imported_card())
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))

    def test_a_second_card_waits_while_the_import_worker_is_registered(self):
        self.background_mode()
        self.run_trigger()
        self.imported_card()
        self.second_card()
        self.run_trigger(PHOTO_OFFLOADER_TEST_WORKER_REGISTERED="1")
        self.assertIsNone(self.imported_card())
        self.assertEqual(self.state.read_text().strip(), str(self.dcim))

    def test_a_card_removed_and_brought_back_is_imported_again(self):
        self.background_mode()
        second = self.second_card()
        self.run_trigger()
        self.imported_card()
        self.run_trigger()
        self.imported_card()
        away = self.root / "first-card-away"
        self.card.rename(away)
        self.run_trigger()
        self.assertIsNone(self.imported_card())
        away.rename(self.card)
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(self.dcim))
        self.run_trigger()
        self.assertIsNone(self.imported_card())
        self.assertTrue(second.is_dir())

    def test_a_different_card_at_a_set_aside_cards_path_is_a_new_card(self):
        self.background_mode()
        (self.card / ".test-volume-uuid").write_text("CARD-A")
        self.second_card()
        self.run_trigger()
        self.imported_card()
        self.run_trigger()
        self.imported_card()
        (self.card / ".test-volume-uuid").write_text("CARD-B")
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(self.dcim))

    def test_a_card_back_after_a_real_removal_is_imported_even_with_a_second_card_mounted(self):
        self.background_mode()
        self.run_trigger()
        self.imported_card()
        (self.state.parent / "state.absent").write_text(str(int(time.time()) - 3600))
        second = self.second_card()
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(self.dcim))
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(second))

    def test_a_different_card_at_the_handled_path_is_imported_before_a_second_card(self):
        self.background_mode()
        (self.card / ".test-volume-uuid").write_text("CARD-A")
        self.run_trigger()
        self.imported_card()
        (self.card / ".test-volume-uuid").write_text("CARD-B")
        second = self.second_card()
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(self.dcim))
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(second))

    def test_a_card_that_failed_is_not_retried_when_a_second_card_arrives(self):
        self.background_mode()
        self.run_trigger()
        self.imported_card()
        (self.install / ".import-status").write_text(f"failed|{self.dcim}|1\n")
        second = self.second_card()
        self.run_trigger()
        self.assertEqual(self.imported_card(), str(second))
        self.run_trigger()
        self.assertIsNone(self.imported_card())


if __name__ == "__main__":
    unittest.main()
