"""One-way, filtered uploads from Photo Offloader to Google Photos via rclone."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Sequence, Tuple

from photo_offloader import catalog, config, jsonfile, logs, settings

UPLOAD_LIST_PREFIX = ".upload-list-"  # in INSTALL_DIR; the dashboard spots our rclone by it
# How long to wait after the Nth attempt before trying an unfinished upload again. The same
# as retry_delay in scripts/trigger.sh, which does the retrying.
RETRY_DELAYS = (60, 5 * 60, 15 * 60, 60 * 60)
STATS_SECONDS = 1  # how often rclone writes a progress line, the same as the graph samples
UPLOAD_FOLLOWS_IMPORT_SECONDS = 30 * 60  # an upload this soon after an import is that import's


def remote_name() -> str:
    """The rclone remote to use, read when called so a long-running caller sees the environment."""
    return os.environ.get("PHOTO_OFFLOADER_GPHOTOS_REMOTE", "gphotos").rstrip(":")


REMOTE_NAME = remote_name()
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
SETUP_GUIDE_URL = "https://github.com/devturner/photo_offloader/blob/main/docs/google-photos.md"
# Text in rclone's errors (lower case) -> what is wrong and how to fix it. Anything else,
# such as a network failure, is not a setup problem, so it never gets a setup answer.
SETUP_PROBLEMS = (
    (
        ("invalid_grant", "expired or revoked", "empty token"),
        "The Google sign-in has expired.",
        "Run: rclone config reconnect {remote}:\n"
        "If this comes back a week later, the Google Cloud app is still in Testing: set it to In production.",
    ),
    (
        ("invalid_client",),
        "Google does not accept the client ID and secret.",
        "Run rclone config, edit '{remote}' and re-enter the client ID and secret, using a Desktop app client.",
    ),
    (
        ("has not been used", "is disabled", "service_disabled"),
        "The Photos Library API is not enabled.",
        "Enable the Photos Library API in the Google Cloud project, wait a few minutes and try again.",
    ),
    (
        ("insufficient",),
        "The sign-in is missing a Google Photos permission.",
        "Add the three Photos scopes to the consent screen, then run: rclone config reconnect {remote}:\n"
        "Also check that read_only is not true on the remote.",
    ),
)


class SetupStep(NamedTuple):
    label: str
    passed: Optional[bool]  # None: not checked, because an earlier step failed
    note: str = ""


def setup_problem(rclone_output: str) -> Optional[Tuple[str, str]]:
    """(what is wrong, how to fix it) if rclone's output names a known Google setup problem."""
    text = rclone_output.lower()
    for needles, problem, fix in SETUP_PROBLEMS:
        if any(needle in text for needle in needles):
            return problem, fix.format(remote=REMOTE_NAME)
    return None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def _rclone_output(command: List[str], timeout: int) -> Tuple[Optional[str], str]:
    """(stdout, "") on success, (None, what rclone said) on failure."""
    try:
        # No stdin: a password-protected rclone config must fail here, not wait for input.
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return (result.stdout, "") if result.returncode == 0 else (None, result.stderr)


def _album_failure_message(rclone_output: str) -> str:
    """One line for the dashboard: the fix for a known setup problem, otherwise rclone's words."""
    problem = setup_problem(rclone_output)
    if problem:
        return f"{problem[0]} {problem[1].splitlines()[0]}"
    return f"Could not create/check Google Photos album: {rclone_output.strip()}"


def check_setup() -> List[SetupStep]:
    """Each setup step in order, stopping at the first failure. Only reads; safe during an upload."""
    labels = (
        "rclone is installed",
        f"rclone has a remote named '{REMOTE_NAME}'",
        "Signed in to Google",
        f"Album '{ALBUM_NAME}'",
    )

    def stopped_at(index: int, note: str) -> List[SetupStep]:
        return (
            [SetupStep(label, True) for label in labels[:index]]
            + [SetupStep(labels[index], False, note)]
            + [SetupStep(label, None) for label in labels[index + 1 :]]
        )

    rclone = find_rclone()
    if not rclone:
        return stopped_at(0, "Install it: brew install rclone")

    remotes, error = _rclone_output([rclone, "listremotes"], timeout=30)
    if remotes is None:
        return stopped_at(1, f"Could not read the rclone config: {_last_line(error)}")
    if f"{REMOTE_NAME}:" not in remotes.splitlines():
        return stopped_at(
            1, f"Run rclone config and add a Google Photos remote named '{REMOTE_NAME}'."
        )

    albums, error = _rclone_output(
        [
            rclone,
            "lsf",
            "--dirs-only",
            f"{REMOTE_NAME}:album",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
        ],
        timeout=60,
    )
    if albums is None:
        problem = setup_problem(error)
        return stopped_at(
            2,
            f"{problem[0]}\n{problem[1]}"
            if problem
            else f"Could not reach Google Photos: {_last_line(error)}",
        )

    found = f"{ALBUM_NAME}/" in albums.splitlines()
    return [SetupStep(label, True) for label in labels[:3]] + [
        SetupStep(labels[3], True, "" if found else "Not created yet; the first sync creates it.")
    ]


def run_setup_check() -> int:
    steps = check_setup()
    marks = {True: "✓", False: "✗", None: "-"}
    print("Google Photos setup check\n")
    for step in steps:
        print(
            f"  {marks[step.passed]} {step.label}"
            + ("  (not checked)" if step.passed is None else "")
        )
        for line in step.note.splitlines():
            print(f"      {line}")
    if all(step.passed for step in steps):
        print("\nAll set. Press Sync now in the dashboard to upload.")
        return 0
    print(f"\nSetup guide: {SETUP_GUIDE_URL}")
    return 1


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


def publish_status(
    state: str,
    message: str,
    new_run: bool = False,
    waiting: Optional[int] = None,
    needs_setup: bool = False,
) -> None:
    """``waiting``: for a stopped upload, how many of its files it didn't send.
    ``needs_setup``: a failure only a change to the setup can fix, so not worth retrying."""
    previous = jsonfile.read_object(config.GOOGLE_STATUS_PATH)
    payload = {
        "state": state,
        "message": message,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_success": previous.get("last_success"),
    }
    if waiting is not None:
        payload["waiting"] = waiting
    if needs_setup:
        payload["needs_setup"] = True
    if state == "complete":
        payload["last_success"] = payload["updated_at"]
    if state == "running":
        # One sync publishes "running" more than once; the dashboard times it from the first.
        # ``new_run`` restarts the clock, even over a "running" left behind by a crash.
        continuing = previous.get("state") == "running" and not new_run
        payload["started_at"] = (previous.get("started_at") if continuing else None) or payload[
            "updated_at"
        ]

    jsonfile.write_json(config.GOOGLE_STATUS_PATH, payload, indent=2)


def remember_google_photos_connection(remote_name: str) -> None:
    """Persist non-secret connection metadata outside the replaceable install directory."""
    preferences = jsonfile.read_object(config.PREFERENCES_PATH)
    preferences["google_photos"] = {
        "configured": True,
        "remote": f"{remote_name}:",
        "last_connection_check": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    jsonfile.write_json(config.PREFERENCES_PATH, preferences, indent=2)


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


@contextlib.contextmanager
def _timed_step(name: str) -> Iterator[None]:
    """Say in the log how long a step took, to show where the wait before an upload goes."""
    started = time.monotonic()
    try:
        yield
    finally:
        log_sync_event(f"Upload step: {name} took {time.monotonic() - started:.1f}s")


def find_rclone() -> Optional[str]:
    found = shutil.which("rclone")
    if found:
        return found

    for candidate in (Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def current_status_state() -> str:
    return str(jsonfile.read_object(config.GOOGLE_STATUS_PATH).get("state", ""))


class Stopped(Exception):
    """The sync got SIGTERM: from Pause uploads, or from anything else that stops it."""


def _stop_on_sigterm(_signum: int, _frame: object) -> None:
    raise Stopped()


def uploads_paused() -> bool:
    return config.GOOGLE_PAUSED_PATH.exists()


def retry_wait(marker: str, now: float) -> Optional[float]:
    """Seconds until the trigger retries the upload a retry marker's text describes (0 if due),
    or None if the text isn't a marker."""
    try:
        attempts, last = (int(part) for part in marker.split()[:2])
    except ValueError:
        return None
    delay = RETRY_DELAYS[min(max(attempts, 1), len(RETRY_DELAYS)) - 1]
    return max(0.0, last + delay - now)


def _retry_attempts() -> int:
    try:
        return int(config.GOOGLE_RETRY_PATH.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def _note_attempt() -> None:
    """Say an upload is under way. Left behind if it doesn't finish, so the trigger retries it,
    waiting longer the more attempts it shows. A crash or power cut leaves it too."""
    try:
        config.GOOGLE_RETRY_PATH.write_text(
            f"{_retry_attempts() + 1} {int(time.time())}\n", encoding="ascii"
        )
    except OSError as exc:
        log_sync_event(f"Could not note the upload for a retry: {exc}")


def _clear_retry() -> None:
    try:
        config.GOOGLE_RETRY_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_sync_event(f"Could not clear the upload retry: {exc}")


def _publish_paused(waiting: Optional[int] = None) -> None:
    _clear_retry()  # Resume uploads starts the sync again
    previous = jsonfile.read_object(config.GOOGLE_STATUS_PATH)
    if waiting is None and previous.get("state") == "paused":
        return  # keep the count the paused upload saved
    count = f" {waiting:,} file(s) waiting." if waiting else ""
    publish_status("paused", f"Uploads are paused.{count}", waiting=waiting)


def _open_lock(path: Path) -> int:
    # An flock is released by the OS the moment its last holder dies, so a crash,
    # SIGKILL or power loss can never leave a stale lock behind. The lock file
    # itself is never deleted (deleting it would reopen a lock/unlink race).
    return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _wait_in_queue(lock_fd: int) -> bool:
    """Wait for the running upload to end, if no other sync is waiting already.

    The running upload only sends the files it listed when it started, so a sync started by a
    later import must not just give up. One waiting sync is enough: it lists every file once
    it gets the lock. True once this sync holds ``lock_fd``.
    """
    queue_fd = _open_lock(config.GOOGLE_QUEUE_LOCK_PATH)
    try:
        if not _try_lock(queue_fd):
            return False
        # For the dashboard, which can't test the lock without taking it for a moment.
        os.ftruncate(queue_fd, 0)
        os.write(queue_fd, str(os.getpid()).encode("ascii"))
        log_sync_event("An upload is running; this sync sends any new files after it.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        finally:
            os.ftruncate(queue_fd, 0)
        return True
    finally:
        os.close(queue_fd)  # the next import's sync may wait now; it lists files after us


def run_sync(argv: Optional[Sequence[str]] = None) -> int:
    config.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if uploads_paused():
        _publish_paused()
        return 0
    lock_fd = _open_lock(config.GOOGLE_LOCK_PATH)
    try:
        if not _try_lock(lock_fd) and not _wait_in_queue(lock_fd):
            # Keep the live run's real progress message if it already published one.
            if current_status_state() != "running":
                publish_status("running", "A Google Photos upload is already running.")
            return 0
        if uploads_paused():  # paused while this sync waited
            _publish_paused()
            return 0
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode("ascii"))  # informational only
        return _sync(lock_fd)
    except Stopped:
        # Stopped before any upload began: nothing was sent, so there is nothing to record.
        if uploads_paused():
            _publish_paused()
        return 0
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
            if (
                path.suffix.lower() not in eligible
                or not path.is_file()
                or path.is_symlink()
                or any(part.startswith(".") for part in relative.parts)
            ):
                continue
            name = relative.as_posix()
            # rclone's --files-from treats leading '#'/';' as comments and trims whitespace.
            if name != name.strip() or name[0] in "#;":
                continue
            info = path.stat()
            if uploaded.get(str(path.resolve())) != (info.st_size, info.st_mtime_ns, info.st_ino):
                pending.append(name)
        except OSError as exc:
            # Not marked as uploaded, so the next sync tries it again.
            log_sync_event(f"Skipping a file that can't be read: {path}: {exc}")
    return pending


def _check_remote(rclone: str) -> Optional[int]:
    """The exit code if the configured remote can't be used, otherwise None."""
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
    return None


def _ensure_album(rclone: str, destination: str) -> Optional[int]:
    """The exit code if the album can't be created or checked, otherwise None."""
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
            _album_failure_message(album.stderr),
            needs_setup=setup_problem(album.stderr) is not None,
        )
        return album.returncode or 1
    return None


def _run_rclone(command: List[str], lock_fd: Optional[int]) -> int:
    """Run rclone until it ends, and stop it if the sync is stopped first.

    rclone inherits the upload lock, so if the sync is killed in a way it can't catch, the
    rclone it leaves behind still holds the lock and no second upload starts beside it.
    """
    process = subprocess.Popen(command, pass_fds=() if lock_fd is None else (lock_fd,))
    try:
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _upload_snapshot(
    rclone: str, destination: str, pending: List[str], lock_fd: Optional[int] = None
) -> Optional[int]:
    """Run rclone on exactly ``pending``. Its exit code, or None if it could not start.

    Files imported while the upload is running are neither sent nor recorded, so the next
    sync picks them up.
    """
    list_fd, list_name = tempfile.mkstemp(
        prefix=UPLOAD_LIST_PREFIX, suffix=".txt", dir=config.INSTALL_DIR
    )
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
            f"{STATS_SECONDS}s",
            "--stats-one-line",
        ]
        publish_status(
            "running",
            f"Uploading {len(pending):,} JPG/video file(s) to Google Photos album '{ALBUM_NAME}'.",
        )
        try:
            return _run_rclone(command, lock_fd)
        except OSError as exc:
            publish_status("failed", f"Could not start rclone: {exc}")
            return None
    finally:
        try:
            os.unlink(list_name)
        except OSError as exc:
            log_sync_event(f"Could not remove the upload list {list_name}: {exc}")


def _record_upload(pending: List[str]) -> int:
    """Mark a finished upload in the catalog and report it. Returns the exit code."""
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


COPIED_LINE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) INFO\s+: (.+): Copied \(")


LOG_STAMP = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) ")


def _log_lines_since(started_at: float) -> List[str]:
    """The sync log's lines stamped at or after ``started_at``."""
    try:
        lines = config.GOOGLE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log_sync_event(f"Could not read the sync log: {exc}")
        return []
    recent = []
    for line in lines:
        match = LOG_STAMP.match(line)
        if not match:
            continue
        stamp = datetime.strptime(match.group(1), "%Y/%m/%d %H:%M:%S").timestamp()
        if stamp >= int(started_at):  # the log has whole seconds
            recent.append(line)
    return recent


def sent_since(pending: Sequence[str], started_at: float) -> List[str]:
    """The files of ``pending`` that rclone's log says it copied since ``started_at``."""
    wanted, sent = set(pending), []
    for line in _log_lines_since(started_at):
        match = COPIED_LINE.match(line)
        if match and match.group(2) in wanted:
            sent.append(match.group(2))
            wanted.discard(match.group(2))
    return sent


def _needs_setup(started_at: float) -> bool:
    """Whether the sync that started at ``started_at`` failed in a way only setup can fix."""
    status = jsonfile.read_object(config.GOOGLE_STATUS_PATH)
    if status.get("state") == "not_configured" or status.get("needs_setup"):
        return True
    return setup_problem("\n".join(_log_lines_since(started_at))) is not None


def _upload_stopped(pending: List[str], started_at: float) -> int:
    """Save what a stopped upload sent, so it isn't sent again, and say why it stopped."""
    sent = sent_since(pending, started_at)
    try:
        catalog.mark_google_photos_uploaded_many(
            [config.LOCAL_DEST_ROOT / name for name in sent], REMOTE_NAME, ALBUM_NAME
        )
    except (OSError, sqlite3.Error) as exc:
        # The next sync lists them again; rclone skips names already in the album.
        log_sync_event(f"Could not save which files were sent before the upload stopped: {exc}")
    waiting = len(pending) - len(sent)
    paused = uploads_paused()
    log_sync_event(
        f"Upload {'paused' if paused else 'stopped'} after {len(sent):,} of {len(pending):,} file(s)."
    )
    if paused:
        _publish_paused(waiting)
    else:
        publish_status("interrupted", "The upload was stopped before it finished.", waiting=waiting)
    # Stopped after the last file, the upload is done: a later sync would have nothing to send.
    outcome = "complete" if not waiting else "paused" if paused else "stopped"
    _record_on_import(outcome, sent, started_at)
    return 0 if paused else 1


def _record_on_import(outcome: str, sent: List[str], started_at: float) -> None:
    catalog.record_upload_on_import(
        {
            "outcome": outcome,
            "files": len(sent),
            "bytes": _pending_bytes(sent),
            "seconds": round(time.time() - started_at, 1),
            "album": ALBUM_NAME,
        },
        started_at=started_at,
        within_seconds=UPLOAD_FOLLOWS_IMPORT_SECONDS,
    )


def _sync(lock_fd: Optional[int] = None) -> int:
    rclone = find_rclone()
    if not rclone:
        publish_status(
            "not_configured",
            "rclone is not installed. Install it and configure a Google Photos remote named 'gphotos'.",
        )
        _clear_retry()
        return 2

    if config.SETTINGS_PROBLEM:
        publish_status("failed", str(settings.SettingsProblem(config.SETTINGS_PROBLEM)))
        return 1

    if not config.LOCAL_DEST_ROOT.is_dir():
        publish_status("failed", f"Local photo archive does not exist: {config.LOCAL_DEST_ROOT}")
        return 1

    try:
        with _timed_step("finding files"):
            pending = list_upload_candidates()
    except sqlite3.Error as exc:
        log_sync_event(f"Could not read the upload records: {exc}")
        publish_status("failed", "Couldn't read the record of past uploads, so nothing was sent.")
        return 1
    if not pending:
        # Sync is launched after every import; skip rclone (and the network) when
        # there is nothing new to send.
        log_sync_event("Google Photos sync skipped: no new JPG/video files to upload.")
        publish_status("complete", f"Google Photos is up to date for album '{ALBUM_NAME}'.")
        _clear_retry()
        return 0
    # Say so before the slower rclone checks, so the dashboard shows the upload straight away.
    publish_status(
        "running", f"Preparing to upload {len(pending):,} JPG/video file(s).", new_run=True
    )
    started_at = time.time()
    _note_attempt()
    try:
        code = _send_pending(rclone, pending, lock_fd)
    except Stopped:
        return _upload_stopped(pending, started_at)
    if code == 0 or _needs_setup(started_at):
        _clear_retry()
    _record_on_import("complete" if code == 0 else "failed", pending, started_at)
    return code


def _pending_bytes(pending: List[str]) -> int:
    """The size of the files to send; a file gone by now just doesn't count."""
    total = 0
    for name in pending:
        try:
            total += (config.LOCAL_DEST_ROOT / name).stat().st_size
        except OSError as exc:
            log_sync_event(f"Could not measure a file for the import report: {name}: {exc}")
    return total


def _send_pending(rclone: str, pending: List[str], lock_fd: Optional[int] = None) -> int:
    """Check the remote and send ``pending``. Publishes the result and returns the exit code."""
    destination = f"{REMOTE_NAME}:album/{ALBUM_NAME}"
    with _timed_step("remote check"):
        problem = _check_remote(rclone)
    # An album that files have been uploaded to exists, so there's no need to wait for Google to say so.
    album_known = catalog.google_photos_album_used(REMOTE_NAME, ALBUM_NAME)
    if album_known:
        log_sync_event("Upload step: album check skipped, it has had uploads before")
    elif problem is None:
        with _timed_step("album check"):
            problem = _ensure_album(rclone, destination)
    if problem is not None:
        return problem

    try:
        remember_google_photos_connection(REMOTE_NAME)
    except (OSError, ValueError) as exc:
        # A preferences-file failure must not block a working upload.
        log_sync_event(f"Could not persist Google Photos connection preference: {exc}")

    logs.rotate_log_file(config.GOOGLE_LOG_PATH, SYNC_LOG_MAX_BYTES, SYNC_LOG_BACKUPS)
    with _timed_step("rclone copy"):
        returncode = _upload_snapshot(rclone, destination, pending, lock_fd)
    if returncode is None:
        return 1
    if returncode == 0:
        return _record_upload(pending)
    if album_known:
        # The check was skipped, so make it now: it says clearly if Google refused the album, and
        # it creates the album if it was deleted, so the next sync starts from a known state.
        problem = _ensure_album(rclone, destination)
        if problem is not None:
            return problem
    publish_status(
        "failed", f"rclone exited with status {returncode}. See {config.GOOGLE_LOG_PATH}"
    )
    return returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Pause uploads stops the sync with SIGTERM: stop rclone and say paused, rather than die.
    # Set here, in the program, so a test that calls run_sync keeps its own SIGTERM.
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    if args == ["--check"]:
        return run_setup_check()
    if args == ["--retry"]:
        # Photo Offloader opening: carry on with an unfinished upload without waiting for the
        # trigger's next retry. Nothing to do if none is owed.
        return run_sync() if config.GOOGLE_RETRY_PATH.exists() else 0
    if args not in ([], ["--sync"]):
        print("Usage: photo-offloader-google-sync [--sync | --retry | --check]", file=sys.stderr)
        return 2
    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
