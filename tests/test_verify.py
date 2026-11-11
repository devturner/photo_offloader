#!/usr/bin/env python3
import csv
import fcntl
import hashlib
import io
import itertools
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from photo_offloader import (
    awake,
    card_status,
    catalog,
    catalog_refresh,
    config,
    fileops,
    jsonfile,
    logs,
    verify,
    verify_report,
    volumes,
)
from photo_offloader.dashboard import display
from tests import support


class VerifyTestCase(support.CardFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.identity = volumes.VolumeIdentity(self.usb_volume, "disk-test", "volume-test")
        for name, value in (
            ("usb_is_mounted", True),
            ("get_volume_identity", self.identity),
            ("volume_identity_matches", True),
        ):
            patcher = patch.object(volumes, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def save(self, relative="2026-10/NIKON-Z-8/Photos/a.jpg", content=b"photo bytes", raw=False):
        """A file saved as an import and backup leave it: both copies, both hashes recorded."""
        digest = hashlib.sha256(content).hexdigest()
        mac = (config.LOCAL_RAW_DEST_ROOT if raw else self.local) / relative
        usb = self.usb / relative
        for path in (mac, usb):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            catalog.catalog_record(path, sha256=digest)
        return mac, usb

    @staticmethod
    def damage(path):
        """Change a byte and keep the size and date, as a failing disk does."""
        before = path.stat()
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = verify.main(list(args))
        return code, out.getvalue()

    @staticmethod
    def last_run():
        return catalog.list_import_history(limit=1)[0]

    @staticmethod
    def report_rows():
        reports = sorted(config.VERIFY_REPORTS_DIR.glob("*.csv"))
        with reports[-1].open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def kinds(self, check):
        return sorted((problem.where, problem.kind) for problem in check.problems)


class TestCheckingCopies(VerifyTestCase):
    def test_every_good_copy_on_both_sides_counts_as_checked(self):
        self.save()
        self.save("2026-10/NIKON-Z-8/Raws/a.nef", b"raw bytes", raw=True)

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(check.checked, {verify.MAC: 2, verify.USB: 2})
        self.assertFalse(check.usb_skipped)

    def test_a_changed_byte_with_the_same_size_and_date_is_damage(self):
        mac, _usb = self.save()
        self.damage(mac)

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.MAC, verify.DAMAGED)])
        self.assertEqual(check.problems[0].path, mac.resolve())

    def test_a_file_saved_over_with_a_new_date_is_called_changed(self):
        _mac, usb = self.save()
        usb.write_bytes(b"edited, and longer than it was")

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.USB, verify.CHANGED)])

    def test_a_new_date_alone_is_no_problem(self):
        mac, _usb = self.save()
        os.utime(mac, (1_000_000_000, 1_000_000_000))

        self.assertEqual(verify.check_archives().problems, [])

    def test_a_deleted_file_is_missing(self):
        mac, _usb = self.save()
        mac.unlink()

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.MAC, verify.MISSING)])

    def test_a_file_that_cannot_be_read_is_reported_and_the_rest_are_still_checked(self):
        bad, _usb = self.save("2026-10/a/Photos/a.jpg")
        self.save("2026-10/a/Photos/b.jpg", b"other photo")
        real_hash = fileops.sha256_file

        def failing(path, *args, **kwargs):
            if Path(path).resolve() == bad.resolve():
                raise OSError(5, "Input/output error")
            return real_hash(path, *args, **kwargs)

        with patch.object(fileops, "sha256_file", side_effect=failing):
            check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.MAC, verify.UNREADABLE)])
        self.assertEqual(check.checked, {verify.MAC: 2, verify.USB: 2})

    def test_each_problem_says_whether_the_other_copy_is_good(self):
        one, _ = self.save("2026-10/a/Photos/one.jpg", b"one")
        both_mac, both_usb = self.save("2026-10/a/Photos/both.jpg", b"both")
        self.damage(one)
        self.damage(both_mac)
        both_usb.unlink()

        check = verify.check_archives()

        other = {
            problem.relative + "|" + problem.where: problem.other for problem in check.problems
        }
        self.assertEqual(other[f"2026-10/a/Photos/one.jpg|{verify.MAC}"], verify.OTHER_GOOD)
        self.assertEqual(other[f"2026-10/a/Photos/both.jpg|{verify.MAC}"], verify.OTHER_BAD)
        self.assertEqual(other[f"2026-10/a/Photos/both.jpg|{verify.USB}"], verify.OTHER_BAD)

    def test_a_copy_is_read_from_the_disk_not_from_memory(self):
        """Without this an earlier upload's pages could answer for a failing disk, as in R1."""
        self.save()
        calls = []
        real_hash = fileops.sha256_file

        def hashed(path, *args, **kwargs):
            calls.append(("hash", kwargs.get("nocache")))
            return real_hash(path, *args, **kwargs)

        with (
            patch.object(
                fileops, "forget_cached_pages", side_effect=lambda p: calls.append(("forget", None))
            ),
            patch.object(fileops, "sha256_file", side_effect=hashed),
        ):
            verify.check_archives()

        self.assertEqual(calls, [("forget", None), ("hash", True)] * 2)


class TestWhatTheCatalogNoLongerKnows(VerifyTestCase):
    def _forget_hash(self, path):
        with catalog.catalog_db() as db:
            db.execute(
                "UPDATE file_locations SET sha256 = NULL WHERE path = ?", (str(path.resolve()),)
            )

    def _forget_row(self, path):
        with catalog.catalog_db() as db:
            db.execute("DELETE FROM file_locations WHERE path = ?", (str(path.resolve()),))

    def test_a_lost_hash_is_checked_against_the_other_copys_hash(self):
        """A catalog update drops the hash of a file whose date moved, even if it's the damage."""
        mac, _usb = self.save()
        self.damage(mac)
        self._forget_hash(mac)

        check = verify.check_archives()

        self.assertEqual([p.where for p in check.problems], [verify.MAC])
        self.assertEqual(check.problems[0].kind, verify.CHANGED)

    def test_a_file_with_no_hash_anywhere_is_counted_as_not_checked(self):
        mac, usb = self.save()
        self._forget_hash(mac)
        self._forget_hash(usb)

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(check.not_checked, 2)

    def test_copies_that_each_match_their_own_hash_must_still_match_each_other(self):
        """A backup records a differing USB file's own hash, so it would pass by itself."""
        _mac, usb = self.save()
        usb.write_bytes(b"something else")
        catalog.catalog_record(usb, sha256=hashlib.sha256(b"something else").hexdigest())

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.BOTH, verify.COPIES_DIFFER)])

    def test_a_file_on_disk_the_catalog_never_heard_of_is_compared_with_its_copy(self):
        mac, usb = self.save()
        self._forget_row(mac)
        self.damage(mac)

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.MAC, verify.CHANGED)])
        self.assertEqual(check.problems[0].expected, fileops.sha256_file(usb))

    def test_a_mac_file_gone_and_forgotten_shows_as_only_on_the_drive(self):
        """A catalog update forgets a deleted file, so this is the only trace of it."""
        mac, _usb = self.save()
        mac.unlink()
        self._forget_row(mac)

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.USB, verify.ONLY_ON_DRIVE)])

    def test_a_file_not_backed_up_yet_is_counted_and_is_not_a_problem(self):
        _mac, usb = self.save()
        usb.unlink()
        self._forget_row(usb)

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(check.waiting_for_backup, 1)

    def test_the_check_never_changes_a_file_or_the_catalog(self):
        mac, _usb = self.save()
        self.damage(mac)
        data, before = mac.read_bytes(), mac.stat().st_mtime_ns
        rows = catalog.catalog_rows_under([self.local, self.usb])

        verify.check_archives()

        self.assertEqual(mac.read_bytes(), data)
        self.assertEqual(mac.stat().st_mtime_ns, before)
        self.assertEqual(catalog.catalog_rows_under([self.local, self.usb]), rows)


class TestTheDrive(VerifyTestCase):
    def test_a_drive_that_is_not_connected_is_skipped_not_called_missing(self):
        self.save()
        self.usb_is_mounted.return_value = False

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertTrue(check.usb_skipped)
        self.assertEqual(check.checked, {verify.MAC: 1, verify.USB: 0})

    def test_mac_problems_say_the_other_copy_was_not_checked_when_the_drive_is_away(self):
        mac, _usb = self.save()
        self.damage(mac)
        self.usb_is_mounted.return_value = False

        check = verify.check_archives()

        self.assertEqual(check.problems[0].other, verify.OTHER_UNKNOWN)

    def test_unplugging_stops_the_check_instead_of_calling_every_file_missing(self):
        files = [self.save(f"2026-10/a/Photos/{index}.jpg", b"x%d" % index) for index in range(5)]
        for _mac, usb in files[2:]:
            usb.unlink()
        # Connected at the start, and gone by the first file that can't be found.
        self.usb_is_mounted.side_effect = [True, False]

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertTrue(check.drive_gone)
        self.assertEqual(check.checked[verify.USB], 2)

    def test_a_swapped_drive_stops_the_check_at_the_first_difference(self):
        _mac, usb = self.save()
        self.damage(usb)
        self.volume_identity_matches.return_value = False

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertTrue(check.drive_gone)


class TestCommand(VerifyTestCase):
    def test_a_settings_problem_checks_nothing_and_records_why(self):
        self.save()
        with (
            patch.object(config, "SETTINGS_PROBLEM", "Line 1: broken."),
            patch.object(verify, "check_archives") as check,
        ):
            code, _output = self.run_main()
        self.assertEqual(code, 1)
        check.assert_not_called()
        self.assertEqual(self.last_run()["outcome"], "failed")
        self.assertIn("settings file", self.last_run()["message"])

    def test_a_clean_check_exits_0_and_saves_a_row_but_no_report(self):
        self.save()

        code, output = self.run_main()

        self.assertEqual(code, 0)
        self.assertIn("All 2 copies match", output)
        row = self.last_run()
        self.assertEqual((row["kind"], row["outcome"], row["files"]), ("verify", "complete", 2))
        self.assertFalse(config.VERIFY_REPORTS_DIR.exists())

    def test_problems_exit_1_with_a_report_a_row_and_a_notification(self):
        mac, _usb = self.save()
        self.damage(mac)

        with patch.object(logs, "notify") as notify:
            code, output = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("1 damaged", output)
        self.assertIn("Nothing was changed", output)
        notify.assert_called_once()
        row = self.last_run()
        self.assertEqual((row["outcome"], row["failed"]), ("failed", 1))
        report = self.report_rows()
        self.assertEqual(report[0]["problem"], verify.DAMAGED)
        self.assertEqual(report[0]["where"], verify.MAC)
        self.assertEqual(report[0]["path"], str(mac.resolve()))
        self.assertEqual(report[0]["other_copy"], verify.OTHER_GOOD)
        self.assertEqual(report[0]["expected_sha256"], hashlib.sha256(b"photo bytes").hexdigest())
        self.assertEqual(report[0]["actual_sha256"], fileops.sha256_file(mac))

    def test_a_card_with_a_damaged_copy_is_no_longer_safe_to_format(self):
        mac, _usb = self.save()
        digest = hashlib.sha256(b"photo bytes").hexdigest()
        jsonfile.write_json(
            config.CARD_MANIFESTS_DIR / "CARD.json",
            {"schema": 1, "files": {"DCIM/a.jpg": {"sha256": digest}}, "history": {}},
        )
        card_status.record(now=1.0)
        self.assertIn("CARD", card_status.safe_since())
        self.damage(mac)

        with patch.object(logs, "notify"):
            self.run_main()

        self.assertEqual(card_status.safe_since(), {})
        self.assertFalse(card_status.all_cards()[0].safe)

    def test_files_gone_from_the_mac_alone_read_as_one_situation(self):
        one, _ = self.save("a/one.jpg", b"1")
        one.unlink()
        two, _ = self.save("a/two.jpg", b"2")
        two.unlink()
        with catalog.catalog_db() as db:
            db.execute("DELETE FROM file_locations WHERE path = ?", (str(two.resolve()),))

        _code, output = self.run_main()

        self.assertIn(
            "2 files are missing from the Mac. The USB drive's copy of each is good.", output
        )
        self.assertNotIn("problems:", self.last_run()["message"])

    def test_with_any_other_problem_it_lists_them_all(self):
        one, _ = self.save("a/one.jpg", b"1")
        one.unlink()
        two, _ = self.save("a/two.jpg", b"2")
        self.damage(two)

        _code, output = self.run_main()

        self.assertIn("2 problems: 1 damaged, 1 missing.", output)

    def test_a_skipped_drive_is_said_out_loud(self):
        self.save()
        self.usb_is_mounted.return_value = False

        code, output = self.run_main()

        self.assertEqual(code, 0)
        self.assertIn("USB drive wasn't connected", output)
        self.assertIn("wasn't connected", self.last_run()["message"])

    def test_a_drive_lost_midway_never_reads_as_a_clean_check(self):
        _mac, usb = self.save()
        usb.unlink()
        self.usb_is_mounted.side_effect = [True, False]

        code, output = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("disconnected", output)
        self.assertEqual(self.last_run()["outcome"], "failed")

    def test_a_catalog_that_cannot_be_read_never_reads_as_a_clean_check(self):
        """Not the importer's habit of treating it as empty: that would pass everything."""
        self.save()
        with patch.object(catalog, "archive_records", side_effect=sqlite3.Error("malformed")):
            code, output = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("Could not finish", output)
        self.assertNotIn("match", output)

    def test_a_cancel_exits_130_and_keeps_what_it_found(self):
        mac, usb = self.save()
        self.damage(mac)
        real_hash = fileops.sha256_file

        def cancel_at_the_drive(path, *args, **kwargs):
            if Path(path).resolve() == usb.resolve():
                raise KeyboardInterrupt
            return real_hash(path, *args, **kwargs)

        with patch.object(fileops, "sha256_file", side_effect=cancel_at_the_drive):
            code, output = self.run_main()

        self.assertEqual(code, 130)
        self.assertIn("Canceled", output)
        self.assertEqual(self.last_run()["outcome"], "canceled")
        self.assertEqual(self.report_rows()[0]["problem"], verify.DAMAGED)

    def test_a_report_that_cannot_be_saved_does_not_hide_the_problems(self):
        mac, _usb = self.save()
        self.damage(mac)
        with patch.object(verify, "_save_report", side_effect=OSError("disk full")):
            code, output = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("1 damaged", output)

    def test_a_name_that_looks_like_a_formula_stays_text_in_the_report(self):
        mac, _usb = self.save("=cmd.jpg")
        mac.unlink()

        self.run_main()

        self.assertEqual(self.report_rows()[0]["relative_path"], "'=cmd.jpg")

    def test_old_reports_are_pruned(self):
        config.VERIFY_REPORTS_DIR.mkdir(parents=True)
        for index in range(verify_report.REPORTS_KEPT + 3):
            (config.VERIFY_REPORTS_DIR / f"2020010{index:02d}-000000.csv").write_text("old")
        mac, _usb = self.save()
        mac.unlink()

        self.run_main()

        reports = sorted(path.name for path in config.VERIFY_REPORTS_DIR.glob("*.csv"))
        self.assertEqual(len(reports), verify_report.REPORTS_KEPT)
        self.assertNotIn("202001000-000000.csv", reports)

    def test_a_second_check_is_refused_and_records_nothing(self):
        self.save()
        config.VERIFY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with config.VERIFY_LOCK_PATH.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, output = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("already running", output)
        self.assertEqual(catalog.list_import_history(limit=1), [])

    def test_it_neither_waits_for_an_import_nor_writes_the_status_it_shows(self):
        """A check takes minutes. A card must still import, and its progress must stay readable."""
        self.save()
        config.SESSION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with config.SESSION_LOCK_PATH.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, _output = self.run_main()

        self.assertEqual(code, 0)
        self.assertFalse(config.STATUS_FILE_PATH.exists())


class TestOneCard(VerifyTestCase):
    """--card rereads only one card's copies, so it can be called safe to format."""

    def setUp(self):
        super().setUp()
        self.save("2026-10/a.jpg", b"from the card")
        self.save("2026-10/b.jpg", b"from another card")
        digest = hashlib.sha256(b"from the card").hexdigest()
        jsonfile.write_json(
            config.CARD_MANIFESTS_DIR / "CARD.json",
            {"schema": 1, "files": {"DCIM/a.jpg": {"sha256": digest, "size": 13}}, "history": {}},
        )

    def test_only_the_cards_copies_are_read_and_the_card_becomes_safe(self):
        self.assertEqual(card_status.all_cards()[0].state, "check")
        with patch.object(logs, "notify") as notify:
            code, output = self.run_main("--card", "CARD")
        self.assertEqual(code, 0, output)
        row = self.last_run()
        self.assertEqual(row["files"], 2)  # one copy on each side, not four
        self.assertEqual(row["details"]["card_id"], "CARD")
        self.assertTrue(row["details"]["whole"])
        self.assertEqual(card_status.all_cards()[0].state, "safe")
        notify.assert_called_once_with(
            "Safe to Format", "Camera card: all 1 file was read again and is good on both sides."
        )

    def test_a_damaged_copy_of_the_card_makes_it_dont_format(self):
        self.damage(self.local / "2026-10/a.jpg")
        with patch.object(logs, "notify"):
            self.assertEqual(self.run_main("--card", "CARD")[0], 1)
        self.assertEqual(card_status.all_cards()[0].state, "unsafe")

    def test_without_the_drive_a_card_is_not_checked(self):
        self.usb_is_mounted.return_value = False
        with patch.object(logs, "notify"):
            code, output = self.run_main("--card", "CARD")
        self.assertEqual(code, 1)
        self.assertIn("Connect the USB drive", output)
        self.assertEqual(card_status.all_cards()[0].state, "check")

    def test_its_progress_says_which_card_is_being_checked(self):
        seen = []
        real = verify.check_archives

        def spy(check, only=None):
            seen.append(jsonfile.read_object(config.VERIFY_STATUS_PATH).get("card"))
            real(check, only)
            seen.append(jsonfile.read_object(config.VERIFY_STATUS_PATH).get("card"))

        with patch.object(verify, "check_archives", side_effect=spy), patch.object(logs, "notify"):
            self.run_main("--card", "CARD")
        self.assertEqual(seen, ["CARD", "CARD"])

    def test_an_unknown_card_is_refused(self):
        with patch.object(logs, "notify"):
            code, output = self.run_main("--card", "NOPE")
        self.assertEqual(code, 1)
        self.assertIn("isn't known", output)


class TestDeletedOnPurpose(VerifyTestCase):
    """Clearing space on the Mac and keeping the drive's copies must not fail every check."""

    def setUp(self):
        super().setUp()
        self.mac, self.usb_copy = self.save()
        self.relative = "2026-10/NIKON-Z-8/Photos/a.jpg"

    def _forget_mac_row(self):
        with catalog.catalog_db() as db:
            db.execute("DELETE FROM file_locations WHERE path = ?", (str(self.mac.resolve()),))

    def test_a_noted_file_is_no_problem_and_is_counted(self):
        self.mac.unlink()
        verify_report.remember_deleted([self.relative])

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(check.kept_on_drive, 1)
        self.assertEqual(check.checked, {verify.MAC: 0, verify.USB: 1})
        self.assertIn("1 file is only on the USB drive", verify.describe(check))

    def test_it_stays_no_problem_after_a_catalog_update_forgets_the_mac_copy(self):
        self.mac.unlink()
        self._forget_mac_row()
        verify_report.remember_deleted([self.relative])

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(check.kept_on_drive, 1)

    def test_any_other_missing_file_is_still_a_problem(self):
        other, _ = self.save("2026-10/NIKON-Z-8/Photos/b.jpg", b"other")
        self.mac.unlink()
        other.unlink()
        verify_report.remember_deleted([self.relative])

        check = verify.check_archives()

        self.assertEqual([p.path for p in check.problems], [other.resolve()])

    def test_the_drives_copy_is_still_read_and_can_still_be_damaged(self):
        self.mac.unlink()
        verify_report.remember_deleted([self.relative])
        self.damage(self.usb_copy)

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.USB, verify.DAMAGED)])
        self.assertEqual(check.problems[0].other, verify.OTHER_BAD)

    def test_losing_the_only_copy_raises_the_alarm_once(self):
        """A catalog update forgets both copies, so this list is the last trace of the file."""
        self.mac.unlink()
        self.usb_copy.unlink()
        self._forget_mac_row()
        with catalog.catalog_db() as db:
            db.execute("DELETE FROM file_locations")
        verify_report.remember_deleted([self.relative])

        first = verify.check_archives()
        second = verify.check_archives()

        self.assertEqual(self.kinds(first), [(verify.USB, verify.MISSING)])
        self.assertEqual(first.problems[0].other, verify.OTHER_BAD)
        self.assertEqual(second.problems, [])

    def test_a_file_back_on_the_mac_is_watched_again(self):
        """A card re-inserted copies it back. If it goes again, that's news."""
        verify_report.remember_deleted([self.relative])
        verify.check_archives()
        self.assertEqual(verify_report.deleted_files(), set())

        self.mac.unlink()

        self.assertEqual(self.kinds(verify.check_archives()), [(verify.MAC, verify.MISSING)])

    def test_an_absent_drive_never_clears_the_list(self):
        self.mac.unlink()
        verify_report.remember_deleted([self.relative])
        self.usb_is_mounted.return_value = False

        check = verify.check_archives()

        self.assertEqual(check.problems, [])
        self.assertEqual(verify_report.deleted_files(), {self.relative})

    def test_a_whole_deleted_folder_can_be_noted_too(self):
        raw, _ = self.save("2026-10/NIKON-Z-8/Raws/a.nef", b"raw", raw=True)
        shutil.rmtree(config.LOCAL_RAW_DEST_ROOT)
        with patch.object(
            catalog_refresh.media_info, "get_exiftool_metadata_batch", return_value={}
        ):
            catalog_refresh.update_file_catalog()

        check = verify.check_archives()
        self.assertEqual(self.kinds(check), [(verify.MAC, verify.MISSING)])
        self.assertEqual(check.problems[0].path, raw.resolve())
        self.assertEqual(check.problems[0].other, verify.OTHER_GOOD)

        verify_report.remember_deleted([check.problems[0].relative])
        self.assertEqual(verify.check_archives().problems, [])

    def test_a_file_only_on_the_drive_is_described_as_the_only_copy(self):
        self.mac.unlink()
        self._forget_mac_row()

        check = verify.check_archives()

        self.assertEqual(self.kinds(check), [(verify.USB, verify.ONLY_ON_DRIVE)])
        self.assertEqual(check.problems[0].other, verify.OTHER_ONLY)


class TestMarkingAReport(VerifyTestCase):
    def failed_row(self):
        self.run_main()
        return self.last_run()

    def test_the_row_groups_files_that_can_be_marked_by_folder(self):
        for index in range(3):
            mac, _ = self.save(f"2026-10/a/Raws/{index}.nef", b"x" * 10, raw=True)
            mac.unlink()
        one, _ = self.save("2026-07/b/Raws/z.nef", b"y" * 5, raw=True)
        one.unlink()

        details = self.failed_row()["details"]["check"]

        self.assertEqual(details["deleted"]["count"], 4)
        self.assertEqual(
            details["deleted"]["folders"],
            [
                {"folder": "2026-10/a/Raws", "count": 3, "bytes": 30},
                {"folder": "2026-07/b/Raws", "count": 1, "bytes": 5},
            ],
        )
        self.assertEqual(details["others"], [])

    def test_damage_and_a_missing_file_with_no_good_copy_cannot_be_marked(self):
        damaged, _ = self.save("a/damaged.jpg", b"one")
        self.damage(damaged)
        gone, gone_usb = self.save("a/gone.jpg", b"two")
        gone.unlink()
        gone_usb.unlink()

        details = self.failed_row()["details"]["check"]

        self.assertIsNone(details["deleted"])
        self.assertEqual(
            sorted(item["path"] for item in details["others"]),
            ["a/damaged.jpg", "a/gone.jpg", "a/gone.jpg"],
        )

    def test_without_the_drive_a_missing_file_cannot_be_marked(self):
        """Nothing has shown that the drive still has it."""
        mac, _ = self.save()
        mac.unlink()
        self.usb_is_mounted.return_value = False

        details = self.failed_row()["details"]["check"]

        self.assertIsNone(details["deleted"])
        self.assertEqual(details["others"][0]["other"], "Not checked")

    def test_without_a_saved_report_nothing_can_be_marked(self):
        """Marking reads the saved report, so offering it here would be a button that fails."""
        mac, _ = self.save()
        mac.unlink()
        with patch.object(verify, "_save_report", side_effect=OSError("disk full")):
            details = self.failed_row()["details"]["check"]

        self.assertIsNone(details["deleted"])
        self.assertEqual(details["others"][0]["kind"], "Missing")

    def test_a_long_list_stops_at_20_and_says_how_many_more(self):
        for index in range(25):
            mac, _ = self.save(f"a/{index}.jpg", b"%d" % index)
            self.damage(mac)

        details = self.failed_row()["details"]["check"]

        self.assertEqual(len(details["others"]), verify_report.FILES_SHOWN)
        self.assertEqual(details["others_more"], 5)

    def test_marking_notes_only_the_files_that_qualify_and_keeps_the_other_problems(self):
        gone, _ = self.save("a/gone.jpg", b"one")
        gone.unlink()
        damaged, _ = self.save("a/damaged.jpg", b"two")
        self.damage(damaged)
        details = self.failed_row()["details"]["check"]

        marked, left = verify_report.mark_deleted(details["report"])

        self.assertEqual(marked, ["a/gone.jpg"])
        self.assertEqual([row["relative_path"] for row in left], ["a/damaged.jpg"])
        self.assertEqual(verify_report.deleted_files(), {"a/gone.jpg"})

    def test_a_name_that_looks_like_a_formula_is_noted_as_it_really_is(self):
        """The spreadsheet copy guards such names, so marking must not read it from there."""
        mac, _ = self.save("=cmd.jpg")
        mac.unlink()
        details = self.failed_row()["details"]["check"]

        verify_report.mark_deleted(details["report"])

        self.assertEqual(verify_report.deleted_files(), {"=cmd.jpg"})
        self.assertEqual(verify.check_archives().problems, [])

    def test_a_report_name_cannot_leave_the_reports_folder(self):
        config.VERIFY_REPORTS_DIR.mkdir(parents=True)
        outside = config.VERIFY_REPORTS_DIR.parent
        row = {"where": verify_report.USB, "problem": verify.ONLY_ON_DRIVE, "relative_path": "x"}
        jsonfile.write_json(outside / "secrets.json", {"rows": [row]})
        (outside / "secrets.csv").write_text("private")

        self.assertEqual(verify_report.mark_deleted("../secrets"), ([], []))
        self.assertIsNone(verify_report.report_csv("../secrets"))

    def test_pruning_a_report_removes_both_of_its_files(self):
        mac, _ = self.save()
        mac.unlink()
        config.VERIFY_REPORTS_DIR.mkdir(parents=True)
        for index in range(verify_report.REPORTS_KEPT + 2):
            for suffix in (".csv", ".json"):
                (config.VERIFY_REPORTS_DIR / f"2020010{index:02d}-000000{suffix}").write_text("{}")

        self.run_main()

        names = [path.name for path in config.VERIFY_REPORTS_DIR.iterdir()]
        self.assertEqual(sum(name.endswith(".csv") for name in names), verify_report.REPORTS_KEPT)
        self.assertEqual(sum(name.endswith(".json") for name in names), verify_report.REPORTS_KEPT)


class TestProgressFile(VerifyTestCase):
    def test_it_says_where_the_check_has_got_to_and_goes_away_at_the_end(self):
        self.save("a/one.jpg", b"1" * 100)
        self.save("a/two.jpg", b"2" * 100)
        seen = []
        real_hash = fileops.sha256_file

        def hashed(path, *args, **kwargs):
            seen.append(jsonfile.read_object(config.VERIFY_STATUS_PATH))
            return real_hash(path, *args, **kwargs)

        with (
            patch.object(fileops, "sha256_file", side_effect=hashed),
            patch.object(verify, "STATUS_SECONDS", 0),
        ):
            self.run_main()

        self.assertEqual(seen[0]["where"], verify.MAC)
        self.assertEqual((seen[0]["files_done"], seen[0]["files_total"]), (0, 4))
        self.assertEqual(seen[0]["bytes_total"], 400)
        self.assertTrue(seen[0]["usb"])
        self.assertEqual(seen[-1]["where"], verify.USB)
        self.assertEqual((seen[-1]["files_done"], seen[-1]["bytes_done"]), (3, 300))
        self.assertFalse(config.VERIFY_STATUS_PATH.exists())

    def test_it_shows_at_once_before_the_archives_have_been_listed(self):
        with patch.object(
            verify,
            "check_archives",
            side_effect=lambda check, only=None: self.assertEqual(
                jsonfile.read_object(config.VERIFY_STATUS_PATH)["where"], ""
            ),
        ):
            self.run_main()

    def test_it_goes_away_after_a_cancel_too(self):
        with patch.object(verify, "check_archives", side_effect=KeyboardInterrupt):
            self.run_main()
        self.assertFalse(config.VERIFY_STATUS_PATH.exists())

    def test_a_full_disk_does_not_stop_the_check(self):
        self.save()
        with patch.object(jsonfile, "write_json", side_effect=OSError("disk full")):
            code, _output = self.run_main()
        self.assertEqual(code, 0)

    def test_the_saved_time_left_counts_the_drive_while_the_mac_is_being_read(self):
        """The Mac is quick, so without the drive the row would promise far too little."""
        mac = verify._Location(verify.MAC, {"a": verify._Entry(Path("a"), size=1000)})
        drive_bytes = int(verify_report.USB_BYTES_PER_SECOND * 600)
        usb = verify._Location(verify.USB, {"a": verify._Entry(Path("a"), size=drive_bytes)})

        with redirect_stdout(io.StringIO()):
            verify._Progress(mac, usb, True).start(verify.MAC)

        self.assertEqual(jsonfile.read_object(config.VERIFY_STATUS_PATH)["seconds_left"], 600)

    def test_time_left_uses_the_speed_so_far_once_there_is_one(self):
        # 10 GB done in 100 s, 10 GB left, and nothing still to come.
        self.assertEqual(verify.seconds_left(10e9, 10e9, 100.0, 1e9, 0), 100)

    def test_time_left_uses_the_measured_speed_before_that(self):
        self.assertEqual(verify.seconds_left(0, 10e9, 0.0, 1e9, 0), 10)

    def test_time_left_allows_for_the_drive_while_the_mac_is_being_read(self):
        left = verify.seconds_left(0, 1e9, 0.0, 1e9, verify_report.USB_BYTES_PER_SECOND * 60)
        self.assertEqual(left, 61)


class TestDriveSpeed(VerifyTestCase):
    """Each check saves how fast it read, so the next estimate fits this Mac and this drive."""

    def run_slowly(self):
        """Every look at the clock 20 s on, so each place reads for long enough to measure."""
        ticks = itertools.count(0.0, 20.0)
        with patch.object(verify.time, "monotonic", side_effect=lambda: next(ticks)):
            return self.run_main()

    def test_a_check_saves_each_places_speed_and_which_drive_it_was(self):
        self.save(content=b"x" * 100_000)
        self.run_slowly()
        details = self.last_run()["details"]
        self.assertEqual(set(details["speeds"]), {verify.MAC, verify.USB})
        self.assertGreater(details["speeds"][verify.USB], 0)
        self.assertEqual(details["drive_id"], "volume-test")

    def test_a_quick_read_saves_no_speed(self):
        """A few seconds mostly measures opening files, not the disk."""
        self.save()
        self.run_main()
        self.assertNotIn("speeds", self.last_run()["details"])

    def test_a_drive_that_went_away_saves_no_drive_speed(self):
        files = [self.save(f"2026-10/a/Photos/{index}.jpg", b"x%d" % index) for index in range(3)]
        files[2][1].unlink()
        self.usb_is_mounted.side_effect = [True, False]
        self.run_slowly()
        self.assertNotIn(verify.USB, self.last_run()["details"]["speeds"])

    def test_the_time_left_starts_from_this_drives_last_speed(self):
        self.save(content=b"x" * 100)
        catalog.record_import_run(
            kind="verify",
            outcome="complete",
            started_at=time.time() - 60,
            finished_at=time.time() - 30,
            usb=True,
            details={
                "whole": True,
                "drive_id": "volume-test",
                "speeds": {verify.MAC: 10.0, verify.USB: 2.0},
            },
        )
        saved = []
        with patch.object(verify, "_write_status", side_effect=lambda **f: saved.append(f)):
            self.run_main()
        first = next(fields for fields in saved if fields["where"] == verify.MAC)
        # 100 bytes on the Mac at 10 a second, then 100 on the drive at 2 a second.
        self.assertEqual(first["seconds_left"], 60)

    def test_another_drives_speed_is_not_used(self):
        self.save(content=b"x" * 100)
        catalog.record_import_run(
            kind="verify",
            outcome="complete",
            started_at=time.time() - 60,
            finished_at=time.time() - 30,
            usb=True,
            details={"whole": True, "drive_id": "another", "speeds": {verify.USB: 2.0}},
        )
        saved = []
        with patch.object(verify, "_write_status", side_effect=lambda **f: saved.append(f)):
            self.run_main()
        first = next(fields for fields in saved if fields["where"] == verify.MAC)
        self.assertEqual(first["seconds_left"], 0)


class TestMeasuredSpeeds(unittest.TestCase):
    @staticmethod
    def row(drive_id, **speeds):
        return {"kind": "verify", "details": {"drive_id": drive_id, "speeds": speeds}}

    def test_the_newest_speed_of_each_place_wins(self):
        rows = [
            self.row("one", **{verify_report.MAC: 2e9}),
            self.row("one", **{verify_report.MAC: 1e9, verify_report.USB: 400e6}),
            self.row("one", **{verify_report.USB: 60e6}),
        ]
        self.assertEqual(
            verify_report.measured_speeds(rows, "one"),
            {verify_report.MAC: 2e9, verify_report.USB: 400e6},
        )

    def test_only_this_drives_speed_counts(self):
        rows = [self.row("two", **{verify_report.USB: 400e6})]
        self.assertEqual(verify_report.measured_speeds(rows, "one"), {})
        self.assertEqual(verify_report.measured_speeds(rows, None), {})

    def test_imports_and_odd_values_are_ignored(self):
        rows = [
            {"kind": "import", "details": {"drive_id": "one", "speeds": {verify_report.USB: 1.0}}},
            {"kind": "verify", "details": {"drive_id": "one", "speeds": "fast"}},
            self.row("one", **{verify_report.USB: "fast", verify_report.MAC: 0}),
        ]
        self.assertEqual(verify_report.measured_speeds(rows, "one"), {})


class TestDashboardRow(unittest.TestCase):
    @staticmethod
    def row(outcome, message):
        return {
            "kind": "verify",
            "outcome": outcome,
            "source": "",
            "files": 6300,
            "copied": 0,
            "skipped": 0,
            "failed": 0,
            "usb": True,
            "message": message,
            "details": "{}",
        }

    def test_a_clean_check_says_what_it_found(self):
        row = {**self.row("complete", "All 6,300 copies match."), "bytes": 138_800_000_000}
        described = display.describe_run({**row, "duration_seconds": 1090.0})
        self.assertEqual(described["title"], "Saved copies checked")
        self.assertEqual(described["tone"], "success")
        self.assertEqual(described["summary"], "All 6,300 copies match · 129.3 GB · 18 min")

    def test_a_failed_check_says_so(self):
        described = display.describe_run(self.row("failed", "1 problem: 1 damaged."))
        self.assertEqual(described["title"], "Saved copies need attention")
        self.assertEqual(described["tone"], "error")
        self.assertEqual(described["summary"], "1 problem: 1 damaged.")

    def test_a_stopped_check_says_so(self):
        described = display.describe_run(self.row("canceled", "Canceled after 12 copies."))
        self.assertEqual(described["title"], "Check of saved copies stopped")
        self.assertEqual(described["tone"], "warning")

    def test_its_report_is_about_the_check_not_a_catalog_update(self):
        row = {**self.row("complete", "All match."), "duration_seconds": 60.0, "bytes": 0}
        step = display.run_report(row)["steps"][0]
        self.assertEqual(step["label"], "Check saved copies")


class TestWarningText(unittest.TestCase):
    MAC, USB = verify_report.MAC, verify_report.USB

    def test_a_drive_checked_before_uses_its_own_speed(self):
        warning = display.verify_warning(True, {self.MAC: 1e9, self.USB: 400e6}, 60e9, 60e9)
        self.assertEqual(warning["time"], "About 4 minutes, going by this drive's last check.")
        self.assertTrue(warning["drive"])

    def test_a_drive_never_checked_is_a_guess_that_allows_for_an_ssd(self):
        warning = display.verify_warning(True, {self.MAC: 1e9}, 69.4e9, 69.4e9)
        self.assertEqual(
            warning["time"],
            "This drive hasn't been checked before, so this is a guess: roughly 18 minutes "
            "for a hard drive, and much less for an SSD.",
        )

    def test_the_mac_alone_uses_its_last_speed(self):
        warning = display.verify_warning(False, {self.MAC: 500e6}, 69.4e9, 69.4e9)
        self.assertEqual(
            warning["time"],
            "About 2 minutes, going by your last check. "
            "The USB drive isn't connected, so only the Mac will be checked.",
        )

    def test_without_the_drive_it_says_only_the_mac_is_checked(self):
        warning = display.verify_warning(False, {}, 69.4e9, 69.4e9)
        self.assertEqual(
            warning["time"],
            "About a minute. The USB drive isn't connected, so only the Mac will be checked.",
        )
        self.assertFalse(warning["drive"])
        self.assertNotIn("USB", warning["what"])


class TestStayingAwake(unittest.TestCase):
    def test_a_check_keeps_the_mac_awake_however_it_was_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            support.isolate_config(self, Path(tmp))
            with patch.object(verify, "_run", return_value=0), redirect_stdout(io.StringIO()):
                verify.main([])
            awake.keep_awake.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
