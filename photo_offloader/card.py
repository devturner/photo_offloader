"""Camera cards: media types, finding and scanning a card, and each card's manifest on the Mac."""

from __future__ import annotations

import functools
import glob
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from photo_offloader import config, fileops, jsonfile, volumes
from photo_offloader.logs import logger

CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".hif"}


RAW_EXTENSIONS = {
    ".3fr",
    ".arq",
    ".arw",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".gpr",
    ".iiq",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}


VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".avi",
    ".mts",
    ".m2ts",
    ".mxf",
    ".mkv",
    ".3gp",
    ".crm",
    ".braw",
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


# A format gives a card a new volume ID, so old manifests would otherwise pile up.
MAX_CARD_MANIFESTS = 50


def card_relative_key(source: Path, dcim_folder: Path) -> str:
    """Stable manifest key: unchanged for DCIM files, ``../PRIVATE/...`` for siblings of DCIM."""
    return Path(os.path.relpath(source.resolve(), dcim_folder.resolve())).as_posix()


@functools.lru_cache(maxsize=4)
def card_volume_id(dcim_folder: Path) -> Optional[str]:
    """The card's volume UUID, read once per run because diskutil takes about 160 ms.

    None means nothing is cached for this card, so its files are hashed. A folder that isn't a
    volume gets None too, or it would share an ID with whatever disk it's on.
    """
    card_root = dcim_folder.resolve().parent
    identity = volumes.get_volume_identity(card_root) if card_root.is_mount() else None
    volume_id = identity.volume_uuid if identity else None
    if volume_id and re.fullmatch(r"[A-Za-z0-9-]+", volume_id):
        return volume_id
    logger.info("No volume ID for %s; its files will be hashed", card_root)
    return None


def card_manifest_path(dcim_folder: Path) -> Optional[Path]:
    """Where this card's manifest is kept on the Mac, or None for a card with no volume ID."""
    volume_id = card_volume_id(dcim_folder)
    return config.CARD_MANIFESTS_DIR / f"{volume_id}.json" if volume_id else None


def load_card_manifest_payload(dcim_folder: Path) -> Dict[str, object]:
    """Read the card's complete manifest, tolerating one that is absent or damaged."""
    path = card_manifest_path(dcim_folder)
    payload = jsonfile.read_object(path) if path else {}
    if payload.get("schema") == 1 and isinstance(payload.get("files"), dict):
        return payload
    return {"schema": 1, "files": {}, "history": {}}


def load_card_manifest(dcim_folder: Path) -> Dict[str, dict]:
    """Read per-file fingerprints and hashes from the card's manifest."""
    return load_card_manifest_payload(dcim_folder)["files"]


def save_card_manifest(
    dcim_folder: Path,
    files: Dict[str, dict],
    history: Optional[Dict[str, object]] = None,
) -> bool:
    """Atomically save the card's manifest on the Mac; failing to never blocks an import."""
    path = card_manifest_path(dcim_folder)
    if path is None:
        return False
    metadata = (
        history if history is not None else load_card_manifest_payload(dcim_folder)["history"]
    )
    try:
        jsonfile.write_json(
            path,
            {"schema": 1, "files": files, "history": metadata},
            separators=(",", ":"),
            sort_keys=True,
        )
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.info("Could not save the card's manifest; continuing with the catalog: %s", exc)
        return False


def _forget_old_manifests() -> None:
    """Keep the most recently used manifests. Losing one only means hashing that card again."""
    try:
        manifests = sorted(
            config.CARD_MANIFESTS_DIR.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in manifests[MAX_CARD_MANIFESTS:]:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not tidy the card manifests: %s", exc)


def reconcile_card_manifest(
    dcim_folder: Path,
    discovered: Dict[str, List[Path]],
) -> Dict[str, int]:
    """Compare the live card inventory with the prior manifest without trusting it to skip copies."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    payload = load_card_manifest_payload(dcim_folder)
    manifest = payload["files"]
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
            entry.update(
                {"size": fingerprint.size, "mtime_ns": fingerprint.mtime_ns, "last_seen": now}
            )
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
    _forget_old_manifests()
    return counts


def expected_new_files(
    dcim_folder: Path, discovered: Dict[str, List[Path]]
) -> Optional[Tuple[int, int]]:
    """(files, bytes) that look unsaved, or None with no verified file on the card to trust.

    Only for showing progress: each file is still checked when it is reached. A file counts as
    saved only with a verified hash, because reconciling lists every file before any copying,
    so after an interrupted import being listed proves nothing.
    """
    manifest = load_card_manifest(dcim_folder)
    new_files = new_bytes = verified = 0
    for category_paths in discovered.values():
        for source in category_paths:
            if card_manifest_entry(manifest, source, dcim_folder):
                verified += 1
                continue
            try:
                new_bytes += source.stat().st_size
            except OSError as exc:
                logger.debug("Leaving %s out of the new file estimate: %s", source, exc)
                continue
            new_files += 1
    return (new_files, new_bytes) if verified else None


def bytes_to_copy(paths: List[Path], manifest: Dict[str, dict], dcim_folder: Path) -> int:
    """The size of the files without a verified hash, which are the ones that look unsaved."""
    return sum(
        source.stat().st_size
        for source in paths
        if not card_manifest_entry(manifest, source, dcim_folder)
    )


def update_card_scan_history(
    dcim_folder: Path,
    files: Dict[str, List[Path]],
    status: str,
    verified_imported: int = 0,
    pending_or_failed: int = 0,
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
    manifest: Dict[str, dict],
    source: Path,
    dcim_folder: Path,
) -> Optional[str]:
    """Return a cached hash only when relative path, size, and mtime still match."""
    try:
        relative = card_relative_key(source, dcim_folder)
        entry = manifest.get(relative)
        fingerprint = fileops.file_fingerprint(source)
        if fileops.card_fingerprint_unchanged(entry, fingerprint) and isinstance(
            entry.get("sha256"), str
        ):
            return entry["sha256"]
    except (OSError, ValueError):
        pass  # a cache miss: the caller hashes the file instead
    return None


def update_card_manifest_entry(
    manifest: Dict[str, dict],
    source: Path,
    dcim_folder: Path,
    digest: str,
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
        pass  # only a cache: without this entry the file is hashed next time


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


def _walk_card_files(root: Path, unreadable: List[Path]) -> Iterator[Path]:
    """Yield regular, non-hidden files under ``root`` (one stat-free scandir pass).

    Anything that can't be read goes in ``unreadable``, so the import can't report success
    while files are left behind: this is how a failing card behaves.
    """
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
                        unreadable.append(Path(entry.path))
        except OSError as exc:
            logger.error("Could not read %s: %s", current, exc)
            unreadable.append(current)


def _optional_card_folders(card_root: Path, unreadable: List[Path]) -> List[Path]:
    """The scan folders besides DCIM that this card has."""
    folders: List[Path] = []
    for name in CARD_SCAN_FOLDERS[1:]:
        folder = card_root / name
        try:
            if stat.S_ISDIR(os.stat(folder).st_mode):
                folders.append(folder)
        except FileNotFoundError:
            pass  # most cards only have some of these
        except OSError as exc:
            logger.error("Could not check %s: %s", folder, exc)
            unreadable.append(folder)
    return folders


def find_media_files(
    dcim_folder: Path, unreadable: Optional[List[Path]] = None
) -> Dict[str, List[Path]]:
    """Inventory a camera card, adding anything that can't be read to ``unreadable``.

    Known photo/RAW/video types are categorised; every other file is returned
    under ``OTHER`` instead of being ignored, so nothing on the card is silently
    left behind (sidecar XML, thumbnails, unfamiliar RAW formats, AVCHD indexes).
    """
    problems: List[Path] = [] if unreadable is None else unreadable
    files: Dict[str, List[Path]] = {category: [] for category in MEDIA_CATEGORIES}
    for root in [dcim_folder, *_optional_card_folders(dcim_folder.parent, problems)]:
        for path in _walk_card_files(root, problems):
            if path.name.lower() in IGNORED_CARD_FILENAMES or fileops.is_camera_index_file(path):
                continue
            files[MEDIA_EXTENSIONS.get(path.suffix.lower(), "OTHER")].append(path)
    for category in files:
        files[category].sort(key=lambda p: str(p).lower())
    return files


def scan_with_retry(
    dcim_folder: Path, unreadable: Optional[List[Path]] = None
) -> Dict[str, List[Path]]:
    """Scan, retrying while a card shows no files. ``unreadable`` reflects the last attempt."""
    problems: List[Path] = [] if unreadable is None else unreadable
    files = find_media_files(dcim_folder, problems)
    if any(files.values()):
        return files
    deadline = time.monotonic() + config.WAIT_SECONDS
    while time.monotonic() < deadline:
        logger.info("No matching files visible yet; checking again...")
        time.sleep(2)
        problems.clear()
        files = find_media_files(dcim_folder, problems)
        if any(files.values()):
            break
    return files
