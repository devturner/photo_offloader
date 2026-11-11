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
import re
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
class FullCheck:
    """A whole check of every copy on both sides, and the copies it found bad."""

    started_at: float
    finished_at: float
    bad: Set[Tuple[str, str]]


@dataclass
class Copies:
    """Hash -> (relative path, catalogued at) on each side, and what the newest check found bad."""

    mac: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    usb: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    deleted: Set[str] = field(default_factory=set)
    bad: Dict[Tuple[str, str], float] = field(default_factory=dict)  # (side, hash) -> check time
    rereads: Dict[str, Dict[str, float]] = field(default_factory=dict)  # hash -> side -> when
    # The newest whole check of everything (key None) and of each card, for checks from before
    # rereads.json kept each copy.
    whole_checks: Dict[Optional[str], "FullCheck"] = field(default_factory=dict)

    def on_mac(self, digest: str) -> bool:
        if self._good("mac", digest, self.mac):
            return True
        usb = self.usb.get(digest)
        return bool(usb) and usb[0] in self.deleted and self.on_usb(digest)

    def on_usb(self, digest: str) -> bool:
        return self._good("usb", digest, self.usb)

    def saved_at(self, digest: str) -> float:
        """When the newer of the file's two copies was catalogued: when it became safe."""
        mac = self.mac[digest][1] if self._good("mac", digest, self.mac) else 0.0
        return max(mac, self.usb[digest][1]) if digest in self.usb else mac

    def sides_to_reread(self, digest: str) -> Tuple[str, ...]:
        """The copies a check must read: both, or only the drive's for a file deleted from the
        Mac on purpose."""
        return ("mac", "usb") if self._good("mac", digest, self.mac) else ("usb",)

    def reread_at(self, side: str, digest: str, card_id: str = "") -> Optional[float]:
        """When a check last read this copy and found it good, if it did since the copy was made."""
        copy = (self.mac if side == "mac" else self.usb).get(digest)
        if not copy:
            return None
        found = [self.rereads.get(digest, {}).get(side) or 0.0]
        for key in (None, card_id or None):
            check = self.whole_checks.get(key)
            if check and copy[1] <= check.started_at and (side, digest) not in check.bad:
                found.append(check.finished_at)
        best = max(found)
        return best if best >= copy[1] and best > 0 else None

    def _good(self, side: str, digest: str, copies: Mapping[str, Tuple[str, float]]) -> bool:
        copy = copies.get(digest)
        checked_bad = self.bad.get((side, digest))
        return bool(copy) and (checked_bad is None or copy[1] > checked_bad)


def _side_copies(db: sqlite3.Connection, roots: List[Path]) -> Dict[str, Tuple[str, float]]:
    resolved = [str(root.resolve()) for root in roots]
    rows = db.execute(
        "SELECT sha256, relative_path, indexed_at FROM file_locations "
        f"WHERE sha256 IS NOT NULL AND root IN ({','.join('?' * len(resolved))}) "
        "ORDER BY indexed_at",  # with two copies of one file, the newer one is kept
        resolved,
    )
    return {digest: (relative, float(indexed_at)) for digest, relative, indexed_at in rows}


def _found_bad(check: Optional[Mapping[str, Any]]) -> Set[Tuple[str, str]]:
    """The copies, as (side, hash), a check found damaged, changed, missing or unreadable."""
    found = ((check or {}).get("details") or {}).get("check") or {}  # None after a clean check
    bad: Set[Tuple[str, str]] = set()
    for row in verify_report.report_rows(str(found.get("report") or "")) if check else []:
        digest = str(row.get("expected_sha256") or "")
        sides = {verify_report.MAC: ("mac",), verify_report.USB: ("usb",)}
        bad |= {(side, digest) for side in sides.get(row.get("where"), ("mac", "usb")) if digest}
    return bad


def _bad_copies(history: List[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
    """Copies the newest check of the saved copies found bad, with when it finished."""
    check = next((row for row in history if row["kind"] == "verify"), None)
    return {pair: float(check["finished_at"]) for pair in _found_bad(check)} if check else {}


def _whole_checks(history: List[Dict[str, Any]]) -> Dict[Optional[str], FullCheck]:
    """The newest whole check of both sides, of everything (None) and of each card. Checks from
    before rereads.json kept each copy count through these."""
    found: Dict[Optional[str], FullCheck] = {}
    for row in history:  # newest first
        details = row.get("details") or {}
        key = details.get("card_id") or None
        if row["kind"] == "verify" and row["usb"] and details.get("whole") and key not in found:
            found[key] = FullCheck(
                float(row["started_at"]), float(row["finished_at"]), _found_bad(row)
            )
    return found


def read_copies() -> Copies:
    """What the catalog knows. An unreadable catalog makes every card not safe, never safe."""
    try:
        with catalog.catalog_db() as db:
            mac = _side_copies(db, [config.LOCAL_DEST_ROOT, config.LOCAL_RAW_DEST_ROOT])
            usb = _side_copies(db, [config.USB_DEST_ROOT])
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read the catalog for card safety: %s", exc)
        return Copies()  # nothing known to be saved, so no card is safe
    history = catalog.list_import_history(limit=catalog.IMPORT_HISTORY_MAX_ROWS)
    return Copies(
        mac,
        usb,
        verify_report.deleted_files(),
        _bad_copies(history),
        verify_report.rereads(),
        _whole_checks(history),
    )


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
    newest_copy: Optional[float] = None  # when the last copy it needs was saved, once complete
    checked_at: Optional[float] = None  # the oldest reread of its copies, once all are reread
    size_bytes: int = 0
    to_reread: int = 0  # files with a copy not read again since it was made
    to_reread_bytes: int = 0

    @property
    def complete(self) -> bool:
        """Every file has a copy, verified when it was made, on the Mac and the USB drive."""
        return self.files > 0 and self.on_mac == self.files and self.on_usb == self.files

    @property
    def safe(self) -> bool:
        """Complete, and every copy read again since: they were good then, and still are."""
        return self.complete and self.checked_at is not None

    @property
    def state(self) -> str:
        """ "safe" to format, "check" before formatting, or "unsafe": don't format."""
        return "safe" if self.safe else "check" if self.complete else "unsafe"

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
            "state": self.state,
            "safe": self.safe,
            "complete": self.complete,
            "safe_since": self.safe_since if self.complete else None,
            "checked_at": self.checked_at if self.complete else None,
            "bytes": self.size_bytes,
            "to_reread": self.to_reread if self.complete else 0,
            "to_reread_bytes": self.to_reread_bytes if self.complete else 0,
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
    newest_copy = 0.0
    size_bytes = 0
    for relative, entry in sorted(files.items()):
        if isinstance(entry, dict) and isinstance(entry.get("size"), int):
            size_bytes += entry["size"]
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not (digest and copies.on_mac(digest)):
            not_on_mac.append(relative)
        if not (digest and copies.on_usb(digest)):
            not_on_usb.append(relative)
        if digest and not (not_on_mac or not_on_usb):
            newest_copy = max(newest_copy, copies.saved_at(digest))
    identity = history.get("identity") if isinstance(history.get("identity"), dict) else {}
    earlier = (imports or {}).get(card_id, ("", ""))
    complete = files and not (not_on_mac or not_on_usb)
    to_reread, to_reread_bytes, oldest = (
        _reread_summary(files, copies, card_id) if complete else (0, 0, None)
    )
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
        newest_copy=newest_copy or None,
        size_bytes=size_bytes,
        checked_at=oldest if complete and not to_reread else None,
        to_reread=to_reread,
        to_reread_bytes=to_reread_bytes,
    )


def _reread_summary(
    files: Mapping[str, Any], copies: Copies, card_id: str
) -> Tuple[int, int, Optional[float]]:
    """How many files, and bytes, have a copy not read again since it was made, and the oldest
    reread of the rest."""
    count, size, oldest = 0, 0, None
    for entry in files.values():
        digest = entry["sha256"]
        times = [copies.reread_at(side, digest, card_id) for side in copies.sides_to_reread(digest)]
        if None in times:
            count += 1
            size += entry.get("size", 0) if isinstance(entry.get("size"), int) else 0
        else:
            first = min(t for t in times if t is not None)
            oldest = first if oldest is None else min(oldest, first)
    return count, size, oldest


def copies_to_reread(card_id: str) -> Optional[Tuple[Set[str], Set[str]]]:
    """The hashes of a card's copies not read again since they were made, on the Mac and on the
    drive, for Check new copies. None for an unknown card."""
    payload = (
        dict(_manifests()).get(card_id) if re.fullmatch(r"[A-Za-z0-9-]+", card_id or "") else None
    )
    if payload is None:
        return None
    copies = read_copies()
    mac: Set[str] = set()
    usb: Set[str] = set()
    for entry in (payload.get("files") or {}).values():
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not digest:
            continue
        for side in copies.sides_to_reread(digest):
            if copies.reread_at(side, digest, card_id) is None:
                (mac if side == "mac" else usb).add(digest)
    return mac, usb


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


def _imports_by_card(history: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """Each card's camera name and first import, from the history: manifests from before 2.6
    don't keep them."""
    found: Dict[str, Tuple[str, str]] = {}
    for row in history:  # newest first
        card_id = (row.get("details") or {}).get("card_id")
        if row["kind"] == "import" and card_id:
            # Rows come newest first, so the first import is the last one seen: its name is the
            # card's own, before a second card mounted beside it got a number added.
            first = datetime.fromtimestamp(float(row["started_at"]), timezone.utc)
            found[card_id] = (str(row["source"]), first.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return found


def card_hashes(card_id: str) -> Optional[Set[str]]:
    """The hashes of a card's files, for a check of only its copies. None for an unknown card."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", card_id or ""):
        return None
    payload = dict(_manifests()).get(card_id)
    if payload is None:
        return None
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    return {
        entry["sha256"]
        for entry in files.values()
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
    }


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
    history = catalog.list_import_history(limit=catalog.IMPORT_HISTORY_MAX_ROWS)
    imports = _imports_by_card(history)
    cards = []
    for card_id, payload in _manifests():
        status = status_of(card_id, payload, copies, imports)
        # A card safe before anything recorded it, such as from before an upgrade, is dated by
        # the newest copy it depends on.
        status.safe_since = since.get(card_id) or (status.newest_copy if status.complete else None)
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
            newly = [card for card in cards if card.complete and card.card_id not in since]
            updated = {
                card.card_id: since.get(card.card_id, now) for card in cards if card.complete
            }
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
