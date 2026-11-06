"""Whether the service is running and a dashboard is open, for code below the dashboard."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any, Dict, Optional

TOKEN_HEADER = "X-Photo-Offloader-Token"


def find_running_instance(path: Path, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """Return the instance record if a dashboard server answers with its own token."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request(
            "GET", "/api/ping", headers={TOKEN_HEADER: token, "Host": f"127.0.0.1:{port}"}
        )
        response = connection.getresponse()
        body = response.read()
        ok = response.status == 200
        connection.close()
        info["idle_seconds"] = float(json.loads(body).get("idle_seconds", 1e9)) if ok else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return info if ok else None
