"""What the dashboard says: pure functions from status data to the words and cards it shows.

Nothing here touches the machine, the clock or the files, so it can all be tested directly.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

CATEGORY_LABELS = {"JPG": "JPG", "RAW": "RAW", "VIDEO": "Video", "OTHER": "Other"}
IMPORT_STAGES = (
    ("scan", "Scan card"),
    ("metadata", "Read photo info"),
    ("copy", "Copy new files"),
    ("usb", "Back up to USB"),
    ("upload", "Upload to Google Photos"),
)


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
    exponent = {"B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2, "GB": 3, "GIB": 3, "TB": 4, "TIB": 4}[
        unit
    ]
    return amount * (1024**exponent)


CHECKING_TITLE = "Checking for new files"


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
    if (
        phase.lower() in ("usb-backup", "usb backup")
        or "usb" in operation
        or "/volumes/photos" in destination
    ):
        return "usb"
    if phase == "copy-decision" or status.get("state") == "copying":
        return "copy"
    return "scan"


def import_stages(
    status: Dict[str, Any], stage: str, upload_planned: bool = False
) -> Optional[List[Dict[str, str]]]:
    """The steps of a card import with done/active/pending/skipped, or None for other runs."""
    if "usb_planned" not in status or stage not in dict(IMPORT_STAGES):
        return None
    order = [step_id for step_id, _ in IMPORT_STAGES]
    current = order.index(stage)
    steps = []
    for index, (step_id, label) in enumerate(IMPORT_STAGES):
        if (step_id == "usb" and not status.get("usb_planned")) or (
            step_id == "upload" and not upload_planned
        ):
            state = "skipped"
        else:
            state = "done" if index < current else "active" if index == current else "pending"
        steps.append({"id": step_id, "label": label, "state": state})
    return steps


def parse_timestamp(value: object) -> Optional[float]:
    """Epoch seconds from an ISO 8601 timestamp written by the importer or the sync, or None."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def saved_on_mac(row: Dict[str, Any]) -> bool:
    """Whether an import's photos were verified on the Mac, so its upload was started."""
    return row["outcome"] == "complete" or bool(_details(row).get("local_verified"))


def stages_after_import(row: Dict[str, Any], upload_state: str) -> List[Dict[str, str]]:
    """Import steps for a finished (history) run, with the Google Photos step as given."""
    outcome, usb, message = row["outcome"], bool(row["usb"]), str(row.get("message") or "")
    usb_failed = outcome == "failed" and usb and "USB" in message
    copy_state = (
        "done"
        if outcome == "complete" or usb_failed
        else ("error" if outcome == "failed" else "skipped")
    )
    states = {
        "scan": "done",
        "metadata": "done",
        "copy": copy_state,
        "usb": "error" if usb_failed else ("done" if usb and outcome == "complete" else "skipped"),
        "upload": upload_state if saved_on_mac(row) else "skipped",
    }
    return [
        {"id": step_id, "label": label, "state": states[step_id]}
        for step_id, label in IMPORT_STAGES
    ]


def activity_text(status: Dict[str, Any], stage: str) -> Tuple[str, str]:
    """The panel's title and detail line for the importer's current step."""
    state = str(status.get("state", "idle"))
    done = int(status.get("bytes_done", 0) or 0)
    total = int(status.get("bytes_total", 0) or 0)
    current_file = str(status.get("current_file", "") or "")
    detail = str(status.get("message", "") or "")
    if stage == "catalog":
        scanned = int(status.get("catalog_scanned", 0) or 0)
        step = str(status.get("catalog_stage", "") or "")
        return "Updating file index", " · ".join(
            part for part in (step, f"{scanned:,} files scanned") if part
        )
    if stage == "usb":
        title = "Backing up to USB" if state == "copying" else "Comparing with USB backup"
        return title, current_file or (
            f"{format_bytes(done)} of {format_bytes(total)}" if total else detail
        )
    if stage == "copy":
        # Every file is first checked, then copied if new: the status flips between the
        # two per file. Name the step by what the session is doing overall so it holds still.
        new_files = int(status.get("new_files", 0) or 0)
        category = CATEGORY_LABELS.get(str(status.get("category", "") or ""), "")
        title = "Copying new files" if state == "copying" or new_files else CHECKING_TITLE
        return title, " · ".join(part for part in (category, current_file) if part) or detail
    if stage == "metadata":
        return "Reading photo info", detail
    return "Scanning card", detail or "Looking for photos and video"


def _step(
    step_id: str,
    label: str,
    state: str,
    headline: str,
    detail: str,
    actions: Sequence[Dict[str, str]] = (),
    percent: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "state": state,
        "headline": headline,
        "detail": detail,
        "actions": list(actions),
        "percent": percent,
    }


def _action(action_id: str, label: str, confirm: str = "") -> Dict[str, str]:
    """A button. ``confirm`` is what the page asks first, in place, before doing it."""
    return {"id": action_id, "label": label, **({"confirm": confirm} if confirm else {})}


def card_contents(files_total: int, expected_new: Optional[int]) -> str:
    """The Camera card box during a copy; with no estimate, only what is on the card is known."""
    if expected_new is None:
        return plural(files_total, "file")
    if expected_new == 0:
        return f"No new files of {files_total:,}"
    return f"{plural(expected_new, 'new file')} of {files_total:,}"


UNSAVED_CARD_EJECT_QUESTION = "This card has files that weren't copied. Eject it anyway?"


def _card_step(
    card: Optional[Path],
    activity: Optional[Dict[str, Any]],
    busy: bool,
    can_reimport: bool,
    unsaved: bool = False,
) -> Dict[str, Any]:
    if activity and activity["kind"] == "import" and activity.get("stage") in ("scan", "metadata"):
        return _step(
            "card", "Camera card", "active", card.name if card else "Card", activity["title"]
        )
    if activity and activity["kind"] == "import" and activity.get("stage") == "copy":
        contents = card_contents(activity["files_total"], activity["new_files_expected"])
        return _step("card", "Camera card", "ok", card.name if card else "Card", contents)
    if card and unsaved:
        actions = (
            []
            if busy
            else [
                _action("import_card", "Try again"),
                _action("eject_card", "Eject", UNSAVED_CARD_EJECT_QUESTION),
            ]
        )
        return _step(
            "card", "Camera card", "error", card.name, "Don't format: files not saved", actions
        )
    if card:
        actions = (
            []
            if busy
            else ([_action("import_card", "Import now")] if can_reimport else [])
            + [_action("eject_card", "Eject")]
        )
        return _step(
            "card",
            "Camera card",
            "ok",
            card.name,
            "Already imported" if can_reimport else "Mounted",
            actions,
        )
    return _step("card", "Camera card", "off", "No card", "Insert a card to begin")


def _archive_step(activity: Optional[Dict[str, Any]], local: Optional[int]) -> Dict[str, Any]:
    detail = (
        (f"{local:,} files indexed" if local else "Nothing indexed yet")
        if local is not None
        else "Catalog unavailable"
    )
    if activity and activity["kind"] == "import" and activity.get("stage") == "copy":
        return _step(
            "archive",
            "Mac archive",
            "active",
            "Checking" if activity["title"] == CHECKING_TITLE else "Copying",
            activity["detail"] or detail,
        )
    return _step("archive", "Mac archive", "ok", "Up to date" if local else "Empty", detail)


def _usb_step(
    activity: Optional[Dict[str, Any]], usb_mounted: bool, usb: Optional[int], busy: bool
) -> Dict[str, Any]:
    if activity and activity["kind"] == "usb_backup":
        return _step("usb", "USB backup", "active", "Backing up", activity["detail"])
    if usb_mounted:
        return _step(
            "usb",
            "USB backup",
            "ok",
            "Connected",
            f"{usb:,} files backed up" if usb else "Ready to back up",
            [] if busy else [_action("usb_backup", "Back up now"), _action("eject_usb", "Eject")],
        )
    return _step("usb", "USB backup", "off", "Not connected", "Plug in the backup drive")


def _cloud_step(
    google: Dict[str, Any], uploaded: int, busy: bool, setup_step: str, in_top_panel: bool
) -> Dict[str, Any]:
    state = google["state"]
    if not google["configured"]:
        return _step("cloud", "Google Photos", "off", "Not set up", setup_step)
    if state == "running":
        upload = google["progress"]
        return _step(
            "cloud",
            "Google Photos",
            "active",
            "Finishing upload" if upload and upload["finalizing"] else "Uploading",
            f"{upload['transferred']} of {upload['total']}" if upload else "Preparing",
            (),
            # The top panel already has this bar, unless an import is using the panel.
            upload["percent"] if upload and not in_top_panel else None,
        )
    if state == "interrupted":
        return _step(
            "cloud",
            "Google Photos",
            "error",
            "Upload interrupted",
            "It stopped before finishing",
            [_action("google_sync", "Resume")],
        )
    if state in ("failed", "error"):
        return _step(
            "cloud",
            "Google Photos",
            "error",
            "Upload failed",
            google["message"] or "See the log",
            [_action("google_sync", "Retry")],
        )
    return _step(
        "cloud",
        "Google Photos",
        "ok",
        "Up to date",
        f"{uploaded:,} files uploaded" if uploaded else "Nothing uploaded yet",
        [] if busy else [_action("google_sync", "Sync now")],
    )


def pipeline(
    card: Optional[Path],
    activity: Optional[Dict[str, Any]],
    counts: Optional[Dict[str, Any]],
    usb_mounted: bool,
    google: Dict[str, Any],
    busy: bool,
    can_reimport: bool,
    setup_step: str,
    card_unsaved: bool = False,
) -> List[Dict[str, Any]]:
    """The four steps a photo moves through (card, Mac, USB, Google) and what each can do now."""
    return [
        _card_step(card, activity, busy, can_reimport, card_unsaved),
        _archive_step(activity, counts["local"] if counts else None),
        _usb_step(activity, usb_mounted, counts["usb"] if counts else None, busy),
        _cloud_step(
            google,
            counts["uploaded"] if counts else 0,
            busy,
            setup_step,
            activity is None,  # only an import's activity comes here; otherwise it shows the upload
        ),
    ]


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _details(row: Dict[str, Any]) -> Dict[str, Any]:
    details = row.get("details")
    return details if isinstance(details, dict) else {}


def _new_files_count(row: Dict[str, Any], details: Dict[str, Any]) -> int:
    new = details.get("new")
    # A run recorded before per-session details existed only has its copy count.
    return sum(int(v) for v in new.values()) if isinstance(new, dict) else int(row["copied"])


def _new_files_text(row: Dict[str, Any], details: Dict[str, Any]) -> str:
    count, new = _new_files_count(row, details), details.get("new")
    if not count:
        return "No new files"
    if not isinstance(new, dict):
        return f"{count:,} new"
    labels = {
        "JPG": lambda n: plural(n, "photo"),
        "RAW": lambda n: f"{n:,} RAW",
        "VIDEO": lambda n: plural(n, "video"),
        "OTHER": lambda n: f"{n:,} other",
    }
    kinds = (labels[key](int(new[key])) for key in ("JPG", "RAW", "VIDEO", "OTHER") if new.get(key))
    return f"{count:,} new: " + ", ".join(kinds)


def _report_step(step_id: str, label: str, place: str, state: str, result: str, **figures: Any):
    """One row of a report. Figures not known are None, and the page shows a dash."""
    size, seconds, files = figures.get("bytes"), figures.get("seconds"), figures.get("files")
    return {
        "id": step_id,
        "label": label,
        "place": place,
        "state": state,
        "result": result,
        "files": files,
        "bytes": size,
        "seconds": seconds,
        # The scan has a size, the whole card's, but only a step that moves files has a speed.
        "speed_bps": size / seconds if size and seconds and files is not None else None,
    }


def _upload_report_step(details: Dict[str, Any], state: str) -> Dict[str, Any]:
    upload = details.get("upload")
    if isinstance(upload, dict):
        if upload.get("outcome") != "complete":
            # Its size is what it set out to send, which says nothing about what got there.
            return _report_step(
                "upload",
                "Upload to Google Photos",
                "Google Photos",
                "error",
                "Stopped",
                seconds=upload.get("seconds"),
            )
        files = int(upload.get("files", 0) or 0)
        return _report_step(
            "upload",
            "Upload to Google Photos",
            "Google Photos",
            "done",
            f"{plural(files, 'file')} to {upload.get('album', 'Google Photos')}",
            files=files,
            bytes=upload.get("bytes"),
            seconds=upload.get("seconds"),
        )
    new = details.get("new")
    nothing_to_send = isinstance(new, dict) and not (new.get("JPG") or new.get("VIDEO"))
    result = {
        "active": "Uploading now",
        "pending": "Starting",
        "skipped": "Skipped",
        "error": "Stopped",
    }.get(state, "Nothing to send" if nothing_to_send else "Not recorded")
    return _report_step("upload", "Upload to Google Photos", "Google Photos", state, result)


def _usb_report_step(details: Dict[str, Any], state: str) -> Dict[str, Any]:
    copied = details.get("usb_copied")
    if state == "skipped":
        result = "Skipped"
    elif state == "error":
        result = "Stopped"
    elif copied is None:
        result = "Backed up"
    else:
        result = f"{plural(int(copied), 'file')} copied" if copied else "Already up to date"
    return _report_step(
        "usb",
        "Back up to USB",
        "USB backup",
        state,
        result,
        files=copied,
        bytes=details.get("usb_bytes"),
        seconds=details.get("usb_seconds"),
    )


CHECK_STEP_LABEL = "Check for new files"


def _import_steps(
    row: Dict[str, Any], details: Dict[str, Any], upload_state: str
) -> List[Dict[str, Any]]:
    states = {
        stage["id"]: stage["state"]
        for stage in stages_after_import(
            row, "pending" if upload_state == "starting" else upload_state
        )
    }
    copy_seconds, usb_seconds = details.get("copy_seconds"), details.get("usb_seconds")
    scan_seconds = (
        max(float(row["duration_seconds"]) - copy_seconds - (usb_seconds or 0), 0.0)
        if copy_seconds is not None
        else None
    )
    files_copied = _new_files_count(row, details)
    only_checked = not files_copied and states["copy"] == "done"
    copied = _new_files_text(row, details)
    problems = details.get("problems")
    on_card = problems and not problems.get("usb")
    if on_card:
        copied = _safe_and_problems(problems)
    elif row["failed"]:
        copied += f" · {int(row['failed'])} failed"
    steps = [
        _report_step(
            "scan",
            "Scan and read photo info",
            "Camera card",
            "done",
            f"{int(row['files']):,} on card · {int(row['skipped']):,} already saved",
            bytes=row["bytes"],
            seconds=scan_seconds,
        ),
        _report_step(
            "copy",
            CHECK_STEP_LABEL if only_checked else "Copy to Mac",
            "Mac archive",
            states["copy"],
            copied,
            files=files_copied,
            bytes=details.get("new_bytes"),
            seconds=copy_seconds,
        ),
        _usb_report_step(details, states["usb"]),
        _upload_report_step(details, states["upload"]),
    ]
    for step in steps:
        if on_card and step["state"] == "skipped":
            step["result"] = "Not started"
        if problems and not on_card and step["id"] == "usb":
            step["result"] = _safe_and_problems(problems)
    return steps


def _safe_and_problems(problems: Dict[str, Any]) -> str:
    return f"{int(problems['safe']):,} safe · {plural(int(problems['problems']), 'problem')}"


def _usb_problem_steps(row: Dict[str, Any], problems: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A backup that failed: where each of the three places stands."""
    files = int(row["files"])
    return [
        _report_step(
            "mac", "Mac archive", "Mac archive", "done", plural(files, "file"), files=files
        ),
        _report_step(
            "usb",
            "Back up to USB",
            "USB backup",
            "error",
            _safe_and_problems(problems),
            seconds=float(row["duration_seconds"]),
        ),
        _report_step(
            "upload", "Upload to Google Photos", "Google Photos", "skipped", "Not part of this"
        ),
    ]


def _single_step(row: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """A run that does one thing: a USB backup or a catalog update."""
    state = {"complete": "done", "failed": "error"}.get(str(row["outcome"]), "skipped")
    seconds = float(row["duration_seconds"])
    if kind == "usb_backup":
        copied = int(row["copied"])
        result = f"{plural(copied, 'file')} copied" if copied else "Already up to date"
        return _report_step(
            "usb", "Back up to USB", "USB backup", state, result, files=copied, seconds=seconds
        )
    result = "Completed" if state == "done" else str(row.get("message") or "Stopped")
    return _report_step(
        "catalog", "Update file catalog", "File catalog", state, result, seconds=seconds
    )


SEGMENT_LABELS = {"scan": "Scan", "copy": "Copy", "usb": "USB backup", "upload": "Upload"}


def _segments(steps: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Where the time went. Without a copy time, the import can't be split up."""
    timed = {step["id"]: step["seconds"] for step in steps}
    labels = dict(SEGMENT_LABELS)
    if any(step["label"] == CHECK_STEP_LABEL for step in steps):
        labels["copy"] = "Check"
    if "scan" in timed and timed["scan"] is None:
        parts = [("import", "Import", duration), ("upload", "Upload", timed["upload"])]
    else:
        parts = [(key, labels.get(key, "Update"), timed[key]) for key in timed]
    return [
        {"id": key, "label": label, "seconds": seconds} for key, label, seconds in parts if seconds
    ]


def run_report(row: Dict[str, Any], upload_state: Optional[str] = None) -> Dict[str, Any]:
    """Everything a finished run's report shows, in either style.

    ``upload_state`` is what is known about the upload if the row doesn't say (see
    ``DashboardCore._upload_state_after``); it only applies to the newest import.
    """
    described, details, kind = describe_run(row), _details(row), str(row["kind"])
    duration = float(row["duration_seconds"])
    if kind == "import":
        steps = _import_steps(row, details, upload_state or "done")
        upload = steps[-1]
        whole_run = upload["seconds"] is not None or upload["result"] in (
            "Skipped",
            "Nothing to send",
        )
    elif kind == "usb_backup" and details.get("problems"):
        steps, whole_run = _usb_problem_steps(row, details["problems"]), True
    else:
        steps, whole_run = [_single_step(row, kind)], True
    upload_seconds = steps[-1]["seconds"] if kind == "import" else None
    return {
        "title": described["title"],
        "detail": described["summary"],
        "tone": described["tone"],
        "steps": steps,
        "segments": _segments(steps, duration),
        "total_seconds": duration + float(upload_seconds or 0),
        "total_note": "Start to finish" if whole_run else "Import only",
    }


def describe_run(row: Dict[str, Any]) -> Dict[str, Any]:
    kind, outcome = str(row["kind"]), str(row["outcome"])
    source = str(row.get("source") or "")
    noun = {
        "import": f"Import from {source}" if source else "Import",
        "usb_backup": "USB backup",
        "catalog": "Catalog update",
    }.get(kind, "Transfer")
    done = {
        "import": f"Imported from {source}" if source else "Import complete",
        "usb_backup": "USB backup complete",
        "catalog": "Catalog updated",
    }.get(kind, "Transfer complete")
    if outcome == "failed":
        title, tone = f"{noun} failed", "error"
    elif outcome == "canceled":
        title, tone = f"{noun} canceled", "warning"
    else:
        title, tone = done, "success"
    parts: List[str] = []
    details = _details(row)
    problems = details.get("problems")
    if problems and (kind == "usb_backup" or (kind == "import" and not problems.get("usb"))):
        label = problems.get("safe_label", "safe on the Mac")
        count = plural(int(problems["problems"]), "problem")
        summary = f"{int(problems['safe']):,} {label} · {count}"
        return {**row, "title": title, "tone": tone, "summary": summary}
    if kind == "import":
        parts.append(_new_files_text(row, details))
        if _new_files_count(row, details) and details.get("new_bytes"):
            parts.append(format_bytes(int(details["new_bytes"])))
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


def headline(
    running: bool,
    paused: bool,
    card: Optional[Path],
    activity: Optional[Dict[str, Any]],
    interrupted: bool,
    last: Optional[Dict[str, Any]],
    status: Dict[str, Any],
    google: Dict[str, Any],
    card_handled: bool = False,
) -> Dict[str, str]:
    if not running:
        return {
            "tone": "stopped",
            "title": "Photo Offloader is stopped",
            "detail": "Camera cards will not be imported until you start it.",
        }
    if activity:
        source = activity["source"].rsplit("/", 1)[-1] if activity["source"] else ""
        if activity["kind"] == "import" and source:
            detail = " · ".join(part for part in (source, activity["detail"]) if part)
            return {"tone": "active", "title": activity["title"], "detail": detail}
        return {"tone": "active", "title": activity["title"], "detail": activity["detail"]}
    if interrupted:
        return {
            "tone": "warning",
            "title": "The last transfer was interrupted",
            "detail": "It stopped before finishing. Insert the card again to continue.",
        }
    if last and last["outcome"] == "failed":
        return {
            "tone": "error",
            "title": last["title"],
            "detail": str(last.get("message") or last["summary"] or "See the log for details."),
        }
    if google["state"] in ("failed", "error", "interrupted"):
        return {
            "tone": "warning",
            "title": "Google Photos upload needs attention",
            "detail": google["message"] or "Sync again to finish uploading.",
        }
    if paused:
        return {
            "tone": "warning",
            "title": "Automatic imports are paused",
            "detail": "Cards will not be imported until you resume.",
        }
    if last:
        return {"tone": "success", "title": last["title"], "detail": last["summary"]}
    if card and card_handled:
        return {
            "tone": "idle",
            "title": f"{card.name} was already imported",
            "detail": "Shot more since? Choose Import now, or eject and re-insert the card.",
        }
    if card:
        return {
            "tone": "idle",
            "title": f"{card.name} is mounted",
            "detail": "The import will start in a moment.",
        }
    return {"tone": "idle", "title": "Ready", "detail": "Insert a camera card to begin."}
