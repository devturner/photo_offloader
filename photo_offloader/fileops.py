"""File primitives: fingerprints, hashing, verified installs and chunked copies."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import mmap
import os
import shutil
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from photo_offloader import config, logs, progress
from photo_offloader.logs import logger

COPY_CHUNK_BYTES = 4 * 1024 * 1024
# Real neighbouring photos differ within the first 750 bytes (308 pairs). On an SD card, reading
# 64 KB at each end costs 2.3 ms a file; 1 MB costs 23 ms, over a minute on a full card.
ENDS_CHECK_BYTES = 64 * 1024
_MAP_FAILED = ctypes.c_void_p(-1).value
_MS_INVALIDATE = 0x0002  # sys/mman.h; the mmap module doesn't offer it


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


def _skip_page_cache(fd: int, path: Path) -> None:
    """Keep this handle's reads and writes out of the macOS page cache.

    Both sides need it. F_NOCACHE on a read still returns pages already in RAM, so a copy
    written without it is "verified" against memory, not the disk (a 50 MB copy: 13.5 GB/s
    with its pages in RAM, 1.4 GB/s from the disk).
    """
    if hasattr(fcntl, "F_NOCACHE"):
        try:
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        except OSError as exc:
            # The hash is still checked, but it may read from RAM rather than the disk.
            logger.debug("Could not bypass the disk cache for %s: %s", path, exc)


def flush_to_disk(fd: int) -> None:
    """Get what was written out of the drive's own cache. Raises OSError if that fails.

    On macOS fsync only hands data to the drive, which can lose it in a power cut or an
    unplug (0.02 ms against 5 ms for F_FULLFSYNC). Some drives don't support the full
    flush, and fsync is the best they offer.
    """
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as exc:
            logger.debug("Full flush is unavailable; using fsync: %s", exc)
    os.fsync(fd)


@logs.timed("hashing")
def sha256_file(path: Path, chunk_size: int = 1024 * 1024, nocache: bool = False) -> str:
    """SHA-256 of a file. ``nocache`` reads from the disk when the file was written with
    the page cache skipped, as copies are; see _skip_page_cache."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if nocache:
            _skip_page_cache(handle.fileno(), path)
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def forget_cached_pages(path: Path) -> bool:
    """Drop the file's pages from RAM, so the next uncached read has to go to the disk.

    F_NOCACHE alone doesn't do it: a file an upload has just read reads at 14 GB/s from memory with
    it, and at 2.2 GB/s after this (50 MB, internal SSD). The mapping is read-only and is never
    touched, so this reads nothing and can't change the file. Returns whether it worked.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_long,
    ]
    libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if not size:
            return True
        address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, handle.fileno(), 0)
        if address == _MAP_FAILED:
            return False
        try:
            return libc.msync(address, size, _MS_INVALIDATE) == 0
        finally:
            libc.munmap(address, size)


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


@logs.timed("end checks")
def ends_match(first: Path, second: Path) -> bool:
    """Whether two files have the same size and the same first and last ``ENDS_CHECK_BYTES``.

    A quick check that a cached hash still describes the file, not proof that they're the same:
    two different photos differ here, in their capture details and image data.
    """
    size = os.stat(first).st_size
    if os.stat(second).st_size != size:
        return False
    with open(first, "rb") as one, open(second, "rb") as other:
        if one.read(ENDS_CHECK_BYTES) != other.read(ENDS_CHECK_BYTES):
            return False
        tail_start = max(size - ENDS_CHECK_BYTES, 0)
        one.seek(tail_start)
        other.seek(tail_start)
        return one.read(ENDS_CHECK_BYTES) == other.read(ENDS_CHECK_BYTES)


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


# Index/database files the camera itself rewrites. A card has one per folder, and they would all be
# filed under one archive path, so a copy would never be a backup: imports leave them out. The USB
# backup still replaces one that differs, because older imports left them in the archive.
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
    elif not install_without_overwrite(temp_path, final_path):
        return False
    _flush_folder(final_path.parent)
    return True


def _flush_folder(folder: Path) -> None:
    """Make a new name in ``folder`` survive a power cut, as well as the file's contents."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        flush_to_disk(fd)
    finally:
        os.close(fd)


@contextmanager
def _cancel_signals_held() -> Iterator[None]:
    """Hold SIGTERM and SIGINT until the block ends, so a cancel can't land partway through it.

    A cancel arrives as a KeyboardInterrupt between any two bytecodes, including after a file is
    created but before anything has recorded that, where no cleanup could know to remove it.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


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
    # No hard links (exFAT): claim the name with an empty file, then rename the verified file over
    # it. Only the file created here is ever replaced, and no partial copy gets the final name.
    with _cancel_signals_held():
        try:
            fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            return False
        try:
            os.close(fd)
            os.replace(temp_path, final_path)
        except BaseException:
            try:
                final_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                logger.warning("Could not remove the empty file %s: %s", final_path, cleanup_exc)
            raise
    return True


@logs.timed("copying")
def _read_source(input_file: BinaryIO, source: Path) -> bytes:
    """Read a chunk. A read error names the file, which ``open`` does but ``read`` doesn't.

    That's how a report tells a failing card from a failing destination.
    """
    try:
        return input_file.read(COPY_CHUNK_BYTES)
    except OSError as exc:
        raise OSError(exc.errno, exc.strerror, str(source)) from exc


def copy_file_with_progress(source: Path, destination: Path) -> str:
    """Copy in chunks so progress and ETA update even for large video files.

    Returns the SHA-256 of the bytes that were read from ``source``, computed in
    the same pass, so the source never has to be read a second time to verify.
    """
    digest = hashlib.sha256()
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        _skip_page_cache(output_file.fileno(), destination)
        while chunk := _read_source(input_file, source):
            digest.update(chunk)
            output_file.write(chunk)
            progress.add_progress_bytes(len(chunk), transferred=True)
        output_file.flush()
        shutil.copystat(source, destination, follow_symlinks=True)  # flushed with the data
        flush_to_disk(output_file.fileno())
    return digest.hexdigest()
