"""Mounted volumes: USB identity, free space and ejecting the card."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from photo_offloader import config
from photo_offloader.logs import logger


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


def usb_is_mounted() -> bool:
    return config.USB_DEST_VOLUME.is_dir() and config.USB_DEST_VOLUME.is_mount()


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
        return DestinationSnapshot((config.LOCAL_DEST_ROOT,), False, None)
    identity = get_volume_identity(config.USB_DEST_VOLUME)
    return DestinationSnapshot((config.LOCAL_DEST_ROOT, config.USB_DEST_ROOT), True, identity)


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
                if free < required_bytes + config.MIN_FREE_SPACE_BUFFER_BYTES:
                    raise RuntimeError(
                        f"Insufficient free space at {destination}: {free} bytes available, "
                        f"{required_bytes} bytes required plus safety buffer."
                    )
        except OSError as exc:
            raise RuntimeError(f"Destination is not writable: {destination}: {exc}") from exc


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
