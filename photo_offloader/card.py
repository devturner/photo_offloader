"""Camera cards: media types, finding and scanning a card, and the card-side manifest."""
from __future__ import annotations

import glob
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from photo_offloader import config, fileops
from photo_offloader.logs import logger

CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".hif"}


RAW_EXTENSIONS = {
    ".3fr", ".arq", ".arw", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf",
    ".fff", ".gpr", ".iiq", ".k25", ".kdc", ".mef", ".mos", ".mrw", ".nef",
    ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf",
    ".srw", ".x3f",
}


VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".mxf", ".mkv", ".3gp",
    ".crm", ".braw",
}


MEDIA_EXTENSIONS = {
    **{ext: "JPG" for ext in PHOTO_EXTENSIONS},
    **{ext: "RAW" for ext in RAW_EXTENSIONS},
    **{ext: "VIDEO" for ext in VIDEO_EXTENSIONS},
}


# Camera cards keep media in more than DCIM: Sony/Canon/Panasonic video lives
# under PRIVATE (M4ROOT, AVCHD, PANA_GRP) and some older bodies use MP_ROOT.
CARD_SCAN_FOLDERS = ("DCIM", "PRIVATE", "MP_ROOT")


# Files that are never worth copying even though they are not hidden.
IGNORED_CARD_FILENAMES = {"thumbs.db", "desktop.ini"}


MEDIA_CATEGORIES = ("JPG", "RAW", "VIDEO", "OTHER")


CARD_MANIFEST_DIR = ".photo-offloader"


CARD_MANIFEST_NAME = "manifest-v1.json"


def card_relative_key(source: Path, dcim_folder: Path) -> str:
    """Stable manifest key: unchanged for DCIM files, ``../PRIVATE/...`` for siblings of DCIM."""
    return Path(os.path.relpath(source.resolve(), dcim_folder.resolve())).as_posix()


def card_manifest_path(dcim_folder: Path) -> Path:
    """Return the optional sidecar manifest location at the card volume root."""
    return dcim_folder.resolve().parent / CARD_MANIFEST_DIR / CARD_MANIFEST_NAME


def load_card_manifest_payload(dcim_folder: Path) -> Dict[str, object]:
    """Read the complete card-side manifest, tolerating absent/corrupt/read-only media."""
    try:
        payload = json.loads(card_manifest_path(dcim_folder).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("schema") == 1 and isinstance(payload.get("files"), dict):
            return payload
    except (OSError, ValueError, AttributeError):
        pass   # the card may be read-only or never imported: start with an empty manifest
    return {"schema": 1, "files": {}, "history": {}}


def load_card_manifest(dcim_folder: Path) -> Dict[str, dict]:
    """Read per-file fingerprints and hashes from the card-side manifest."""
    payload = load_card_manifest_payload(dcim_folder)
    files = payload.get("files", {})
    return files if isinstance(files, dict) else {}


def save_card_manifest(
    dcim_folder: Path, files: Dict[str, dict], history: Optional[Dict[str, object]] = None,
) -> bool:
    """Atomically persist card metadata; inability to write never blocks import."""
    path = card_manifest_path(dcim_folder)
    temp_path: Optional[Path] = None
    try:
        previous = load_card_manifest_payload(dcim_folder)
        metadata = history if history is not None else previous.get("history", {})
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "files": files, "history": metadata},
                      handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.info("Card-side cache is unavailable; continuing with Mac catalog: %s", exc)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.debug("Could not remove %s: %s", temp_path, cleanup_exc)
        return False


def reconcile_card_manifest(
    dcim_folder: Path, discovered: Dict[str, List[Path]],
) -> Dict[str, int]:
    """Compare the live card inventory with the prior manifest without trusting it to skip copies."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    payload = load_card_manifest_payload(dcim_folder)
    manifest = payload.get("files", {})
    if not isinstance(manifest, dict):
        manifest = {}
    prior_paths = set(manifest)
    current_paths = set()
    new_files = changed_files = unchanged_files = 0
    for category_paths in discovered.values():
        for source in category_paths:
            try:
                relative = card_relative_key(source, dcim_folder)
                fingerprint = fileops.file_fingerprint(source)
            except (OSError, ValueError) as exc:
                logger.debug("Leaving %s out of the card manifest check: %s", source, exc)
                continue
            current_paths.add(relative)
            previous = manifest.get(relative)
            if not isinstance(previous, dict):
                new_files += 1
                entry = {}
            elif not fileops.card_fingerprint_unchanged(previous, fingerprint):
                changed_files += 1
                entry = dict(previous)
                # Invalidate stale hashes; a changed file must be hashed and verified again.
                entry.pop("sha256", None)
            else:
                unchanged_files += 1
                entry = dict(previous)
            entry.update({"size": fingerprint.size, "mtime_ns": fingerprint.mtime_ns,
                          "last_seen": now})
            manifest[relative] = entry
    counts = {
        "new_files": new_files,
        "changed_files": changed_files,
        "unchanged_manifest_files": unchanged_files,
        "missing_from_card": len(prior_paths - current_paths),
    }
    history = payload.get("history", {})
    history = dict(history) if isinstance(history, dict) else {}
    history.update(counts)
    history["last_seen"] = now
    # This is an inventory observation, not a completed import.
    save_card_manifest(dcim_folder, manifest, history)
    return counts


def update_card_scan_history(
    dcim_folder: Path, files: Dict[str, List[Path]], status: str,
    verified_imported: int = 0, pending_or_failed: int = 0,
) -> bool:
    """Record scan/import reconciliation metadata without claiming failed scans succeeded."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    previous = load_card_manifest_payload(dcim_folder)
    history = previous.get("history", {})
    if not isinstance(history, dict):
        history = {}
    history = dict(history)
    history["last_seen"] = now
    history["last_scan_status"] = status
    history["files_discovered"] = sum(len(items) for items in files.values())
    history["files_verified_imported"] = int(verified_imported)
    history["pending_or_failed"] = int(pending_or_failed)
    if status == "complete":
        history["last_scan_completed"] = now
        history["last_successful_import"] = now
    return save_card_manifest(dcim_folder, load_card_manifest(dcim_folder), history)


def card_manifest_entry(
    manifest: Dict[str, dict], source: Path, dcim_folder: Path,
) -> Optional[str]:
    """Return a card-cached hash only when relative path, size, and mtime still match."""
    try:
        relative = card_relative_key(source, dcim_folder)
        entry = manifest.get(relative)
        fingerprint = fileops.file_fingerprint(source)
        if fileops.card_fingerprint_unchanged(entry, fingerprint) and isinstance(entry.get("sha256"), str):
            return entry["sha256"]
    except (OSError, ValueError):
        pass   # a cache miss: the caller hashes the file instead
    return None


def update_card_manifest_entry(
    manifest: Dict[str, dict], source: Path, dcim_folder: Path, digest: str,
) -> None:
    """Cache a verified source hash keyed by card-relative path and stable fingerprint."""
    try:
        relative = card_relative_key(source, dcim_folder)
        fingerprint = fileops.file_fingerprint(source)
        manifest[relative] = {
            "size": fingerprint.size,
            "mtime_ns": fingerprint.mtime_ns,
            "sha256": digest,
        }
    except (OSError, ValueError):
        pass   # only a cache: without this entry the file is hashed next time


def find_card_dcim() -> Path:
    matches: List[Path] = []
    for pattern in CARD_VOLUME_GLOBS:
        for raw in glob.glob(pattern):
            path = Path(raw)
            if path.is_dir():
                matches.append(path)
    matches = sorted(set(matches), key=lambda p: str(p).lower())
    default = str(matches[0].parent) if len(matches) == 1 else ""
    prompt = "Source card volume or DCIM folder"
    if default:
        prompt += f" [{default}]"
    entered = input(prompt + ": ").strip()
    if entered:
        return resolve_dcim_path(Path(entered).expanduser())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print("\nMultiple matching cards found:")
        for i, path in enumerate(matches, 1):
            print(f"  {i}. {path.parent}")
        try:
            return matches[int(input("Enter the number of the source card: ")) - 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Invalid card selection.") from exc
    raise RuntimeError("No recognized card was found.")


def resolve_dcim_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.name.lower() == "dcim" and path.is_dir():
        return path
    dcim = path / "DCIM"
    if dcim.is_dir():
        return dcim
    raise RuntimeError(f"DCIM folder not found at {dcim if path.name.lower() != 'dcim' else path}")


def _walk_card_files(root: Path) -> Iterator[Path]:
    """Yield regular, non-hidden files under ``root`` (one stat-free scandir pass)."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError as exc:
                        logger.error("Could not inspect %s: %s", entry.path, exc)
        except OSError as exc:
            logger.error("Could not read %s: %s", current, exc)


def find_media_files(dcim_folder: Path) -> Dict[str, List[Path]]:
    """Inventory a camera card.

    Known photo/RAW/video types are categorised; every other file is returned
    under ``OTHER`` instead of being ignored, so nothing on the card is silently
    left behind (sidecar XML, thumbnails, unfamiliar RAW formats, AVCHD indexes).
    """
    files: Dict[str, List[Path]] = {category: [] for category in MEDIA_CATEGORIES}
    card_root = dcim_folder.parent
    roots = [dcim_folder] + [
        card_root / name for name in CARD_SCAN_FOLDERS[1:] if (card_root / name).is_dir()
    ]
    for root in roots:
        for path in _walk_card_files(root):
            if path.name.lower() in IGNORED_CARD_FILENAMES:
                continue
            files[MEDIA_EXTENSIONS.get(path.suffix.lower(), "OTHER")].append(path)
    for category in files:
        files[category].sort(key=lambda p: str(p).lower())
    return files


def scan_with_retry(dcim_folder: Path) -> Dict[str, List[Path]]:
    files = find_media_files(dcim_folder)
    if any(files.values()):
        return files
    deadline = time.monotonic() + config.WAIT_SECONDS
    while time.monotonic() < deadline:
        logger.info("No matching files visible yet; checking again...")
        time.sleep(2)
        files = find_media_files(dcim_folder)
        if any(files.values()):
            break
    return files
