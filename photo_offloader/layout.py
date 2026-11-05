"""Where Photo Offloader's programs live, relative to the app root.

The app root is the repository checkout when run from source and the install
directory once installed; both share this layout. Shell scripts and the
installer use the same relative paths.
"""
from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"

IMPORTER = "bin/photo-offloader-import"
DASHBOARD = "bin/photo-offloader-dashboard"
MENUBAR = "bin/photo-offloader-menubar"
GOOGLE_SYNC = "bin/photo-offloader-google-sync"
USB_BACKUP = "bin/photo-offloader-usb-backup"
TRIGGER = "scripts/trigger.sh"
SUPERVISOR = "scripts/import_supervisor.sh"
SERVICE = "scripts/service.sh"
