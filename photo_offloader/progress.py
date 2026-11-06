"""Publish import progress for the dashboard (status.json) and the terminal bar."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Optional

from photo_offloader import config, jsonfile
from photo_offloader.logs import logger

try:
    from alive_progress import alive_bar
    HAS_ALIVE_PROGRESS = True
except ImportError:  # pragma: no cover
    HAS_ALIVE_PROGRESS = False

    class _NullProgress:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_value, traceback): return False
        def __call__(self, *args, **kwargs): return None

    def alive_bar(*args, **kwargs):
        return _NullProgress()


_PROGRESS = {"state": "idle", "bytes_done": 0, "bytes_total": 0, "bytes_transferred": 0}


_PROGRESS_STARTED: Optional[float] = None


def update_status(**updates: object) -> None:
    """Atomically publish importer progress for the menu-bar dashboard."""
    global _PROGRESS_STARTED, _LAST_STATUS_WRITE
    if _PROGRESS_STARTED is None:
        _PROGRESS_STARTED = time.monotonic()
    _PROGRESS.update(updates)
    elapsed = max(time.monotonic() - _PROGRESS_STARTED, 0.001)
    done = int(_PROGRESS.get("bytes_done", 0))
    total = int(_PROGRESS.get("bytes_total", 0))
    speed = done / elapsed
    transferred = int(_PROGRESS.get("bytes_transferred", 0))
    transfer_speed = transferred / elapsed
    remaining = max(total - done, 0)
    status = dict(_PROGRESS)
    status.update({
        "speed_bps": speed,
        "transfer_speed_bps": transfer_speed,
        "eta_seconds": remaining / speed if speed > 0 and total > done else 0,
        "elapsed_seconds": elapsed,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        jsonfile.write_json(config.STATUS_FILE_PATH, status, ensure_ascii=False)
        _LAST_STATUS_WRITE = time.monotonic()
    except OSError as exc:
        logger.debug("Could not publish dashboard status: %s", exc)


def add_progress_bytes(amount: int, transferred: bool = False) -> None:
    _PROGRESS["bytes_done"] = int(_PROGRESS.get("bytes_done", 0)) + amount
    if transferred:
        _PROGRESS["bytes_transferred"] = int(_PROGRESS.get("bytes_transferred", 0)) + amount
    # Counters stay exact on every chunk; the on-disk status file (read by the
    # dashboard about once a second) is rewritten at most a few times a second.
    if time.monotonic() - _LAST_STATUS_WRITE >= STATUS_MIN_INTERVAL_SECONDS:
        update_status()


STATUS_MIN_INTERVAL_SECONDS = 0.25


_LAST_STATUS_WRITE = 0.0


def progress_bar(total: int, label: str):
    """Terminal progress bar that never decorates log output.

    alive-progress hooks print() and logging stream handlers while a bar is open
    and prefixes each line with the bar position ("on 12: ..."); that ends up in
    the log files. The bar is only drawn when stdout is a terminal.
    """
    return alive_bar(total, title=label, bar="filling", monitor=True, force_tty=False,
                     enrich_print=False, disable=not sys.stdout.isatty())
