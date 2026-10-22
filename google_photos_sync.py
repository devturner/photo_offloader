#!/usr/bin/env python3
"""One-way, filtered uploads from Photo Offloader to Google Photos via rclone."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

import camera_offloader_v2 as catalog

LOCAL_PHOTOS = Path.home() / "Pictures" / "CameraImports"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
STATUS_PATH = SUPPORT_DIR / "google_photos_status.json"
LOG_PATH = SUPPORT_DIR / "google_photos_sync.log"
LOCK_PATH = SUPPORT_DIR / ".google-photos-sync.lock"
REMOTE_NAME = os.environ.get("PHOTO_OFFLOADER_GPHOTOS_REMOTE", "gphotos").rstrip(":")
ALBUM_NAME = os.environ.get("PHOTO_OFFLOADER_GPHOTOS_ALBUM", "Photo Offloader")
INCLUDE_EXTENSIONS = (
    "*.jpg",
    "*.jpeg",
    "*.mov",
    "*.mp4",
    "*.avi",
    "*.mts",
    "*.m2ts",
    "*.m4v",
    "*.3gp",
    "*.3g2",
    "*.asf",
    "*.divx",
    "*.mkv",
    "*.mpg",
    "*.wmv",
)


def parse_rclone_stats_line(line: str) -> Optional[dict[str, object]]:
    """Parse one rclone one-line stats record from the upload log."""
    match = re.search(
        r"(?:Transferred:\s*)?([\d.]+\s+\w+)\s*/\s*([\d.]+\s+\w+),\s*"
        r"([\d.]+)%,\s*([\d.]+\s+\w+/s),\s*ETA\s+(.+?)"
        r"(?:\s+\(xfr#|$)",
        line,
    )
    if not match:
        return None
    transferred, total, percent, speed, eta = match.groups()
    return {
        "transferred": transferred,
        "total": total,
        "percent": float(percent),
        "speed": speed,
        "eta": eta.strip(),
    }


def publish_status(state: str, message: str) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}

    payload = {
        "state": state,
        "message": message,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_success": previous.get("last_success"),
    }
    if state == "complete":
        payload["last_success"] = payload["updated_at"]

    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def log_sync_event(message: str) -> None:
    """Append an application-level lifecycle message to the rclone log."""
    stamp = datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")
    try:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} INFO  : Photo Offloader: {message}\n")
    except OSError:
        # Logging must never turn an otherwise successful upload into a failure.
        pass

def find_rclone() -> Optional[str]:
    found = shutil.which("rclone")
    if found:
        return found

    for candidate in (Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_sync(argv: Optional[Sequence[str]] = None) -> int:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        publish_status("running", "A Google Photos upload is already running.")
        return 0

    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return _sync()
    finally:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


def _sync() -> int:
    rclone = find_rclone()
    if not rclone:
        publish_status(
            "not_configured",
            "rclone is not installed. Install it and configure a Google Photos remote named 'gphotos'.",
        )
        return 2

    if not LOCAL_PHOTOS.is_dir():
        publish_status("failed", f"Local photo archive does not exist: {LOCAL_PHOTOS}")
        return 1

    try:
        remotes = subprocess.run(
            [rclone, "listremotes"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        publish_status("failed", f"Could not inspect rclone remotes: {exc}")
        return 1

    if f"{REMOTE_NAME}:" not in remotes:
        publish_status(
            "not_configured",
            f"rclone remote '{REMOTE_NAME}:' is not configured. Run: rclone config",
        )
        return 2

    destination = f"{REMOTE_NAME}:album/{ALBUM_NAME}"
    try:
        album = subprocess.run(
            [rclone, "mkdir", destination],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        publish_status("failed", f"Could not create/check Google Photos album: {exc}")
        return 1

    if album.returncode != 0:
        publish_status(
            "failed",
            f"Could not create/check Google Photos album: {album.stderr.strip()}",
        )
        return album.returncode or 1

    command = [
        rclone,
        "copy",
        str(LOCAL_PHOTOS),
        destination,
        "--ignore-case",
        "--log-level",
        "INFO",
        "--log-file",
        str(LOG_PATH),
        "--stats",
        "5s",
        "--stats-one-line",
    ]
    for ext in INCLUDE_EXTENSIONS:
        command.extend(["--include", ext])
    command.extend(["--exclude", "*"])

    publish_status(
        "running",
        f"Uploading JPGs and supported videos to Google Photos album '{ALBUM_NAME}'.",
    )
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        publish_status("failed", f"Could not start rclone: {exc}")
        return 1

    if result.returncode == 0:
        try:
            eligible_extensions = {ext[1:] for ext in INCLUDE_EXTENSIONS}
            for path in LOCAL_PHOTOS.rglob("*"):
                if path.is_file() and path.suffix.lower() in eligible_extensions:
                    catalog.mark_google_photos_uploaded(path, REMOTE_NAME, ALBUM_NAME)
        except (OSError, sqlite3.Error) as exc:
            publish_status(
                "failed",
                f"Upload completed, but per-file catalog status could not be saved: {exc}",
            )
            return 1

        log_sync_event(
            f"Google Photos sync completed successfully for album '{ALBUM_NAME}'; "
            "rclone exited successfully and per-file catalog status was saved."
        )
        publish_status(
            "complete",
            f"Google Photos upload finished for JPGs and supported videos in '{ALBUM_NAME}'; "
            "per-file status saved in the catalog.",
        )
        return 0

    publish_status("failed", f"rclone exited with status {result.returncode}. See {LOG_PATH}")
    return result.returncode or 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--sync"]):
        print("Usage: google_photos_sync.py [--sync]", file=sys.stderr)
        return 2
    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
