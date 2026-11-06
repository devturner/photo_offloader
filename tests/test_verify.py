#!/usr/bin/env python3
import csv
import fcntl
import hashlib
import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from photo_offloader import catalog, config, fileops, logs, verify, volumes
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

    def run_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = verify.main([])
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
        for index in range(verify.REPORTS_KEPT + 3):
            (config.VERIFY_REPORTS_DIR / f"2020010{index:02d}-000000.csv").write_text("old")
        mac, _usb = self.save()
        mac.unlink()

        self.run_main()

        reports = sorted(path.name for path in config.VERIFY_REPORTS_DIR.iterdir())
        self.assertEqual(len(reports), verify.REPORTS_KEPT)
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
        described = display.describe_run(self.row("complete", "All 6,300 copies match."))
        self.assertEqual(described["title"], "Archive check passed")
        self.assertEqual(described["tone"], "success")
        self.assertEqual(described["summary"], "All 6,300 copies match.")

    def test_a_failed_check_says_so(self):
        described = display.describe_run(self.row("failed", "1 problem: 1 damaged."))
        self.assertEqual(described["title"], "Archive check failed")
        self.assertEqual(described["tone"], "error")
        self.assertEqual(described["summary"], "1 problem: 1 damaged.")


if __name__ == "__main__":
    unittest.main()
