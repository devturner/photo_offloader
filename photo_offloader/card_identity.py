"""Telling camera cards apart, since every card from one camera gets the same volume name.

What macOS says depends on the reader. The built-in SD slot gives the card's maker, model and
serial number, and the serial stays the same when the card is formatted. A CFexpress reader on
USB is an NVMe bridge: only the size and the bridge's name come through. Reading this only asks
macOS about the disk; nothing is written to the card.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from photo_offloader.logs import logger

TOOL_SECONDS = 10  # diskutil can wait on a failing card; an import must not

# SD card manufacturer IDs that are well known. Unknown ones are left out rather than guessed.
SD_MAKERS = {
    "0x01": "Panasonic",
    "0x02": "Toshiba",
    "0x03": "SanDisk",
    "0x1b": "Samsung",
    "0x28": "Lexar",
    "0x41": "Kingston",
    "0x74": "Transcend",
    "0x82": "Sony",
}

# Capacities as printed on cards, in GB. A size within 8% of one is shown as that one.
USUAL_GB = (2, 4, 8, 16, 32, 64, 80, 128, 160, 240, 256, 320, 480, 512, 640, 1000, 2000, 4000)


def _plist(args: list) -> Dict[str, Any]:
    try:
        result = subprocess.run(args, capture_output=True, timeout=TOOL_SECONDS, check=False)
        if result.returncode == 0:
            loaded = plistlib.loads(result.stdout)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError) as exc:
        logger.debug("Could not run %s: %s", args[0], exc)
    return {}


def _card_readers() -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", "-json", "SPCardReaderDataType"],
            capture_output=True,
            timeout=TOOL_SECONDS,
            check=False,
        )
        if result.returncode == 0:
            loaded = json.loads(result.stdout)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("Could not ask about card readers: %s", exc)
    return {}


def read(card_root: Path) -> Dict[str, Any]:
    """What can be told about the card mounted at ``card_root``; empty when nothing can.

    ``volume_name`` is the card's name, kept beside its identity rather than in it.
    """
    volume = _plist(["/usr/sbin/diskutil", "info", "-plist", str(card_root)])
    if not volume:
        return {}
    whole = str(volume.get("ParentWholeDisk") or "")
    whole_disk = _plist(["/usr/sbin/diskutil", "info", "-plist", whole]) if whole else {}
    readers = _card_readers() if volume.get("BusProtocol") == "Secure Digital" else {}
    identity = describe(volume, whole_disk, readers)
    if isinstance(volume.get("VolumeName"), str) and volume["VolumeName"].strip():
        # The card's own name. Its mount folder can differ: with two cards from one camera
        # inserted, macOS mounts the second at "NIKON Z 8   1".
        identity["volume_name"] = volume["VolumeName"]
    return identity


def _reader_card(readers: Mapping[str, Any], whole: str) -> Mapping[str, Any]:
    for reader in readers.get("SPCardReaderDataType") or []:
        for item in reader.get("_items") or [] if isinstance(reader, dict) else []:
            if isinstance(item, dict) and item.get("bsd_name") == whole:
                return item
    return {}


def _kind(volume: Mapping[str, Any], whole_disk: Mapping[str, Any]) -> Optional[str]:
    if volume.get("BusProtocol") == "Secure Digital":
        return "SD"
    name = str(whole_disk.get("MediaName") or "").upper()
    if "NVME" in name or "CFEXPRESS" in name:
        return "CFexpress"
    if "SD" in name.replace("SSD", ""):
        return "SD"
    if "CF" in name or "COMPACTFLASH" in name:
        return "CompactFlash"
    return None


def describe(
    volume: Mapping[str, Any], whole_disk: Mapping[str, Any], readers: Mapping[str, Any]
) -> Dict[str, Any]:
    """The card's kind, size, and maker, model and serial when the reader passes them on."""
    identity: Dict[str, Any] = {}
    kind = _kind(volume, whole_disk)
    if kind:
        identity["kind"] = kind
    size = whole_disk.get("TotalSize") or volume.get("TotalSize")
    if isinstance(size, int) and size > 0:
        identity["size_bytes"] = size
    card = _reader_card(readers, str(volume.get("ParentWholeDisk") or ""))
    maker = SD_MAKERS.get(str(card.get("spcardreader_card_manufacturer-id") or "").lower())
    for key, value in (
        ("maker", maker),
        ("model", card.get("spcardreader_card_productname")),
        ("serial", card.get("spcardreader_card_serialnumber")),
    ):
        if isinstance(value, str) and value.strip():
            identity[key] = value.strip()
    return identity


def capacity_text(size_bytes: int) -> str:
    """The size as printed on the card: 63.86 GB of usable space is a 64 GB card."""
    gb = size_bytes / 1e9
    nearest = min(USUAL_GB, key=lambda usual: abs(usual - gb))
    shown = nearest if abs(nearest - gb) <= nearest * 0.08 else round(gb)
    return f"{shown // 1000} TB" if shown >= 1000 and shown % 1000 == 0 else f"{shown} GB"


def label(identity: Optional[Mapping[str, Any]]) -> str:
    """For example "64 GB SanDisk SD" or "256 GB CFexpress"; empty when nothing is known."""
    if not identity:
        return ""
    parts = []
    if isinstance(identity.get("size_bytes"), int):
        parts.append(capacity_text(identity["size_bytes"]))
    parts += [str(identity[key]) for key in ("maker", "kind") if identity.get(key)]
    return " ".join(parts)
