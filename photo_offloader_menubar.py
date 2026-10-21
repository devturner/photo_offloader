#!/usr/bin/env python3
"""Native macOS menu-bar control for Photo Offloader."""
from __future__ import annotations

import subprocess
from pathlib import Path

try:
    import rumps
except ImportError as exc:  # pragma: no cover - dependency is installed by install.sh
    raise SystemExit("Photo Offloader menu-bar UI requires rumps. Re-run ./install.sh.") from exc

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
PAUSE_FILE = INSTALL_DIR / ".paused"
TRIGGER_SCRIPT = INSTALL_DIR / "camera_offloader_trigger.sh"
DASHBOARD_SCRIPT = INSTALL_DIR / "photo_offloader_ui.py"
PYTHON = INSTALL_DIR / "venv" / "bin" / "python3"


class PhotoOffloaderMenuBar(rumps.App):
    def __init__(self) -> None:
        super().__init__("Photo Offloader", title="📷", quit_button=None)
        self.pause_item = rumps.MenuItem(self.pause_title(), callback=self.toggle_pause)
        self.menu = [
            rumps.MenuItem("Open Dashboard", callback=self.open_dashboard),
            self.pause_item,
            None,
            rumps.MenuItem("Quit Menu Bar App", callback=self.quit_app),
        ]

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
        rumps.quit_application()


if __name__ == "__main__":
    PhotoOffloaderMenuBar().run()
