"""File primitives: fingerprints, hashing, verified installs and chunked copies."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from photo_offloader import config, progress
from photo_offloader.logs import logger

COPY_CHUNK_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int
    inode: int


_HOUR_NS = 3600 * 1_000_000_000


def card_fingerprint_unchanged(entry: object, fingerprint: "FileFingerprint") -> bool:
    """Same size and mtime, allowing an exact whole-hour mtime shift (up to 24 h).

    Cameras write FAT/exFAT timestamps in local time, so after a DST change or a
    time-zone change macOS reports every file on the card N hours off, to the
    nanosecond. Treating that as "changed" re-hashes the whole card. A genuine
    edit that keeps the size and moves mtime by an exact multiple of 3600.000 s
    does not happen in practice; anything else still counts as changed.
    """
    if not isinstance(entry, dict) or entry.get("size") != fingerprint.size:
        return False
    recorded = entry.get("mtime_ns")
    if not isinstance(recorded, int):
        return False
    delta = abs(fingerprint.mtime_ns - recorded)
    return delta % _HOUR_NS == 0 and delta <= 24 * _HOUR_NS


def sha256_file(path: Path, chunk_size: int = 1024 * 1024, nocache: bool = False) -> str:
    """SHA-256 of a file. ``nocache`` bypasses the macOS page cache so a verification
    read comes from the disk that was just written, not from RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if nocache and hasattr(fcntl, "F_NOCACHE"):
            try:
                fcntl.fcntl(handle.fileno(), fcntl.F_NOCACHE, 1)
            except OSError as exc:
                # The hash is still checked, but it may read from RAM rather than the disk.
                logger.debug("Could not bypass the disk cache for %s: %s", path, exc)
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
        if config.VERIFY_WITH_SHA256 or force_sha256:
            return sha256_file(source) == sha256_file(destination)
        return True
    except OSError:
        return False


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


# Index/database files the camera itself rewrites on every shoot. They never match
# the previous copy, so keeping conflict copies would pile up one per import; the
# newest version replaces the old one instead (locally and on USB).
CAMERA_INDEX_FILE_NAMES = frozenset(
    {
        "NC_FLLST.DAT",  # Nikon file list
        "MEDIAPRO.XML",  # Sony clip index (PRIVATE/M4ROOT)
    }
)


def is_camera_index_file(path: Path) -> bool:
    return path.name.upper() in CAMERA_INDEX_FILE_NAMES


def install_file(temp_path: Path, final_path: Path, replace: bool = False) -> bool:
    """Install a verified temp file; only camera index files may replace an existing file."""
    if replace:
        os.replace(temp_path, final_path)  # same directory, so atomic
        return True
    return install_without_overwrite(temp_path, final_path)


def install_without_overwrite(temp_path: Path, final_path: Path) -> bool:
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
    try:
        fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as output, temp_path.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            final_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning("Could not remove the partial copy %s: %s", final_path, cleanup_exc)
        raise
    return True


def copy_file_with_progress(source: Path, destination: Path) -> str:
    """Copy in chunks so progress and ETA update even for large video files.

    Returns the SHA-256 of the bytes that were read from ``source``, computed in
    the same pass, so the source never has to be read a second time to verify.
    """
    digest = hashlib.sha256()
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            output_file.write(chunk)
            progress.add_progress_bytes(len(chunk), transferred=True)
        output_file.flush()
        os.fsync(output_file.fileno())
    shutil.copystat(source, destination, follow_symlinks=True)
    return digest.hexdigest()
