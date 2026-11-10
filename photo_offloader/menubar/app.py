"""Native macOS menu-bar control for Photo Offloader."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import List, Optional

try:
    import rumps
except ImportError as exc:  # pragma: no cover - dependency is installed by install.sh
    raise SystemExit("Photo Offloader menu-bar UI requires rumps. Re-run ./install.sh.") from exc

from photo_offloader import config, jsonfile
from photo_offloader.dashboard.core import DashboardCore
from photo_offloader.logs import logger
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
ICON = Path(__file__).with_name("icon.png")  # drawn by macos/draw_icon.py


class PhotoOffloaderMenuBar(rumps.App):
    """The menu bar stays alive whether or not the service runs, so it can always offer Start."""

    def __init__(self) -> None:
        icon = str(ICON) if ICON.is_file() else None  # without it, the title shows a 📷
        super().__init__(
            "Photo Offloader", title=None if icon else "📷", icon=icon, quit_button=None
        )
        self.handlers = {
            "open_dashboard": self.open_dashboard,
            "copy_usb": self.copy_to_usb,
            "pause": self.toggle_pause,
            "upload": self.toggle_uploads,
            "close_dashboard": self.close_dashboard,
            "stop": self.stop_service,
            "stop_exit": self.stop_and_exit,
            "start": self.start_service,
            "view_log": self.view_log,
        }
        self._snapshot: Optional[Snapshot] = None
        self.refresh()
        # Pick up changes made elsewhere (the dashboard, the command line, a crash).
        self._timer = rumps.Timer(self.refresh, 3)
        self._timer.start()

    @staticmethod
    def read_snapshot() -> Snapshot:
        usb = USB_VOLUME.is_mount()
        dashboard = bool(dashboard_pids())
        try:
            result = subprocess.run(
                ["/bin/bash", str(SERVICE_SCRIPT), "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return Snapshot(running=False, usb=usb, dashboard=dashboard)
        snapshot = parse_status(result.stdout, result.returncode, usb=usb)
        return dataclasses.replace(snapshot, dashboard=dashboard, upload=upload_state())

    def refresh(self, _sender: object = None) -> None:
        snapshot = self.read_snapshot()
        if snapshot == self._snapshot:
            return  # rebuilding an open menu would close it under the user's cursor
        self._snapshot = snapshot
        self.title = menu_title(snapshot, icon=self.icon is not None)
        self.menu.clear()
        for entry in menu_entries(snapshot):
            if entry.key is None:
                self.menu.add(rumps.separator)
            else:
                handler = self.handlers.get(entry.key) if entry.enabled else None
                self.menu.add(rumps.MenuItem(entry.title, callback=handler))

    def copy_to_usb(self, _sender: object = None) -> None:
        if not USB_VOLUME.is_mount():
            rumps.notification(
                "Photo Offloader",
                "USB drive not found",
                "Mount the Photos drive to run the backup.",
            )
            return
        try:
            subprocess.Popen(
                [str(PYTHON), str(BACKUP_LAUNCHER_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.open_dashboard()
            rumps.notification(
                "Photo Offloader",
                "USB backup started",
                "Missing files will be copied from the Mac archive.",
            )
        except OSError as exc:
            rumps.notification("Photo Offloader", "Could not start USB backup", str(exc))

    def open_dashboard(self, _sender: object = None) -> None:
        # In Photo Offloader.app the window carries the app's name and icon, not Python's.
        app = config.APP_BUNDLE / config.APP_PROGRAM
        command = [str(app), "--dashboard"] if os.access(app, os.X_OK) else None
        subprocess.Popen(
            command or [str(PYTHON), str(DASHBOARD_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def close_dashboard(self, _sender: object = None) -> None:
        """Close every dashboard of this installation. Transfers carry on; they run elsewhere."""
        for pid in dashboard_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue  # already gone, or not ours to stop
        self.refresh()

    def view_log(self, _sender: object = None) -> None:
        """Open the import log in Console, which follows new lines as they are written."""
        log = config.LOG_FILE_PATH
        if not log.is_file():
            rumps.notification(
                "Photo Offloader", "No import log yet", "It is created by the first import."
            )
            return
        subprocess.Popen(
            ["/usr/bin/open", "-a", "Console", str(log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def toggle_pause(self, _sender: object = None) -> None:
        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not resume", str(exc))
                return
            rumps.notification(
                "Photo Offloader", "Automatic imports resumed", "Checking for a camera card now."
            )
            # Reset trigger deduplication: a card may have been inserted while paused.
            # Evaluate the current mount state immediately instead of waiting for another event.
            try:
                (INSTALL_DIR / ".last_camera_source").unlink(missing_ok=True)
                (INSTALL_DIR / ".last_camera_source.parked").unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not reset the last-card record; a mounted card "
                    "may not import until it is re-inserted: %s",
                    exc,
                )
            subprocess.Popen(
                ["/bin/bash", str(TRIGGER_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.refresh()
        else:
            try:
                INSTALL_DIR.mkdir(parents=True, exist_ok=True)
                PAUSE_FILE.touch()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not pause", str(exc))
                return
            rumps.notification(
                "Photo Offloader",
                "Automatic imports paused",
                "Already-running copies will continue safely.",
            )
            self.refresh()

    def toggle_uploads(self, _sender: object = None) -> None:
        """Pause or resume Google Photos uploads, the same way the dashboard does."""
        action = "google_resume" if upload_state() == "paused" else "google_pause"
        result = DashboardCore().act(action)
        rumps.notification("Photo Offloader", "", str(result.get("message", "")))
        self.refresh()

    @staticmethod
    def transfer_running() -> bool:
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", str(IMPORTER_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return bool(result.stdout.strip())

    def ask_how_to_stop(self, exiting: bool) -> Optional[List[str]]:
        """The service.sh stop flags the user chose, or None if they cancelled.

        A running transfer is only interrupted if the user explicitly chooses to cancel it.
        """
        if self.transfer_running():
            choice = rumps.alert(
                title="A transfer is in progress",
                message="Photo Offloader can stop once the current transfer finishes, "
                "or cancel it now. Cancelled copies are safe to resume later.",
                ok="Stop After Transfer",
                cancel="Keep Running",
                other="Cancel Transfer and Stop",
            )
            if choice == 0:
                return None
            return ["--wait"] if choice == 1 else ["--force"]
        if exiting and upload_state() == "running":
            choice = rumps.alert(
                title="Stop and exit Photo Offloader?",
                message="A Google Photos upload is running. Cards won't be imported, and the "
                "menu bar and dashboard close, until you open Photo Offloader again.",
                ok="Exit, Keep Uploading",
                cancel="Cancel",
                other="Pause Upload and Exit",
            )
            if choice == 0:
                return None
            if choice == -1:  # "other"
                DashboardCore().act("google_pause")
            return []
        if exiting:
            title, message, ok = (
                "Stop and exit Photo Offloader?",
                "Cards won't be imported, and the menu bar and dashboard close, until you "
                "open Photo Offloader again or log in again.",
                "Stop and Exit",
            )
        else:
            title, message, ok = (
                "Stop the service?",
                "Cards won't be imported until you start it again (or log in again).",
                "Stop",
            )
        if rumps.alert(title=title, message=message, ok=ok, cancel="Cancel") != 1:
            return None
        return []

    def run_stop(self, args: List[str]) -> bool:
        try:
            # A new session: stopping everything also ends this menu bar, and the stop
            # has to carry on after it.
            subprocess.Popen(
                ["/bin/bash", str(SERVICE_SCRIPT), "stop", *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            rumps.notification("Photo Offloader", "Could not stop the service", str(exc))
            return False
        return True

    def stop_service(self, _sender: object = None) -> None:
        """Stop only the card watcher. This menu bar stays to show "stopped" and offer Start."""
        flags = self.ask_how_to_stop(exiting=False)
        if flags is not None and self.run_stop(["--keep-ui", *flags]):
            self.refresh()

    def stop_and_exit(self, _sender: object = None) -> None:
        """Stop the card watcher, close every dashboard and end this menu bar."""
        flags = self.ask_how_to_stop(exiting=True)
        if flags is None or not self.run_stop(flags):
            return
        # service.sh ends this menu bar by unloading its LaunchAgent (KeepAlive would restart
        # a plain exit). Started some other way, there's no agent to unload, so exit here.
        if not menubar_agent_loaded() and "--wait" not in flags:
            rumps.quit_application()

    def start_service(self, _sender: object = None) -> None:
        try:
            result = subprocess.run(
                ["/bin/bash", str(SERVICE_SCRIPT), "start"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            rumps.notification("Photo Offloader", "Could not start the service", str(exc))
            return
        if result.returncode != 0:
            rumps.notification(
                "Photo Offloader",
                "Could not start the service",
                (result.stderr or result.stdout).strip()[:200],
            )
        else:
            rumps.notification(
                "Photo Offloader", "Photo Offloader started", "Camera cards will be imported again."
            )
        self.refresh()


def upload_state() -> str:
    """The Google Photos upload as the menu shows it: "running", "paused" or nothing."""
    if config.GOOGLE_PAUSED_PATH.exists():
        return "paused"
    if jsonfile.read_object(config.GOOGLE_STATUS_PATH).get("state") != "running":
        return ""
    return "running" if DashboardCore().google_running() else ""


def dashboard_pids() -> List[int]:
    """Processes showing this installation's dashboard, as a script or in Photo Offloader.app."""
    own, pids = {os.getpid(), os.getppid()}, []
    for program in (DASHBOARD_SCRIPT, config.APP_BUNDLE / config.APP_PROGRAM):
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", str(program)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Could not look for an open dashboard: %s", exc)
            continue
        pids += [int(n) for n in result.stdout.split() if n.isdigit() and int(n) not in own]
    return pids


def menubar_agent_loaded() -> bool:
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{UI_LABEL}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # assume launchd will end us, rather than exit and be restarted
    return result.returncode == 0


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
