"""Whether each camera card is safe to format: every file on it is on the Mac and on the USB drive.

A card's manifest lists every file on it at its last import, the old ones a photographer leaves
on the card included, with their SHA-256. A file counts as saved where the catalog has a verified
copy with that hash: on the Mac (or deleted from the Mac on purpose, with the drive's copy kept),
and on the USB drive. The newest check of the saved copies overrules the catalog for any copy it
found damaged or missing, until that copy is made again. A file without a hash counts as not saved.

The card needn't be inserted, so a backup a week after the import is what makes a card safe.
Only the importer and the check write ``card_safety.json`` (when each card became safe); the
dashboard and menu bar read it.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple

from photo_offloader import card_identity, catalog, config, jsonfile, verify_report
from photo_offloader.logs import logger

FILES_LISTED = 20  # files named in a card's report; the counts cover the rest


@dataclass
class Copies:
    """Hash -> (relative path, catalogued at) on each side, and what the newest check found bad."""

    mac: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    usb: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    deleted: Set[str] = field(default_factory=set)
    bad: Dict[Tuple[str, str], float] = field(default_factory=dict)  # (side, hash) -> check time

    def on_mac(self, digest: str) -> bool:
        if self._good("mac", digest, self.mac):
            return True
        usb = self.usb.get(digest)
        return bool(usb) and usb[0] in self.deleted and self.on_usb(digest)

    def on_usb(self, digest: str) -> bool:
        return self._good("usb", digest, self.usb)

    def _good(self, side: str, digest: str, copies: Mapping[str, Tuple[str, float]]) -> bool:
        copy = copies.get(digest)
        checked_bad = self.bad.get((side, digest))
        return bool(copy) and (checked_bad is None or copy[1] > checked_bad)


def _side_copies(db: sqlite3.Connection, roots: List[Path]) -> Dict[str, Tuple[str, float]]:
    resolved = [str(root.resolve()) for root in roots]
    rows = db.execute(
        "SELECT sha256, relative_path, indexed_at FROM file_locations "
        f"WHERE sha256 IS NOT NULL AND root IN ({','.join('?' * len(resolved))})",
        resolved,
    )
    return {digest: (relative, float(indexed_at)) for digest, relative, indexed_at in rows}


def _bad_copies() -> Dict[Tuple[str, str], float]:
    """Copies the newest check of the saved copies found damaged, changed, missing or unreadable."""
    rows = catalog.list_import_history(limit=catalog.IMPORT_HISTORY_MAX_ROWS)
    check = next((row for row in rows if row["kind"] == "verify"), None)
    found = ((check or {}).get("details") or {}).get("check") or {}  # None after a clean check
    report = str(found.get("report") or "")
    bad: Dict[Tuple[str, str], float] = {}
    for row in verify_report.report_rows(report) if check else []:
        digest = str(row.get("expected_sha256") or "")
        sides = {verify_report.MAC: ("mac",), verify_report.USB: ("usb",)}
        for side in sides.get(row.get("where"), ("mac", "usb")):
            if digest:
                bad[(side, digest)] = float(check["finished_at"])
    return bad


def read_copies() -> Copies:
    """What the catalog knows. An unreadable catalog makes every card not safe, never safe."""
    try:
        with catalog.catalog_db() as db:
            mac = _side_copies(db, [config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT])
            usb = _side_copies(db, [config.USB_DEST_ROOT])
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read the catalog for card safety: %s", exc)
        return Copies()  # nothing known to be saved, so no card is safe
    return Copies(mac, usb, verify_report.deleted_files(), _bad_copies())


@dataclass
class CardStatus:
    card_id: str
    camera: str
    identity: Dict[str, Any]
    files: int
    on_mac: int
    on_usb: int
    not_on_mac: List[str]
    not_on_usb: List[str]
    first_seen: str
    last_seen: str
    safe_since: Optional[float] = None

    @property
    def safe(self) -> bool:
        return self.files > 0 and self.on_mac == self.files and self.on_usb == self.files

    @property
    def label(self) -> str:
        return card_identity.label(self.identity)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.card_id,
            "camera": self.camera,
            "label": self.label,
            "serial": self.identity.get("serial") or None,
            "files": self.files,
            "on_mac": self.on_mac,
            "on_usb": self.on_usb,
            "not_on_mac": self.not_on_mac,
            "not_on_usb": self.not_on_usb,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "safe": self.safe,
            "safe_since": self.safe_since if self.safe else None,
        }


def status_of(
    card_id: str,
    payload: Mapping[str, Any],
    copies: Copies,
    imports: Optional[Mapping[str, Tuple[str, str]]] = None,
) -> CardStatus:
    """``imports``: a card's camera and first import from the history, for older manifests."""
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    history = payload.get("history") if isinstance(payload.get("history"), dict) else {}
    not_on_mac: List[str] = []
    not_on_usb: List[str] = []
    for relative, entry in sorted(files.items()):
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not (digest and copies.on_mac(digest)):
            not_on_mac.append(relative)
        if not (digest and copies.on_usb(digest)):
            not_on_usb.append(relative)
    identity = history.get("identity") if isinstance(history.get("identity"), dict) else {}
    earlier = (imports or {}).get(card_id, ("", ""))
    return CardStatus(
        card_id=card_id,
        camera=str(history.get("volume_name") or earlier[0]).strip() or "Camera card",
        identity=dict(identity),
        files=len(files),
        on_mac=len(files) - len(not_on_mac),
        on_usb=len(files) - len(not_on_usb),
        not_on_mac=not_on_mac[:FILES_LISTED],
        not_on_usb=not_on_usb[:FILES_LISTED],
        first_seen=str(history.get("first_seen") or earlier[1] or history.get("last_seen") or ""),
        last_seen=str(history.get("last_seen") or ""),
    )


def _manifests() -> Iterator[Tuple[str, Dict[str, Any]]]:
    try:
        paths = sorted(config.CARD_MANIFESTS_DIR.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            yield path.stem, payload


def _imports_by_card() -> Dict[str, Tuple[str, str]]:
    """Each card's camera name and first import, from the history: manifests from before 2.6
    don't keep them."""
    found: Dict[str, Tuple[str, str]] = {}
    for row in catalog.list_import_history(limit=catalog.IMPORT_HISTORY_MAX_ROWS):  # newest first
        card_id = (row.get("details") or {}).get("card_id")
        if row["kind"] == "import" and card_id:
            # Rows come newest first, so the first import is the last one seen: its name is the
            # card's own, before a second card mounted beside it got a number added.
            first = datetime.fromtimestamp(float(row["started_at"]), timezone.utc)
            found[card_id] = (str(row["source"]), first.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return found


def safe_since() -> Dict[str, float]:
    return {
        key: float(value)
        for key, value in jsonfile.read_object(config.CARD_SAFETY_PATH).items()
        if isinstance(value, (int, float))
    }


def all_cards(copies: Optional[Copies] = None) -> List[CardStatus]:
    """Every card with a manifest, most recently seen first."""
    copies = copies if copies is not None else read_copies()
    since = safe_since()
    imports = _imports_by_card()
    cards = []
    for card_id, payload in _manifests():
        status = status_of(card_id, payload, copies, imports)
        status.safe_since = since.get(card_id)
        cards.append(status)
    return sorted(cards, key=lambda status: status.last_seen, reverse=True)


@contextmanager
def _locked() -> Iterator[None]:
    """The importer and a check can finish at the same time; both rewrite card_safety.json."""
    config.CARD_SAFETY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.CARD_SAFETY_PATH.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record(now: Optional[float] = None) -> List[CardStatus]:
    """Note when each card became safe, and forget it when it stops being. Returns newly safe ones.

    Never raises: this is reporting, and a failure must not turn a good import into a failed one.
    """
    now = time.time() if now is None else now
    try:
        with _locked():
            since = safe_since()
            cards = all_cards()
            newly = [card for card in cards if card.safe and card.card_id not in since]
            updated = {card.card_id: since.get(card.card_id, now) for card in cards if card.safe}
            if updated != since:
                jsonfile.write_json(config.CARD_SAFETY_PATH, updated)
            for card in newly:
                card.safe_since = now
            return newly
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not record which cards are safe to format: %s", exc)
        return []


def forget_formatted(card_id: str, identity: Mapping[str, Any], camera: str) -> List[str]:
    """Drop the entries of cards that this one is, formatted. Returns the volume IDs dropped.

    Formatting gives a card a new volume ID. The same SD serial number is the same card. Without a
    serial, a card from the same camera with the same kind and size replaces an old one only if
    that one was safe to format, so a second identical card can never hide one that isn't.
    """
    serial = identity.get("serial")
    since = safe_since()
    dropped = []
    for other_id, payload in _manifests():
        if other_id == card_id:
            continue
        history = payload.get("history") if isinstance(payload.get("history"), dict) else {}
        other = history.get("identity") if isinstance(history.get("identity"), dict) else {}
        same_serial = bool(serial) and other.get("serial") == serial
        lookalike = (
            not serial
            and not other.get("serial")
            and other_id in since
            and str(history.get("volume_name") or "").strip() == camera.strip()
            and bool(other.get("size_bytes"))
            and other.get("size_bytes") == identity.get("size_bytes")
            and other.get("kind") == identity.get("kind")
        )
        if same_serial or lookalike:
            dropped.append(other_id)
    for other_id in dropped:
        logger.info(
            "Card %s was formatted and came back as %s; forgetting the old one", other_id, card_id
        )
        (config.CARD_MANIFESTS_DIR / f"{other_id}.json").unlink(missing_ok=True)
    if dropped:
        try:
            with _locked():
                kept = {key: value for key, value in safe_since().items() if key not in dropped}
                jsonfile.write_json(config.CARD_SAFETY_PATH, kept)
        except OSError as exc:
            logger.debug("Could not tidy card_safety.json: %s", exc)
    return dropped
