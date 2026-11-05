"""Logging and macOS notifications shared by the importer and its helpers."""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
from pathlib import Path

LOG_MAX_BYTES = 5 * 1024 * 1024


LOG_BACKUP_COUNT = 3


logger = logging.getLogger("camera_offloader")


def rotate_log_file(path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUP_COUNT) -> bool:
    """Rotate ``path`` to ``path.1`` (shifting older copies) once it exceeds ``max_bytes``.

    Used for logs written by other programs (rclone, shell scripts) that cannot
    rotate themselves. Returns True when a rotation happened.
    """
    try:
        if max_bytes <= 0 or backups < 1 or path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        path.with_name(f"{path.name}.{backups}").unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError as exc:
        logger.debug("Could not rotate %s: %s", path, exc)
        return False
    return True


def notify(title: str, text: str) -> None:
    script = """
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, text],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning("Notification failed: %s", result.stderr.strip())
    except OSError as exc:
        logger.warning("Could not show notification: %s", exc)
