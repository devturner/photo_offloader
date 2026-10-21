#!/usr/bin/env python3
"""Small native Tk dashboard for Photo Offloader progress."""
from __future__ import annotations

import json
import os
import subprocess
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict

INSTALL_DIR = Path.home() / "Library" / "Application Support" / "PhotoOffloader"
STATUS_FILE = Path(os.environ.get("PHOTO_OFFLOADER_STATUS_FILE", str(INSTALL_DIR / "status.json")))
PAUSE_FILE = Path(os.environ.get("PHOTO_OFFLOADER_PAUSE_FILE", str(INSTALL_DIR / ".paused")))
BACKUP_LAUNCHER_SCRIPT = INSTALL_DIR / "photo_offloader_backup_launcher.py"
PYTHON = INSTALL_DIR / "venv" / "bin" / "python3"
USB_VOLUME = Path("/Volumes/Photos")


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
        self.root.geometry("610x600")
        self.root.minsize(540, 560)
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
        self.eta_var = tk.StringVar(value="—")
        self.progress_var = tk.StringVar(value="Waiting for a camera card")
        for column, title, variable in (
            (0, "COPY SPEED", self.speed_var), (1, "TIME REMAINING", self.eta_var)
        ):
            card = ttk.Frame(metrics, style="Card.TFrame", padding=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="Metric.TLabel").pack(anchor="w", pady=(5, 0))
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

        ttk.Label(outer, text="TRANSFER SPEED", style="Subtitle.TLabel").pack(anchor="w", pady=(14, 4))
        self.speed_history = deque([0.0] * 60, maxlen=60)
        self.last_sample_time = time.monotonic()
        self.last_transferred_bytes = 0
        self.graph = tk.Canvas(outer, height=110, bg="#1b2638", highlightthickness=0)
        self.graph.pack(fill="x", expand=False)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Copy to USB Drive", command=self.start_usb_backup).pack(side="left")
        ttk.Label(actions, text="Copies missing files from the Mac archive",
                  style="Subtitle.TLabel").pack(side="left", padx=(10, 0))

        self.root.after(500, self.refresh)
        self.root.mainloop()

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
        self.speed_var.set(f"{format_bytes(display_speed)}/s" if display_speed > 0 else "—")
        if state == "copying" and total > done and speed > 0:
            self.eta_var.set(format_duration(eta))
        elif state == "complete":
            self.eta_var.set("Complete")
        elif state == "failed":
            self.eta_var.set("Stopped")
        elif state == "canceled":
            self.eta_var.set("Canceled")
        else:
            self.eta_var.set("Estimating…" if state == "scanning" else "—")

        percent = min(100.0, (done / total * 100.0) if total else 0.0)
        self.progress.configure(value=percent)
        if state == "copying":
            self.progress_var.set(f"{format_bytes(done)} of {format_bytes(total)} processed  •  {percent:.0f}%")
        elif state == "complete":
            self.progress_var.set(f"Import complete  •  {format_bytes(done)} processed")
        elif state == "failed":
            self.progress_var.set("Import stopped — see the Photo Offloader log")
        elif state == "scanning":
            self.progress_var.set(str(status.get("message", "") or "Scanning source and checking destination space…"))
        elif state == "canceled":
            self.progress_var.set("Import canceled")
        elif PAUSE_FILE.exists():
            self.progress_var.set("Automatic imports are paused")
        else:
            self.progress_var.set("Waiting for a camera card")

        current_file = str(status.get("current_file", "") or "")
        self.file_var.set(current_file if current_file else "No active import")
        self.source_var.set(f"Source: {status.get('source', '—') or '—'}")
        self.message_var.set(str(status.get("message", "") or ""))
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
