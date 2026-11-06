"""Every file and program the dashboard reads, writes or launches."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from photo_offloader import config


@dataclass(frozen=True)
class Paths:
    install_dir: Path
    data_dir: Path
    volumes_root: Path
    usb_volume: Path
    local_archives: Tuple[Path, ...]
    usb_archives: Tuple[Path, ...]
    python: Path
    launchctl: str = "/bin/launchctl"
    pgrep: str = "/usr/bin/pgrep"
    rclone_config: Optional[Path] = None   # where rclone keeps its remotes (not ours to change)

    @classmethod
    def default(cls) -> "Paths":
        install = Path(os.environ.get("PHOTO_OFFLOADER_INSTALL_DIR", str(config.INSTALL_DIR)))
        venv_python = install / "venv" / "bin" / "python3"
        return cls(
            install_dir=install,
            data_dir=config.CATALOG_DB_PATH.parent,
            volumes_root=Path("/Volumes"),
            usb_volume=config.USB_DEST_VOLUME,
            local_archives=(config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT),
            usb_archives=(config.USB_DEST_ROOT,),
            python=venv_python if venv_python.exists() else Path(sys.executable),
            rclone_config=Path(os.environ.get("RCLONE_CONFIG", str(config.RCLONE_CONFIG_PATH))),
        )

    # files the importer / trigger / service maintain
    @property
    def status_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_STATUS_FILE", str(self.install_dir / "status.json")))

    @property
    def pause_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_PAUSE_FILE", str(self.install_dir / ".paused")))

    @property
    def terminal_output_file(self) -> Path:
        return self.install_dir / ".terminal-output-enabled"

    @property
    def auto_eject_file(self) -> Path:
        return self.install_dir / ".auto-eject-enabled"

    @property
    def handled_state_file(self) -> Path:
        return self.install_dir / ".last_camera_source"

    @property
    def google_status_file(self) -> Path:
        return self.install_dir / "google_photos_status.json"

    @property
    def import_log_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_IMPORT_LOG", str(config.LOG_FILE_PATH)))

    @property
    def trigger_log_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_TRIGGER_LOG",
                                   str(config.TRIGGER_LOG_PATH)))

    @property
    def events_log_file(self) -> Path:
        """What the dashboard showed and why, one line per change (View logs > Dashboard)."""
        return self.install_dir / "dashboard_events.log"

    @property
    def google_log_file(self) -> Path:
        return self.install_dir / "google_photos_sync.log"

    @property
    def preferences_file(self) -> Path:
        return self.data_dir / "preferences.json"

    @property
    def catalog_db(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    @property
    def dashboard_state_file(self) -> Path:
        # Next to the catalog so it survives reinstalls, like the history it refers to.
        return self.data_dir / "dashboard_state.json"

    @property
    def importer_script(self) -> Path:
        return self.install_dir / config.IMPORTER

    @property
    def backup_launcher(self) -> Path:
        return self.install_dir / config.USB_BACKUP

    @property
    def google_script(self) -> Path:
        return self.install_dir / config.GOOGLE_SYNC

    @property
    def service_script(self) -> Path:
        return self.install_dir / config.SERVICE

    @property
    def trigger_script(self) -> Path:
        return self.install_dir / config.TRIGGER
