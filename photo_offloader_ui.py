#!/usr/bin/env python3
"""Small native Tk dashboard for Photo Offloader progress."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict

from google_photos_sync import parse_rclone_stats_line

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
STATUS_FILE = Path(os.environ.get("PHOTO_OFFLOADER_STATUS_FILE", str(INSTALL_DIR / "status.json")))
PAUSE_FILE = Path(os.environ.get("PHOTO_OFFLOADER_PAUSE_FILE", str(INSTALL_DIR / ".paused")))
TERMINAL_OUTPUT_FILE = INSTALL_DIR / ".terminal-output-enabled"
BACKUP_LAUNCHER_SCRIPT = INSTALL_DIR / "photo_offloader_backup_launcher.py"
IMPORTER_SCRIPT = INSTALL_DIR / "camera_offloader_v2.py"
GOOGLE_PHOTOS_SCRIPT = INSTALL_DIR / "google_photos_sync.py"
GOOGLE_PHOTOS_STATUS = INSTALL_DIR / "google_photos_status.json"
PYTHON = INSTALL_DIR / "venv" / "bin" / "python3"
USB_VOLUME = Path("/Volumes/Photos")
LOCAL_ARCHIVES = (Path.home() / "Pictures" / "CameraImports", Path.home() / "Pictures" / "CameraImportsRaw")
USB_ARCHIVES = (USB_VOLUME / "CameraImports",)
CATALOG_DB = Path.home() / "Library" / "Application Support" / "PhotoOffloaderData" / "catalog.sqlite3"
TRIGGER_LOG = Path.home() / ".camera_transfer_trigger.log"
IMPORT_LOG = Path.home() / ".camera_transfer.log"


def format_bytes(value: float) -> str:
    amount = max(float(value), 0.0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def format_duration(value: float) -> str:
    seconds = max(int(value or 0), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def read_status() -> Dict[str, Any]:
    try:
        with STATUS_FILE.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Dashboard:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Photo Offloader")
        # Leave enough vertical space for the live graph and both action buttons.
        # The previous 610x600 default clipped the bottom controls on macOS.
        self.root.geometry("900x900")
        self.root.minsize(760, 780)
        self.root.configure(bg="#101827")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#101827")
        style.configure("Card.TFrame", background="#1b2638")
        style.configure("Title.TLabel", background="#101827", foreground="#f3f6fb",
                        font=("SF Pro Display", 22, "bold"))
        style.configure("Subtitle.TLabel", background="#101827", foreground="#9caec4",
                        font=("SF Pro Text", 10))
        style.configure("CardTitle.TLabel", background="#1b2638", foreground="#9caec4",
                        font=("SF Pro Text", 10))
        style.configure("Metric.TLabel", background="#1b2638", foreground="#f3f6fb",
                        font=("SF Pro Display", 18, "bold"))
        style.configure("Body.TLabel", background="#101827", foreground="#d7e0ed",
                        font=("SF Pro Text", 10))
        style.configure("File.TLabel", background="#1b2638", foreground="#f3f6fb",
                        font=("SF Pro Text", 11, "bold"))
        style.configure("Horizontal.TProgressbar", troughcolor="#303d50",
                        background="#53c7a6", thickness=9)

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Photo Offloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Camera ingest • live transfer monitor",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(2, 18))

        metrics = ttk.Frame(outer)
        metrics.pack(fill="x", pady=(0, 14))
        self.speed_var = tk.StringVar(value="—")
        self.eta_var = tk.StringVar(value="ETA —")
        self.upload_speed_var = tk.StringVar(value="—")
        self.upload_eta_var = tk.StringVar(value="ETA —")
        self.upload_progress_var = tk.StringVar(value="Waiting for upload")
        self.upload_percent_var = tk.DoubleVar(value=0.0)
        self.progress_var = tk.StringVar(value="Waiting for a camera card")
        self.speed_history = deque([0.0] * 60, maxlen=60)
        self.last_sample_time = time.monotonic()
        self.last_transferred_bytes = 0

        speed_card = ttk.Frame(metrics, style="Card.TFrame", padding=14)
        speed_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        speed_header = ttk.Frame(speed_card, style="Card.TFrame")
        speed_header.pack(fill="x")
        ttk.Label(speed_header, text="COPY SPEED", style="CardTitle.TLabel").pack(side="left", anchor="w")
        ttk.Label(speed_header, textvariable=self.eta_var, style="CardTitle.TLabel").pack(side="right", anchor="e")
        ttk.Label(speed_card, textvariable=self.speed_var, style="Metric.TLabel").pack(anchor="w", pady=(5, 0))
        self.graph = tk.Canvas(speed_card, height=82, bg="#1b2638", highlightthickness=0)
        self.graph.pack(fill="x", pady=(5, 0))

        upload_card = ttk.Frame(metrics, style="Card.TFrame", padding=14)
        upload_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        upload_header = ttk.Frame(upload_card, style="Card.TFrame")
        upload_header.pack(fill="x")
        ttk.Label(upload_header, text="GOOGLE PHOTOS UPLOAD", style="CardTitle.TLabel").pack(side="left", anchor="w")
        ttk.Label(upload_header, textvariable=self.upload_eta_var, style="CardTitle.TLabel").pack(side="right", anchor="e")
        ttk.Label(upload_card, textvariable=self.upload_speed_var, style="Metric.TLabel").pack(anchor="w", pady=(5, 0))
        self.upload_progress = ttk.Progressbar(
            upload_card, mode="determinate", maximum=100, variable=self.upload_percent_var
        )
        self.upload_progress.pack(fill="x", pady=(10, 3))
        ttk.Label(upload_card, textvariable=self.upload_progress_var, style="CardTitle.TLabel").pack(anchor="w")
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)

        detail = ttk.Frame(outer, style="Card.TFrame", padding=16)
        detail.pack(fill="x", pady=(0, 12))
        ttk.Label(detail, text="CURRENT FILE", style="CardTitle.TLabel").pack(anchor="w")
        self.file_var = tk.StringVar(value="No active import")
        ttk.Label(detail, textvariable=self.file_var, style="File.TLabel").pack(anchor="w", pady=(5, 8))
        self.source_var = tk.StringVar(value="Source: —")
        ttk.Label(detail, textvariable=self.source_var, style="CardTitle.TLabel").pack(anchor="w")

        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill="x", pady=(4, 7))
        ttk.Label(outer, textvariable=self.progress_var, style="Body.TLabel").pack(anchor="w")
        self.message_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.message_var, style="Subtitle.TLabel",
                  wraplength=550).pack(anchor="w", pady=(9, 0))

        ttk.Label(outer, text="AUTOMATIC IMPORT STATUS", style="Subtitle.TLabel").pack(anchor="w", pady=(12, 4))
        auto_status = ttk.Frame(outer, style="Card.TFrame", padding=10)
        auto_status.pack(fill="x")
        self.auto_status_var = tk.StringVar(value="Waiting for trigger activity…")
        ttk.Label(auto_status, textvariable=self.auto_status_var, style="Body.TLabel",
                  wraplength=760).pack(anchor="w")
        status_actions = ttk.Frame(auto_status, style="Card.TFrame")
        status_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(status_actions, text="Toggle Live Debug Log", command=self.toggle_debug_log).pack(side="left")
        self.terminal_output_button = ttk.Button(
            status_actions, text=self.terminal_output_label(), command=self.toggle_terminal_output
        )
        self.terminal_output_button.pack(side="left", padx=(8, 0))
        self.google_photos_var = tk.StringVar(value=self.google_photos_summary())
        ttk.Label(auto_status, textvariable=self.google_photos_var, style="Body.TLabel", wraplength=760).pack(anchor="w", pady=(8, 0))
        ttk.Button(status_actions, text="Sync Google Photos", command=self.start_google_photos_sync).pack(side="left", padx=(8, 0))
        self.debug_window = None
        self.debug_text = None
        self.debug_refresh_job = None

        ttk.Label(outer, text="DATABASE STATUS", style="Subtitle.TLabel").pack(anchor="w", pady=(12, 4))
        catalog_status = ttk.Frame(outer, style="Card.TFrame", padding=10)
        catalog_status.pack(fill="x")
        self.local_catalog_var = tk.StringVar(value="Checking catalog…")
        self.usb_catalog_var = tk.StringVar(value="Checking catalog…")
        ttk.Label(catalog_status, textvariable=self.local_catalog_var, style="Body.TLabel").pack(anchor="w")
        ttk.Label(catalog_status, textvariable=self.usb_catalog_var, style="Body.TLabel").pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Copy to USB Drive", command=self.start_usb_backup).pack(side="left")
        ttk.Button(actions, text="Update File Catalog", command=self.start_catalog_update).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(outer, text="Catalog update scans the Mac archive and mounted USB drive; it reuses valid metadata and does not hash every file.",
                  style="Subtitle.TLabel", wraplength=550).pack(anchor="w", pady=(6, 0))

        self.root.after(500, self.refresh)
        self.root.mainloop()

    @staticmethod
    def terminal_output_label() -> str:
        return ("Disable Terminal Output" if TERMINAL_OUTPUT_FILE.exists()
                else "Enable Terminal Output")

    def toggle_terminal_output(self) -> None:
        try:
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            if TERMINAL_OUTPUT_FILE.exists():
                TERMINAL_OUTPUT_FILE.unlink()
                self.message_var.set("Terminal windows disabled. Imports will run in the background.")
            else:
                TERMINAL_OUTPUT_FILE.touch()
                self.message_var.set("Terminal windows enabled for future automatic imports.")
            self.terminal_output_button.configure(text=self.terminal_output_label())
        except OSError as exc:
            self.message_var.set(f"Could not change terminal output setting: {exc}")

    def trigger_status_summary(self) -> str:
        try:
            line = next((line for line in reversed(TRIGGER_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()) if line.strip()), "")
            if not line:
                return "Trigger: no log entries yet. If this remains blank, the launchd trigger may not be running."
            try:
                stamp = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                age = max(int(time.time() - stamp.timestamp()), 0)
                age_text = f"{format_duration(age)} ago"
            except (ValueError, OSError):
                age_text = "time unknown"
            return f"Trigger log • {age_text} • {line}"
        except OSError:
            return "Trigger: log file has not been created yet. Check the Photo Offloader LaunchAgent."

    def toggle_debug_log(self) -> None:
        if self.debug_window is not None and self.debug_window.winfo_exists():
            self.debug_window.destroy()
            self.debug_window = None
            self.debug_text = None
            if self.debug_refresh_job is not None:
                self.root.after_cancel(self.debug_refresh_job)
                self.debug_refresh_job = None
            return
        window = tk.Toplevel(self.root)
        window.title("Photo Offloader — Live Debug Log")
        window.geometry("1000x560")
        window.minsize(700, 350)
        frame = ttk.Frame(window, padding=8)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none", state="disabled", font=("Menlo", 10))
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.debug_window = window
        self.debug_text = text
        window.protocol("WM_DELETE_WINDOW", self.toggle_debug_log)
        self.refresh_debug_log()

    def refresh_debug_log(self) -> None:
        if self.debug_window is None or not self.debug_window.winfo_exists() or self.debug_text is None:
            return
        sections = []
        for label, path in (
            ("TRIGGER LOG", TRIGGER_LOG),
            ("IMPORTER LOG", IMPORT_LOG),
            ("DASHBOARD LOG", INSTALL_DIR / "dashboard.log"),
            ("MENUBAR ERROR LOG", INSTALL_DIR / "menubar-error.log"),
        ):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
                sections.append(f"===== {label}: {path} =====\n" + "\n".join(lines))
            except OSError as exc:
                sections.append(f"===== {label}: {path} =====\n(unavailable: {exc})")
        self.debug_text.configure(state="normal")
        self.debug_text.delete("1.0", "end")
        self.debug_text.insert("end", "\n\n".join(sections))
        self.debug_text.see("end")
        self.debug_text.configure(state="disabled")
        self.debug_refresh_job = self.root.after(1000, self.refresh_debug_log)

    def refresh_google_photos_progress(self) -> None:
        """Read the latest rclone stats line without interfering with the active upload."""
        try:
            with GOOGLE_PHOTOS_STATUS.open(encoding="utf-8") as handle:
                status = json.load(handle)
        except (OSError, ValueError):
            status = {}
        state = str(status.get("state", "unknown"))

        if state == "complete":
            self.upload_speed_var.set("—")
            self.upload_eta_var.set("ETA Complete")
            self.upload_percent_var.set(100.0)
            self.upload_progress_var.set("Upload complete")
            return
        if state != "running":
            self.upload_speed_var.set("—")
            self.upload_eta_var.set("ETA —")
            self.upload_percent_var.set(0.0)
            self.upload_progress_var.set("Waiting for upload")
            return

        try:
            with LOG_PATH.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(size - 65536, 0))
                if size > 65536:
                    handle.readline()
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            lines = []

        progress = next(
            (parsed for line in reversed(lines) if (parsed := parse_rclone_stats_line(line))),
            None,
        )
        if not progress:
            self.upload_speed_var.set("—")
            self.upload_eta_var.set("ETA Estimating…")
            self.upload_progress_var.set("Preparing upload…")
            return

        percent = float(progress["percent"])
        self.upload_speed_var.set(f"{progress['speed']}")
        self.upload_eta_var.set(f"ETA {progress['eta']}")
        self.upload_percent_var.set(min(100.0, max(0.0, percent)))
        self.upload_progress_var.set(
            f"{percent:.0f}% • {progress['transferred']} of {progress['total']} uploaded"
        )

    def google_photos_summary(self) -> str:
        try:
            payload = json.loads(GOOGLE_PHOTOS_STATUS.read_text(encoding="utf-8"))
            state = str(payload.get("state", "unknown")).replace("_", " ").title()
            message = str(payload.get("message", ""))
            last_success = payload.get("last_success")
            suffix = f" • Last success: {last_success}" if last_success else ""
            return f"Google Photos: {state} • {message}{suffix}"
        except (OSError, ValueError):
            return "Google Photos: Not configured yet • Install rclone and configure a remote named gphotos."

    def start_google_photos_sync(self) -> None:
        if not GOOGLE_PHOTOS_SCRIPT.is_file():
            self.message_var.set(f"Google Photos helper is missing: {GOOGLE_PHOTOS_SCRIPT}. Reinstall Photo Offloader.")
            return
        try:
            subprocess.Popen([str(PYTHON), str(GOOGLE_PHOTOS_SCRIPT), "--sync"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.message_var.set("Started Google Photos upload. Status will update here.")
        except OSError as exc:
            self.message_var.set(f"Could not start Google Photos upload: {exc}")

    def start_usb_backup(self) -> None:
        if not USB_VOLUME.is_mount():
            self.message_var.set("USB backup drive is not mounted at /Volumes/Photos.")
            return
        try:
            subprocess.Popen([str(PYTHON), str(BACKUP_LAUNCHER_SCRIPT)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.message_var.set("Started USB backup. Existing matching files will be skipped.")
        except OSError as exc:
            self.message_var.set(f"Could not start USB backup: {exc}")

    def refresh_catalog_status(self, status: Dict[str, Any]) -> None:
        state = str(status.get("state", "idle"))
        phase = str(status.get("phase", ""))
        current_path = str(status.get("current_path", "") or "")
        catalog_root = str(status.get("catalog_root", "") or "")
        catalog_running = state == "scanning" and phase == "catalog-update"

        try:
            if not CATALOG_DB.is_file():
                local_count = usb_count = 0
                refreshes = {}
            else:
                with sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True, timeout=0.5) as db:
                    local_roots = tuple(str(root.resolve()) for root in LOCAL_ARCHIVES)
                    usb_roots = tuple(str(root.resolve()) for root in USB_ARCHIVES)
                    local_count = db.execute(
                        "SELECT COUNT(*) FROM file_locations WHERE root IN (?, ?)", local_roots
                    ).fetchone()[0]
                    usb_count = db.execute(
                        "SELECT COUNT(*) FROM file_locations WHERE root = ?", (usb_roots[0],)
                    ).fetchone()[0]
                    try:
                        refreshes = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
                            "SELECT root, last_updated, files_scanned, status FROM catalog_refreshes"
                        )}
                    except sqlite3.OperationalError:
                        refreshes = {}
        except (OSError, sqlite3.Error) as exc:
            self.local_catalog_var.set(f"Internal SSD database: Status unavailable ({exc})")
            self.usb_catalog_var.set("Backup USB database: Status unavailable")
            return

        def line(label: str, roots: tuple[Path, ...], count: int) -> str:
            keys = tuple(str(root.resolve()) for root in roots)
            if catalog_running and any(
                catalog_root == key or current_path == key or current_path.startswith(key + os.sep)
                for key in keys
            ):
                scanned = int(status.get("catalog_scanned", 0) or 0)
                return f"{label}: Updating • {scanned:,} files scanned • {count:,} indexed so far"
            infos = [refreshes[key] for key in keys if key in refreshes]
            if infos:
                updated = max(info[0] for info in infos)
                scanned = sum(info[1] for info in infos)
                refresh_state = "complete" if all(info[2] == "complete" for info in infos) else "in progress"
                when = datetime.fromtimestamp(updated).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                return f"{label}: {refresh_state.title()} • Last updated {when} • {scanned:,} files scanned • {count:,} indexed"
            if count:
                return f"{label}: Catalog exists • Last refresh time not recorded • {count:,} indexed"
            if label == "Backup USB" and not USB_VOLUME.is_dir():
                return f"{label}: Drive not mounted • Previous catalog records are preserved"
            return f"{label}: Not yet updated"

        self.local_catalog_var.set(line("Internal SSD", LOCAL_ARCHIVES, local_count))
        self.usb_catalog_var.set(line("Backup USB", USB_ARCHIVES, usb_count))

    def start_catalog_update(self) -> None:
        try:
            subprocess.Popen([str(PYTHON), str(IMPORTER_SCRIPT), "--update-catalog"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.message_var.set("Started catalog update. Progress will appear here.")
        except OSError as exc:
            self.message_var.set(f"Could not update catalog: {exc}")

    def draw_speed_graph(self) -> None:
        self.graph.delete("all")
        width = max(self.graph.winfo_width(), 200)
        height = max(self.graph.winfo_height(), 80)
        left, right, top, bottom = 8, width - 8, 8, height - 18
        for fraction in (0.25, 0.5, 0.75, 1.0):
            y = bottom - (bottom - top) * fraction
            self.graph.create_line(left, y, right, y, fill="#34445b", dash=(2, 3))
        values = list(self.speed_history)
        peak = max(values, default=0.0)
        if peak <= 0:
            self.graph.create_text(left + 4, top + 5, anchor="nw",
                                   text="Waiting for transfer data…", fill="#9caec4",
                                   font=("SF Pro Text", 9))
            return
        points = []
        span_x = right - left
        span_y = bottom - top
        for index, value in enumerate(values):
            x = left + span_x * index / max(len(values) - 1, 1)
            y = bottom - span_y * (value / peak)
            points.extend((x, y))
        if len(points) >= 4:
            self.graph.create_line(*points, fill="#53c7a6", width=2, smooth=True)
        self.graph.create_text(right - 2, top + 2, anchor="ne",
                               text=f"Peak {format_bytes(peak)}/s", fill="#9caec4",
                               font=("SF Pro Text", 9))

    def catalog_eta(self, status: Dict[str, Any]) -> str:
        """Estimate remaining catalog time only when a meaningful scan rate exists."""
        stage = str(status.get("catalog_stage", "") or "")
        if stage == "Reading metadata batches":
            # ExifTool runs as a batch with no per-file progress callback.
            return "Reading metadata…"
        if stage == "Preparing metadata":
            return "Preparing…"
        if stage not in ("Finding files needing metadata", "Indexing files"):
            return "Estimating…"

        now = time.monotonic()
        marker = (status.get("run_id"), status.get("catalog_root"), stage)
        if getattr(self, "_catalog_eta_marker", None) != marker:
            self._catalog_eta_marker = marker
            self._catalog_eta_started = now
            self._catalog_eta_progress = 0
        elapsed = max(now - getattr(self, "_catalog_eta_started", now), 0.0)

        if stage == "Finding files needing metadata":
            progress = int(status.get("catalog_discovered", 0) or 0)
            estimate_total = int(status.get("catalog_estimated_total", 0) or 0)
        else:
            progress = int(status.get("catalog_root_scanned", 0) or 0)
            estimate_total = int(status.get("catalog_root_estimated_total", 0) or 0)

        previous_progress = int(getattr(self, "_catalog_eta_progress", 0) or 0)
        if progress < previous_progress:
            self._catalog_eta_started = now
            elapsed = 0.0
        self._catalog_eta_progress = progress
        if progress <= 0 or estimate_total <= progress or elapsed < 2:
            return "Estimating…"
        rate = progress / elapsed
        if rate <= 0:
            return "Estimating…"
        remaining = max(estimate_total - progress, 0) / rate
        return format_duration(remaining)

    def refresh(self) -> None:
        status = read_status()
        state = str(status.get("state", "idle"))
        done = int(status.get("bytes_done", 0) or 0)
        total = int(status.get("bytes_total", 0) or 0)
        average_speed = float(status.get("transfer_speed_bps", 0) or 0)
        transferred = int(status.get("bytes_transferred", 0) or 0)
        now = time.monotonic()
        sample_elapsed = max(now - self.last_sample_time, 0.001)
        if transferred >= self.last_transferred_bytes:
            speed = (transferred - self.last_transferred_bytes) / sample_elapsed
        else:
            # A new copy stage resets its byte counter; don't draw a false spike.
            speed = 0.0
        self.last_sample_time = now
        self.last_transferred_bytes = transferred
        eta = float(status.get("eta_seconds", 0) or 0)
        self.speed_history.append(max(speed, 0.0))
        self.draw_speed_graph()

        display_speed = speed if speed > 0 else average_speed if state == "copying" else 0
        decision_phase = state == "scanning" and status.get("phase") == "copy-decision"
        if decision_phase:
            checked = int(status.get("decision_files_checked", 0) or 0)
            decision_total = int(status.get("decision_files_total", 0) or 0)
            self.speed_var.set(f"{checked / max(now - getattr(self, '_decision_started', now), 1):.1f} files/s"
                               if checked else "Checking files…")
            if not getattr(self, "_decision_started", 0) or checked < getattr(self, "_decision_last_checked", 0):
                self._decision_started = now
                self._decision_last_checked = checked
            elapsed_decision = max(now - getattr(self, "_decision_started", now), 0.1)
            if checked > 0 and decision_total > checked:
                self.eta_var.set(format_duration((decision_total - checked) / (checked / elapsed_decision)))
            elif decision_total and checked >= decision_total:
                self.eta_var.set("Starting copy…")
            else:
                self.eta_var.set("Estimating…")
            self._decision_last_checked = checked
        else:
            self.speed_var.set(f"{format_bytes(display_speed)}/s" if display_speed > 0 else "—")
        if decision_phase:
            pass
        elif state == "copying" and total > done and speed > 0:
            self.eta_var.set(format_duration(eta))
        elif state == "complete":
            self.eta_var.set("Complete")
        elif state == "failed":
            self.eta_var.set("Stopped")
        elif state == "canceled":
            self.eta_var.set("Canceled")
        elif state == "scanning" and status.get("phase") == "catalog-update":
            self.eta_var.set(self.catalog_eta(status))
        else:
            self.eta_var.set("Estimating…" if state == "scanning" else "—")

        catalog_running = state == "scanning" and status.get("phase") == "catalog-update"
        if decision_phase:
            if getattr(self, "_catalog_progress_animating", False):
                self.progress.stop()
                self._catalog_progress_animating = False
            self.progress.configure(mode="determinate")
            decision_total = int(status.get("decision_files_total", 0) or 0)
            decision_checked = int(status.get("decision_files_checked", 0) or 0)
            self.progress.configure(value=min(100.0, decision_checked / decision_total * 100.0)
                                    if decision_total else 0.0)
        elif catalog_running:
            # Catalog scans have no reliable total byte count. Use an indeterminate
            # bar instead of snapping a determinate bar back to zero between roots.
            self.progress.configure(mode="indeterminate")
            if not getattr(self, "_catalog_progress_animating", False):
                self.progress.start(12)
                self._catalog_progress_animating = True
        else:
            if getattr(self, "_catalog_progress_animating", False):
                self.progress.stop()
                self._catalog_progress_animating = False
            self.progress.configure(mode="determinate")
            percent = min(100.0, (done / total * 100.0) if total else 0.0)
            self.progress.configure(value=percent)
        if decision_phase:
            self.progress_var.set(
                f"Checking existing files  •  {int(status.get('decision_files_checked', 0) or 0)} "
                f"of {int(status.get('decision_files_total', 0) or 0)}"
            )
        elif state == "copying":
            self.progress_var.set(f"{format_bytes(done)} of {format_bytes(total)} processed  •  {percent:.0f}%")
        elif state == "complete":
            self.progress_var.set(f"Import complete  •  {format_bytes(done)} processed")
        elif state == "failed":
            self.progress_var.set("Import stopped — see the Photo Offloader log")
        elif state == "scanning":
            if status.get("phase") == "catalog-update":
                root_index = int(status.get("catalog_root_index", 1) or 1)
                roots_total = int(status.get("catalog_roots_total", 1) or 1)
                scanned = int(status.get("catalog_total_scanned", status.get("catalog_scanned", 0)) or 0)
                root = str(status.get("catalog_root", "") or "")
                location = "Internal SSD" if "CameraImports" in root and "/Volumes/" not in root else "Backup USB"
                stage = str(status.get("catalog_stage", "Indexing files") or "Indexing files")
                if stage == "Finding files needing metadata":
                    detail = f"checking files • {int(status.get('catalog_discovered', 0) or 0):,} found"
                elif stage == "Reading metadata batches":
                    detail = f"reading metadata for {int(status.get('catalog_candidates', 0) or 0):,} new/changed files"
                else:
                    detail = f"{scanned:,} files scanned"
                self.progress_var.set(
                    f"Updating database • {location} ({root_index} of {roots_total}) • {stage}: {detail}"
                )
            else:
                self.progress_var.set(str(status.get("message", "") or "Scanning source and checking destination space…"))
        elif state == "canceled":
            self.progress_var.set("Import canceled")
        elif PAUSE_FILE.exists():
            self.progress_var.set("Automatic imports are paused")
        else:
            self.progress_var.set("Waiting for a camera card")

        self.refresh_catalog_status(status)
        self.auto_status_var.set(self.trigger_status_summary())
        self.google_photos_var.set(self.google_photos_summary())
        self.refresh_google_photos_progress()

        current_file = str(status.get("current_file", "") or "")
        self.file_var.set(current_file if current_file else "No active import")
        self.source_var.set(f"Source: {status.get('source', '—') or '—'}")
        message = str(status.get("message", "") or "")
        # The progress line already presents catalog scan progress; don't repeat it.
        self.message_var.set("" if state == "scanning" and status.get("phase") == "catalog-update"
                             else message)
        self.root.after(1000, self.refresh)


def log_build_provenance() -> None:
    try:
        info = json.loads((INSTALL_DIR / "build_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {"version": "unknown", "git_commit": "unknown", "git_branch": "unknown"}
    print(
        "Photo Offloader dashboard startup: "
        f"version={info.get('version', 'unknown')} "
        f"git_commit={info.get('git_commit', 'unknown')} "
        f"git_branch={info.get('git_branch', 'unknown')} "
        f"git_tag={info.get('git_tag', 'unknown')} "
        f"git_dirty={info.get('git_dirty', 'unknown')} "
        f"build_time_utc={info.get('build_time_utc', 'unknown')}",
        flush=True,
    )


if __name__ == "__main__":
    log_build_provenance()
    Dashboard()
