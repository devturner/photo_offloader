#!/usr/bin/env python3
"""Native macOS menu-bar control for Photo Offloader."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

try:
    import rumps
except ImportError as exc:  # pragma: no cover - dependency is installed by install.sh
    raise SystemExit("Photo Offloader menu-bar UI requires rumps. Re-run ./install.sh.") from exc

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
PAUSE_FILE = INSTALL_DIR / ".paused"
TRIGGER_SCRIPT = INSTALL_DIR / "camera_offloader_trigger.sh"
DASHBOARD_SCRIPT = INSTALL_DIR / "photo_offloader_ui.py"
IMPORTER_SCRIPT = INSTALL_DIR / "camera_offloader_v2.py"
BACKUP_LAUNCHER_SCRIPT = INSTALL_DIR / "photo_offloader_backup_launcher.py"
USB_VOLUME = Path("/Volumes/Photos")
PYTHON = INSTALL_DIR / "venv" / "bin" / "python3"


class PhotoOffloaderMenuBar(rumps.App):
    def __init__(self) -> None:
        super().__init__("Photo Offloader", title="📷", quit_button=None)
        self.pause_item = rumps.MenuItem(self.pause_title(), callback=self.toggle_pause)
        self.backup_item = rumps.MenuItem("Copy to USB Drive", callback=self.copy_to_usb)
        self.catalog_item = rumps.MenuItem("Update File Catalog", callback=self.update_catalog)
        self.menu = [
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard),
            self.backup_item,
            self.catalog_item,
            self.pause_item,
            None,
            rumps.MenuItem("Quit Menu Bar App", callback=self.quit_app),
        ]

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

    def update_catalog(self, _sender: object = None) -> None:
        try:
            subprocess.Popen([str(PYTHON), str(IMPORTER_SCRIPT), "--update-catalog"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.open_dashboard()
            rumps.notification("Photo Offloader", "Catalog update started",
                               "Scanning the Mac archive and mounted USB backup.")
        except OSError as exc:
            rumps.notification("Photo Offloader", "Could not update catalog", str(exc))

    @staticmethod
    def pause_title() -> str:
        return "Resume Automatic Imports" if PAUSE_FILE.exists() else "Pause Automatic Imports"

    def open_dashboard(self, _sender: object = None) -> None:
        subprocess.Popen([str(PYTHON), str(DASHBOARD_SCRIPT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_pause(self, _sender: object = None) -> None:
        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not resume", str(exc))
                return
            self.pause_item.title = self.pause_title()
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
        else:
            try:
                INSTALL_DIR.mkdir(parents=True, exist_ok=True)
                PAUSE_FILE.touch()
            except OSError as exc:
                rumps.notification("Photo Offloader", "Could not pause", str(exc))
                return
            self.pause_item.title = self.pause_title()
            rumps.notification("Photo Offloader", "Automatic imports paused",
                               "Already-running copies will continue safely.")

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


if __name__ == "__main__":
    log_build_provenance()
    PhotoOffloaderMenuBar().run()
