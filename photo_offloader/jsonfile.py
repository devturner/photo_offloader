"""JSON state files shared between processes: tolerant reads and atomic writes.

The importer, the sync and the dashboard read each other's files while they are being
replaced, so a reader must never see half a file. This module imports nothing from the
project, so any other module can use it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


def read_object(path: Path) -> Dict[str, Any]:
    """The JSON object in ``path``, or ``{}`` if it is missing, corrupt or not an object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Any, **dumps_options: Any) -> None:
    """Atomically replace ``path`` with ``value`` as JSON. Raises OSError if that fails.

    The temporary file gets the default permissions, so this is not for secrets
    (``dashboard.server.write_instance_file`` keeps its token owner-only).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, **dumps_options) + "\n", encoding="utf-8")
    os.replace(temporary, path)
