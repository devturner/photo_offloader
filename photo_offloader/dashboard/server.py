#!/usr/bin/env python3
"""Local HTTP server behind the Photo Offloader dashboard.

Binds to 127.0.0.1 only and serves one HTML page plus a small JSON API. Because the API can
start backups and stop the service, it must not be drivable by a web page that happens to be
open in your browser. Every API request therefore needs:

* a random per-session token in the ``X-Photo-Offloader-Token`` header. A custom header forces a
  CORS preflight, which this server never grants, so other sites cannot send it;
* a ``Host`` header that names this server (defeats DNS rebinding);
* for POST, ``Content-Type: application/json`` and, when present, a same-origin ``Origin``.
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from photo_offloader.dashboard.core import DashboardCore

TOKEN_HEADER = "X-Photo-Offloader-Token"
MAX_BODY_BYTES = 8192
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8")}
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class _QuietServer(ThreadingHTTPServer):
    """A browser closing a tab mid-request is routine; don't print a traceback for it."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class DashboardServer:
    def __init__(self, core: DashboardCore, web_root: Path, *, token: Optional[str] = None,
                 host: str = "127.0.0.1", port: int = 0, idle_timeout: Optional[float] = None) -> None:
        self.core = core
        self.web_root = web_root
        self.token = token or secrets.token_urlsafe(24)
        self.idle_timeout = idle_timeout
        self.last_request = time.monotonic()
        handler = self._make_handler()
        self.httpd = _QuietServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        # The token travels in the URL fragment: it is never sent to a server or written to logs.
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.1), name="dashboard-http", daemon=True)
        self._thread.start()
        if self.idle_timeout:
            threading.Thread(target=self._watchdog, name="dashboard-idle", daemon=True).start()

    def serve_forever(self) -> None:
        if self.idle_timeout:
            threading.Thread(target=self._watchdog, name="dashboard-idle", daemon=True).start()
        self.httpd.serve_forever(poll_interval=0.1)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _watchdog(self) -> None:
        """Exit once nothing has polled for a while (the browser tab was closed)."""
        assert self.idle_timeout
        while True:
            time.sleep(min(5.0, self.idle_timeout / 2))
            if time.monotonic() - self.last_request > self.idle_timeout:
                self.httpd.shutdown()
                return

    # ------------------------------------------------------------------------- handler
    def _make_handler(self) -> type:
        return type("Handler", (_DashboardHandler,), {"dashboard": self})



# ------------------------------------------------------------------ single-instance support
def write_instance_file(path: Path, server: DashboardServer, mode: str = "browser") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "port": server.port, "token": server.token, "mode": mode}
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def find_running_instance(path: Path, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """Return the instance record if a dashboard server answers with its own token."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request("GET", "/api/ping", headers={TOKEN_HEADER: token, "Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read()
        ok = response.status == 200
        connection.close()
        info["idle_seconds"] = float(json.loads(body).get("idle_seconds", 1e9)) if ok else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return info if ok else None


class _DashboardHandler(BaseHTTPRequestHandler):
    """Routes for one dashboard server; ``dashboard`` is set per server by _make_handler."""

    dashboard: "DashboardServer"

    server_version = "PhotoOffloaderDashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # keep the console quiet
        return

    # ---- helpers
    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.dashboard.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urllib.parse.urlsplit(origin)
        return parsed.scheme == "http" and parsed.netloc.lower() in self.dashboard.allowed_hosts

    def _token_ok(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(supplied.encode("utf-8"), self.dashboard.token.encode("utf-8"))

    def _guard_api(self, touch: bool = True) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "bad host"})
            return False
        if not self._origin_ok():
            self._json(403, {"error": "bad origin"})
            return False
        if not self._token_ok():
            self._json(401, {"error": "missing or invalid token"})
            return False
        if touch:
            self.dashboard.last_request = time.monotonic()
        return True

    # ---- routes
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in STATIC_FILES:
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return
            name, content_type = STATIC_FILES[parsed.path]
            try:
                body = (self.dashboard.web_root / name).read_bytes()
            except OSError:
                self._json(500, {"error": "dashboard page is missing; re-run install.sh"})
                return
            self.dashboard.last_request = time.monotonic()
            self._send(200, body, content_type, {"Content-Security-Policy": CONTENT_SECURITY_POLICY})
            return
        if parsed.path == "/api/ping":
            # A ping asks "is a page showing the dashboard?", so it must not count as one.
            if self._guard_api(touch=False):
                self._json(200, {"ok": True, "pid": os.getpid(),
                                 "idle_seconds": round(time.monotonic() - self.dashboard.last_request, 3)})
            return
        if parsed.path == "/api/state":
            if self._guard_api():
                self._json(200, self.dashboard.core.state())
            return
        if parsed.path == "/api/history":
            if self._guard_api():
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = min(max(int(query.get("limit", ["15"])[0]), 1), 100)
                except ValueError:
                    limit = 15
                self._json(200, {"history": self.dashboard.core.history(limit=limit)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        prefix = "/api/action/"
        if not parsed.path.startswith(prefix):
            self._json(404, {"error": "not found"})
            return
        if not self._guard_api():
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._json(415, {"error": "expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": "bad body size"})
            return
        try:
            params = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(params, dict):
                raise ValueError("body must be an object")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return
        result = self.dashboard.core.act(parsed.path[len(prefix):], params)
        self._json(200 if result.get("ok") or result.get("needs_choice") else 400, result)

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_PUT  # noqa: N815
