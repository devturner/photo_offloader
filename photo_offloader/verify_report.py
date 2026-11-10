"""What a check of the saved copies found, and the files someone has said they deleted.

The check (``verify``) writes this and the dashboard reads it. The dashboard can't import ``verify``,
which brings the whole importer with it, so what they share lives here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from photo_offloader import config, jsonfile, run_report

MAC = "Mac"
USB = "USB drive"
BOTH = "Both"

DAMAGED = "damaged"  # the contents changed, but not the size or date: nothing meant to do that
CHANGED = "changed"  # the contents differ, and something has also touched the file
MISSING = "missing"
UNREADABLE = "unreadable"
ONLY_ON_DRIVE = "only on the drive"
COPIES_DIFFER = "copies differ"
KINDS_IN_ORDER = (DAMAGED, CHANGED, MISSING, UNREADABLE, ONLY_ON_DRIVE, COPIES_DIFFER)

OTHER_GOOD = "good"
OTHER_BAD = "no good copy"
OTHER_UNKNOWN = "not checked"
OTHER_UNSURE = "can't tell which is right"
OTHER_ONLY = "the only copy"

# Until a check has measured them. From the first real check, 69.4 GB on each side: about 65 s on
# the Mac and 1,025 s on a 5,400 rpm hard drive. An SSD reads several times faster.
MAC_BYTES_PER_SECOND = 1e9
USB_BYTES_PER_SECOND = 68e6
USUAL_SPEEDS = {MAC: MAC_BYTES_PER_SECOND, USB: USB_BYTES_PER_SECOND}

REPORTS_KEPT = 20
FILES_SHOWN = 20  # in a run's row; the saved list has them all
FOLDERS_SHOWN = 20
REPORT_COLUMNS = (
    "where",
    "problem",
    "other_copy",
    "path",
    "relative_path",
    "bytes",
    "expected_sha256",
    "actual_sha256",
)


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def measured_speeds(rows: Iterable[Mapping[str, Any]], drive_id: Optional[str]) -> Dict[str, float]:
    """The newest speed a check read the Mac at, and this drive at, from history rows newest first.

    A drive's speed is its own: the disk, its cable and the port all set it, so another drive's
    says nothing. A place with no measured speed is left out.
    """
    found: Dict[str, float] = {}
    for row in rows:
        details = row.get("details")
        if row.get("kind") != "verify" or not isinstance(details, Mapping):
            continue
        speeds = details.get("speeds")
        if not isinstance(speeds, Mapping):
            continue
        same_drive = bool(drive_id) and details.get("drive_id") == drive_id
        for place in (MAC, USB) if same_drive else (MAC,):
            speed = speeds.get(place)
            if place not in found and isinstance(speed, (int, float)) and speed > 0:
                found[place] = float(speed)
    return found


def problems_text(kinds: Sequence[str]) -> str:
    counts = [f"{kinds.count(kind):,} {kind}" for kind in KINDS_IN_ORDER if kind in kinds]
    return f"{plural(len(kinds), 'problem')}: {', '.join(counts)}."


def message_for(rows: Sequence[Mapping[str, Any]]) -> str:
    """Files gone only from the Mac are one situation however the catalog happens to list them."""
    if rows and all(can_mark_deleted(row) for row in rows):
        verb = "is" if len(rows) == 1 else "are"
        each = "it" if len(rows) == 1 else "each"
        return (
            f"{plural(len(rows), 'file')} {verb} missing from the Mac. "
            f"The USB drive's copy of {each} is good."
        )
    return problems_text([str(row["problem"]) for row in rows])


# ---- the saved report ------------------------------------------------------------------------


def _report_path(name: str, suffix: str) -> Optional[Path]:
    """A report's file. Only ever one directly in the folder: the name comes from the page."""
    if not name or Path(name).name != name or name in (".", ".."):
        return None
    path = config.VERIFY_REPORTS_DIR / (name + suffix)
    return path if path.is_file() else None


def report_csv(name: str) -> Optional[Path]:
    return _report_path(name, ".csv")


def report_rows(name: str) -> List[Dict[str, Any]]:
    """The spreadsheet copy changes names that look like formulas, so this one is what's relied on."""
    path = _report_path(name, ".json")
    if path is None:
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    except (OSError, ValueError, AttributeError):
        return []
    return rows if isinstance(rows, list) else []


def save_report(rows: Sequence[Mapping[str, Any]], started_at: float) -> str:
    """Save one check's problems, and return the report's name. Raises OSError if that fails."""
    folder = config.VERIFY_REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
    run_report.write_csv(folder / f"{name}.csv", REPORT_COLUMNS, rows)
    jsonfile.write_json(folder / f"{name}.json", {"rows": list(rows)})
    for old in sorted(folder.glob("*.csv"))[:-REPORTS_KEPT]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return name


def can_mark_deleted(row: Mapping[str, Any]) -> bool:
    """Only when the drive's copy has been read and is good: anything else may be a real loss."""
    if row["where"] == MAC:
        return row["problem"] == MISSING and row["other_copy"] == OTHER_GOOD
    return row["where"] == USB and row["problem"] == ONLY_ON_DRIVE


def _other_copy_text(row: Mapping[str, Any]) -> str:
    other = str(row["other_copy"])
    if other == OTHER_GOOD:
        return f"Good, on the {USB if row['where'] == MAC else MAC}"
    return "This is the only copy" if other == OTHER_ONLY else other.capitalize()


def _deleted_summary(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    folders: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        folder = str(Path(str(row["relative_path"])).parent)
        entry = folders.setdefault(folder, {"folder": folder, "count": 0, "bytes": 0})
        entry["count"] += 1
        entry["bytes"] += int(row.get("bytes") or 0)
    ordered = sorted(folders.values(), key=lambda item: (-item["count"], item["folder"]))
    return {
        "count": len(rows),
        "bytes": sum(item["bytes"] for item in ordered),
        "folders": ordered[:FOLDERS_SHOWN],
        "folders_more": max(len(ordered) - FOLDERS_SHOWN, 0),
    }


def details_for(rows: Sequence[Mapping[str, Any]], name: str) -> Dict[str, Any]:
    """What a run's row in Recent activity shows. The saved report is what can be marked."""
    markable = [row for row in rows if name and can_mark_deleted(row)]
    others = [row for row in rows if not (name and can_mark_deleted(row))]
    return {
        "report": name,
        "deleted": _deleted_summary(markable),
        "others": [
            {
                "path": row["relative_path"],
                "where": row["where"],
                "kind": str(row["problem"]).capitalize(),
                "other": _other_copy_text(row),
            }
            for row in others[:FILES_SHOWN]
        ],
        "others_more": max(len(others) - FILES_SHOWN, 0),
    }


# ---- files someone has said they deleted from the Mac -----------------------------------------


def deleted_files() -> Set[str]:
    """Relative paths. Later checks expect these to be gone from the Mac, and read the drive's copy."""
    return set(jsonfile.read_object(config.DELETED_FROM_MAC_PATH))


def remember_deleted(relatives: Iterable[str]) -> None:
    noted = jsonfile.read_object(config.DELETED_FROM_MAC_PATH)
    noted.update(dict.fromkeys(relatives, time.time()))
    jsonfile.write_json(config.DELETED_FROM_MAC_PATH, noted)


def forget_deleted(relatives: Iterable[str]) -> None:
    noted = jsonfile.read_object(config.DELETED_FROM_MAC_PATH)
    for relative in relatives:
        noted.pop(relative, None)
    jsonfile.write_json(config.DELETED_FROM_MAC_PATH, noted)


def mark_deleted(name: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Note the files in a report that qualify. Returns them, and the report's other problems.

    This only changes what later checks say. It never deletes or moves anything.
    """
    rows = report_rows(name)
    marked = [str(row["relative_path"]) for row in rows if can_mark_deleted(row)]
    if marked:
        remember_deleted(marked)
    return marked, [row for row in rows if not can_mark_deleted(row)]
