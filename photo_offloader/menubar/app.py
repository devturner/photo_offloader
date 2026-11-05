"""Native macOS menu-bar control for Photo Offloader."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

try:
    import rumps
except ImportError as exc:  # pragma: no cover - dependency is installed by install.sh
    raise SystemExit("Photo Offloader menu-bar UI requires rumps. Re-run ./install.sh.") from exc

from photo_offloader import config
from photo_offloader.menubar.model import Snapshot, menu_entries, menu_title, parse_status

INSTALL_DIR = config.INSTALL_DIR
PAUSE_FILE = INSTALL_DIR / ".paused"
TRIGGER_SCRIPT = INSTALL_DIR / config.TRIGGER
DASHBOARD_SCRIPT = INSTALL_DIR / config.DASHBOARD
IMPORTER_SCRIPT = INSTALL_DIR / config.IMPORTER
SERVICE_SCRIPT = INSTALL_DIR / config.SERVICE
BACKUP_LAUNCHER_SCRIPT = INSTALL_DIR / config.USB_BACKUP
USB_VOLUME = Path("/Volumes/Photos")
UI_LABEL = "com.user.photooffloader.menubar"
PYTHON = INSTALL_DIR / "venv" / "bin" / "python3"


class PhotoOffloaderMenuBar(rumps.App):
    """The menu bar stays alive whether or not the service runs, so it can always offer Start."""

    def __init__(self) -> None:
        super().__init__("Photo Offloader", title="📷", quit_button=None)
        self.handlers = {
            "open_dashboard": self.open_dashboard, "copy_usb": self.copy_to_usb,
            "pause": self.toggle_pause, "stop": self.stop_service,
            "start": self.start_service, "quit": self.quit_app, "view_log": self.view_log,
        }
        self._snapshot: Optional[Snapshot] = None
        self.refresh()
        # Pick up changes made elsewhere (the dashboard, the command line, a crash).
        self._timer = rumps.Timer(self.refresh, 3)
        self._timer.start()

    @staticmethod
    def read_snapshot() -> Snapshot:
        usb = USB_VOLUME.is_mount()
        try:
            result = subprocess.run(["/bin/bash", str(SERVICE_SCRIPT), "status"], check=False,
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return Snapshot(running=False, usb=usb)
        return parse_status(result.stdout, result.returncode, usb=usb)

    def refresh(self, _sender: object = None) -> None:
        snapshot = self.read_snapshot()
        if snapshot == self._snapshot:
            return  # rebuilding an open menu would close it under the user's cursor
        self._snapshot = snapshot
        self.title = menu_title(snapshot)
        self.menu.clear()
        for entry in menu_entries(snapshot):
            if entry.key is None:
                self.menu.add(rumps.separator)
            else:
                handler = self.handlers.get(entry.key) if entry.enabled else None
                self.menu.add(rumps.MenuItem(entry.title, callback=handler))

    def copy_to_usb(self, _sender: object = None) -> None:
        if not USB_VOLUME.is_mount():
            rumps.notification("Photo Offloader", "USB drive not found",
                               "Mount the Photos drive to run the backup.")
            return
        try:
            subprocess.Popen([str(PYTHON), str(BACKUP_LAUNCHER_SCRIPT)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.open_dashboard()
            rumps.notification("Photo Offloader", "USB backup started",
                               "Missing files will be copied from the Mac archive.")
        except OSError as exc:
            rumps.notification("Photo Offloader", "Could not start USB backup", str(exc))

    def open_dashboard(self, _sender: object = None) -> None:
        subprocess.Popen([str(PYTHON), str(DASHBOARD_SCRIPT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def view_log(self, _sender: object = None) -> None:
        """Open the import log in Console, which follows new lines as they are written."""
        log = config.LOG_FILE_PATH
        if not log.is_file():
            rumps.notification("Photo Offloader", "No import log yet", "It is created by the first import.")
            return
        subprocess.Popen(["/usr/bin/open", "-a", "Console", str(log)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_pause(self, _sender: object = None) -> None:
        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not resume", str(exc))
                return
            rumps.notification("Photo Offloader", "Automatic imports resumed",
                               "Checking for a camera card now.")
            # Reset trigger deduplication: a card may have been inserted while paused.
            # Evaluate the current mount state immediately instead of waiting for another event.
            try:
                (INSTALL_DIR / ".last_camera_source").unlink(missing_ok=True)
            except OSError:
                pass
            subprocess.Popen(["/bin/bash", str(TRIGGER_SCRIPT)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.refresh()
        else:
            try:
                INSTALL_DIR.mkdir(parents=True, exist_ok=True)
                PAUSE_FILE.touch()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not pause", str(exc))
                return
            rumps.notification("Photo Offloader", "Automatic imports paused",
                               "Already-running copies will continue safely.")
            self.refresh()

    @staticmethod
    def transfer_running() -> bool:
        try:
            result = subprocess.run(["/usr/bin/pgrep", "-f", str(IMPORTER_SCRIPT)],
                                    check=False, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return bool(result.stdout.strip())

    def stop_service(self, _sender: object = None) -> None:
        """Stop the background service (the trigger that watches for cards).

        The menu bar and any open dashboard stay alive so they can show "stopped" and offer
        Start. A running transfer is only interrupted if the user explicitly chooses to cancel it.
        """
        flags: list[str] = []
        if self.transfer_running():
            choice = rumps.alert(
                title="A transfer is in progress",
                message="Photo Offloader can stop once the current transfer finishes, "
                        "or cancel it now. Cancelled copies are safe to resume later.",
                ok="Stop After Transfer", cancel="Keep Running",
                other="Cancel Transfer and Stop",
            )
            if choice == 0:
                return
            flags = ["--wait"] if choice == 1 else ["--force"]
        elif rumps.alert(
            title="Stop Photo Offloader?",
            message="Cards will no longer be detected until you start it again "
                    "(or log in again).",
            ok="Stop", cancel="Cancel",
        ) != 1:
            return
        try:
            # --keep-ui: only the trigger stops. This menu bar stays alive (showing "stopped"
            # with a Start item) instead of being torn down along with the service.
            subprocess.Popen(["/bin/bash", str(SERVICE_SCRIPT), "stop", "--keep-ui", *flags],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            rumps.notification("Photo Offloader", "Could not stop the service", str(exc))
            return
        self.refresh()

    def start_service(self, _sender: object = None) -> None:
        try:
            result = subprocess.run(["/bin/bash", str(SERVICE_SCRIPT), "start"], check=False,
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            rumps.notification("Photo Offloader", "Could not start the service", str(exc))
            return
        if result.returncode != 0:
            rumps.notification("Photo Offloader", "Could not start the service",
                               (result.stderr or result.stdout).strip()[:200])
        else:
            rumps.notification("Photo Offloader", "Photo Offloader started",
                               "Camera cards will be imported again.")
        self.refresh()

    def quit_app(self, _sender: object = None) -> None:
        """Stop the dashboard and unload the KeepAlive menu-bar agent before exiting."""
        # launchd has KeepAlive=true for login startup. Simply quitting the process
        # makes launchd immediately restart it, so unload the agent first.
        try:
            subprocess.run(
                ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{UI_LABEL}"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            rumps.notification("Photo Offloader", "Could not disable menu-bar auto-restart", str(exc))
            return

        # The dashboard is a separate Tk process. Close dashboard instances started
        # from this installation, but deliberately leave any active importer/backup
        # running so quitting the UI cannot interrupt a photo transfer or catalog write.
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", str(DASHBOARD_SCRIPT)],
                check=False, capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                try:
                    pid = int(line)
                    if pid != os.getpid() and pid != os.getppid():
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    continue
        except (OSError, subprocess.TimeoutExpired):
            pass
        rumps.quit_application()


def log_build_provenance() -> None:
    build_info = INSTALL_DIR / "build_info.json"
    try:
        info = json.loads(build_info.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {"version": "unknown", "git_commit": "unknown", "git_branch": "unknown"}
    print(
        "Photo Offloader menu-bar startup: "
        f"version={info.get('version', 'unknown')} "
        f"git_commit={info.get('git_commit', 'unknown')} "
        f"git_branch={info.get('git_branch', 'unknown')} "
        f"git_tag={info.get('git_tag', 'unknown')} "
        f"git_dirty={info.get('git_dirty', 'unknown')} "
        f"build_time_utc={info.get('build_time_utc', 'unknown')}",
        flush=True,
    )


def main() -> int:
    log_build_provenance()
    PhotoOffloaderMenuBar().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
