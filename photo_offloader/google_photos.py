"""One-way, filtered uploads from Photo Offloader to Google Photos via rclone."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from photo_offloader import catalog, config, logs

REMOTE_NAME = os.environ.get("PHOTO_OFFLOADER_GPHOTOS_REMOTE", "gphotos").rstrip(":")
ALBUM_NAME = os.environ.get("PHOTO_OFFLOADER_GPHOTOS_ALBUM", "Photo Offloader")
SYNC_LOG_MAX_BYTES = 5 * 1024 * 1024
SYNC_LOG_BACKUPS = 2
INCLUDE_EXTENSIONS = (
    "*.jpg",
    "*.jpeg",
    "*.heic",
    "*.heif",
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
        r"(?:\s+\(xfr#(\d+)/(\d+)\))?\s*$",
        line,
    )
    if not match:
        return None
    transferred, total, percent, speed, eta, files_done, files_total = match.groups()
    return {
        "transferred": transferred,
        "total": total,
        "percent": float(percent),
        "speed": speed,
        "eta": eta.strip(),
        # rclone leaves the file count off once every byte is sent.
        "files_done": int(files_done) if files_done else None,
        "files_total": int(files_total) if files_total else None,
    }


def publish_status(state: str, message: str, new_run: bool = False) -> None:
    config.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(config.GOOGLE_STATUS_PATH.read_text(encoding="utf-8"))
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
    if state == "running":
        # One sync publishes "running" more than once; the dashboard times it from the first.
        # ``new_run`` restarts the clock, even over a "running" left behind by a crash.
        continuing = previous.get("state") == "running" and not new_run
        payload["started_at"] = (previous.get("started_at") if continuing else None) or payload["updated_at"]

    temporary = config.GOOGLE_STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config.GOOGLE_STATUS_PATH)


def remember_google_photos_connection(remote_name: str) -> None:
    """Persist non-secret connection metadata outside the replaceable install directory."""
    config.PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        preferences = json.loads(config.PREFERENCES_PATH.read_text(encoding="utf-8"))
        if not isinstance(preferences, dict):
            preferences = {}
    except (OSError, ValueError):
        preferences = {}
    preferences["google_photos"] = {
        "configured": True,
        "remote": f"{remote_name}:",
        "last_connection_check": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = config.PREFERENCES_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(preferences, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config.PREFERENCES_PATH)


def log_sync_event(message: str) -> None:
    """Append an application-level lifecycle message to the rclone log."""
    stamp = datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")
    try:
        config.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        with config.GOOGLE_LOG_PATH.open("a", encoding="utf-8") as handle:
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


def current_status_state() -> str:
    try:
        return str(json.loads(config.GOOGLE_STATUS_PATH.read_text(encoding="utf-8")).get("state", ""))
    except (OSError, ValueError):
        return ""


def run_sync(argv: Optional[Sequence[str]] = None) -> int:
    config.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    # An flock is released by the OS the moment its holder dies, so a crash,
    # SIGKILL or power loss can never leave a stale lock behind. The lock file
    # itself is never deleted (deleting it would reopen a lock/unlink race).
    lock_fd = os.open(config.GOOGLE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Keep the live run's real progress message if it already published one.
            if current_status_state() != "running":
                publish_status("running", "A Google Photos upload is already running.")
            return 0
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode("ascii"))  # informational only
        return _sync()
    finally:
        os.close(lock_fd)


def list_upload_candidates() -> List[str]:
    """Snapshot (relative POSIX paths) of eligible files not yet recorded as uploaded."""
    eligible = {ext[1:] for ext in INCLUDE_EXTENSIONS}
    uploaded = catalog.google_photos_uploaded_fingerprints()
    pending: List[str] = []
    for path in sorted(config.LOCAL_DEST_ROOT.rglob("*"), key=lambda item: str(item).lower()):
        try:
            relative = path.relative_to(config.LOCAL_DEST_ROOT)
            if (path.suffix.lower() not in eligible or not path.is_file() or path.is_symlink()
                    or any(part.startswith(".") for part in relative.parts)):
                continue
            name = relative.as_posix()
            # rclone's --files-from treats leading '#'/';' as comments and trims whitespace.
            if name != name.strip() or name[0] in "#;":
                continue
            info = path.stat()
            if uploaded.get(str(path.resolve())) != (info.st_size, info.st_mtime_ns, info.st_ino):
                pending.append(name)
        except OSError:
            continue
    return pending


def _sync() -> int:
    rclone = find_rclone()
    if not rclone:
        publish_status(
            "not_configured",
            "rclone is not installed. Install it and configure a Google Photos remote named 'gphotos'.",
        )
        return 2

    if not config.LOCAL_DEST_ROOT.is_dir():
        publish_status("failed", f"Local photo archive does not exist: {config.LOCAL_DEST_ROOT}")
        return 1

    pending = list_upload_candidates()
    if not pending:
        # Sync is launched after every import; skip rclone (and the network) when
        # there is nothing new to send.
        log_sync_event("Google Photos sync skipped: no new JPG/video files to upload.")
        publish_status("complete", f"Google Photos is up to date for album '{ALBUM_NAME}'.")
        return 0
    # Say so before the slower rclone checks, so the dashboard shows the upload straight away.
    publish_status("running", f"Preparing to upload {len(pending):,} JPG/video file(s).", new_run=True)

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

    try:
        remember_google_photos_connection(REMOTE_NAME)
    except (OSError, ValueError) as exc:
        # A preferences-file failure must not block a working upload.
        log_sync_event(f"Could not persist Google Photos connection preference: {exc}")

    logs.rotate_log_file(config.GOOGLE_LOG_PATH, SYNC_LOG_MAX_BYTES, SYNC_LOG_BACKUPS)
    # Upload exactly the snapshot taken above. Files imported while the upload is
    # running are neither sent nor recorded, so the next sync picks them up.
    list_fd, list_name = tempfile.mkstemp(prefix=".upload-list-", suffix=".txt", dir=config.INSTALL_DIR)
    try:
        with os.fdopen(list_fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(pending) + "\n")
        command = [
            rclone,
            "copy",
            str(config.LOCAL_DEST_ROOT),
            destination,
            "--files-from",
            list_name,
            "--ignore-case",
            "--log-level",
            "INFO",
            "--log-file",
            str(config.GOOGLE_LOG_PATH),
            "--stats",
            "5s",
            "--stats-one-line",
        ]
        publish_status(
            "running",
            f"Uploading {len(pending):,} JPG/video file(s) to Google Photos album '{ALBUM_NAME}'.",
        )
        try:
            result = subprocess.run(command, check=False)
        except OSError as exc:
            publish_status("failed", f"Could not start rclone: {exc}")
            return 1
    finally:
        try:
            os.unlink(list_name)
        except OSError:
            pass

    if result.returncode == 0:
        try:
            catalog.mark_google_photos_uploaded_many(
                [config.LOCAL_DEST_ROOT / name for name in pending], REMOTE_NAME, ALBUM_NAME
            )
        except (OSError, sqlite3.Error) as exc:
            publish_status(
                "failed",
                f"Upload completed, but per-file catalog status could not be saved: {exc}",
            )
            return 1

        log_sync_event(
            f"Google Photos sync completed successfully for album '{ALBUM_NAME}'; "
            f"{len(pending):,} file(s) uploaded and recorded in the catalog."
        )
        publish_status(
            "complete",
            f"Google Photos upload finished for {len(pending):,} file(s) in '{ALBUM_NAME}'; "
            "per-file status saved in the catalog.",
        )
        return 0

    publish_status("failed", f"rclone exited with status {result.returncode}. See {config.GOOGLE_LOG_PATH}")
    return result.returncode or 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--sync"]):
        print("Usage: photo-offloader-google-sync [--sync]", file=sys.stderr)
        return 2
    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
