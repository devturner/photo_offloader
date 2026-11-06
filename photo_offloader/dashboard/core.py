#!/usr/bin/env python3
"""Photo Offloader dashboard core: gathers everything the dashboard shows and runs its actions.

There is deliberately no GUI code here. ``DashboardCore.state()`` returns one JSON-able
dict that the web front end renders, and ``DashboardCore.act()`` performs the few things the
dashboard can do (pause, back up, sync, stop, ...). Everything that touches the machine
(launchctl, pgrep, subprocesses, the clock) is injectable so it can be tested.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from photo_offloader import catalog, google_photos, jsonfile, logs
from photo_offloader.dashboard import display
from photo_offloader.dashboard.paths import Paths
from photo_offloader.google_photos import parse_rclone_stats_line

SERVICE_LABEL = "com.user.photooffloader"
LAST_TRANSFER_TTL_SECONDS = 10 * 60  # completed/canceled results fade after this
STALE_STATUS_SECONDS = 20  # an "active" status this old, with no importer, is stale
SPEED_SAMPLES = 180  # three minutes at one poll a second; SPARK_MAX_POINTS in web/index.html
UPLOAD_FOLLOWS_IMPORT_SECONDS = 30 * 60  # an upload this soon after an import continues its steps
UPLOAD_START_GRACE_SECONDS = 30  # importer done, sync process still starting
FINISHING_GRACE_SECONDS = 30  # status says "complete", history row not written yet
EVENTS_LOG_MAX_BYTES = 512 * 1024
THEMES = ("system", "light", "dark")
SPEED_WINDOW_SAMPLES = 10


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
        self._upload_files: Optional[Tuple[int, int]] = None
        self._last_view: Optional[tuple] = None

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
                result = self._run(
                    [self.paths.launchctl, "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            return result.returncode == 0

        return self._cached("service", 3.0, probe)

    def _script_running(self, script: Path) -> bool:
        try:
            result = self._run(
                [self.paths.pgrep, "-f", str(script)], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return bool(result.stdout.strip())

    def importer_running(self) -> bool:
        return self._cached(
            "importer", 2.0, lambda: self._script_running(self.paths.importer_script)
        )

    def google_running(self) -> bool:
        return self._cached("google", 2.0, lambda: self._script_running(self.paths.google_script))

    def mounted_card(self) -> Optional[Path]:
        def probe() -> Optional[Path]:
            try:
                for volume in sorted(self.paths.volumes_root.iterdir()):
                    if not volume.is_symlink() and (volume / "DCIM").is_dir():
                        return volume
            except OSError:
                pass  # /Volumes can't be listed: show the same as having no card
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
        return self._cached(
            "usb", 3.0, lambda: self.paths.usb_volume.is_mount() or self.paths.usb_volume.is_dir()
        )

    # ---- persisted dismissals ("clear" for the last-transfer card) -----------------
    def _dashboard_state(self) -> Dict[str, Any]:
        return jsonfile.read_object(self.paths.dashboard_state_file)

    def _save_dashboard_state(self, **updates: Any) -> None:
        current = self._dashboard_state()
        current.update(updates)
        try:
            jsonfile.write_json(self.paths.dashboard_state_file, current, indent=2, sort_keys=True)
        except OSError as exc:
            logs.logger.warning(
                "Could not save dashboard settings to %s: %s", self.paths.dashboard_state_file, exc
            )

    # ---- importer status ----------------------------------------------------------------
    def read_status(self) -> Dict[str, Any]:
        return jsonfile.read_object(self.paths.status_file)

    @staticmethod
    def _status_stamp(status: Dict[str, Any]) -> str:
        return str(status.get("updated_at", "") or "")

    @staticmethod
    def _status_age(status: Dict[str, Any], now: float) -> Optional[float]:
        stamp = display.parse_timestamp(status.get("updated_at"))
        return None if stamp is None else max(now - stamp, 0.0)

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
            self._status_stamp(status) != self._dashboard_state().get("dismissed_status_stamp")
        )

    def _sample_speed(self, run_key: str, transferred: int, now: float) -> None:
        """Add one throughput sample to the graph; a new run (or a counter reset) starts a new graph."""
        last = self._last_sample
        if last and last[2] == run_key and transferred >= last[1] and now > last[0]:
            self._samples.append((transferred - last[1]) / (now - last[0]))
        elif last is None or last[2] != run_key or transferred < last[1]:
            self._samples.clear()
        self._last_sample = (now, transferred, run_key)

    def _end_graph(self, unless: Optional[str] = None) -> None:
        """Forget the speed graph, except one that belongs to the ``unless`` run."""
        if unless is None or not self._last_sample or self._last_sample[2] != unless:
            self._samples.clear()
            self._last_sample = None

    def _transfer_speed(self, status: Dict[str, Any], kind: str, stage: str, now: float) -> float:
        """Record this poll on the speed graph and return the speed to show."""
        if stage in ("copy", "usb"):
            transferred = int(status.get("bytes_transferred", 0) or 0)
            self._sample_speed(f"{kind}|{status.get('source', '')}", transferred, now)
        else:
            self._end_graph()
        # Average over ~10 s of polls: between copies (checking unchanged files) no bytes
        # move, and a shorter window would make the speed blink to nothing and back.
        recent = list(self._samples)[-SPEED_WINDOW_SAMPLES:]
        return (
            sum(recent) / len(recent) if recent else float(status.get("transfer_speed_bps", 0) or 0)
        )

    def _activity(
        self, status: Dict[str, Any], now: float, upload_planned: bool = False
    ) -> Optional[Dict[str, Any]]:
        state = str(status.get("state", "idle"))
        if state not in ("scanning", "copying") or self._stale(status, now):
            self._end_graph("upload")  # an upload running now keeps its own graph
            return None

        stage = display.activity_stage(status)
        done = int(status.get("bytes_done", 0) or 0)
        total = int(status.get("bytes_total", 0) or 0)
        kind = {"catalog": "catalog", "usb": "usb_backup"}.get(stage, "import")
        title, detail = display.activity_text(status, stage)
        new_only = stage == "copy" and int(status.get("expected_new_bytes") or 0) > 0
        if new_only:
            # The new files are the work left: already-saved files take no copying time.
            total = int(status["expected_new_bytes"])
            done = min(int(status.get("bytes_transferred", 0) or 0), total)
        # Without an estimate, one measure for checks and copies (skipped files count as done).
        percent = min(100.0, done / total * 100.0) if stage in ("copy", "usb") and total else None
        speed = self._transfer_speed(status, kind, stage, now)
        eta = 0.0 if new_only else float(status.get("eta_seconds", 0) or 0)
        if eta <= 0 and speed > 0 and total > done:
            eta = (total - done) / speed
        moving = stage in ("copy", "usb")
        return {
            "kind": kind,
            "stage": stage,
            "title": title,
            "detail": detail,
            "percent": percent,
            "indeterminate": percent is None,
            "bytes_done": done,
            "bytes_total": total,
            "files_done": int(status.get("files_done", 0) or 0),
            "files_total": int(status.get("files_total", 0) or 0),
            "new_files": int(status.get("new_media_files", status.get("new_files", 0)) or 0),
            "new_files_expected": status.get("expected_new_files") if stage == "copy" else None,
            "speed_bps": speed if moving else 0.0,
            "eta_seconds": eta if moving and total > done and eta > 0 else None,
            "elapsed_seconds": float(status.get("elapsed_seconds", 0) or 0),
            "source": str(status.get("source", "") or ""),
            "speed_history": [round(sample) for sample in self._samples],
            "stages": display.import_stages(status, stage, upload_planned),
        }

    # ---- catalog ------------------------------------------------------------------------
    def archive_counts(self) -> Optional[Dict[str, Any]]:
        def query() -> Optional[Dict[str, Any]]:
            if not self.paths.catalog_db.is_file():
                return {
                    "local": 0,
                    "usb": 0,
                    "uploaded": 0,
                    "local_updated": None,
                    "usb_updated": None,
                }
            try:
                with contextlib.closing(
                    sqlite3.connect(f"file:{self.paths.catalog_db}?mode=ro", uri=True, timeout=0.5)
                ) as db:
                    local_roots = [str(root.resolve()) for root in self.paths.local_archives]
                    usb_roots = [str(root.resolve()) for root in self.paths.usb_archives]
                    marks = ",".join("?" * len(local_roots))
                    local = db.execute(
                        f"SELECT COUNT(*) FROM file_locations WHERE root IN ({marks})", local_roots
                    ).fetchone()[0]
                    usb = db.execute(
                        "SELECT COUNT(*) FROM file_locations WHERE root = ?", (usb_roots[0],)
                    ).fetchone()[0]
                    try:
                        uploaded = db.execute(
                            "SELECT COUNT(*) FROM google_photos_uploads"
                        ).fetchone()[0]
                    except sqlite3.OperationalError:
                        uploaded = 0
                    try:
                        refreshes = {
                            row[0]: row[1]
                            for row in db.execute(
                                "SELECT root, last_updated FROM catalog_refreshes"
                            )
                        }
                    except sqlite3.OperationalError:
                        refreshes = {}
            except (OSError, sqlite3.Error):
                return None

            def newest(roots: List[str]) -> Optional[float]:
                stamps = [refreshes[root] for root in roots if root in refreshes]
                return max(stamps) if stamps else None

            return {
                "local": local,
                "usb": usb,
                "uploaded": uploaded,
                "local_updated": newest(local_roots),
                "usb_updated": newest(usb_roots),
            }

        return self._cached("counts", 5.0, query)

    # ---- Google Photos ----------------------------------------------------------------------
    def _google(self, now: float) -> Dict[str, Any]:
        status = jsonfile.read_object(self.paths.google_status_file)
        preferences = jsonfile.read_object(self.paths.preferences_file).get("google_photos", {})
        # rclone's own config is the source of truth: our status file is replaced on every
        # install and the preference is only written after a sync that uploaded something.
        configured = (
            bool(preferences.get("configured"))
            or self.rclone_remote_configured()
            or bool(status)
            and status.get("state") != "not_configured"
        )
        state = str(status.get("state", "")) or ("idle" if configured else "not_configured")
        info: Dict[str, Any] = {
            "configured": configured,
            "state": state,
            "message": str(status.get("message", "") or ""),
            "last_success": status.get("last_success"),
            "progress": None,
            "updated_at": display.parse_timestamp(status.get("updated_at")),
        }
        if state == "running" and not self.google_running():
            # The sync only writes status on state changes, so age says nothing; a missing
            # process does: it crashed or was killed mid-upload.
            info["state"], info["message"] = "interrupted", "The last upload was interrupted."
        elif state == "running":
            info["progress"] = self._upload_progress()
            info["started_at"] = display.parse_timestamp(
                status.get("started_at") or status.get("updated_at")
            )
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
        rclone_config = self.paths.rclone_config
        if rclone_config is None:
            return False

        remote = google_photos.remote_name()

        def probe() -> bool:
            try:
                with rclone_config.open(encoding="utf-8", errors="replace") as handle:
                    return any(line.strip() == f"[{remote}]" for line in handle)
            except OSError:
                return False

        return self._cached("rclone", 30.0, probe)

    def _google_setup_step(self) -> str:
        """The first thing missing, from local checks only: the page polls, so no network here."""
        installed = self._cached(
            "rclone_installed", 30.0, lambda: bool(google_photos.find_rclone())
        )
        return (
            f"Add an rclone remote named {google_photos.remote_name()}"
            if installed
            else "Install rclone to set this up"
        )

    def _upload_state_after(self, row: Dict[str, Any], google: Dict[str, Any], now: float) -> str:
        """Where the Google Photos step stands for the import in ``row``."""
        if not google["configured"] or row["outcome"] != "complete":
            return "skipped"
        if google["state"] == "running":
            return "active"
        if google["updated_at"] is None or google["updated_at"] < float(row["finished_at"]) - 1:
            # Not run since this import: the importer launches it as its last step.
            starting = now - float(row["finished_at"]) < UPLOAD_START_GRACE_SECONDS
            return "starting" if starting else "skipped"
        return "error" if google["state"] in ("failed", "error", "interrupted") else "done"

    def _upload_activity(
        self, google: Dict[str, Any], row: Optional[Dict[str, Any]], now: float
    ) -> Optional[Dict[str, Any]]:
        follows_import = (
            row is not None
            and row["kind"] == "import"
            and row["outcome"] == "complete"
            and now - float(row["finished_at"]) < UPLOAD_FOLLOWS_IMPORT_SECONDS
        )
        upload_state = self._upload_state_after(row, google, now) if follows_import else ""
        if google["state"] != "running" and upload_state != "starting":
            self._upload_files = None
            return None
        progress = google["progress"] or {}
        source = str(row.get("source") or "") if follows_import else ""
        files_done, files_total = progress.get("files_done"), progress.get("files_total")
        if files_total:
            what = f"{files_done:,} of {display.plural(files_total, 'file')}"
        else:
            what = "Finishing upload" if progress.get("finalizing") else "Preparing upload"
        done = int(display.parse_display_bytes(str(progress.get("transferred") or "")))
        total = int(display.parse_display_bytes(str(progress.get("total") or "")))
        if progress:
            self._sample_speed("upload", done, now)
        started = google.get("started_at")
        recent = list(self._samples)[-SPEED_WINDOW_SAMPLES:]
        speed = (
            sum(recent) / len(recent)
            if recent
            else display.parse_display_bytes(str(progress.get("speed") or "").replace("/s", ""))
        )
        starting = upload_state == "starting"
        return {
            "kind": "upload",
            "stage": "upload",
            "title": "Starting upload to Google Photos"
            if starting
            else "Uploading to Google Photos",
            "detail": " · ".join(part for part in (source, what) if part),
            "percent": progress.get("percent"),
            "indeterminate": not progress,
            "bytes_done": done,
            "bytes_total": total,
            "speed_bps": speed,
            "eta_seconds": self._eta_seconds(progress.get("eta")),
            "elapsed_seconds": max(now - started, 0.0) if started and not starting else 0.0,
            "files_done": files_done or 0,
            "files_total": files_total or 0,
            "new_files": 0,
            "source": "",
            "speed_history": [round(sample) for sample in self._samples],
            "stages": display.stages_after_import(row, "active") if follows_import else None,
        }

    def _finished(
        self, last: Optional[Dict[str, Any]], google: Dict[str, Any], now: float
    ) -> Optional[Dict[str, Any]]:
        """The end of an import, shown in the transfer panel until it fades or is dismissed."""
        if last is None or last["kind"] != "import":
            return None
        details = last.get("details") or {}
        new_files = (
            sum(int(v) for v in (details.get("new") or {}).values())
            if "new" in details
            else int(last["copied"])
        )
        new_bytes = int(details.get("new_bytes") or 0)
        duration = float(last["duration_seconds"])
        upload_state = self._upload_state_after(last, google, now)
        return {
            "title": last["title"],
            "detail": last["summary"],
            "tone": last["tone"],
            "percent": 100.0 if last["outcome"] == "complete" else None,
            "bytes_done": new_bytes,
            "bytes_total": new_bytes,
            "speed_bps": new_bytes / duration if duration > 0 else 0.0,
            "files_done": new_files,
            "files_total": int(last["files"]),
            "elapsed_seconds": duration,
            "stages": display.stages_after_import(
                last, "pending" if upload_state == "starting" else upload_state
            ),
        }

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
        parsed = next(
            (p for p in (parse_rclone_stats_line(line) for line in reversed(lines)) if p), None
        )
        if not parsed:
            return None
        percent = min(100.0, max(0.0, float(parsed["percent"])))
        if parsed.get("files_total"):
            self._upload_files = (int(parsed["files_done"]), int(parsed["files_total"]))
        elif percent >= 100.0 and self._upload_files:
            self._upload_files = (
                self._upload_files[1],
                self._upload_files[1],
            )  # rclone drops the count at 100%
        files_done, files_total = self._upload_files or (None, None)
        return {
            "percent": percent,
            "transferred": parsed.get("transferred"),
            "total": parsed.get("total"),
            "speed": parsed.get("speed"),
            "eta": parsed.get("eta"),
            "finalizing": percent >= 100.0,
            "files_done": files_done,
            "files_total": files_total,
        }

    # ---- last transfer & history --------------------------------------------------------

    def history(self, limit: int = 15) -> List[Dict[str, Any]]:
        return [display.describe_run(row) for row in catalog.list_import_history(limit=limit)]

    def _last_transfer(self, now: float, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        if int(row["id"]) <= int(self._dashboard_state().get("dismissed_history_id", 0) or 0):
            return None
        # Failures stay until dismissed; everything else fades after a while.
        if (
            row["outcome"] != "failed"
            and now - float(row["finished_at"]) > LAST_TRANSFER_TTL_SECONDS
        ):
            return None
        return display.describe_run(row)

    # ---- the one payload the front end renders -------------------------------------------
    def state(self) -> Dict[str, Any]:
        now = self._clock()
        status = self.read_status()
        running = self.service_running()
        paused = self.paths.pause_file.exists()
        card = self.mounted_card()
        google = self._google(now)
        rows = catalog.list_import_history(limit=1)
        latest = rows[0] if rows else None
        import_activity = self._activity(status, now, google["configured"]) or self._finishing(
            status, latest, google, now
        )
        interrupted = self._interrupted(status, now)
        last = self._last_transfer(now, latest)
        counts = self.archive_counts()
        usb_mounted = self.usb_mounted()
        busy = import_activity is not None  # the importer is working (uploads don't block it)
        activity = import_activity or self._upload_activity(google, latest, now)

        handled = self.card_handled(card)
        headline = display.headline(
            running, paused, card, activity, interrupted, last, status, google, handled
        )
        payload = {
            "generated_at": now,
            "version": jsonfile.read_object(self.paths.install_dir / "build_info.json").get(
                "version", ""
            ),
            "service": {"running": running, "paused": paused},
            "card": {"mounted": card is not None, "name": card.name if card else None},
            "headline": headline,
            "busy": busy,
            "activity": activity,
            "interrupted": interrupted,
            "last_transfer": last,
            "finished": None if activity else self._finished(last, google, now),
            "pipeline": display.pipeline(
                card,
                import_activity,
                counts,
                usb_mounted,
                google,
                busy,
                handled and running and not paused,
                "" if google["configured"] else self._google_setup_step(),
            ),
            "upload": google["progress"],
            "settings": {
                "auto_eject": self.paths.auto_eject_file.exists(),
                "terminal_output": self.paths.terminal_output_file.exists(),
                "theme": self.theme(),
            },
        }
        self._log_view(now, payload, status, google)
        return payload

    def _log_view(
        self, now: float, payload: Dict[str, Any], status: Dict[str, Any], google: Dict[str, Any]
    ) -> None:
        """Append a line to the events log whenever what the dashboard shows changes."""
        head, activity, finished = payload["headline"], payload["activity"], payload["finished"]
        view = (
            head["tone"],
            head["title"],
            activity and activity["kind"],
            activity and activity["title"],
            finished and finished["title"],
        )
        if view == self._last_view:
            return
        self._last_view = view
        shown = (
            f"activity={activity['kind']}:{activity['title']!r}"
            if activity
            else (f"summary={finished['title']!r}" if finished else "resting")
        )
        line = (
            f"{datetime.fromtimestamp(now).isoformat(timespec='milliseconds')} "
            f"headline={head['tone']}:{head['title']!r} {shown} "
            f"importer={status.get('state', '-')}/{status.get('stage', '-')} "
            f"importer_alive={self.importer_running()} google={google['state']} "
            f"card={payload['card']['name'] or '-'}\n"
        )
        try:
            target = self.paths.events_log_file
            target.parent.mkdir(parents=True, exist_ok=True)
            logs.rotate_log_file(target, EVENTS_LOG_MAX_BYTES, 1)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass  # a diagnostic log must never break the dashboard

    def _finishing(
        self,
        status: Dict[str, Any],
        latest: Optional[Dict[str, Any]],
        google: Dict[str, Any],
        now: float,
    ) -> Optional[Dict[str, Any]]:
        """The importer's last moments: status says "complete" but the run is not in history yet.

        Without this the panel fell back to the card headline between copying and the upload.
        """
        stamp = display.parse_timestamp(status.get("updated_at"))
        if (
            status.get("state") != "complete"
            or "usb_planned" not in status
            or stamp is None
            or now - stamp > FINISHING_GRACE_SECONDS
            or (latest is not None and float(latest["finished_at"]) >= stamp - 1)
        ):
            return None
        stages = display.import_stages(status, "upload", google["configured"]) or []
        if stages and google["configured"]:
            stages[-1]["state"] = "pending"
        source = str(status.get("source", "") or "").rstrip("/").rsplit("/", 1)[-1]
        total = int(status.get("bytes_total", 0) or 0)
        return {
            "kind": "import",
            "stage": "finishing",
            "title": "Finishing import",
            "detail": "",
            "percent": 100.0,
            "indeterminate": False,
            "bytes_done": total,
            "bytes_total": total,
            "files_done": int(status.get("files_done", 0) or 0),
            "files_total": int(status.get("files_total", 0) or 0),
            "new_files": int(status.get("new_files", 0) or 0),
            "speed_bps": 0.0,
            "eta_seconds": None,
            "elapsed_seconds": float(status.get("elapsed_seconds", 0) or 0),
            "source": source,
            "speed_history": [round(sample) for sample in self._samples],
            "stages": stages,
        }

    # ---- actions ---------------------------------------------------------------------------
    def _spawn(self, argv: List[str]) -> None:
        self._popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def act(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        handlers: Dict[str, Callable[[], Dict[str, Any]]] = {
            "pause": self._pause,
            "resume": self._resume,
            "import_card": self._import_card,
            "usb_backup": self._usb_backup,
            "eject_card": self._eject_card,
            "eject_usb": self._eject_usb,
            "update_catalog": self._update_catalog,
            "google_sync": self._google_sync,
            "toggle_auto_eject": lambda: self._toggle(self.paths.auto_eject_file, "Auto-eject"),
            "toggle_terminal_output": lambda: self._toggle(
                self.paths.terminal_output_file, "Terminal windows"
            ),
            "service_start": self._service_start,
            "service_stop": lambda: self._service_stop(str(params.get("mode", ""))),
            "dismiss_last": self._dismiss_last,
            "clear_history": self._clear_history,
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
        return {
            "ok": True,
            "message": "Automatic imports paused. Running copies will finish safely.",
        }

    def _resume(self) -> Dict[str, Any]:
        self.paths.pause_file.unlink(missing_ok=True)
        # A card may have been inserted while paused: forget the handled-state and re-check now.
        self.paths.handled_state_file.unlink(missing_ok=True)
        if self.paths.trigger_script.is_file():
            self._spawn(["/bin/bash", str(self.paths.trigger_script)])
        return {"ok": True, "message": "Automatic imports resumed. Checking for a card now."}

    def _open_log(self, log_id: str) -> Dict[str, Any]:
        # Only these ids are accepted; the page never sends a path.
        logs = {
            "import": ("Import log", self.paths.import_log_file),
            "trigger": ("Card detection log", self.paths.trigger_log_file),
            "google": ("Google Photos log", self.paths.google_log_file),
            "dashboard": ("Dashboard log", self.paths.events_log_file),
        }
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

    def _eject(self, volume: Path, what: str) -> Dict[str, Any]:
        result = self._run(
            ["/usr/sbin/diskutil", "eject", str(volume)], capture_output=True, text=True, timeout=60
        )
        self._invalidate("card", "usb")
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "").strip() or "it may be in use"
            return {"ok": False, "message": f"Could not eject the {what}: {reason}"}
        return {"ok": True, "message": f"{volume.name} was ejected. You can remove it now."}

    def _eject_card(self) -> Dict[str, Any]:
        card = self.mounted_card()
        if card is None:
            return {"ok": False, "message": "No camera card is mounted."}
        if self.importer_running():
            return {
                "ok": False,
                "message": "The card is being imported. Eject it when the import has finished.",
            }
        return self._eject(card, "card")

    def _eject_usb(self) -> Dict[str, Any]:
        if not self.usb_mounted():
            return {"ok": False, "message": "The backup drive is not connected."}
        if self.importer_running():
            return {
                "ok": False,
                "message": "A transfer is using the drive. Eject it when it has finished.",
            }
        return self._eject(self.paths.usb_volume, "backup drive")

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
            return {
                "ok": False,
                "message": f"The backup drive is not mounted at {self.paths.usb_volume}.",
            }
        self._spawn([str(self.paths.python), str(self.paths.backup_launcher)])
        return {"ok": True, "message": "USB backup started. Files that already match are skipped."}

    def _update_catalog(self) -> Dict[str, Any]:
        self._spawn([str(self.paths.python), str(self.paths.importer_script), "--update-catalog"])
        return {"ok": True, "message": "Catalog update started."}

    def _google_sync(self) -> Dict[str, Any]:
        if not self.paths.google_script.is_file():
            return {
                "ok": False,
                "message": "The Google Photos helper is missing. Reinstall Photo Offloader.",
            }
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
        result = self._run(
            ["/bin/bash", str(self.paths.service_script), "start"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "message": (result.stderr or result.stdout).strip() or "Could not start.",
            }
        return {"ok": True, "message": "Photo Offloader started."}

    def _service_stop(self, mode: str) -> Dict[str, Any]:
        if not self.paths.service_script.is_file():
            return {"ok": False, "message": "The service script is missing. Re-run install.sh."}
        if mode not in ("", "wait", "force"):
            return {"ok": False, "message": f"Unknown stop mode: {mode}"}
        if self.importer_running() and not mode:
            return {
                "ok": False,
                "needs_choice": True,
                "message": "A transfer is in progress. Stop after it finishes, or cancel it now?",
            }
        # --keep-ui: the dashboard and menu bar stay open so you can start it again.
        argv = ["/bin/bash", str(self.paths.service_script), "stop", "--keep-ui"]
        if mode:
            argv.append(f"--{mode}")
        self._spawn(argv)
        return {
            "ok": True,
            "message": "Stopping Photo Offloader…"
            if not mode
            else (
                "Will stop when the transfer finishes."
                if mode == "wait"
                else "Canceling the transfer and stopping…"
            ),
        }

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
        return {
            "ok": True,
            "message": f"Cleared {removed} history entr{'y' if removed == 1 else 'ies'}.",
        }
