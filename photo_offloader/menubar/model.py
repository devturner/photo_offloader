#!/usr/bin/env python3
"""What the menu-bar menu offers in each state. Pure logic, no rumps/AppKit, so it is testable.

The menu bar stays alive whether or not the background service is running, so it can always
offer Start. Items that don't apply are left out rather than greyed out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class CardLine:
    """The inserted card, and whether it is safe to format, as the dashboard's cards row says."""

    title: str
    safe: bool
    status: str


@dataclass(frozen=True)
class Snapshot:
    running: bool
    paused: bool = False
    transfer: bool = False
    usb: bool = False
    dashboard: bool = False
    upload: str = ""  # the Google Photos upload: "running", "paused" or nothing
    card: Optional[CardLine] = None  # the inserted card, once the importer has seen it


@dataclass(frozen=True)
class Entry:
    key: Optional[str]  # None = separator
    title: str = ""
    enabled: bool = True


def parse_status(output: str, returncode: int, usb: bool = False) -> Snapshot:
    """Read the output of ``scripts/service.sh status``."""
    fields = {}
    for line in output.splitlines():
        name, _, value = line.partition(":")
        fields[name.strip().lower()] = value.strip().lower()
    running = returncode == 0 and fields.get("service") == "running"
    return Snapshot(
        running=running,
        paused=fields.get("imports") == "paused",
        transfer=fields.get("transfer") == "in progress",
        usb=usb,
    )


def menu_title(snapshot: Snapshot, icon: bool = False) -> str:
    """The text in the menu bar. Beside the app's icon it is only the state; without it, a 📷."""
    if not snapshot.running:
        state = " Off"
    elif snapshot.paused:
        state = " ⏸"
    elif snapshot.transfer:
        state = " ↻"
    elif snapshot.card:
        state = " ✓" if snapshot.card.safe else " !"
    else:
        state = ""
    return state if icon else "📷" + state


def menu_entries(snapshot: Snapshot) -> List[Entry]:
    entries = []
    if snapshot.card:
        entries += [
            Entry("card", snapshot.card.title, enabled=False),
            Entry("card_status", snapshot.card.status, enabled=False),
            Entry(None),
        ]
    entries.append(Entry("open_dashboard", "Open Dashboard"))
    if snapshot.dashboard:
        entries.append(Entry("close_dashboard", "Close Dashboard"))
    entries.append(Entry(None))
    if snapshot.upload in ("running", "paused"):
        # Uploads carry on with the service stopped, so this is offered either way.
        resume = snapshot.upload == "paused"
        entries.append(Entry("upload", "Resume Uploads" if resume else "Pause Uploads"))
    if snapshot.running:
        if snapshot.usb and not snapshot.transfer:
            entries.append(Entry("copy_usb", "Copy to USB Drive"))
        entries.append(
            Entry(
                "pause",
                "Resume Automatic Imports" if snapshot.paused else "Pause Automatic Imports",
            )
        )
    entries += [
        Entry("view_log", "View Import Log"),
        Entry("edit_settings", "Edit Settings…"),
        Entry(None),
    ]
    entries.append(
        Entry("stop", "Stop Service…") if snapshot.running else Entry("start", "Start Service")
    )
    entries.append(Entry("stop_exit", "Stop and Exit…"))
    return entries
