#!/usr/bin/env python3
"""Photo Offloader dashboard core: gathers everything the dashboard shows and runs its actions.

There is deliberately no GUI code here. ``DashboardCore.state()`` returns one JSON-able
dict that the web front end renders, and ``DashboardCore.act()`` performs the few things the
dashboard can do (pause, back up, sync, stop, ...). Everything that touches the machine
(launchctl, pgrep, subprocesses, the clock) is injectable so it can be tested.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from photo_offloader import importer as catalog
from photo_offloader import layout
from photo_offloader.google_photos import parse_rclone_stats_line

SERVICE_LABEL = "com.user.photooffloader"
LAST_TRANSFER_TTL_SECONDS = 10 * 60   # completed/canceled results fade after this
STALE_STATUS_SECONDS = 20             # an "active" status this old, with no importer, is stale
SPEED_SAMPLES = 60


# --------------------------------------------------------------------------- formatting
def format_bytes(value: float) -> str:
    amount = max(float(value), 0.0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def parse_display_bytes(value: str) -> float:
    """Convert rclone's human-readable byte totals into bytes."""
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\s*", value, re.IGNORECASE)
    if not match:
        return 0.0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    exponent = {"B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2,
                "GB": 3, "GIB": 3, "TB": 4, "TIB": 4}[unit]
    return amount * (1024 ** exponent)


CATEGORY_LABELS = {"JPG": "JPG", "RAW": "RAW", "VIDEO": "Video", "OTHER": "Other"}
IMPORT_STAGES = (("scan", "Scan card"), ("metadata", "Read photo info"),
                 ("copy", "Copy new files"), ("usb", "Back up to USB"))
THEMES = ("system", "light", "dark")
SPEED_WINDOW_SAMPLES = 10
def google_remote_name() -> str:
    """Same rule as REMOTE_NAME in photo_offloader.google_photos."""
    return os.environ.get("PHOTO_OFFLOADER_GPHOTOS_REMOTE", "gphotos").rstrip(":")


def activity_stage(status: Dict[str, Any]) -> str:
    """Which step the importer is on; older importers only report phase/state."""
    stage = str(status.get("stage", "") or "")
    if stage in ("scan", "metadata", "copy", "usb", "catalog"):
        return stage
    phase = str(status.get("phase", "") or "")
    operation = f"{status.get('operation', '')} {status.get('message', '')}".lower()
    destination = str(status.get("destination", "") or "").lower()
    if phase == "catalog-update":
        return "catalog"
    if phase.lower() in ("usb-backup", "usb backup") or "usb" in operation or "/volumes/photos" in destination:
        return "usb"
    if phase == "copy-decision" or status.get("state") == "copying":
        return "copy"
    return "scan"


def import_stages(status: Dict[str, Any], stage: str) -> Optional[List[Dict[str, str]]]:
    """The steps of a card import with done/active/pending/skipped, or None for other runs."""
    if "usb_planned" not in status or stage not in dict(IMPORT_STAGES):
        return None
    order = [step_id for step_id, _ in IMPORT_STAGES]
    current = order.index(stage)
    steps = []
    for index, (step_id, label) in enumerate(IMPORT_STAGES):
        if step_id == "usb" and not status.get("usb_planned"):
            state = "skipped"
        else:
            state = "done" if index < current else "active" if index == current else "pending"
        steps.append({"id": step_id, "label": label, "state": state})
    return steps


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------------- paths
@dataclass(frozen=True)
class Paths:
    install_dir: Path
    data_dir: Path
    volumes_root: Path
    usb_volume: Path
    local_archives: Tuple[Path, ...]
    usb_archives: Tuple[Path, ...]
    python: Path
    launchctl: str = "/bin/launchctl"
    pgrep: str = "/usr/bin/pgrep"
    rclone_config: Optional[Path] = None   # where rclone keeps its remotes (not ours to change)

    @classmethod
    def default(cls) -> "Paths":
        support = Path.home() / "Library" / "Application Support"
        install = Path(os.environ.get("PHOTO_OFFLOADER_INSTALL_DIR", str(support / "PhotoOffloader")))
        usb = Path("/Volumes/Photos")
        venv_python = install / "venv" / "bin" / "python3"
        return cls(
            install_dir=install,
            data_dir=catalog.CATALOG_DB_PATH.parent,
            volumes_root=Path("/Volumes"),
            usb_volume=usb,
            local_archives=(Path.home() / "Pictures" / "CameraImports",
                            Path.home() / "Pictures" / "CameraImportsRaw"),
            usb_archives=(usb / "CameraImports",),
            python=venv_python if venv_python.exists() else Path(sys.executable),
            rclone_config=Path(os.environ.get("RCLONE_CONFIG", str(Path.home() / ".config" / "rclone" / "rclone.conf"))),
        )

    # files the importer / trigger / service maintain
    @property
    def status_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_STATUS_FILE", str(self.install_dir / "status.json")))

    @property
    def pause_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_PAUSE_FILE", str(self.install_dir / ".paused")))

    @property
    def terminal_output_file(self) -> Path:
        return self.install_dir / ".terminal-output-enabled"

    @property
    def auto_eject_file(self) -> Path:
        return self.install_dir / ".auto-eject-enabled"

    @property
    def handled_state_file(self) -> Path:
        return self.install_dir / ".last_camera_source"

    @property
    def google_status_file(self) -> Path:
        return self.install_dir / "google_photos_status.json"

    @property
    def import_log_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_IMPORT_LOG", str(Path.home() / ".camera_transfer.log")))

    @property
    def trigger_log_file(self) -> Path:
        return Path(os.environ.get("PHOTO_OFFLOADER_TRIGGER_LOG",
                                   str(Path.home() / ".camera_transfer_trigger.log")))

    @property
    def google_log_file(self) -> Path:
        return self.install_dir / "google_photos_sync.log"

    @property
    def preferences_file(self) -> Path:
        return self.data_dir / "preferences.json"

    @property
    def catalog_db(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    @property
    def dashboard_state_file(self) -> Path:
        # Next to the catalog so it survives reinstalls, like the history it refers to.
        return self.data_dir / "dashboard_state.json"

    @property
    def importer_script(self) -> Path:
        return self.install_dir / layout.IMPORTER

    @property
    def backup_launcher(self) -> Path:
        return self.install_dir / layout.USB_BACKUP

    @property
    def google_script(self) -> Path:
        return self.install_dir / layout.GOOGLE_SYNC

    @property
    def service_script(self) -> Path:
        return self.install_dir / layout.SERVICE

    @property
    def trigger_script(self) -> Path:
        return self.install_dir / layout.TRIGGER


# ------------------------------------------------------------------------------- the core
class DashboardCore:
    def __init__(
        self,
        paths: Optional[Paths] = None,
        *,
        clock: Callable[[], float] = time.time,
        run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.paths = paths or Paths.default()
        self._clock = clock
        self._run = run
        self._popen = popen
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._samples: Deque[float] = deque(maxlen=SPEED_SAMPLES)
        self._last_sample: Optional[Tuple[float, int, str]] = None

    # ---- tiny TTL cache so a 1 s poll doesn't fork or hit SQLite every time
    def _cached(self, key: str, ttl: float, produce: Callable[[], Any]) -> Any:
        now = self._clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = produce()
        self._cache[key] = (now, value)
        return value

    def _invalidate(self, *keys: str) -> None:
        for key in keys:
            self._cache.pop(key, None)

    # ---- machine probes ------------------------------------------------------------
    def service_running(self) -> bool:
        def probe() -> bool:
            try:
                result = self._run([self.paths.launchctl, "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                                   capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return result.returncode == 0
        return self._cached("service", 3.0, probe)

    def importer_running(self) -> bool:
        def probe() -> bool:
            try:
                result = self._run([self.paths.pgrep, "-f", str(self.paths.importer_script)],
                                   capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return bool(result.stdout.strip())
        return self._cached("importer", 2.0, probe)

    def google_running(self) -> bool:
        def probe() -> bool:
            try:
                result = self._run([self.paths.pgrep, "-f", str(self.paths.google_script)],
                                   capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return bool(result.stdout.strip())
        return self._cached("google", 2.0, probe)

    def mounted_card(self) -> Optional[Path]:
        def probe() -> Optional[Path]:
            try:
                for volume in sorted(self.paths.volumes_root.iterdir()):
                    if not volume.is_symlink() and (volume / "DCIM").is_dir():
                        return volume
            except OSError:
                pass
            return None
        return self._cached("card", 3.0, probe)

    def card_handled(self, card: Optional[Path]) -> bool:
        """True when the trigger has already dealt with this mounted card and will not start again."""
        if card is None:
            return False
        try:
            return self.paths.handled_state_file.read_text().strip() == str(card / "DCIM")
        except OSError:
            return False

    def usb_mounted(self) -> bool:
        return self._cached("usb", 3.0, lambda: self.paths.usb_volume.is_mount()
                            or self.paths.usb_volume.is_dir())

    # ---- persisted dismissals ("clear" for the last-transfer card) -----------------
    def _dashboard_state(self) -> Dict[str, Any]:
        return read_json(self.paths.dashboard_state_file)

    def _save_dashboard_state(self, **updates: Any) -> None:
        current = self._dashboard_state()
        current.update(updates)
        target = self.paths.dashboard_state_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(target)
        except OSError:
            pass

    # ---- importer status ----------------------------------------------------------------
    def read_status(self) -> Dict[str, Any]:
        return read_json(self.paths.status_file)

    @staticmethod
    def _status_stamp(status: Dict[str, Any]) -> str:
        return str(status.get("updated_at", "") or "")

    @staticmethod
    def _status_age(status: Dict[str, Any], now: float) -> Optional[float]:
        try:
            return max(now - datetime.fromisoformat(str(status["updated_at"])).timestamp(), 0.0)
        except (KeyError, ValueError, OSError):
            return None

    def _stale(self, status: Dict[str, Any], now: float) -> bool:
        """An 'active' status nobody is updating any more (importer crashed or was killed)."""
        if str(status.get("state", "")) not in ("scanning", "copying"):
            return False
        if self.importer_running():
            return False
        age = self._status_age(status, now)
        return age is not None and age >= STALE_STATUS_SECONDS

    def _interrupted(self, status: Dict[str, Any], now: float) -> bool:
        """A stale status the user has not dismissed yet: worth telling them about."""
        return self._stale(status, now) and (
            self._status_stamp(status) != self._dashboard_state().get("dismissed_status_stamp"))

    def _activity(self, status: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
        state = str(status.get("state", "idle"))
        if state not in ("scanning", "copying") or self._stale(status, now):
            self._samples.clear()
            self._last_sample = None
            return None

        stage = activity_stage(status)
        done = int(status.get("bytes_done", 0) or 0)
        total = int(status.get("bytes_total", 0) or 0)
        current_file = str(status.get("current_file", "") or "")
        category = CATEGORY_LABELS.get(str(status.get("category", "") or ""), "")
        new_files = int(status.get("new_files", 0) or 0)
        detail = str(status.get("message", "") or "")
        kind = {"catalog": "catalog", "usb": "usb_backup"}.get(stage, "import")
        percent: Optional[float] = None

        if stage == "catalog":
            title = "Updating file index"
            scanned = int(status.get("catalog_scanned", 0) or 0)
            step = str(status.get("catalog_stage", "") or "")
            detail = " · ".join(part for part in (step, f"{scanned:,} files scanned") if part)
        elif stage == "usb":
            title = "Backing up to USB" if state == "copying" else "Comparing with USB backup"
            detail = current_file or (f"{format_bytes(done)} of {format_bytes(total)}" if total else detail)
        elif stage == "copy":
            # Every file is first checked, then copied if new: the status flips between the
            # two per file. Name the step by what the session is doing overall so it holds still.
            title = "Copying new files" if state == "copying" or new_files else "Checking for new files"
            detail = " · ".join(part for part in (category, current_file) if part) or detail
        elif stage == "metadata":
            title = "Reading photo info"
        else:
            title, detail = "Scanning card", detail or "Looking for photos and video"
        if stage in ("copy", "usb") and total:
            # One overall measure for checks and copies alike (skipped files count as done).
            percent = min(100.0, done / total * 100.0)

        run_key = f"{kind}|{status.get('source', '')}"
        transferred = int(status.get("bytes_transferred", 0) or 0)
        if stage in ("copy", "usb"):
            last = self._last_sample
            if last and last[2] == run_key and transferred >= last[1] and now > last[0]:
                self._samples.append((transferred - last[1]) / (now - last[0]))
            elif last is None or last[2] != run_key or transferred < last[1]:
                self._samples.clear()  # new transfer: start a fresh graph
            self._last_sample = (now, transferred, run_key)
        else:
            self._samples.clear()
            self._last_sample = None

        # Average over ~10 s of polls: between copies (checking unchanged files) no bytes
        # move, and a shorter window would make the speed blink to nothing and back.
        recent = list(self._samples)[-SPEED_WINDOW_SAMPLES:]
        speed = sum(recent) / len(recent) if recent else float(status.get("transfer_speed_bps", 0) or 0)
        eta = float(status.get("eta_seconds", 0) or 0)
        if eta <= 0 and speed > 0 and total > done:
            eta = (total - done) / speed
        moving = stage in ("copy", "usb")
        return {
            "kind": kind, "stage": stage, "title": title, "detail": detail, "percent": percent,
            "indeterminate": percent is None, "bytes_done": done, "bytes_total": total,
            "files_done": int(status.get("files_done", 0) or 0),
            "files_total": int(status.get("files_total", 0) or 0), "new_files": new_files,
            "speed_bps": speed if moving else 0.0,
            "eta_seconds": eta if moving and total > done and eta > 0 else None,
            "elapsed_seconds": float(status.get("elapsed_seconds", 0) or 0),
            "source": str(status.get("source", "") or ""),
            "speed_history": [round(sample) for sample in self._samples],
            "stages": import_stages(status, stage),
        }

    # ---- catalog ------------------------------------------------------------------------
    def archive_counts(self) -> Optional[Dict[str, Any]]:
        def query() -> Optional[Dict[str, Any]]:
            if not self.paths.catalog_db.is_file():
                return {"local": 0, "usb": 0, "uploaded": 0, "local_updated": None, "usb_updated": None}
            try:
                with sqlite3.connect(f"file:{self.paths.catalog_db}?mode=ro", uri=True, timeout=0.5) as db:
                    local_roots = [str(root.resolve()) for root in self.paths.local_archives]
                    usb_roots = [str(root.resolve()) for root in self.paths.usb_archives]
                    marks = ",".join("?" * len(local_roots))
                    local = db.execute(f"SELECT COUNT(*) FROM file_locations WHERE root IN ({marks})",
                                       local_roots).fetchone()[0]
                    usb = db.execute("SELECT COUNT(*) FROM file_locations WHERE root = ?",
                                     (usb_roots[0],)).fetchone()[0]
                    try:
                        uploaded = db.execute("SELECT COUNT(*) FROM google_photos_uploads").fetchone()[0]
                    except sqlite3.OperationalError:
                        uploaded = 0
                    try:
                        refreshes = {row[0]: row[1] for row in db.execute(
                            "SELECT root, last_updated FROM catalog_refreshes")}
                    except sqlite3.OperationalError:
                        refreshes = {}
            except (OSError, sqlite3.Error):
                return None
            def newest(roots: List[str]) -> Optional[float]:
                stamps = [refreshes[root] for root in roots if root in refreshes]
                return max(stamps) if stamps else None
            return {"local": local, "usb": usb, "uploaded": uploaded,
                    "local_updated": newest(local_roots), "usb_updated": newest(usb_roots)}
        return self._cached("counts", 5.0, query)

    # ---- Google Photos ----------------------------------------------------------------------
    def _google(self, now: float) -> Dict[str, Any]:
        status = read_json(self.paths.google_status_file)
        preferences = read_json(self.paths.preferences_file).get("google_photos", {})
        # rclone's own config is the source of truth: our status file is replaced on every
        # install and the preference is only written after a sync that uploaded something.
        configured = (bool(preferences.get("configured")) or self.rclone_remote_configured()
                      or bool(status) and status.get("state") != "not_configured")
        state = str(status.get("state", "")) or ("idle" if configured else "not_configured")
        info: Dict[str, Any] = {
            "configured": configured, "state": state, "message": str(status.get("message", "") or ""),
            "last_success": status.get("last_success"), "progress": None,
        }
        if state == "running" and not self.google_running():
            # The sync only writes status on state changes, so age says nothing; a missing
            # process does: it crashed or was killed mid-upload.
            info["state"], info["message"] = "interrupted", "The last upload was interrupted."
        elif state == "running":
            info["progress"] = self._upload_progress()
        return info

    @staticmethod
    def _eta_seconds(text: object) -> Optional[float]:
        match = re.fullmatch(r"\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*", str(text or ""))
        if not match or not any(match.groups()):
            return None
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return float(hours * 3600 + minutes * 60 + seconds)

    def rclone_remote_configured(self) -> bool:
        """Whether rclone has a [gphotos] remote. Reads section names only, never secrets."""
        config = self.paths.rclone_config
        if config is None:
            return False

        remote = google_remote_name()

        def probe() -> bool:
            try:
                with config.open(encoding="utf-8", errors="replace") as handle:
                    return any(line.strip() == f"[{remote}]" for line in handle)
            except OSError:
                return False
        return self._cached("rclone", 30.0, probe)

    def _upload_activity(self, google: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if google["state"] != "running":
            return None
        progress = google["progress"]
        if not progress:
            return {"kind": "upload", "title": "Uploading to Google Photos", "detail": "Preparing upload",
                    "percent": None, "indeterminate": True, "bytes_done": 0, "bytes_total": 0,
                    "speed_bps": 0.0, "eta_seconds": None, "elapsed_seconds": 0.0, "source": "",
                    "speed_history": []}
        return {"kind": "upload", "title": "Uploading to Google Photos",
                "detail": f"{progress['transferred']} of {progress['total']}",
                "percent": progress["percent"], "indeterminate": False,
                "bytes_done": int(parse_display_bytes(str(progress["transferred"]))),
                "bytes_total": int(parse_display_bytes(str(progress["total"]))),
                "speed_bps": parse_display_bytes(str(progress["speed"] or "").replace("/s", "")),
                "eta_seconds": self._eta_seconds(progress["eta"]), "elapsed_seconds": 0.0,
                "source": "", "speed_history": []}

    def _upload_progress(self) -> Optional[Dict[str, Any]]:
        try:
            with self.paths.google_log_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(size - 65536, 0))
                if size > 65536:
                    handle.readline()
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return None
        parsed = next((p for p in (parse_rclone_stats_line(line) for line in reversed(lines)) if p), None)
        if not parsed:
            return None
        percent = min(100.0, max(0.0, float(parsed["percent"])))
        return {"percent": percent, "transferred": parsed.get("transferred"), "total": parsed.get("total"),
                "speed": parsed.get("speed"), "eta": parsed.get("eta"), "finalizing": percent >= 100.0}

    # ---- last transfer & history --------------------------------------------------------
    @staticmethod
    def describe_run(row: Dict[str, Any]) -> Dict[str, Any]:
        kind, outcome = str(row["kind"]), str(row["outcome"])
        source = str(row.get("source") or "")
        noun = {"import": f"Import from {source}" if source else "Import",
                "usb_backup": "USB backup", "catalog": "Catalog update"}.get(kind, "Transfer")
        done = {"import": f"Imported from {source}" if source else "Import complete",
                "usb_backup": "USB backup complete", "catalog": "Catalog updated"}.get(kind, "Transfer complete")
        if outcome == "failed":
            title, tone = f"{noun} failed", "error"
        elif outcome == "canceled":
            title, tone = f"{noun} canceled", "warning"
        else:
            title, tone = done, "success"
        parts: List[str] = []
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        if kind == "import":
            new = details.get("new")
            if isinstance(new, dict):
                count = sum(int(v) for v in new.values())
                labels = {"JPG": lambda n: plural(n, "photo"), "RAW": lambda n: f"{n:,} RAW",
                          "VIDEO": lambda n: plural(n, "video"), "OTHER": lambda n: f"{n:,} other"}
                if count:
                    parts.append(f"{count:,} new: " + ", ".join(
                        labels[key](int(new[key])) for key in ("JPG", "RAW", "VIDEO", "OTHER") if new.get(key)))
                    if details.get("new_bytes"):
                        parts.append(format_bytes(int(details["new_bytes"])))
                else:
                    parts.append("No new files")
            else:  # recorded before per-session details existed
                parts.append(f"{int(row['copied']):,} new" if row["copied"] else "No new files")
            if row["files"]:
                safe = outcome == "complete" and not row["failed"]
                parts.append(f"{int(row['files']):,} on card" + (", all backed up" if safe else ""))
            if row["failed"]:
                parts.append(f"{int(row['failed'])} failed")
            if "usb_copied" in details:
                parts.append(f"{int(details['usb_copied']):,} copied to USB")
            elif row["usb"] and outcome == "complete":
                parts.append("backed up to USB")
        elif kind == "usb_backup":
            parts.append(f"{int(row['copied']):,} copied")
            if row["skipped"]:
                parts.append(f"{int(row['skipped']):,} already there")
            if row["failed"]:
                parts.append(f"{int(row['failed'])} failed")
        if outcome != "complete" and row.get("message"):
            parts.append(str(row["message"]))
        return {**row, "title": title, "tone": tone, "summary": " · ".join(parts)}

    def history(self, limit: int = 15) -> List[Dict[str, Any]]:
        return [self.describe_run(row) for row in catalog.list_import_history(limit=limit)]

    def _last_transfer(self, now: float) -> Optional[Dict[str, Any]]:
        rows = catalog.list_import_history(limit=1)
        if not rows:
            return None
        row = rows[0]
        if int(row["id"]) <= int(self._dashboard_state().get("dismissed_history_id", 0) or 0):
            return None
        # Failures stay until dismissed; everything else fades after a while.
        if row["outcome"] != "failed" and now - float(row["finished_at"]) > LAST_TRANSFER_TTL_SECONDS:
            return None
        return self.describe_run(row)

    # ---- the one payload the front end renders -------------------------------------------
    def state(self) -> Dict[str, Any]:
        now = self._clock()
        status = self.read_status()
        running = self.service_running()
        paused = self.paths.pause_file.exists()
        card = self.mounted_card()
        import_activity = self._activity(status, now)
        interrupted = self._interrupted(status, now)
        last = self._last_transfer(now)
        counts = self.archive_counts()
        google = self._google(now)
        usb_mounted = self.usb_mounted()
        busy = import_activity is not None          # the importer is working (uploads don't block it)
        activity = import_activity or self._upload_activity(google)

        handled = self.card_handled(card)
        headline = self._headline(running, paused, card, activity, interrupted, last, status, google, handled)
        return {
            "generated_at": now,
            "version": read_json(self.paths.install_dir / "build_info.json").get("version", ""),
            "service": {"running": running, "paused": paused},
            "card": {"mounted": card is not None, "name": card.name if card else None},
            "headline": headline,
            "busy": busy,
            "activity": activity,
            "interrupted": interrupted,
            "last_transfer": last,
            "pipeline": self._pipeline(card, import_activity, counts, usb_mounted, google, busy, now,
                                       handled and running and not paused),
            "upload": google["progress"],
            "settings": {
                "auto_eject": self.paths.auto_eject_file.exists(),
                "terminal_output": self.paths.terminal_output_file.exists(),
                "theme": self.theme(),
            },
        }

    @staticmethod
    def _headline(running: bool, paused: bool, card: Optional[Path], activity: Optional[Dict[str, Any]],
                  interrupted: bool, last: Optional[Dict[str, Any]], status: Dict[str, Any],
                  google: Dict[str, Any], card_handled: bool = False) -> Dict[str, str]:
        if not running:
            return {"tone": "stopped", "title": "Photo Offloader is stopped",
                    "detail": "Camera cards will not be imported until you start it."}
        if activity:
            source = activity["source"].rsplit("/", 1)[-1] if activity["source"] else ""
            if activity["kind"] == "import" and source:
                detail = " · ".join(part for part in (source, activity["detail"]) if part)
                return {"tone": "active", "title": activity["title"], "detail": detail}
            return {"tone": "active", "title": activity["title"], "detail": activity["detail"]}
        if interrupted:
            return {"tone": "warning", "title": "The last transfer was interrupted",
                    "detail": "It stopped before finishing. Insert the card again to continue."}
        if last and last["outcome"] == "failed":
            return {"tone": "error", "title": last["title"],
                    "detail": str(last.get("message") or last["summary"] or "See the log for details.")}
        if google["state"] in ("failed", "error", "interrupted"):
            return {"tone": "warning", "title": "Google Photos upload needs attention",
                    "detail": google["message"] or "Sync again to finish uploading."}
        if paused:
            return {"tone": "warning", "title": "Automatic imports are paused",
                    "detail": "Cards will not be imported until you resume."}
        if last:
            return {"tone": "success", "title": last["title"], "detail": last["summary"]}
        if card and card_handled:
            return {"tone": "idle", "title": f"{card.name} was already imported",
                    "detail": "Shot more since? Choose Import now, or eject and re-insert the card."}
        if card:
            return {"tone": "idle", "title": f"{card.name} is mounted",
                    "detail": "The import will start in a moment."}
        return {"tone": "idle", "title": "Ready", "detail": "Insert a camera card to begin."}

    def _pipeline(self, card: Optional[Path], activity: Optional[Dict[str, Any]],
                  counts: Optional[Dict[str, Any]], usb_mounted: bool, google: Dict[str, Any],
                  busy: bool, now: float, can_reimport: bool = False) -> List[Dict[str, Any]]:
        kind = activity["kind"] if activity else None
        local = counts["local"] if counts else None
        usb = counts["usb"] if counts else None

        def step(step_id: str, label: str, state: str, headline: str, detail: str,
                 action: Optional[Dict[str, str]] = None, percent: Optional[float] = None) -> Dict[str, Any]:
            return {"id": step_id, "label": label, "state": state, "headline": headline,
                    "detail": detail, "action": action, "percent": percent}

        stage = activity.get("stage") if activity else None
        if kind == "import" and stage in ("scan", "metadata"):
            card_step = step("card", "Camera card", "active", card.name if card else "Card", activity["title"])
        elif card:
            card_step = step("card", "Camera card", "ok", card.name, "Already imported" if can_reimport else "Mounted",
                             {"id": "import_card", "label": "Import now"} if can_reimport and not busy else None)
        else:
            card_step = step("card", "Camera card", "off", "No card", "Insert a card to begin")

        if kind == "import" and stage == "copy":
            archive_state, archive_head = "active", "Copying"
        else:
            archive_state, archive_head = "ok", "Up to date" if local else "Empty"
        archive_detail = (f"{local:,} files indexed" if local else "Nothing indexed yet") if local is not None \
            else "Catalog unavailable"
        if kind == "import" and stage == "copy":
            archive_detail = activity["detail"] or archive_detail
        archive_step = step("archive", "Mac archive", archive_state, archive_head, archive_detail,
                            None, activity["percent"] if kind == "import" and stage == "copy" else None)

        if kind == "usb_backup":
            usb_step = step("usb", "USB backup", "active", "Backing up", activity["detail"],
                            None, activity["percent"])
        elif usb_mounted:
            usb_step = step("usb", "USB backup", "ok", "Connected",
                            f"{usb:,} files backed up" if usb else "Ready to back up",
                            None if busy else {"id": "usb_backup", "label": "Back up now"})
        else:
            usb_step = step("usb", "USB backup", "off", "Not connected", "Plug in the backup drive")

        gstate = google["state"]
        if not google["configured"]:
            cloud_step = step("cloud", "Google Photos", "off", "Not set up", "Configure an rclone remote named gphotos")
        elif gstate == "running":
            progress = google["progress"]
            cloud_step = step("cloud", "Google Photos", "active",
                              "Finishing upload" if progress and progress["finalizing"] else "Uploading",
                              f"{progress['transferred']} of {progress['total']}" if progress else "Preparing",
                              None, progress["percent"] if progress else None)
        elif gstate == "interrupted":
            cloud_step = step("cloud", "Google Photos", "error", "Upload interrupted",
                              "It stopped before finishing", {"id": "google_sync", "label": "Resume"})
        elif gstate in ("failed", "error"):
            cloud_step = step("cloud", "Google Photos", "error", "Upload failed", google["message"] or "See the log",
                              {"id": "google_sync", "label": "Retry"})
        else:
            uploaded = counts["uploaded"] if counts else 0
            cloud_step = step("cloud", "Google Photos", "ok", "Up to date",
                              f"{uploaded:,} files uploaded" if uploaded else "Nothing uploaded yet",
                              None if busy else {"id": "google_sync", "label": "Sync now"})
        return [card_step, archive_step, usb_step, cloud_step]

    # ---- actions ---------------------------------------------------------------------------
    def _spawn(self, argv: List[str]) -> None:
        self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)

    def act(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        handlers: Dict[str, Callable[[], Dict[str, Any]]] = {
            "pause": self._pause, "resume": self._resume,
            "import_card": self._import_card, "usb_backup": self._usb_backup, "update_catalog": self._update_catalog,
            "google_sync": self._google_sync,
            "toggle_auto_eject": lambda: self._toggle(self.paths.auto_eject_file, "Auto-eject"),
            "toggle_terminal_output": lambda: self._toggle(self.paths.terminal_output_file, "Terminal windows"),
            "service_start": self._service_start,
            "service_stop": lambda: self._service_stop(str(params.get("mode", ""))),
            "dismiss_last": self._dismiss_last, "clear_history": self._clear_history,
            "set_theme": lambda: self._set_theme(str(params.get("theme", ""))),
            "open_log": lambda: self._open_log(str(params.get("log", ""))),
        }
        handler = handlers.get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown action: {name}"}
        try:
            result = handler()
        except OSError as exc:
            result = {"ok": False, "message": f"Could not complete that: {exc}"}
        self._invalidate("service", "importer", "usb", "counts", "card")
        return result

    def _pause(self) -> Dict[str, Any]:
        self.paths.install_dir.mkdir(parents=True, exist_ok=True)
        self.paths.pause_file.touch()
        return {"ok": True, "message": "Automatic imports paused. Running copies will finish safely."}

    def _resume(self) -> Dict[str, Any]:
        self.paths.pause_file.unlink(missing_ok=True)
        # A card may have been inserted while paused: forget the handled-state and re-check now.
        self.paths.handled_state_file.unlink(missing_ok=True)
        if self.paths.trigger_script.is_file():
            self._spawn(["/bin/bash", str(self.paths.trigger_script)])
        return {"ok": True, "message": "Automatic imports resumed. Checking for a card now."}

    def _open_log(self, log_id: str) -> Dict[str, Any]:
        # Only these ids are accepted; the page never sends a path.
        logs = {"import": ("Import log", self.paths.import_log_file),
                "trigger": ("Card detection log", self.paths.trigger_log_file),
                "google": ("Google Photos log", self.paths.google_log_file)}
        if log_id not in logs:
            return {"ok": False, "message": "Unknown log."}
        label, path = logs[log_id]
        if not path.is_file():
            return {"ok": False, "message": f"There is no {label.lower()} yet."}
        # Console follows the file live, like the old debug log window did.
        self._spawn(["/usr/bin/open", "-a", "Console", str(path)])
        return {"ok": True, "message": f"Opening the {label.lower()} in Console."}

    def theme(self) -> str:
        theme = str(self._dashboard_state().get("theme", "system"))
        return theme if theme in THEMES else "system"

    def _set_theme(self, theme: str) -> Dict[str, Any]:
        if theme not in THEMES:
            return {"ok": False, "message": f"Unknown appearance: {theme}"}
        self._save_dashboard_state(theme=theme)
        return {"ok": True, "theme": theme}

    def _import_card(self) -> Dict[str, Any]:
        if self.paths.pause_file.exists():
            return {"ok": False, "message": "Automatic imports are paused. Resume them first."}
        if self.mounted_card() is None:
            return {"ok": False, "message": "No camera card is mounted."}
        # Forget that this card was handled and let the trigger start a normal import;
        # only files that are new or changed since the last run are copied.
        self.paths.handled_state_file.unlink(missing_ok=True)
        if self.paths.trigger_script.is_file():
            self._spawn(["/bin/bash", str(self.paths.trigger_script)])
        return {"ok": True, "message": "Checking the card for new photos."}

    def _usb_backup(self) -> Dict[str, Any]:
        if not self.usb_mounted():
            return {"ok": False, "message": f"The backup drive is not mounted at {self.paths.usb_volume}."}
        self._spawn([str(self.paths.python), str(self.paths.backup_launcher)])
        return {"ok": True, "message": "USB backup started. Files that already match are skipped."}

    def _update_catalog(self) -> Dict[str, Any]:
        self._spawn([str(self.paths.python), str(self.paths.importer_script), "--update-catalog"])
        return {"ok": True, "message": "Catalog update started."}

    def _google_sync(self) -> Dict[str, Any]:
        if not self.paths.google_script.is_file():
            return {"ok": False, "message": "The Google Photos helper is missing. Reinstall Photo Offloader."}
        self._spawn([str(self.paths.python), str(self.paths.google_script), "--sync"])
        return {"ok": True, "message": "Google Photos sync started."}

    def _toggle(self, flag: Path, label: str) -> Dict[str, Any]:
        self.paths.install_dir.mkdir(parents=True, exist_ok=True)
        if flag.exists():
            flag.unlink()
            return {"ok": True, "message": f"{label} turned off.", "enabled": False}
        flag.touch()
        return {"ok": True, "message": f"{label} turned on.", "enabled": True}

    def _service_start(self) -> Dict[str, Any]:
        if not self.paths.service_script.is_file():
            return {"ok": False, "message": "The service script is missing. Re-run install.sh."}
        result = self._run(["/bin/bash", str(self.paths.service_script), "start"],
                           capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"ok": False, "message": (result.stderr or result.stdout).strip() or "Could not start."}
        return {"ok": True, "message": "Photo Offloader started."}

    def _service_stop(self, mode: str) -> Dict[str, Any]:
        if not self.paths.service_script.is_file():
            return {"ok": False, "message": "The service script is missing. Re-run install.sh."}
        if mode not in ("", "wait", "force"):
            return {"ok": False, "message": f"Unknown stop mode: {mode}"}
        if self.importer_running() and not mode:
            return {"ok": False, "needs_choice": True,
                    "message": "A transfer is in progress. Stop after it finishes, or cancel it now?"}
        # --keep-ui: the dashboard and menu bar stay open so you can start it again.
        argv = ["/bin/bash", str(self.paths.service_script), "stop", "--keep-ui"]
        if mode:
            argv.append(f"--{mode}")
        self._spawn(argv)
        return {"ok": True, "message": "Stopping Photo Offloader…" if not mode else
                ("Will stop when the transfer finishes." if mode == "wait" else "Canceling the transfer and stopping…")}

    def _dismiss_last(self) -> Dict[str, Any]:
        rows = catalog.list_import_history(limit=1)
        updates: Dict[str, Any] = {"dismissed_status_stamp": self._status_stamp(self.read_status())}
        if rows:
            updates["dismissed_history_id"] = int(rows[0]["id"])
        self._save_dashboard_state(**updates)
        return {"ok": True, "message": "Cleared."}

    def _clear_history(self) -> Dict[str, Any]:
        removed = catalog.clear_import_history()
        self._dismiss_last()
        return {"ok": True, "message": f"Cleared {removed} history entr{'y' if removed == 1 else 'ies'}."}
