#!/usr/bin/env python3
"""
Camera Card Offloader v2
------------------------
macOS-focused camera media importer.

Workflow:
    1. Detect/select a camera card/DCIM folder.
    2. Scan supported media under DCIM.
    3. Copy each file to local storage and, when mounted, the configured USB
       destination.
    4. Verify every completed copy by size and optionally SHA-256.
    5. Never overwrite an existing destination file.
    6. Eject the card only when the required destinations have succeeded.
    7. Write a session log and send a macOS notification.

Designed for Python 3.9+ on macOS.
Dependencies:
    python3 -m pip install -r requirements.txt
    # alive-progress is optional; Pillow is used for EXIF dates
"""

from __future__ import annotations

import argparse
import errno
import glob
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from alive_progress import alive_bar
except ImportError:
    HAS_ALIVE_PROGRESS = False

    class _NullProgress:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def __call__(self, *args, **kwargs):
            pass

    def alive_bar(*args, **kwargs):
        return _NullProgress()
else:
    HAS_ALIVE_PROGRESS = True

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency at runtime
    Image = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]

LOCAL_DEST_ROOT = Path.home() / "Pictures" / "CameraImports"

USB_DEST_VOLUME = Path("/Volumes/Photos")
USB_DEST_ROOT = USB_DEST_VOLUME / "CameraImports"

LOG_FILE_PATH = Path.home() / ".camera_transfer.log"

WAIT_SECONDS = 30
EJECT_CARD_AFTER_SUCCESS = True

# Media recognized by the importer.
MEDIA_EXTENSIONS = {
    ".jpg": "JPG",
    ".jpeg": "JPG",
    ".heic": "JPG",
    ".heif": "JPG",
    ".nef": "RAW",
    ".arw": "RAW",
    ".cr2": "RAW",
    ".cr3": "RAW",
    ".dng": "RAW",
    ".raf": "RAW",
    ".orf": "RAW",
    ".rw2": "RAW",
    ".mov": "VIDEO",
    ".mp4": "VIDEO",
    ".avi": "VIDEO",
    ".mts": "VIDEO",
    ".m2ts": "VIDEO",
}

# SHA-256 is safer than size-only verification, but costs extra I/O.
VERIFY_WITH_SHA256 = False

# Preserve camera folders such as 100NIKON / 100MSDCF beneath YYYY-MM.
PRESERVE_CAMERA_SUBFOLDERS = True

# If True, use the file's macOS creation time for YYYY-MM. Otherwise use mtime.
USE_FILE_BIRTH_TIME = True


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ImportStats:
    found: int = 0
    destination_copies: int = 0
    destination_skips: int = 0
    verified_copies: int = 0
    failed: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def copied(self) -> int:
        return self.destination_copies

    @property
    def skipped(self) -> int:
        return self.destination_skips

    @property
    def verified(self) -> int:
        return self.verified_copies

    @property
    def successful(self) -> bool:
        return self.failed == 0 and not self.errors


@dataclass
class DestinationResult:
    destination: Path
    status: str  # "copied", "skipped", "failed"
    path: Optional[Path] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Logging / notifications
# ---------------------------------------------------------------------------

logger = logging.getLogger("camera_offloader")


def configure_logging(verbose: bool = False) -> None:
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not HAS_ALIVE_PROGRESS:
        logger.warning(
            "alive-progress is not installed; progress output is disabled. "
            "Install it with: python3 -m pip install alive-progress"
        )

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def notify(title: str, text: str) -> None:
    """Show a macOS notification without making notification failure fatal."""
    script = """
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, text],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("Notification failed: %s", result.stderr.strip())
    except OSError as exc:
        logger.warning("Could not show notification: %s", exc)


# ---------------------------------------------------------------------------
# Card discovery
# ---------------------------------------------------------------------------

def find_card_dcim() -> Path:
    """Find an automatically recognized DCIM folder or prompt for one."""
    matches: List[Path] = []

    for pattern in CARD_VOLUME_GLOBS:
        for raw_path in glob.glob(pattern):
            path = Path(raw_path)
            if path.is_dir():
                matches.append(path)

    # Deduplicate while preserving deterministic order.
    matches = sorted(set(matches), key=lambda p: str(p).lower())

    default = str(matches[0].parent) if len(matches) == 1 else ""

    prompt = "Source card volume or DCIM folder"
    if default:
        prompt += f" [{default}]"
    prompt += ": "

    entered = input(prompt).strip()

    if entered:
        return resolve_dcim_path(Path(entered).expanduser())

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        print("\nMultiple matching cards found:")
        for index, path in enumerate(matches, start=1):
            print(f"  {index}. {path.parent}")

        selection = input("Enter the number of the source card: ").strip()
        try:
            return matches[int(selection) - 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Invalid card selection.") from exc

    raise RuntimeError(
        "No recognized card was found. Enter the source volume path "
        "or DCIM folder when prompted."
    )


def resolve_dcim_path(path: Path) -> Path:
    """Accept either a volume path or a DCIM path."""
    path = path.expanduser().resolve()

    if path.name.lower() == "dcim" and path.is_dir():
        return path

    dcim = path / "DCIM"
    if dcim.is_dir():
        return dcim

    raise RuntimeError(f"DCIM folder not found at {dcim if path.name.lower() != 'dcim' else path}")


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def find_media_files(dcim_folder: Path) -> Dict[str, List[Path]]:
    """Recursively find supported media files beneath DCIM."""
    files: Dict[str, List[Path]] = {
        "JPG": [],
        "RAW": [],
        "VIDEO": [],
    }

    for path in dcim_folder.rglob("*"):
        try:
            # Ignore macOS AppleDouble files and hidden metadata.
            if path.name.startswith("._") or path.name.startswith("."):
                continue

            if not path.is_file() or path.is_symlink():
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


# ---------------------------------------------------------------------------
# File verification / installation
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def files_match(source: Path, destination: Path) -> bool:
    """Verify a destination against its source."""
    try:
        if source.stat().st_size != destination.stat().st_size:
            return False

        if VERIFY_WITH_SHA256:
            return sha256_file(source) == sha256_file(destination)

        return True
    except OSError:
        return False


def install_without_overwrite(temp_path: Path, final_path: Path) -> bool:
    """
    Install a completed temporary copy without overwriting the destination.

    Returns False if another file already occupies final_path.
    """
    try:
        os.link(temp_path, final_path)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        unsupported = {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", -1),
            getattr(errno, "ENOTSUP", -1),
        }
        if exc.errno not in unsupported:
            raise

    # Cross-filesystem / filesystem-without-hard-links fallback.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(final_path, flags, 0o666)

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


def get_exif_datetime(path: Path) -> Optional[datetime]:
    """Return the EXIF capture date for an image/video when available."""
    if Image is None:
        return None

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None

            for tag in (36867, 36868, 306):
                value = exif.get(tag)
                if not value:
                    continue

                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")

                if not isinstance(value, str):
                    continue

                try:
                    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    try:
                        return datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        continue
    except Exception as exc:  # pragma: no cover - best effort metadata parsing
        logger.debug("Could not read EXIF date for %s: %s", path, exc)

    return None


def get_month_folder(source: Path) -> str:
    timestamp = get_exif_datetime(source)

    if timestamp is None:
        info = source.stat()
        value = None
        if USE_FILE_BIRTH_TIME:
            value = getattr(info, "st_birthtime", None)

        if value is None:
            value = info.st_mtime

        timestamp = datetime.fromtimestamp(value)

    return timestamp.strftime("%Y-%m")


def build_destination_path(
    source: Path,
    dcim_folder: Path,
    destination_base: Path,
) -> Path:
    month_folder = get_month_folder(source)

    if PRESERVE_CAMERA_SUBFOLDERS:
        relative_parent = source.relative_to(dcim_folder).parent
    else:
        relative_parent = Path()

    return destination_base / month_folder / relative_parent / source.name


def copy_one_to_destination(
    source: Path,
    dcim_folder: Path,
    destination_base: Path,
) -> DestinationResult:
    """Copy one source file safely to one destination and verify it."""
    final_path = build_destination_path(source, dcim_folder, destination_base)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    # Existing correct file: skip it safely.
    if final_path.exists():
        if final_path.is_file() and files_match(source, final_path):
            return DestinationResult(
                destination=destination_base,
                status="skipped",
                path=final_path,
                message="already exists and matches source",
            )

        # Never overwrite a conflicting file. Create a deterministic alternate
        # name instead, so a same-named but different file is never destroyed.
        stem = final_path.stem
        suffix = final_path.suffix

        counter = 1
        while True:
            candidate = final_path.with_name(f"{stem}__conflict-{counter}{suffix}")
            if not candidate.exists():
                final_path = candidate
                break
            counter += 1

        logger.warning(
            "Filename conflict for %s; using %s",
            source,
            final_path.name,
        )

    temp_path: Optional[Path] = None

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".photo-copy-",
            suffix=".tmp",
            dir=final_path.parent,
        )
        os.close(fd)
        temp_path = Path(temp_name)

        # copy2 preserves useful filesystem metadata where supported.
        shutil.copy2(source, temp_path)

        if not files_match(source, temp_path):
            raise IOError(f"Verification failed for temporary copy: {source}")

        installed = install_without_overwrite(temp_path, final_path)

        if not installed:
            # Another process won the race. Verify the winner.
            if files_match(source, final_path):
                return DestinationResult(
                    destination=destination_base,
                    status="skipped",
                    path=final_path,
                    message="another process created a matching file",
                )
            raise FileExistsError(
                f"Destination appeared during copy and does not match source: "
                f"{final_path}"
            )

        # Final verification.
        if not files_match(source, final_path):
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove failed destination %s", final_path)
            raise IOError(f"Final verification failed: {final_path}")

        return DestinationResult(
            destination=destination_base,
            status="copied",
            path=final_path,
            message="copied and verified",
        )

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary file %s: %s", temp_path, exc)


# ---------------------------------------------------------------------------
# Destination management
# ---------------------------------------------------------------------------

def usb_is_mounted() -> bool:
    return USB_DEST_VOLUME.is_dir() and USB_DEST_VOLUME.is_mount()


def build_destinations() -> List[Path]:
    """
    Return destinations for this session.

    Local storage is always required.
    USB is included when mounted.
    """
    destinations = [LOCAL_DEST_ROOT]

    if usb_is_mounted():
        destinations.append(USB_DEST_ROOT)
    else:
        logger.info("USB destination is not mounted: %s", USB_DEST_VOLUME)

    return destinations


def validate_destinations(destinations: Sequence[Path]) -> None:
    for destination in destinations:
        try:
            destination.mkdir(parents=True, exist_ok=True)

            # Ensure it is writable before scanning/copying.
            probe = destination / ".camera-offloader-write-test"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)

        except OSError as exc:
            raise RuntimeError(
                f"Destination is not writable: {destination}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Copy workflow
# ---------------------------------------------------------------------------

def copy_category(
    files: Sequence[Path],
    dcim_folder: Path,
    destinations: Sequence[Path],
    label: str,
    stats: ImportStats,
) -> None:
    if not files:
        return

    logger.info("%s: processing %d file(s)", label, len(files))

    with alive_bar(
        len(files),
        title=label,
        bar="filling",
        monitor=True,
        force_tty=False,
    ) as bar:
        for source in files:
            destination_results: List[DestinationResult] = []

            for destination in destinations:
                try:
                    result = copy_one_to_destination(
                        source,
                        dcim_folder,
                        destination,
                    )
                    destination_results.append(result)

                    if result.status == "copied":
                        stats.destination_copies += 1
                        stats.verified_copies += 1
                    elif result.status == "skipped":
                        stats.destination_skips += 1
                        stats.verified_copies += 1
                    elif result.status == "failed":
                        stats.failed += 1
                        message = f"{source} -> {destination}: {result.message or 'copy failed'}"
                        stats.errors.append((str(source), message))
                        logger.error(message)

                except Exception as exc:
                    stats.failed += 1
                    message = f"{source} -> {destination}: {exc}"
                    stats.errors.append((str(source), message))
                    logger.exception(message)

            # Every configured destination must succeed for this source.
            # Exceptions are already counted above. A destination absent from
            # results means it failed before producing a result.
            if len(destination_results) != len(destinations):
                logger.error("One or more destinations failed for %s", source)

            bar()


# ---------------------------------------------------------------------------
# Ejection
# ---------------------------------------------------------------------------

def eject_volume(volume_path: Path) -> bool:
    """Safely eject a mounted macOS volume."""
    if not volume_path.is_mount():
        logger.info("Skipping eject: %s is not mounted.", volume_path)
        return False

    try:
        result = subprocess.run(
            ["diskutil", "eject", str(volume_path)],
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.info("Ejected: %s", volume_path)
            return True

        message = result.stderr.strip() or result.stdout.strip()
        logger.error("Could not eject %s: %s", volume_path, message)
        return False

    except OSError as exc:
        logger.error("Could not eject %s: %s", volume_path, exc)
        return False


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely import camera media to local and mounted backup storage."
    )

    parser.add_argument(
        "--source",
        type=Path,
        help=(
            "Camera volume or DCIM path to import. If omitted, the script "
            "will detect available cards and prompt you to choose one."
        ),
    )

    parser.add_argument(
        "--no-usb",
        action="store_true",
        help="Do not copy to the configured USB destination, even if mounted.",
    )

    parser.add_argument(
        "--no-eject",
        action="store_true",
        help="Do not eject the camera card after a successful import.",
    )

    parser.add_argument(
        "--sha256",
        action="store_true",
        help="Verify copies using SHA-256 instead of size-only verification.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    return parser.parse_args(argv)


def print_summary(
    dcim_folder: Path,
    destinations: Sequence[Path],
    stats: ImportStats,
    eject_attempted: bool,
    eject_success: bool,
) -> None:
    print("\n" + "=" * 64)
    print("CAMERA IMPORT SUMMARY")
    print("=" * 64)
    print(f"Source:       {dcim_folder}")
    print("Destinations:")
    for destination in destinations:
        print(f"  - {destination}")
    print(f"Files found:  {stats.found}")
    print(f"Destination copies: {stats.destination_copies}")
    print(f"Destination skips:  {stats.destination_skips}")
    print(f"Verified copies:    {stats.verified_copies}")
    print(f"Failures:     {stats.failed}")

    if eject_attempted:
        print(f"Eject:        {'SUCCESS' if eject_success else 'FAILED'}")
    else:
        print("Eject:        not requested")

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
        if args.source:
            logger.info("Using explicit source path: %s", args.source)
            dcim_folder = resolve_dcim_path(args.source)
        else:
            print("Select the source card.")
            logger.info("No source provided; detecting mounted camera card(s).")
            dcim_folder = find_card_dcim()

        card_volume = dcim_folder.parent

        logger.info("Source card volume: %s", card_volume)
        logger.info("Scanning: %s", dcim_folder)

        # Let a newly mounted card settle.
        time.sleep(1)

        files = scan_with_retry(dcim_folder)

        if not any(files.values()):
            raise RuntimeError(
                f"No supported media files found under {dcim_folder}"
            )

        total_found = sum(len(items) for items in files.values())

        stats = ImportStats(found=total_found)

        logger.info(
            "Scan complete: %d files (%d JPG, %d RAW, %d VIDEO)",
            total_found,
            len(files["JPG"]),
            len(files["RAW"]),
            len(files["VIDEO"]),
        )

        destinations = build_destinations()

        if args.no_usb:
            destinations = [LOCAL_DEST_ROOT]

        # If the USB was mounted at the beginning, require it as part of the
        # destination set. Do not silently downgrade a two-copy import.
        usb_expected = usb_is_mounted() and not args.no_usb

        validate_destinations(destinations)

        print("\nImport destinations:")
        for destination in destinations:
            print(f"  - {destination}")

        if usb_expected:
            logger.info("USB destination is mounted and will be used.")
        else:
            logger.info("USB destination is not required for this run.")

        for category in ("JPG", "RAW", "VIDEO"):
            copy_category(
                files[category],
                dcim_folder,
                destinations,
                category,
                stats,
            )

        # If USB was expected but became unavailable during the run, count the
        # session as failed rather than ejecting a card with an incomplete backup.
        if usb_expected and not usb_is_mounted():
            stats.failed += 1
            message = "USB destination was disconnected during the import."
            stats.errors.append((str(USB_DEST_VOLUME), message))
            logger.error(message)

        eject_attempted = False
        eject_success = False

        # Eject only if every required destination succeeded.
        if (
            stats.failed == 0
            and EJECT_CARD_AFTER_SUCCESS
            and not args.no_eject
        ):
            eject_attempted = True
            eject_success = eject_volume(card_volume)

        print_summary(
            dcim_folder,
            destinations,
            stats,
            eject_attempted,
            eject_success,
        )

        if stats.failed > 0:
            logger.error("Import completed with %d failure(s).", stats.failed)
            notify(
                "Photo Import Incomplete",
                f"{stats.failed} import failure(s). Card was not automatically ejected.",
            )
            return 1

        if eject_attempted and not eject_success:
            logger.error("Import succeeded, but automatic card ejection failed.")
            notify(
                "Photo Import Complete — Eject Failed",
                "All files were imported successfully, but the camera card could not be ejected.",
            )
            return 2

        logger.info(
            "Import completed successfully: %d destination copies, %d destination skips.",
            stats.destination_copies,
            stats.destination_skips,
        )
        notify(
            "Photo Import Complete",
            f"{total_found} media files processed successfully.",
        )
        return 0

    except KeyboardInterrupt:
        logger.warning("Process canceled by user.")
        print("\nProcess canceled by user.")
        notify("Photo Import Canceled", "The import was canceled.")
        return 130

    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        print(f"\nFatal error: {exc}")
        notify("Photo Import Stopped", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
