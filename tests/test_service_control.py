#!/usr/bin/env python3
"""Tests for scripts/service.sh (start / stop / status of the background service).

launchctl and pgrep are replaced with stubs: launchctl keeps a "loaded labels"
directory and records every call, pgrep reports whatever the test says is running.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "service.sh"
TRIGGER = "com.user.photooffloader"
MENUBAR = "com.user.photooffloader.menubar"
IMPORT_WORKER = "com.user.photooffloader.import"


class TestServiceControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / "install"
        self.agents = self.root / "agents"
        self.loaded = self.root / "loaded"
        self.calls = self.root / "launchctl-calls.txt"
        self.running = self.root / "running-pids.txt"   # lines: "<pid> <pattern>"
        self.killed = self.root / "killed.txt"
        self.teardown_seconds = self.root / "teardown-seconds.txt"
        for folder in (self.install, self.agents, self.loaded):
            folder.mkdir()
        for label in (TRIGGER, MENUBAR):
            (self.agents / f"{label}.plist").write_text("<plist/>")
            (self.loaded / label).touch()

        launchctl = self.root / "launchctl"
        launchctl.write_text(f"""#!/bin/bash
echo "$@" >> "{self.calls}"
label="${{@: -1}}"; label="${{label##*/}}"
case "$1" in
  print)     [[ -e "{self.loaded}/$label" ]] ;;
  bootout)
    [[ -e "{self.loaded}/$label" ]] || exit 3
    # Real launchd returns from bootout before a running job has been removed.
    if [[ -f "{self.teardown_seconds}" ]]; then
      (sleep "$(cat "{self.teardown_seconds}")"; rm -f "{self.loaded}/$label") >/dev/null 2>&1 &
    else
      rm -f "{self.loaded}/$label"
    fi ;;
  bootstrap)
    plist="${{@: -1}}"; label="$(basename "$plist" .plist)"
    # Real launchd refuses a label it still has ("Bootstrap failed: 5: Input/output error").
    if [[ -e "{self.loaded}/$label" ]]; then echo "Bootstrap failed: 5" >&2; exit 5; fi
    touch "{self.loaded}/$label" ;;
  *) exit 0 ;;
esac
""")
        launchctl.chmod(0o755)
        pgrep = self.root / "pgrep"
        pgrep.write_text(f"""#!/bin/bash
pattern="${{@: -1}}"
[[ -f "{self.running}" ]] || exit 1
grep -F -- "$pattern" "{self.running}" | cut -d' ' -f1 | grep .
""")
        pgrep.chmod(0o755)
        kill = self.root / "kill"
        kill.write_text(f'#!/bin/bash\necho "$@" >> "{self.killed}"\n')
        kill.chmod(0o755)
        self.launchctl, self.pgrep, self.kill = launchctl, pgrep, kill
        self.importer = self.install / "bin" / "photo-offloader-import"
        self.dashboard = self.install / "bin" / "photo-offloader-dashboard"

    def run_service(self, *args, **extra_env):
        env = {
            **os.environ,
            "PHOTO_OFFLOADER_INSTALL_DIR": str(self.install),
            "PHOTO_OFFLOADER_AGENTS_DIR": str(self.agents),
            "PHOTO_OFFLOADER_LAUNCHCTL": str(self.launchctl),
            "PHOTO_OFFLOADER_PGREP": str(self.pgrep),
            "PHOTO_OFFLOADER_KILL": str(self.kill),
            "PHOTO_OFFLOADER_STOP_POLL_SECONDS": "0.05",
            "PHOTO_OFFLOADER_STOP_TERM_GRACE_SECONDS": "1",
            **extra_env,
        }
        return subprocess.run(["/bin/bash", str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def pretend_running(self, pid, path):
        with self.running.open("a") as handle:
            handle.write(f"{pid} {path}\n")

    def launchctl_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    # --------------------------------------------------------------- status
    def test_status_reports_running_service(self):
        result = self.run_service("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Service:", result.stdout)
        self.assertRegex(result.stdout, r"Service:\s+running")
        self.assertRegex(result.stdout, r"Transfer:\s+none")

    def test_status_reports_stopped_service_with_nonzero_exit(self):
        (self.loaded / TRIGGER).unlink()
        result = self.run_service("status")
        self.assertEqual(result.returncode, 3)
        self.assertRegex(result.stdout, r"Service:\s+stopped")

    def test_status_reports_a_transfer_in_progress(self):
        self.pretend_running(4242, self.importer)
        self.assertRegex(self.run_service("status").stdout, r"Transfer:\s+in progress")

    # ----------------------------------------------------------------- stop
    def test_stop_unloads_the_trigger_and_the_menu_bar(self):
        result = self.run_service("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.loaded / TRIGGER).exists(), "trigger agent is still loaded")
        self.assertFalse((self.loaded / MENUBAR).exists(), "menu-bar agent is still loaded")

    def test_stop_unloads_the_trigger_before_the_menu_bar(self):
        # The menu bar may be the process that invoked us; it must go last.
        self.run_service("stop")
        order = [c.split()[-1].rsplit("/", 1)[-1] for c in self.launchctl_calls() if c.startswith("bootout")]
        self.assertEqual(order, [TRIGGER, MENUBAR])

    def test_stop_closes_open_dashboard_windows(self):
        self.pretend_running(777, self.dashboard)
        self.run_service("stop")
        self.assertIn("777", self.killed.read_text())

    def test_stop_is_idempotent_when_already_stopped(self):
        self.assertEqual(self.run_service("stop").returncode, 0)
        again = self.run_service("stop")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already stopped", again.stdout.lower())

    def test_stop_refuses_to_interrupt_a_transfer_by_default(self):
        self.pretend_running(4242, self.importer)
        result = self.run_service("stop")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--wait", result.stdout + result.stderr)
        self.assertIn("--force", result.stdout + result.stderr)
        self.assertTrue((self.loaded / TRIGGER).exists(), "nothing should be stopped")
        self.assertTrue((self.loaded / MENUBAR).exists())
        self.assertFalse(self.killed.exists())

    def test_stop_force_cancels_the_transfer_with_sigterm_and_stops_everything(self):
        self.pretend_running(4242, self.importer)
        result = self.run_service("stop", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-TERM 4242", self.killed.read_text())
        self.assertFalse((self.loaded / TRIGGER).exists())
        self.assertFalse((self.loaded / MENUBAR).exists())

    def test_stop_wait_blocks_new_imports_first_then_waits_for_the_transfer(self):
        self.pretend_running(4242, self.importer)
        # The "transfer" finishes shortly after we start waiting.
        finisher = subprocess.Popen(
            ["/bin/bash", "-c", f'sleep 0.5; : > "{self.running}"'])
        self.addCleanup(finisher.wait)
        result = self.run_service("stop", "--wait")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.loaded / TRIGGER).exists())
        self.assertFalse((self.loaded / MENUBAR).exists())
        self.assertFalse(self.killed.exists(), "--wait must never signal the importer")
        bootouts = [c for c in self.launchctl_calls() if c.startswith("bootout")]
        self.assertTrue(bootouts[0].endswith(TRIGGER), "trigger must be unloaded before waiting")

    def test_stop_wait_gives_up_after_the_timeout_and_leaves_the_menu_bar_running(self):
        self.pretend_running(4242, self.importer)
        result = self.run_service("stop", "--wait", PHOTO_OFFLOADER_STOP_WAIT_SECONDS="1")
        self.assertEqual(result.returncode, 3)
        self.assertTrue((self.loaded / MENUBAR).exists())
        self.assertFalse((self.loaded / TRIGGER).exists())

    def test_stop_disable_persists_across_logins(self):
        self.run_service("stop", "--disable")
        calls = " | ".join(self.launchctl_calls())
        self.assertIn(f"disable gui/{os.getuid()}/{TRIGGER}", calls)
        self.assertIn(f"disable gui/{os.getuid()}/{MENUBAR}", calls)

    # ---------------------------------------------------------------- start
    def test_start_loads_both_agents_and_clears_any_disable(self):
        self.run_service("stop", "--disable")
        self.calls.unlink()
        result = self.run_service("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.loaded / TRIGGER).exists())
        self.assertTrue((self.loaded / MENUBAR).exists())
        calls = " | ".join(self.launchctl_calls())
        self.assertIn(f"enable gui/{os.getuid()}/{TRIGGER}", calls)

    def test_start_without_installed_agents_explains_what_to_do(self):
        for plist in self.agents.glob("*.plist"):
            plist.unlink()
        (self.loaded / TRIGGER).unlink()
        result = self.run_service("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install.sh", result.stdout + result.stderr)

    def test_start_is_idempotent_when_already_running(self):
        result = self.run_service("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([c for c in self.launchctl_calls() if c.startswith("bootstrap")],
                         "an already-loaded agent must not be bootstrapped again")

    # ------------------------------------------------ --keep-ui (menu bar / dashboard Stop)
    def test_stop_keep_ui_leaves_the_menu_bar_and_dashboards_running(self):
        # The menu bar's Stop used to unload the menu bar itself, stranding the user with a
        # dead, greyed-out menu and no way to start the service again.
        self.pretend_running(777, self.dashboard)
        result = self.run_service("stop", "--keep-ui")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.loaded / TRIGGER).exists(), "the trigger must still be stopped")
        self.assertTrue((self.loaded / MENUBAR).exists(), "the menu bar must stay loaded")
        self.assertFalse(self.killed.exists(), "dashboards must stay open")
        self.assertIn("start", result.stdout.lower())

    def test_after_a_keep_ui_stop_status_shows_a_stopped_service_with_a_live_menu_bar(self):
        self.run_service("stop", "--keep-ui")
        status = self.run_service("status")
        self.assertEqual(status.returncode, 3)
        self.assertRegex(status.stdout, r"Service:\s+stopped")
        self.assertRegex(status.stdout, r"Menu bar:\s+running")

    def test_start_after_a_keep_ui_stop_only_needs_to_load_the_trigger(self):
        self.run_service("stop", "--keep-ui")
        self.calls.unlink()
        result = self.run_service("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.loaded / TRIGGER).exists())
        bootstraps = [c for c in self.launchctl_calls() if c.startswith("bootstrap")]
        self.assertEqual(len(bootstraps), 1)
        self.assertTrue(bootstraps[0].endswith(f"{TRIGGER}.plist"))

    def test_keep_ui_still_refuses_to_interrupt_a_transfer_by_default(self):
        self.pretend_running(4242, self.importer)
        result = self.run_service("stop", "--keep-ui")
        self.assertEqual(result.returncode, 3)
        self.assertTrue((self.loaded / TRIGGER).exists())

    def test_keep_ui_force_cancels_the_transfer_and_keeps_the_ui(self):
        self.pretend_running(4242, self.importer)
        self.pretend_running(777, self.dashboard)
        result = self.run_service("stop", "--keep-ui", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        killed = self.killed.read_text()
        self.assertIn("-TERM 4242", killed)
        self.assertNotIn("777", killed, "the dashboard must not be closed")
        self.assertTrue((self.loaded / MENUBAR).exists())

    def test_keep_ui_wait_finishes_the_transfer_then_stops_without_touching_the_ui(self):
        self.pretend_running(4242, self.importer)
        finisher = subprocess.Popen(["/bin/bash", "-c", f'sleep 0.5; : > "{self.running}"'])
        self.addCleanup(finisher.wait)
        result = self.run_service("stop", "--keep-ui", "--wait")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.loaded / TRIGGER).exists())
        self.assertTrue((self.loaded / MENUBAR).exists())

    def test_keep_ui_on_an_already_stopped_service_is_a_no_op(self):
        self.run_service("stop", "--keep-ui")
        again = self.run_service("stop", "--keep-ui")
        self.assertEqual(again.returncode, 0)
        self.assertIn("already stopped", again.stdout.lower())

    # -------------------------------------------------------------- restart
    def test_restart_waits_for_launchd_to_finish_unloading(self):
        # launchctl bootout returns while launchd is still removing a running job. An
        # upgrade lost that race by 27 ms and left both agents unloaded.
        self.teardown_seconds.write_text("0.4")
        result = self.run_service("restart")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Could not load", result.stderr)
        self.assertEqual(len([c for c in self.launchctl_calls() if c.startswith("bootstrap")]), 2)

    def test_restart_reports_an_agent_that_never_unloads(self):
        self.teardown_seconds.write_text("5")
        result = self.run_service("restart", PHOTO_OFFLOADER_UNLOAD_WAIT_SECONDS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still unloading", result.stderr)
        self.assertNotIn("started", result.stdout)

    # ---------------------------------------------------------------- usage
    def test_unknown_command_prints_usage(self):
        result = self.run_service("explode")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
