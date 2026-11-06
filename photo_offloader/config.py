"""Every path and tunable Photo Offloader uses, in one place.

Code reads these as ``config.NAME`` at call time (never ``from config import NAME``),
so a test can redirect all of them with ``tests.support.isolate_config`` and no
module keeps a stale copy.

Locations:
    APP_ROOT       the repository checkout when run from source, the install
                   directory once installed; both share the layout below.
    INSTALL_DIR    the installed app and its runtime state (replaced on install).
    DATA_DIR       what must survive reinstalls: catalog, history, preferences.
"""

from __future__ import annotations

from pathlib import Path

HOME = Path.home()
APP_ROOT = Path(__file__).resolve().parent.parent
INSTALL_DIR = HOME / "Library" / "Application Support" / "PhotoOffloader"
DATA_DIR = HOME / "Library" / "Application Support" / "PhotoOffloaderData"

# Programs, relative to APP_ROOT / INSTALL_DIR. Shell scripts and install.sh use the same paths.
IMPORTER = "bin/photo-offloader-import"
DASHBOARD = "bin/photo-offloader-dashboard"
MENUBAR = "bin/photo-offloader-menubar"
GOOGLE_SYNC = "bin/photo-offloader-google-sync"
USB_BACKUP = "bin/photo-offloader-usb-backup"
TRIGGER = "scripts/trigger.sh"
SUPERVISOR = "scripts/import_supervisor.sh"
SERVICE = "scripts/service.sh"

# Archives.
LOCAL_DEST_ROOT = HOME / "Pictures" / "CameraImports"  # photos, video, other
LOCAL_RAW_DEST_ROOT = HOME / "Pictures" / "CameraImportsRaw"  # camera RAW
USB_DEST_VOLUME = Path("/Volumes/Photos")
USB_DEST_ROOT = USB_DEST_VOLUME / "CameraImports"

# Importer state and logs.
LOG_FILE_PATH = HOME / ".camera_transfer.log"
TRIGGER_LOG_PATH = HOME / ".camera_transfer_trigger.log"  # written by scripts/trigger.sh
SESSION_LOCK_PATH = HOME / "Library" / "Caches" / "PhotoOffloader" / "import.lock"
STATUS_FILE_PATH = INSTALL_DIR / "status.json"
CATALOG_DB_PATH = DATA_DIR / "catalog.sqlite3"
LEGACY_CATALOG_DB_PATH = INSTALL_DIR / "catalog.sqlite3"  # before the catalog moved to DATA_DIR
BUILD_INFO_PATH = APP_ROOT / "build_info.json"
VERSION_PATH = APP_ROOT / "VERSION"
GOOGLE_PHOTOS_SYNC_PATH = APP_ROOT / GOOGLE_SYNC

# Google Photos sync. Preferences live in DATA_DIR so they survive reinstalls; rclone
# keeps the credentials in its own config.
GOOGLE_STATUS_PATH = INSTALL_DIR / "google_photos_status.json"
GOOGLE_LOG_PATH = INSTALL_DIR / "google_photos_sync.log"
GOOGLE_LAUNCHER_LOG_PATH = INSTALL_DIR / "google_photos_sync_launcher.log"
GOOGLE_LOCK_PATH = INSTALL_DIR / ".google-photos-sync.lock"
PREFERENCES_PATH = DATA_DIR / "preferences.json"
RCLONE_CONFIG_PATH = HOME / ".config" / "rclone" / "rclone.conf"  # rclone's own; read only

# Import behaviour.
WAIT_SECONDS = 30  # how long to wait for a card to appear
EJECT_CARD_AFTER_SUCCESS = True  # still requires the dashboard's auto-eject setting
VERIFY_WITH_SHA256 = False  # --sha256 forces a full hash comparison
USE_FILE_BIRTH_TIME = True  # month folder fallback when a file has no EXIF date
MIN_FREE_SPACE_BUFFER_BYTES = 100 * 1024 * 1024
