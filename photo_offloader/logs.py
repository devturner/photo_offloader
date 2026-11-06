"""Logging and macOS notifications shared by the importer and its helpers."""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from photo_offloader import presence

LOG_MAX_BYTES = 5 * 1024 * 1024


LOG_BACKUP_COUNT = 3


logger = logging.getLogger("camera_offloader")


_TIMINGS: Dict[str, float] = {}


@contextmanager
def timed(section: str) -> Iterator[None]:
    """Add the time spent here to ``section``. Also works as a function decorator."""
    started = time.perf_counter()
    try:
        yield
    finally:
        _TIMINGS[section] = _TIMINGS.get(section, 0.0) + time.perf_counter() - started


def reset_timings() -> None:
    _TIMINGS.clear()


def timing_summary(total_seconds: float) -> str:
    """The sections since the last reset, slowest first, then whatever they don't account for.

    Status writes during a copy are counted in both, so "other" can read lower than it is.
    """
    sections = sorted(_TIMINGS.items(), key=lambda item: item[1], reverse=True)
    other = max(total_seconds - sum(seconds for _, seconds in sections), 0.0)
    parts = [f"{name} {seconds:.1f} s" for name, seconds in [*sections, ("other", other)]]
    return f"{total_seconds:.1f} s: " + ", ".join(parts)


def rotate_log_file(
    path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUP_COUNT
) -> bool:
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


def _notification_needed(title: str) -> bool:
    """Only when nothing else would say so: the panel says it if a dashboard is showing, and a stopped
    service means someone started this by hand. If in doubt, notify: this can be a failure."""
    try:
        if not presence.service_running():
            logger.info("Not notifying, as the service is stopped: %s", title)
            return False
        if presence.dashboard_showing():
            logger.info("Not notifying, as the dashboard is showing: %s", title)
            return False
    except Exception as exc:
        logger.warning("Could not check whether to notify: %s", exc)
    return True


def notify(title: str, text: str) -> None:
    if not _notification_needed(title):
        return
    script = """
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, text],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("Notification failed: %s", result.stderr.strip())
    except OSError as exc:
        logger.warning("Could not show notification: %s", exc)
