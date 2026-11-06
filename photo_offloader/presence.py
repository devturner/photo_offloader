"""Whether the service is running and a dashboard is open, for code below the dashboard."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from photo_offloader import config

SERVICE_LABEL = "com.user.photooffloader"
# The page polls every second while it's visible and every 5 s while it's hidden, so this tells them
# apart: a window that's been minimized won't show a failure to anyone.
PAGE_SHOWING_WITHIN_SECONDS = 3.0
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


def service_running() -> bool:
    """Whether launchd has the service. Unable to tell counts as yes: see ``logs.notify``."""
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode == 0


def dashboard_showing() -> bool:
    """Whether someone can see the dashboard's panel right now, as far as the page can tell."""
    install = Path(os.environ.get("PHOTO_OFFLOADER_INSTALL_DIR", str(config.INSTALL_DIR)))
    info = find_running_instance(install / "dashboard.json")
    idle = info.get("idle_seconds") if info else None
    return idle is not None and float(idle) < PAGE_SHOWING_WITHIN_SECONDS
