#!/usr/bin/env python3
"""Photo Offloader dashboard launcher.

Starts (or re-opens) the local dashboard server and shows it in a native window when
``pywebview`` is installed, otherwise in the default browser. Only one dashboard runs at a
time: launching again just brings the existing one forward, so the menu bar and the
auto-open on import can both call this safely.

The page itself lives in ``dashboard_web/``; the state it shows comes from ``dashboard_core``.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from dashboard_core import DashboardCore, Paths
from dashboard_server import DashboardServer, find_running_instance, write_instance_file

BROWSER_IDLE_TIMEOUT = 180.0  # tolerant: browsers throttle polling in background tabs
PAGE_OPEN_WITHIN_SECONDS = 30.0  # a page that polled this recently is considered still open
# Fixed size: the page fills it exactly and Recent activity scrolls inside it.
WINDOW_SIZE = (960, 860)


def _import_webview() -> Any:
    try:
        import webview  # type: ignore[import-not-found]
    except Exception:  # not installed, or its Cocoa bindings are unusable
        return None
    return webview


def _activate_pid(pid: int) -> bool:
    """Best effort: bring the process that owns the native window to the front."""
    try:
        from AppKit import (  # type: ignore
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except Exception:
        return False
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return bool(app and app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    paths: Optional[Paths] = None,
    web_root: Optional[Path] = None,
    open_browser: Optional[Callable[[str], Any]] = None,
    webview_module: Any = "auto",
    idle_timeout: Optional[float] = None,
    activate: Optional[Callable[[int], bool]] = None,
) -> int:
    # --if-closed: used by the trigger at every import start. Show the dashboard only if no
    # window or tab is currently showing it (never steal focus from one that is).
    if_closed = "--if-closed" in (sys.argv[1:] if argv is None else list(argv))
    paths = paths or Paths.default()
    web_root = web_root or Path(__file__).resolve().parent / "dashboard_web"
    open_browser = open_browser or webbrowser.open
    activate = activate or _activate_pid

    if not (web_root / "index.html").is_file():
        print("The dashboard page is missing. Re-run ./install.sh.", file=sys.stderr)
        return 1

    paths.install_dir.mkdir(parents=True, exist_ok=True)
    record = paths.install_dir / "dashboard.json"

    # Serialise "is one running? / start one" so two simultaneous launches cannot both win.
    lock_fd = os.open(paths.install_dir / "dashboard.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = find_running_instance(record)
        if existing:
            if if_closed and float(existing.get("idle_seconds") or 1e9) < PAGE_OPEN_WITHIN_SECONDS:
                return 0
            url = f"http://127.0.0.1:{existing['port']}/#t={existing['token']}"
            if not (existing.get("mode") == "window" and activate(int(existing.get("pid", 0)))):
                open_browser(url)
            return 0

        webview = _import_webview() if webview_module == "auto" else webview_module
        window = webview is not None and os.environ.get("PHOTO_OFFLOADER_DASHBOARD_MODE") != "browser"
        server = DashboardServer(DashboardCore(paths), web_root,
                                 idle_timeout=None if window else (idle_timeout or BROWSER_IDLE_TIMEOUT))
        server.start()
        write_instance_file(record, server, mode="window" if window else "browser")
    finally:
        os.close(lock_fd)

    try:
        if window:
            webview.create_window("Photo Offloader", server.url, width=WINDOW_SIZE[0],
                                  height=WINDOW_SIZE[1], resizable=False)
            webview.start()  # blocks until the window is closed
        else:
            open_browser(server.url)
            # The page polls the server; once nothing has for a while, the server (and we) exit.
            server._thread.join()
    finally:
        try:
            if record.is_file() and int(json.loads(record.read_text())["pid"]) == os.getpid():
                record.unlink()
        except (OSError, ValueError, KeyError):
            pass
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
