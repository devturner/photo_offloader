"""Keep the Mac from idle-sleeping while long work runs."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from photo_offloader.logs import logger

_caffeinate: Optional[subprocess.Popen] = None  # kept so it isn't reported as abandoned


def keep_awake() -> None:
    """Hold off idle sleep until this process ends, however it ends.

    ``-w`` covers a crash or SIGKILL, and leaves the program's own name out of caffeinate's command
    line, because the trigger and dashboard take a process naming the importer to be an import.
    Closing the lid still sleeps the Mac. Failing to start this must never stop the work.
    """
    global _caffeinate
    try:
        _caffeinate = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("Could not keep the Mac awake for this run: %s", exc)
