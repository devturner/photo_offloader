"""Safe to format: every file on a card has a verified copy on the Mac and on the USB drive."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import card_status, catalog, config, jsonfile, verify_report
from tests import support

SD = {"kind": "SD", "size_bytes": 63_864_569_856, "maker": "SanDisk", "serial": "0x1"}
CFEXPRESS = {"kind": "CFexpress", "size_bytes": 256_060_514_304}


class CardCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        support.isolate_config(self, self.root)
        config.CARD_MANIFESTS_DIR.mkdir(parents=True)

    def card(
        self, card_id, files, identity=None, camera="NIKON Z 8  ", last_seen="2026-11-10T16:41:19Z"
    ):
        """files: card path -> hash (None for a file that was never hashed)."""
        history = {"last_seen": last_seen, "volume_name": camera}
        if identity is not None:
            history["identity"] = identity
        payload = {
            "schema": 1,
            "files": {
                name: ({"size": 1, "mtime_ns": 1} | ({"sha256": digest} if digest else {}))
                for name, digest in files.items()
            },
            "history": history,
        }
        jsonfile.write_json(config.CARD_MANIFESTS_DIR / f"{card_id}.json", payload)

    def saved(self, root, relative, digest):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(digest.encode())
        catalog.catalog_record(path, sha256=digest)

    def on_mac(self, relative, digest):
        self.saved(config.LOCAL_DEST_ROOT, relative, digest)

    def on_usb(self, relative, digest):
        self.saved(config.USB_DEST_ROOT, relative, digest)

    def status(self, card_id="A"):
        return next(card for card in card_status.all_cards() if card.card_id == card_id)


class TestSafeToFormat(CardCase):
    def test_every_file_on_the_mac_and_the_drive_is_safe(self):
        self.card("A", {"DCIM/100/a.NEF": "h1", "DCIM/100/a.JPG": "h2"}, SD)
        for name, digest in (("2026-11/a.NEF", "h1"), ("2026-11/a.JPG", "h2")):
            self.on_mac(name, digest)
            self.on_usb(name, digest)
        status = self.status()
        self.assertTrue(status.complete)
        self.assertEqual((status.files, status.on_mac, status.on_usb), (2, 2, 2))
        self.assertEqual(status.label, "64 GB SanDisk SD")
        self.assertEqual(status.camera, "NIKON Z 8")

    def test_raw_files_count_in_the_raw_archive(self):
        self.card("A", {"a.NEF": "h1"})
        self.saved(config.LOCAL_RAW_DEST_ROOT, "2026-11/a.NEF", "h1")
        self.on_usb("2026-11/a.NEF", "h1")
        self.assertTrue(self.status().complete)

    def test_a_file_not_on_the_drive_is_not_safe_and_is_named(self):
        self.card("A", {"old.NEF": "h1", "new.NEF": "h2"})
        self.on_mac("old.NEF", "h1")
        self.on_mac("new.NEF", "h2")
        self.on_usb("old.NEF", "h1")
        status = self.status()
        self.assertFalse(status.complete)
        self.assertEqual(status.on_usb, 1)
        self.assertEqual(status.not_on_usb, ["new.NEF"])

    def test_a_file_never_hashed_is_not_counted_as_saved(self):
        self.card("A", {"a.NEF": None})
        self.assertFalse(self.status().complete)
        self.assertEqual(self.status().not_on_mac, ["a.NEF"])

    def test_an_empty_card_is_not_called_safe(self):
        self.card("A", {})
        self.assertFalse(self.status().complete)

    def test_a_file_deleted_from_the_mac_on_purpose_counts_if_the_drive_has_it(self):
        self.card("A", {"a.NEF": "h1"})
        self.on_usb("2026-11/a.NEF", "h1")
        self.assertFalse(self.status().complete)
        verify_report.remember_deleted(["2026-11/a.NEF"])
        self.assertTrue(self.status().complete)

    def test_a_copy_the_last_check_found_bad_does_not_count_until_made_again(self):
        self.card("A", {"a.NEF": "h1"})
        self.on_mac("a.NEF", "h1")
        self.on_usb("a.NEF", "h1")
        name = verify_report.save_report(
            [
                {
                    "where": verify_report.USB,
                    "problem": "damaged",
                    "other_copy": "good",
                    "path": "x",
                    "relative_path": "a.NEF",
                    "bytes": 1,
                    "expected_sha256": "h1",
                    "actual_sha256": "zz",
                }
            ],
            time.time(),
        )
        catalog.record_import_run(
            kind="verify",
            outcome="failed",
            started_at=time.time(),
            finished_at=time.time() + 1,
            details={"check": {"report": name}},
        )
        status = self.status()
        self.assertFalse(status.complete)
        self.assertEqual(status.on_mac, 1)
        with patch("time.time", return_value=time.time() + 5):  # copied again after the check
            catalog.catalog_record(config.USB_DEST_ROOT / "a.NEF", sha256="h1")
        self.assertTrue(self.status().complete)

    def test_a_clean_check_or_one_with_its_files_marked_deleted_leaves_the_copies_counted(self):
        # The user's catalog, 2026-11-11: the newest check row has {"check": None}.
        self.card("A", {"a.NEF": "h1"})
        self.on_mac("a.NEF", "h1")
        self.on_usb("a.NEF", "h1")
        for details in ({"check": None}, {}, {"check": {"report": ""}}):
            catalog.record_import_run(
                kind="verify", outcome="complete", started_at=1.0, finished_at=2.0, details=details
            )
            self.assertTrue(self.status().complete, details)

    def test_an_unreadable_catalog_makes_no_card_safe(self):
        self.card("A", {"a.NEF": "h1"})
        self.on_mac("a.NEF", "h1")
        self.on_usb("a.NEF", "h1")
        with patch.object(catalog, "catalog_db", side_effect=OSError("locked")):
            self.assertFalse(self.status().complete)

    def test_a_card_listed_before_names_were_kept_takes_its_name_from_the_history(self):
        payload = {
            "files": {"a": {"sha256": "h"}},
            "history": {"last_seen": "2026-11-06T22:59:11Z"},
        }
        jsonfile.write_json(config.CARD_MANIFESTS_DIR / "OLD.json", payload)
        # With two cards in, macOS mounts the second as "NIKON Z 8   1"; the first import is
        # the likelier to have the card's own name.
        for started, source in (
            (1_792_000_000.0, "NIKON Z 8  "),
            (1_792_500_000.0, "NIKON Z 8   1"),
        ):
            catalog.record_import_run(
                kind="import",
                source=source,
                outcome="complete",
                started_at=started,
                finished_at=started + 60,
                details={"card_id": "OLD"},
            )
        catalog.record_import_run(
            kind="import",
            source="EOS R5",
            outcome="complete",
            started_at=1.0,
            finished_at=2.0,
            details={"card_id": "OTHER"},
        )
        status = self.status("OLD")
        self.assertEqual(status.camera, "NIKON Z 8")
        self.assertEqual(status.first_seen, "2026-10-14T17:46:40Z")

    def test_cards_are_listed_most_recently_seen_first(self):
        self.card("OLD", {"a": "h"}, last_seen="2026-10-01T00:00:00Z")
        self.card("NEW", {"b": "h"}, last_seen="2026-11-01T00:00:00Z")
        self.assertEqual([c.card_id for c in card_status.all_cards()], ["NEW", "OLD"])

    def test_a_damaged_manifest_is_skipped(self):
        (config.CARD_MANIFESTS_DIR / "BAD.json").write_text("{not json", encoding="utf-8")
        self.card("A", {"a": "h"})
        self.assertEqual([c.card_id for c in card_status.all_cards()], ["A"])


class TestRecord(CardCase):
    def test_the_time_a_card_became_safe_is_kept_and_reported_once(self):
        self.card("A", {"a.NEF": "h1"})
        self.on_mac("a.NEF", "h1")
        self.assertEqual(card_status.record(now=100.0), [])  # not on the drive yet
        self.on_usb("a.NEF", "h1")
        newly = card_status.record(now=200.0)
        self.assertEqual([card.card_id for card in newly], ["A"])
        self.assertEqual(card_status.record(now=300.0), [])
        self.assertEqual(self.status().safe_since, 200.0)
        self.assertEqual(self.status().as_dict()["safe_since"], 200.0)

    def test_a_card_that_stops_being_safe_loses_its_date(self):
        self.card("A", {"a.NEF": "h1"})
        self.on_mac("a.NEF", "h1")
        self.on_usb("a.NEF", "h1")
        card_status.record(now=200.0)
        self.card("A", {"a.NEF": "h1", "b.NEF": "h2"})  # imported again with a new photo
        card_status.record(now=300.0)
        self.assertIsNone(self.status().safe_since)
        self.assertEqual(json.loads(config.CARD_SAFETY_PATH.read_text()), {})

    def test_a_failure_to_record_is_logged_not_raised(self):
        self.card("A", {"a.NEF": "h1"})
        with patch.object(card_status.jsonfile, "read_object", side_effect=OSError("disk")):
            self.assertEqual(card_status.record(), [])


class TestSafeSinceWithoutARecord(CardCase):
    """A card safe before anything recorded it (an upgrade) is dated by its newest copy."""

    def at(self, when, saver, *args):
        with patch("time.time", return_value=when):
            saver(*args)

    def test_the_newest_copy_the_card_depends_on_dates_it(self):
        self.card("A", {"a.NEF": "h1", "b.NEF": "h2"})
        self.at(100.0, self.on_mac, "a.NEF", "h1")
        self.at(200.0, self.on_usb, "a.NEF", "h1")
        self.at(150.0, self.on_mac, "b.NEF", "h2")
        self.at(300.0, self.on_usb, "b.NEF", "h2")
        self.assertEqual(self.status().safe_since, 300.0)

    def test_a_recorded_date_wins(self):
        self.card("A", {"a.NEF": "h1"})
        self.at(100.0, self.on_mac, "a.NEF", "h1")
        self.at(200.0, self.on_usb, "a.NEF", "h1")
        card_status.record(now=250.0)
        self.assertEqual(self.status().safe_since, 250.0)

    def test_a_file_deleted_from_the_mac_on_purpose_is_dated_by_the_drive(self):
        self.card("A", {"a.NEF": "h1"})
        self.at(200.0, self.on_usb, "2026-11/a.NEF", "h1")
        verify_report.remember_deleted(["2026-11/a.NEF"])
        self.assertEqual(self.status().safe_since, 200.0)

    def test_a_card_that_is_not_safe_has_no_date(self):
        self.card("A", {"a.NEF": "h1"})
        self.at(100.0, self.on_mac, "a.NEF", "h1")
        self.assertIsNone(self.status().safe_since)


class TestThreeStates(CardCase):
    """Don't format, check before formatting, safe: the copies must be reread after they're made."""

    def setUp(self):
        super().setUp()
        self.card("A", {"a.NEF": "h1"})
        with patch("time.time", return_value=100.0):
            self.on_mac("a.NEF", "h1")
            self.on_usb("a.NEF", "h1")

    def checked(self, started, **fields):
        row = dict(
            kind="verify",
            outcome="complete",
            started_at=started,
            finished_at=started + 50,
            usb=True,
            details={"whole": True},
        )
        row.update(fields)
        catalog.record_import_run(**row)

    def test_copies_on_both_sides_not_reread_since_need_a_check(self):
        status = self.status()
        self.assertTrue(status.complete)
        self.assertFalse(status.safe)
        self.assertEqual(status.state, "check")

    def test_a_whole_check_after_the_newest_copy_makes_it_safe(self):
        self.checked(200.0)
        status = self.status()
        self.assertEqual((status.state, status.checked_at), ("safe", 250.0))
        self.assertTrue(status.as_dict()["safe"])

    def test_a_check_of_this_card_counts_and_one_of_another_card_does_not(self):
        self.checked(200.0, details={"whole": True, "card_id": "OTHER"})
        self.assertEqual(self.status().state, "check")
        self.checked(300.0, details={"whole": True, "card_id": "A"})
        self.assertEqual(self.status().state, "safe")

    def test_a_check_from_before_the_newest_copy_does_not_count(self):
        self.checked(50.0)
        self.assertEqual(self.status().state, "check")

    def test_a_check_that_skipped_the_drive_or_stopped_part_way_does_not_count(self):
        self.checked(200.0, usb=False)
        self.checked(300.0, outcome="canceled", details={"whole": False})
        self.assertEqual(self.status().state, "check")

    def test_a_check_that_found_problems_elsewhere_still_reread_this_card(self):
        self.checked(200.0, outcome="failed")
        self.assertEqual(self.status().state, "safe")

    def test_missing_copies_are_dont_format_whatever_was_checked(self):
        self.card("A", {"a.NEF": "h1", "b.NEF": "h2"})
        self.checked(200.0)
        self.assertEqual(self.status().state, "unsafe")


class TestForgetFormatted(CardCase):
    def setUp(self):
        super().setUp()
        self.card("OLD", {"a.NEF": "h1"}, SD)
        self.on_mac("a.NEF", "h1")
        self.on_usb("a.NEF", "h1")
        card_status.record(now=1.0)

    def ids(self):
        return [c.card_id for c in card_status.all_cards()]

    def test_the_same_sd_serial_under_a_new_volume_id_is_the_card_formatted(self):
        self.card("NEW", {}, SD)
        self.assertEqual(card_status.forget_formatted("NEW", SD, "NIKON Z 8"), ["OLD"])
        self.assertEqual(self.ids(), ["NEW"])
        self.assertNotIn("OLD", card_status.safe_since())

    def test_a_different_serial_is_a_different_card(self):
        other = dict(SD, serial="0x2")
        self.card("NEW", {}, other)
        self.assertEqual(card_status.forget_formatted("NEW", other, "NIKON Z 8"), [])
        self.assertEqual(sorted(self.ids()), ["NEW", "OLD"])

    def test_without_serials_a_lookalike_replaces_only_a_safe_card(self):
        self.card("CF1", {"b.NEF": "h2"}, CFEXPRESS)
        self.on_mac("b.NEF", "h2")
        self.on_usb("b.NEF", "h2")
        self.card("CF2", {"c.NEF": "h3"}, CFEXPRESS)  # not backed up: must never be hidden
        card_status.record(now=2.0)
        self.card("CF3", {}, CFEXPRESS)
        self.assertEqual(card_status.forget_formatted("CF3", CFEXPRESS, "NIKON Z 8  "), ["CF1"])
        self.assertIn("CF2", self.ids())

    def test_a_lookalike_from_another_camera_or_size_is_kept(self):
        self.card("CF1", {"b.NEF": "h2"}, CFEXPRESS)
        self.on_mac("b.NEF", "h2")
        self.on_usb("b.NEF", "h2")
        card_status.record(now=2.0)
        bigger = dict(CFEXPRESS, size_bytes=512_000_000_000)
        self.assertEqual(card_status.forget_formatted("X", bigger, "NIKON Z 8"), [])
        self.assertEqual(card_status.forget_formatted("X", CFEXPRESS, "NIKON Z 9"), [])


if __name__ == "__main__":
    unittest.main()
