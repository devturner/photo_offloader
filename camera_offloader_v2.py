#!/usr/bin/env python3
"""Camera Card Offloader v2.3.

Conservative macOS media ingestion:
    discover -> lock -> snapshot -> scan -> capacity check -> copy -> verify
    -> report -> eject

The importer never deletes source media and never overwrites an existing
same-name destination. It treats the USB backup as a required destination once
it was present at the beginning of an import, and it refuses to eject a card
when any required destination fails.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    from alive_progress import alive_bar
    HAS_ALIVE_PROGRESS = True
except ImportError:  # pragma: no cover
    HAS_ALIVE_PROGRESS = False

    class _NullProgress:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_value, traceback): return False
        def __call__(self, *args, **kwargs): return None

    def alive_bar(*args, **kwargs):
        return _NullProgress()

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]
LOCAL_DEST_ROOT = Path.home() / "Pictures" / "CameraImports"
USB_DEST_VOLUME = Path("/Volumes/Photos")
USB_DEST_ROOT = USB_DEST_VOLUME / "CameraImports"
LOG_FILE_PATH = Path.home() / ".camera_transfer.log"
SESSION_LOCK_PATH = Path.home() / "Library" / "Caches" / "PhotoOffloader" / "import.lock"
WAIT_SECONDS = 30
EJECT_CARD_AFTER_SUCCESS = True
VERIFY_WITH_SHA256 = False
USE_FILE_BIRTH_TIME = True
MIN_FREE_SPACE_BUFFER_BYTES = 100 * 1024 * 1024

MEDIA_EXTENSIONS = {
    ".jpg": "JPG", ".jpeg": "JPG", ".heic": "JPG", ".heif": "JPG",
    ".nef": "RAW", ".arw": "RAW", ".cr2": "RAW", ".cr3": "RAW",
    ".dng": "RAW", ".raf": "RAW", ".orf": "RAW", ".rw2": "RAW",
    ".mov": "VIDEO", ".mp4": "VIDEO", ".avi": "VIDEO",
    ".mts": "VIDEO", ".m2ts": "VIDEO",
}

logger = logging.getLogger("camera_offloader")


@dataclass
class ImportStats:
    found: int = 0
    source_bytes: int = 0
    destination_copies: int = 0
    destination_skips: int = 0
    verified_copies: int = 0
    failed: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def copied(self) -> int: return self.destination_copies

    @property
    def skipped(self) -> int: return self.destination_skips

    @property
    def verified(self) -> int: return self.verified_copies

    @property
    def successful(self) -> bool: return self.failed == 0 and not self.errors


@dataclass
class DestinationResult:
    destination: Path
    status: str  # copied, skipped, failed
    path: Optional[Path] = None
    message: str = ""


@dataclass(frozen=True)
class VolumeIdentity:
    mount_point: Path
    device_identifier: Optional[str]
    volume_uuid: Optional[str]


@dataclass(frozen=True)
class DestinationSnapshot:
    destinations: Tuple[Path, ...]
    usb_expected: bool
    usb_identity: Optional[VolumeIdentity]


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    inode: int


def configure_logging(verbose: bool = False) -> None:
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    if not HAS_ALIVE_PROGRESS:
        logger.warning("alive-progress is not installed; progress output is disabled.")


def notify(title: str, text: str) -> None:
    script = """
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, text],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning("Notification failed: %s", result.stderr.strip())
    except OSError as exc:
        logger.warning("Could not show notification: %s", exc)


@contextmanager
def import_session_lock() -> Iterator[None]:
    SESSION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = SESSION_LOCK_PATH.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Photo Offloader import session is already running.") from exc
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            try:
                SESSION_LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass


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


def find_media_files(dcim_folder: Path) -> Dict[str, List[Path]]:
    files = {"JPG": [], "RAW": [], "VIDEO": []}
    for path in dcim_folder.rglob("*"):
        try:
            if path.name.startswith(".") or not path.is_file() or path.is_symlink():
                continue
            category = MEDIA_EXTENSIONS.get(path.suffix.lower())
            if category:
                files[category].append(path)
        except OSError as exc:
            logger.error("Could not inspect %s: %s", path, exc)
    for category in files:
        files[category].sort(key=lambda p: str(p).lower())
    return files


def scan_with_retry(dcim_folder: Path) -> Dict[str, List[Path]]:
    files = find_media_files(dcim_folder)
    if any(files.values()):
        return files
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        logger.info("No matching files visible yet; checking again...")
        time.sleep(2)
        files = find_media_files(dcim_folder)
        if any(files.values()):
            break
    return files


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> FileFingerprint:
    info = path.stat()
    return FileFingerprint(info.st_size, info.st_mtime_ns, info.st_ino)


def files_match(source: Path, destination: Path, force_sha256: bool = False) -> bool:
    try:
        if source.stat().st_size != destination.stat().st_size:
            return False
        if VERIFY_WITH_SHA256 or force_sha256:
            return sha256_file(source) == sha256_file(destination)
        return True
    except OSError:
        return False


def install_without_overwrite(temp_path: Path, final_path: Path) -> bool:
    try:
        os.link(temp_path, final_path)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        unsupported = {errno.EXDEV, errno.EPERM, errno.EACCES,
                       getattr(errno, "EOPNOTSUPP", -1), getattr(errno, "ENOTSUP", -1)}
        if exc.errno not in unsupported:
            raise
    fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as output, temp_path.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _parse_exif_datetime_value(value: str) -> Optional[datetime]:
    text = value.strip().replace("Z", "+00:00").replace("/", "-")
    normalized = re.sub(
        r"^(\d{4})[-:](\d{2})[-:](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        r"\1-\2-\3 \4:\5:\6", text,
    )
    for candidate in (text, normalized):
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                     "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                     "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    return None


def get_exif_datetime(path: Path) -> Optional[datetime]:
    if Image is None or path.suffix.lower() in {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts"}:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag in (36867, 36868, 306):
                value = exif.get(tag) if exif else None
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")
                if isinstance(value, str):
                    parsed = _parse_exif_datetime_value(value)
                    if parsed:
                        return parsed
    except Exception as exc:  # best effort
        logger.debug("Could not read EXIF date for %s: %s", path, exc)
    return None


def normalize_camera_model(make: object, model: object) -> str:
    """Return a stable, filesystem-safe camera identifier from EXIF values."""
    def clean(value: object) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if not isinstance(value, str):
            return ""
        return re.sub(r"\\s+", " ", value).strip()

    make_text = clean(make)
    model_text = clean(model)
    if make_text and model_text:
        combined = model_text if model_text.lower().startswith(make_text.lower()) else f"{make_text} {model_text}"
    else:
        combined = model_text or make_text
    combined = re.sub(r"[^A-Za-z0-9._-]+", "-", combined)
    combined = re.sub(r"-+", "-", combined).strip(".-_")
    return combined or "Unknown-Camera"


def get_camera_model_from_exiftool(path: Path) -> str:
    """Read camera make/model from video metadata when exiftool is available."""
    if path.suffix.lower() not in {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts"}:
        return "Unknown-Camera"
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return "Unknown-Camera"
    try:
        result = subprocess.run(
            [exiftool, "-j", "-Make", "-Model", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "Unknown-Camera"
        metadata = json.loads(result.stdout)
        if not metadata:
            return "Unknown-Camera"
        item = metadata[0]
        return normalize_camera_model(item.get("Make"), item.get("Model"))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not read video camera metadata for %s: %s", path, exc)
        return "Unknown-Camera"


def get_camera_model(path: Path) -> str:
    """Read camera make/model, using EXIF or video metadata when available."""
    if path.suffix.lower() in {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts"}:
        return get_camera_model_from_exiftool(path)
    if Image is None:
        return "Unknown-Camera"
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if exif:
                return normalize_camera_model(exif.get(271), exif.get(272))
    except Exception as exc:  # best effort
        logger.debug("Could not read camera EXIF for %s: %s", path, exc)
    return "Unknown-Camera"


def get_month_folder(source: Path) -> str:
    timestamp = get_exif_datetime(source)
    if timestamp is None:
        info = source.stat()
        birth = getattr(info, "st_birthtime", None) if USE_FILE_BIRTH_TIME else None
        timestamp = datetime.fromtimestamp(birth if birth is not None else info.st_mtime)
    return timestamp.strftime("%Y-%m")


def get_media_type_folder(source: Path) -> str:
    """Map a supported media extension to its human-friendly destination folder."""
    category = MEDIA_EXTENSIONS.get(source.suffix.lower())
    return {"JPG": "Photos", "RAW": "Raws", "VIDEO": "Movies"}.get(category, "Other")


def build_destination_path(source: Path, dcim_folder: Path, destination_base: Path) -> Path:
    return (
        destination_base
        / get_month_folder(source)
        / get_camera_model(source)
        / get_media_type_folder(source)
        / source.name
    )


def copy_one_to_destination(source: Path, dcim_folder: Path, destination_base: Path) -> DestinationResult:
    before = file_fingerprint(source)
    final_path = build_destination_path(source, dcim_folder, destination_base)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists():
        if final_path.is_file() and files_match(source, final_path, force_sha256=True):
            return DestinationResult(destination_base, "skipped", final_path, "already exists and matches source")
        stem, suffix = final_path.stem, final_path.suffix
        counter = 1
        while True:
            candidate = final_path.with_name(f"{stem}__conflict-{counter}{suffix}")
            if not candidate.exists():
                final_path = candidate
                break
            counter += 1
        logger.warning("Filename conflict for %s; using %s", source, final_path.name)

    temp_path: Optional[Path] = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".photo-copy-", suffix=".tmp", dir=final_path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        shutil.copy2(source, temp_path)
        after_copy = file_fingerprint(source)
        if after_copy != before:
            raise IOError(f"Source changed during copy: {source}")
        if not files_match(source, temp_path):
            raise IOError(f"Verification failed for temporary copy: {source}")
        if not install_without_overwrite(temp_path, final_path):
            if files_match(source, final_path, force_sha256=True):
                return DestinationResult(destination_base, "skipped", final_path,
                                         "another process created a matching file")
            raise FileExistsError(f"Destination appeared during copy and does not match source: {final_path}")
        if not files_match(source, final_path):
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove failed destination %s", final_path)
            raise IOError(f"Final verification failed: {final_path}")

        # Camera media can arrive with owner-only or executable mode bits
        # (for example, 0700). Do not propagate those source permissions to
        # imported photos/videos. Imported media should be regular user files,
        # not executables, while remaining private to the importing user.
        os.chmod(final_path, stat.S_IRUSR | stat.S_IWUSR)
        return DestinationResult(destination_base, "copied", final_path, "copied and verified")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary file %s: %s", temp_path, exc)


def usb_is_mounted() -> bool:
    return USB_DEST_VOLUME.is_dir() and USB_DEST_VOLUME.is_mount()


def get_volume_identity(volume_path: Path) -> Optional[VolumeIdentity]:
    if not volume_path.is_dir():
        return None
    try:
        result = subprocess.run(["diskutil", "info", "-plist", str(volume_path)],
                                check=False, capture_output=True)
        if result.returncode != 0:
            return None
        info = plistlib.loads(result.stdout)
        return VolumeIdentity(volume_path.resolve(), info.get("DeviceIdentifier"), info.get("VolumeUUID"))
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def volume_identity_matches(expected: Optional[VolumeIdentity], current: Optional[VolumeIdentity]) -> bool:
    if expected is None:
        return True
    if current is None or expected.mount_point != current.mount_point:
        return False
    if expected.volume_uuid and current.volume_uuid:
        return expected.volume_uuid == current.volume_uuid
    if expected.device_identifier and current.device_identifier:
        return expected.device_identifier == current.device_identifier
    return expected.mount_point == current.mount_point


def snapshot_destinations(no_usb: bool = False) -> DestinationSnapshot:
    if no_usb or not usb_is_mounted():
        return DestinationSnapshot((LOCAL_DEST_ROOT,), False, None)
    identity = get_volume_identity(USB_DEST_VOLUME)
    return DestinationSnapshot((LOCAL_DEST_ROOT, USB_DEST_ROOT), True, identity)


def build_destinations() -> List[Path]:
    return list(snapshot_destinations().destinations)


def required_source_bytes(files: Dict[str, List[Path]]) -> int:
    total = 0
    for items in files.values():
        for path in items:
            total += path.stat().st_size
    return total


def available_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def validate_destinations(destinations: Sequence[Path], required_bytes: int = 0) -> None:
    for destination in destinations:
        try:
            destination.mkdir(parents=True, exist_ok=True)
            probe = destination / ".camera-offloader-write-test"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
            if required_bytes:
                free = available_bytes(destination)
                if free < required_bytes + MIN_FREE_SPACE_BUFFER_BYTES:
                    raise RuntimeError(
                        f"Insufficient free space at {destination}: {free} bytes available, "
                        f"{required_bytes} bytes required plus safety buffer."
                    )
        except OSError as exc:
            raise RuntimeError(f"Destination is not writable: {destination}: {exc}") from exc


def copy_category(files: Sequence[Path], dcim_folder: Path, destinations: Sequence[Path],
                  label: str, stats: ImportStats) -> None:
    if not files:
        return
    logger.info("%s: processing %d file(s)", label, len(files))
    with alive_bar(len(files), title=label, bar="filling", monitor=True, force_tty=False) as bar:
        for source in files:
            results: List[DestinationResult] = []
            for destination in destinations:
                try:
                    result = copy_one_to_destination(source, dcim_folder, destination)
                    results.append(result)
                    if result.status == "copied":
                        stats.destination_copies += 1
                        stats.verified_copies += 1
                    elif result.status == "skipped":
                        stats.destination_skips += 1
                        stats.verified_copies += 1
                    else:
                        stats.failed += 1
                        stats.errors.append((str(source), result.message or "copy failed"))
                except Exception as exc:
                    stats.failed += 1
                    message = f"{source} -> {destination}: {exc}"
                    stats.errors.append((str(source), message))
                    logger.exception(message)
            if len(results) != len(destinations):
                logger.error("One or more destinations failed for %s", source)
            bar()


def eject_volume(volume_path: Path) -> bool:
    if not volume_path.is_mount():
        logger.info("Skipping eject: %s is not mounted.", volume_path)
        return False
    try:
        result = subprocess.run(["diskutil", "eject", str(volume_path)], check=False,
                                capture_output=True, text=True)
        if result.returncode == 0:
            logger.info("Ejected: %s", volume_path)
            return True
        logger.error("Could not eject %s: %s", volume_path, result.stderr.strip() or result.stdout.strip())
        return False
    except OSError as exc:
        logger.error("Could not eject %s: %s", volume_path, exc)
        return False


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely import camera media to local and backup storage.")
    parser.add_argument("--source", type=Path, help="Camera volume or DCIM path.")
    parser.add_argument("--no-usb", action="store_true", help="Use local storage only.")
    parser.add_argument("--no-eject", action="store_true", help="Do not eject the camera card.")
    parser.add_argument("--sha256", action="store_true", help="Use SHA-256 for all copy verification.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    return parser.parse_args(argv)


def print_summary(dcim_folder: Path, destinations: Sequence[Path], stats: ImportStats,
                  eject_attempted: bool, eject_success: bool) -> None:
    print("\n" + "=" * 64)
    print("CAMERA IMPORT SUMMARY")
    print("=" * 64)
    print(f"Source: {dcim_folder}")
    print("Destinations:")
    for destination in destinations:
        print(f"  - {destination}")
    print(f"Files found:          {stats.found}")
    print(f"Source bytes:         {stats.source_bytes}")
    print(f"Destination copies:   {stats.destination_copies}")
    print(f"Destination skips:    {stats.destination_skips}")
    print(f"Verified copies:      {stats.verified_copies}")
    print(f"Failures:             {stats.failed}")
    print(f"Eject:                {'SUCCESS' if eject_attempted and eject_success else 'FAILED' if eject_attempted else 'not requested'}")
    if stats.errors:
        print("\nErrors:")
        for source, message in stats.errors:
            print(f"  - {source}: {message}")
    print("=" * 64)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    global VERIFY_WITH_SHA256
    VERIFY_WITH_SHA256 = args.sha256
    configure_logging(args.verbose)
    logger.info("--- Starting new photo offloader session ---")

    try:
        with import_session_lock():
            if args.source:
                logger.info("Using explicit source path: %s", args.source)
                dcim_folder = resolve_dcim_path(args.source)
            else:
                dcim_folder = find_card_dcim()
            card_volume = dcim_folder.parent.resolve()
            logger.info("Source card volume: %s", card_volume)
            logger.info("Scanning: %s", dcim_folder)
            time.sleep(1)
            files = scan_with_retry(dcim_folder)
            if not any(files.values()):
                raise RuntimeError(f"No supported media files found under {dcim_folder}")

            total_found = sum(len(items) for items in files.values())
            stats = ImportStats(found=total_found, source_bytes=required_source_bytes(files))
            logger.info("Scan complete: %d files (%d JPG, %d RAW, %d VIDEO)",
                        total_found, len(files["JPG"]), len(files["RAW"]), len(files["VIDEO"]))
            snapshot = snapshot_destinations(args.no_usb)
            validate_destinations(snapshot.destinations, stats.source_bytes)
            logger.info("Import destinations: %s", ", ".join(map(str, snapshot.destinations)))
            if snapshot.usb_expected:
                logger.info("USB destination is mounted and will be used.")
            else:
                logger.info("USB destination is not required for this run.")

            for category in ("JPG", "RAW", "VIDEO"):
                copy_category(files[category], dcim_folder, snapshot.destinations, category, stats)

            if snapshot.usb_expected:
                if not usb_is_mounted() or not volume_identity_matches(snapshot.usb_identity, get_volume_identity(USB_DEST_VOLUME)):
                    stats.failed += 1
                    message = "Configured USB backup volume was disconnected or replaced during the import."
                    stats.errors.append((str(USB_DEST_VOLUME), message))
                    logger.error(message)

            eject_attempted = False
            eject_success = False
            if stats.failed == 0 and EJECT_CARD_AFTER_SUCCESS and not args.no_eject:
                eject_attempted = True
                eject_success = eject_volume(card_volume)

            print_summary(dcim_folder, snapshot.destinations, stats, eject_attempted, eject_success)
            if stats.failed:
                logger.error("Import completed with %d failure(s). Card was not automatically ejected.", stats.failed)
                notify("Photo Import Incomplete", f"{stats.failed} import failure(s). Card was not automatically ejected.")
                return 1
            if eject_attempted and not eject_success:
                logger.error("Import succeeded, but automatic card ejection failed.")
                notify("Photo Import Complete — Eject Failed", "All files imported successfully, but the camera card could not be ejected.")
                return 2
            logger.info("Import completed successfully: %d destination copies, %d destination skips.",
                        stats.destination_copies, stats.destination_skips)
            notify("Photo Import Complete", f"{total_found} media files processed successfully.")
            return 0
    except KeyboardInterrupt:
        logger.warning("Process canceled by user.")
        notify("Photo Import Canceled", "The import was canceled.")
        return 130
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        notify("Photo Import Stopped", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
