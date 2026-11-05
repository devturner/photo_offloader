"""Capture date and camera model (EXIF, ExifTool) and where each file goes in the archive."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from photo_offloader import card, catalog, config, progress
from photo_offloader.logs import logger

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


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


def get_exiftool_metadata_batch(paths: List[Path], tags: List[str], batch_size: int = 100) -> dict:
    """Read selected tags for many files per ExifTool process, keyed by absolute path."""
    exiftool = find_exiftool()
    if not exiftool or not paths:
        return {}
    results = {}
    total_batches = (len(paths) + batch_size - 1) // batch_size
    started = time.monotonic()
    logger.info(
        "ExifTool metadata phase started: %d uncached files in %d batches (batch size %d).",
        len(paths), total_batches, batch_size,
    )
    for offset in range(0, len(paths), batch_size):
        batch = paths[offset:offset + batch_size]
        batch_number = offset // batch_size + 1
        batch_started = time.monotonic()
        logger.info(
            "ExifTool heartbeat: starting batch %d/%d (%d files; %d/%d submitted).",
            batch_number, total_batches, len(batch), offset, len(paths),
        )
        try:
            result = subprocess.run(
                [exiftool, "-j", "-charset", "filename=UTF8", *tags, *map(str, batch)],
                check=False, capture_output=True, text=True, timeout=max(15, len(batch) // 2),
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.warning("ExifTool batch %d/%d returned no metadata after %.1fs: %s",
                               batch_number, total_batches, time.monotonic() - batch_started,
                               result.stderr.strip())
                continue
            records = json.loads(result.stdout)
            batch_results = 0
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict) or not record.get("SourceFile"):
                    continue
                results[str(Path(record["SourceFile"]).resolve())] = record
                batch_results += 1
            logger.info(
                "ExifTool heartbeat: finished batch %d/%d; metadata records %d/%d; "
                "%.1fs this batch, %.1fs total.",
                batch_number, total_batches, min(offset + len(batch), len(paths)), len(paths),
                time.monotonic() - batch_started, time.monotonic() - started,
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            logger.warning("ExifTool batch %d/%d failed after %.1fs: %s",
                           batch_number, total_batches, time.monotonic() - batch_started, exc)
    logger.info("ExifTool metadata phase complete: %d/%d records in %.1fs.",
                len(results), len(paths), time.monotonic() - started)
    return results


def get_exiftool_metadata(path: Path, tags: List[str]) -> dict:
    """Read selected metadata with ExifTool for formats Pillow cannot decode."""
    exiftool = find_exiftool()
    if not exiftool:
        logger.debug("exiftool is unavailable; metadata fallback skipped for %s", path)
        return {}
    try:
        result = subprocess.run(
            [exiftool, "-j", *tags, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug("ExifTool returned no metadata for %s: %s", path, result.stderr.strip())
            return {}
        metadata = json.loads(result.stdout)
        return metadata[0] if metadata and isinstance(metadata[0], dict) else {}
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not read ExifTool metadata for %s: %s", path, exc)
        return {}


def get_exif_datetime(path: Path) -> Optional[datetime]:
    if path.suffix.lower() in card.VIDEO_EXTENSIONS or path.suffix.lower() not in card.MEDIA_EXTENSIONS:
        return None

    # Pillow often cannot identify proprietary camera RAW formats such as Nikon NEF.
    if path.suffix.lower() in card.RAW_EXTENSIONS:
        metadata = get_exiftool_metadata(
            path, ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate"]
        )
        for tag in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
            value = metadata.get(tag)
            if isinstance(value, str):
                parsed = _parse_exif_datetime_value(value)
                if parsed:
                    return parsed
        return None

    if Image is not None:
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

    # Fallback is useful for formats that Pillow cannot open despite having EXIF.
    metadata = get_exiftool_metadata(
        path, ["-DateTimeOriginal", "-CreateDate", "-DateTimeDigitized", "-ModifyDate"]
    )
    for tag in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
        value = metadata.get(tag)
        if isinstance(value, str):
            parsed = _parse_exif_datetime_value(value)
            if parsed:
                return parsed
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
    make_prefix = re.sub(r"\s+(?:corporation|corp\.?|inc\.?|co\.?)$", "", make_text, flags=re.IGNORECASE)
    if make_text and model_text:
        combined = (
            model_text
            if model_text.lower().startswith(make_prefix.lower())
            else f"{make_prefix} {model_text}"
        )
    else:
        combined = model_text or make_prefix or make_text
    combined = re.sub(r"[^A-Za-z0-9._-]+", "-", combined)
    combined = re.sub(r"-+", "-", combined).strip(".-_")
    return combined or "Unknown-Camera"


@lru_cache(maxsize=1)
def find_exiftool() -> Optional[str]:
    """Find exiftool once per importer process, even with launchd’s minimal PATH."""
    exiftool = shutil.which("exiftool")
    if exiftool:
        return exiftool

    # launchd jobs often do not inherit Homebrew's PATH entries.
    for candidate in (
        Path("/opt/homebrew/bin/exiftool"),
        Path("/usr/local/bin/exiftool"),
        Path("/opt/local/bin/exiftool"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            logger.debug("Found exiftool at its standard install path: %s", candidate)
            return str(candidate)
    return None


def get_camera_model_from_exiftool(path: Path) -> str:
    """Read camera make/model from video or camera RAW metadata."""
    if path.suffix.lower() not in card.VIDEO_EXTENSIONS and path.suffix.lower() not in card.RAW_EXTENSIONS:
        return "Unknown-Camera"
    metadata = get_exiftool_metadata(path, ["-Make", "-Model"])
    return normalize_camera_model(metadata.get("Make"), metadata.get("Model"))


def get_camera_model(path: Path) -> str:
    """Read camera make/model once per unchanged file and cache it in SQLite."""
    if path.suffix.lower() not in card.MEDIA_EXTENSIONS:
        return "Unknown-Camera"
    cached = catalog.catalog_row(path)
    if cached and cached[5]:
        return str(cached[5])
    suffix = path.suffix.lower()
    model = ""
    if suffix in card.VIDEO_EXTENSIONS or suffix in card.RAW_EXTENSIONS:
        model = get_camera_model_from_exiftool(path)
    else:
        if Image is not None:
            try:
                with Image.open(path) as image:
                    exif = image.getexif()
                    if exif:
                        model = normalize_camera_model(exif.get(271), exif.get(272))
            except Exception as exc:  # best effort
                logger.debug("Could not read camera EXIF for %s: %s", path, exc)
        if not model or model == "Unknown-Camera":
            metadata = get_exiftool_metadata(path, ["-Make", "-Model"])
            model = normalize_camera_model(metadata.get("Make"), metadata.get("Model"))
    catalog.catalog_record(path, camera_model=model or "Unknown-Camera")
    return model or "Unknown-Camera"


def _month_from_file_time(source: Path) -> str:
    info = source.stat()
    birth = getattr(info, "st_birthtime", None) if config.USE_FILE_BIRTH_TIME else None
    return datetime.fromtimestamp(birth if birth is not None else info.st_mtime).strftime("%Y-%m")


def get_month_folder(source: Path) -> str:
    cached = catalog.catalog_row(source)
    if cached and cached[4]:
        return str(cached[4])
    timestamp = get_exif_datetime(source)
    month = timestamp.strftime("%Y-%m") if timestamp is not None else _month_from_file_time(source)
    catalog.catalog_record(source, capture_month=month)
    return month


EXIF_DATE_TAGS = ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate")


def prefetch_card_metadata(files: Dict[str, List[Path]]) -> int:
    """Read capture month and camera for uncached RAW/video files in batched ExifTool runs.

    The per-file path (get_month_folder/get_camera_model) starts ExifTool twice per
    RAW, about 0.3 s each time; for a fresh card of ~1,200 RAWs that is minutes
    before copying starts. Results are cached in the catalog, which the per-file
    path then reads. It produces the same values as the per-file path: RAW months
    come from EXIF dates, video months from file time (see get_exif_datetime).
    Anything the batch cannot answer is left to the per-file path.
    """
    candidates: List[Path] = []
    for category in ("RAW", "VIDEO"):
        for path in files.get(category, []):
            row = catalog.catalog_row(path)
            if not (row and row[4] and row[5]):
                candidates.append(path)
    if not candidates or not find_exiftool():
        return 0
    progress.update_status(state="scanning", phase="card-to-ssd", stage="metadata", current_file="",
                  message=f"Reading photo info for {len(candidates):,} new files")
    metadata = get_exiftool_metadata_batch(candidates, [f"-{tag}" for tag in EXIF_DATE_TAGS] + ["-Make", "-Model"])
    entries: List[Tuple[Path, Dict[str, Optional[str]]]] = []
    for path in candidates:
        record = metadata.get(str(path.resolve()))
        if not record:
            continue
        timestamp = None
        if path.suffix.lower() in card.RAW_EXTENSIONS:
            for tag in EXIF_DATE_TAGS:
                value = record.get(tag)
                if isinstance(value, str):
                    timestamp = _parse_exif_datetime_value(value)
                    if timestamp:
                        break
        try:
            month = timestamp.strftime("%Y-%m") if timestamp else _month_from_file_time(path)
        except OSError:
            continue
        entries.append((path, {"capture_month": month,
                               "camera_model": normalize_camera_model(record.get("Make"), record.get("Model"))}))
    return catalog.catalog_record_many(entries)


def get_media_type_folder(source: Path) -> str:
    """Map a supported media extension to its human-friendly destination folder."""
    category = card.MEDIA_EXTENSIONS.get(source.suffix.lower())
    return {"JPG": "Photos", "RAW": "Raws", "VIDEO": "Movies"}.get(category, "Other")


def local_root_for_category(category: str) -> Path:
    """Choose the top-level local archive by media category."""
    return config.LOCAL_RAW_DEST_ROOT if category == "RAW" else config.LOCAL_DEST_ROOT


def build_destination_path(source: Path, dcim_folder: Path, destination_base: Path) -> Path:
    return (
        destination_base
        / get_month_folder(source)
        / get_camera_model(source)
        / get_media_type_folder(source)
        / source.name
    )
