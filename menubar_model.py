#!/usr/bin/env python3
"""What the menu-bar menu offers in each state. Pure logic, no rumps/AppKit, so it is testable.

The menu bar stays alive whether or not the background service is running. When the service
is stopped the menu says so and offers Start; nothing is greyed out except the explanatory line.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Snapshot:
    running: bool
    paused: bool = False
    transfer: bool = False
    usb: bool = False


@dataclass(frozen=True)
class Entry:
    key: Optional[str]          # None = separator
    title: str = ""
    enabled: bool = True


def parse_status(output: str, returncode: int, usb: bool = False) -> Snapshot:
    """Read the output of ``photo_offloader_service.sh status``."""
    fields = {}
    for line in output.splitlines():
        name, _, value = line.partition(":")
        fields[name.strip().lower()] = value.strip().lower()
    running = returncode == 0 and fields.get("service") == "running"
    return Snapshot(running=running, paused=fields.get("imports") == "paused",
                    transfer=fields.get("transfer") == "in progress", usb=usb)


def menu_title(snapshot: Snapshot) -> str:
    if not snapshot.running:
        return "📷 Off"
    if snapshot.paused:
        return "📷 ⏸"
    if snapshot.transfer:
        return "📷 ↻"
    return "📷"


def menu_entries(snapshot: Snapshot) -> List[Entry]:
    quit_entry = Entry("quit", "Quit Menu Bar (Service Keeps Running)" if snapshot.running else "Quit Menu Bar")
    if not snapshot.running:
        return [
            Entry("info", "Photo Offloader is stopped", enabled=False),
            Entry("start", "Start Photo Offloader"),
            Entry("open_dashboard", "Open Dashboard"),
            Entry(None),
            quit_entry,
        ]
    entries = [Entry("open_dashboard", "Open Dashboard")]
    if snapshot.usb and not snapshot.transfer:
        entries.append(Entry("copy_usb", "Copy to USB Drive"))
    entries.append(Entry("pause", "Resume Automatic Imports" if snapshot.paused else "Pause Automatic Imports"))
    entries += [Entry(None), Entry("stop", "Stop Photo Offloader…"), quit_entry]
    return entries
