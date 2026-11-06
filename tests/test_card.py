#!/usr/bin/env python3
import errno
import os
import unittest
from unittest.mock import MagicMock, patch

from photo_offloader import card, config, fileops
from tests import support


class TestCard(support.CardFixtureTestCase):
    def test_card_manifest_records_scan_history_and_preserves_file_hashes(self):
        media = self.media()
        manifest = {
            "100NIKON/photo.jpg": {
                "size": 3,
                "mtime_ns": media["JPG"].stat().st_mtime_ns,
                "sha256": "verified-hash",
            }
        }
        self.assertTrue(card.save_card_manifest(self.dcim, manifest, {"prior": "kept"}))

        self.assertTrue(
            card.update_card_scan_history(
                self.dcim,
                {"JPG": [media["JPG"]], "RAW": [media["RAW"]], "VIDEO": [media["VIDEO"]]},
                "complete",
                3,
                0,
            )
        )
        payload = card.load_card_manifest_payload(self.dcim)
        self.assertEqual(payload["files"]["100NIKON/photo.jpg"]["sha256"], "verified-hash")
        history = payload["history"]
        self.assertEqual(history["last_scan_status"], "complete")
        self.assertEqual(history["files_discovered"], 3)
        self.assertEqual(history["files_verified_imported"], 3)
        self.assertEqual(history["pending_or_failed"], 0)
        self.assertIn("last_seen", history)
        self.assertIn("last_scan_completed", history)
        self.assertIn("last_successful_import", history)

    def test_incomplete_card_scan_does_not_advance_success_timestamps(self):
        media = self.media()
        prior = {"last_scan_completed": "old-scan", "last_successful_import": "old-import"}
        self.assertTrue(card.save_card_manifest(self.dcim, {}, prior))
        self.assertTrue(
            card.update_card_scan_history(self.dcim, {"JPG": [media["JPG"]]}, "incomplete", 0, 1)
        )
        history = card.load_card_manifest_payload(self.dcim)["history"]
        self.assertEqual(history["last_scan_completed"], "old-scan")
        self.assertEqual(history["last_successful_import"], "old-import")
        self.assertEqual(history["last_scan_status"], "incomplete")
        self.assertEqual(history["pending_or_failed"], 1)

    def test_reconcile_card_manifest_detects_new_changed_and_missing_files(self):
        media = self.media()
        old_only = "100NIKON/removed.jpg"
        unchanged = media["JPG"]
        changed = media["RAW"]
        manifest = {
            "100NIKON/photo.jpg": {
                "size": unchanged.stat().st_size,
                "mtime_ns": unchanged.stat().st_mtime_ns,
                "sha256": "keep-this-hash",
            },
            "100NIKON/photo.nef": {"size": 999, "mtime_ns": 1, "sha256": "stale-hash"},
            old_only: {"size": 1, "mtime_ns": 1, "sha256": "old-hash"},
        }
        self.assertTrue(card.save_card_manifest(self.dcim, manifest, {}))
        counts = card.reconcile_card_manifest(
            self.dcim,
            {
                "JPG": [unchanged],
                "RAW": [changed],
                "VIDEO": [media["VIDEO"]],
            },
        )
        self.assertEqual(
            counts,
            {
                "new_files": 1,
                "changed_files": 1,
                "unchanged_manifest_files": 1,
                "missing_from_card": 1,
            },
        )
        payload = card.load_card_manifest_payload(self.dcim)
        self.assertEqual(payload["files"]["100NIKON/photo.jpg"]["sha256"], "keep-this-hash")
        self.assertNotIn("sha256", payload["files"]["100NIKON/photo.nef"])
        self.assertEqual(payload["history"]["new_files"], 1)
        self.assertEqual(payload["history"]["missing_from_card"], 1)

    def _verified_entry(self, path, digest="a-verified-hash"):
        return {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns, "sha256": digest}

    def test_expected_new_files_counts_what_is_not_verified_as_saved(self):
        media = self.media()
        saved, unsaved = media["JPG"], media["RAW"]
        card.save_card_manifest(self.dcim, {"100NIKON/photo.jpg": self._verified_entry(saved)}, {})
        self.assertEqual(
            card.expected_new_files(self.dcim, {"JPG": [saved], "RAW": [unsaved]}),
            (1, unsaved.stat().st_size),
        )

    def test_a_file_seen_by_an_interrupted_import_still_counts_as_new(self):
        # Reconciling lists every file in the manifest before anything is copied.
        media = self.media()
        saved, unsaved = media["JPG"], media["RAW"]
        card.save_card_manifest(self.dcim, {"100NIKON/photo.jpg": self._verified_entry(saved)}, {})
        files = {"JPG": [saved], "RAW": [unsaved]}
        card.reconcile_card_manifest(self.dcim, files)
        self.assertEqual(card.reconcile_card_manifest(self.dcim, files)["new_files"], 0)
        self.assertEqual(card.expected_new_files(self.dcim, files)[0], 1)

    def test_a_file_changed_since_it_was_verified_counts_as_new(self):
        media = self.media()
        saved, changed = media["JPG"], media["RAW"]
        stale = {**self._verified_entry(changed), "size": 999}
        card.save_card_manifest(
            self.dcim,
            {"100NIKON/photo.jpg": self._verified_entry(saved), "100NIKON/photo.nef": stale},
            {},
        )
        self.assertEqual(
            card.expected_new_files(self.dcim, {"JPG": [saved], "RAW": [changed]})[0], 1
        )

    def test_camera_index_files_are_not_new_photos(self):
        media = self.media()
        saved = media["JPG"]
        index = saved.parent / "NC_FLLST.DAT"
        index.write_bytes(b"rewritten by the camera")
        card.save_card_manifest(self.dcim, {"100NIKON/photo.jpg": self._verified_entry(saved)}, {})
        self.assertEqual(
            card.expected_new_files(self.dcim, {"JPG": [saved], "OTHER": [index]}), (0, 0)
        )

    def test_no_estimate_without_a_verified_file_to_trust(self):
        media = self.media()
        files = {"JPG": [media["JPG"]], "RAW": [media["RAW"]]}
        self.assertIsNone(
            card.expected_new_files(self.dcim, files)
        )  # no manifest, or read-only card
        card.reconcile_card_manifest(self.dcim, files)  # listed, but nothing verified yet
        self.assertIsNone(card.expected_new_files(self.dcim, files))

    def test_a_damaged_manifest_gives_no_estimate(self):
        media = self.media()
        path = card.card_manifest_path(self.dcim)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIsNone(card.expected_new_files(self.dcim, {"JPG": [media["JPG"]]}))

    def test_a_file_that_vanishes_is_left_out_of_the_estimate(self):
        media = self.media()
        saved = media["JPG"]
        card.save_card_manifest(self.dcim, {"100NIKON/photo.jpg": self._verified_entry(saved)}, {})
        gone = saved.parent / "gone.jpg"
        self.assertEqual(card.expected_new_files(self.dcim, {"JPG": [saved, gone]}), (0, 0))

    def test_reconcile_card_manifest_does_not_mark_scan_successful(self):
        media = self.media()
        self.assertTrue(
            card.save_card_manifest(
                self.dcim,
                {},
                {
                    "last_scan_completed": "previous-scan",
                    "last_successful_import": "previous-import",
                },
            )
        )
        card.reconcile_card_manifest(self.dcim, {"JPG": [media["JPG"]]})
        history = card.load_card_manifest_payload(self.dcim)["history"]
        self.assertEqual(history["last_scan_completed"], "previous-scan")
        self.assertEqual(history["last_successful_import"], "previous-import")

    def test_resolve_dcim(self):
        self.assertEqual(card.resolve_dcim_path(self.card), self.dcim.resolve())
        self.assertEqual(card.resolve_dcim_path(self.dcim), self.dcim.resolve())

    def test_resolve_dcim_rejects_missing(self):
        with self.assertRaises(RuntimeError):
            card.resolve_dcim_path(self.root / "missing")

    @patch("glob.glob", return_value=[])
    def test_find_card_no_match(self, glob):
        with patch("builtins.input", return_value=""), self.assertRaises(RuntimeError):
            card.find_card_dcim()

    def test_scan_filters_hidden_and_unsupported(self):
        self.media()
        (self.dcim / ".hidden.jpg").write_bytes(b"x")
        (self.dcim / "._meta.jpg").write_bytes(b"x")
        (self.dcim / "notes.txt").write_bytes(b"x")
        found = card.find_media_files(self.dcim)
        self.assertEqual([p.name for p in found["JPG"]], ["photo.jpg"])
        self.assertEqual([p.name for p in found["RAW"]], ["photo.nef"])
        self.assertEqual([p.name for p in found["VIDEO"]], ["clip.mp4"])

    def unreadable_folder(self, name="101NIKON"):
        if os.geteuid() == 0:
            self.skipTest("root can read every folder")
        folder = self.dcim / name
        folder.mkdir()
        (folder / "lost.jpg").write_bytes(b"x")
        folder.chmod(0)  # TemporaryDirectory restores this when it cleans up
        return folder

    def test_a_folder_that_cannot_be_read_is_reported_and_the_rest_still_found(self):
        media = self.media()
        folder = self.unreadable_folder()
        unreadable = []
        found = card.find_media_files(self.dcim, unreadable)
        self.assertEqual(unreadable, [folder])
        self.assertEqual(found["JPG"], [media["JPG"]])

    def test_a_top_level_folder_that_cannot_be_checked_is_reported(self):
        private = self.card / "PRIVATE"
        real_stat = os.stat

        def failing_stat(path, *args, **kwargs):
            if str(path) == str(private):
                raise OSError(errno.EIO, "disk error")
            return real_stat(path, *args, **kwargs)

        unreadable = []
        with patch.object(card.os, "stat", failing_stat):
            card.find_media_files(self.dcim, unreadable)
        self.assertEqual(unreadable, [private])

    def test_a_card_without_the_optional_folders_has_nothing_unreadable(self):
        self.media()
        unreadable = []
        card.find_media_files(self.dcim, unreadable)
        self.assertEqual(unreadable, [])

    def test_an_entry_that_cannot_be_inspected_is_reported(self):
        class BadEntry:
            name = "bad.jpg"
            path = str(self.dcim / "bad.jpg")

            def is_symlink(self):
                raise OSError(errno.EIO, "disk error")

        scan = MagicMock()
        scan.return_value.__enter__.return_value = [BadEntry()]
        unreadable = []
        with patch.object(card.os, "scandir", scan):
            card.find_media_files(self.dcim, unreadable)
        self.assertEqual(unreadable, [self.dcim / "bad.jpg"])

    def test_a_retried_scan_reports_only_what_the_last_attempt_could_not_read(self):
        folder = self.dcim / "100NIKON"
        results = iter(
            [
                ({}, [folder]),
                ({"JPG": [folder / "x.jpg"]}, []),
            ]
        )

        def scan(_dcim, unreadable):
            found, problems = next(results)
            unreadable.extend(problems)
            return found

        config.WAIT_SECONDS = 30
        unreadable = []
        with (
            patch.object(card, "find_media_files", scan),
            patch("time.monotonic", side_effect=[0, 1]),
            patch("time.sleep"),
        ):
            card.scan_with_retry(self.dcim, unreadable)
        self.assertEqual(unreadable, [])

    @patch("photo_offloader.card.find_media_files")
    def test_scan_retry(self, scan):
        empty = {"JPG": [], "RAW": [], "VIDEO": []}
        found = {"JPG": [self.dcim / "x.jpg"], "RAW": [], "VIDEO": []}
        scan.side_effect = [empty, found]
        config.WAIT_SECONDS = 30
        with patch("time.monotonic", side_effect=[0, 1]), patch("time.sleep"):
            self.assertEqual(card.scan_with_retry(self.dcim), found)

    def test_card_manifest_round_trip_and_fingerprint_invalidation(self):
        source = self.dcim / "100NIKON" / "IMG_0001.NEF"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"raw-data")
        digest = fileops.sha256_file(source)
        manifest = {}
        card.update_card_manifest_entry(manifest, source, self.dcim, digest)
        self.assertTrue(card.save_card_manifest(self.dcim, manifest))
        loaded = card.load_card_manifest(self.dcim)
        self.assertEqual(card.card_manifest_entry(loaded, source, self.dcim), digest)

        source.write_bytes(b"changed raw data")
        self.assertIsNone(card.card_manifest_entry(loaded, source, self.dcim))

    def test_reconcile_treats_whole_hour_shift_as_unchanged_but_not_other_shifts(self):
        media = self.media()
        card.save_card_manifest(self.dcim, {})
        card.reconcile_card_manifest(self.dcim, {"RAW": [media["RAW"]], "JPG": [media["JPG"]]})
        self.shift_mtime(media["RAW"], -3600)  # DST
        self.shift_mtime(media["JPG"], 1800)  # a real change
        counts = card.reconcile_card_manifest(
            self.dcim, {"RAW": [media["RAW"]], "JPG": [media["JPG"]]}
        )
        self.assertEqual((counts["unchanged_manifest_files"], counts["changed_files"]), (1, 1))

    def test_scan_finds_video_stored_outside_dcim(self):
        # Sony XAVC/AVCHD video lives in PRIVATE/, not DCIM/.
        clip = self.card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"video")
        found = card.find_media_files(self.dcim)
        self.assertIn(clip, found["VIDEO"])

    def test_scan_recognizes_additional_camera_formats(self):
        folder = self.dcim / "100MSDCF"
        folder.mkdir()
        for name in ("a.HIF", "b.X3F", "c.CRW", "d.MXF", "e.PEF", "f.SRW"):
            (folder / name).write_bytes(b"x")
        found = card.find_media_files(self.dcim)
        self.assertEqual([p.name for p in found["JPG"]], ["a.HIF"])
        self.assertEqual([p.name for p in found["RAW"]], ["b.X3F", "c.CRW", "e.PEF", "f.SRW"])
        self.assertEqual([p.name for p in found["VIDEO"]], ["d.MXF"])

    def test_unrecognized_files_are_collected_not_silently_dropped(self):
        folder = self.dcim / "100NIKON"
        folder.mkdir()
        sidecar = folder / "C0001M01.XML"
        sidecar.write_bytes(b"<xml/>")
        (folder / ".hidden.xml").write_bytes(b"x")
        (folder / "._AppleDouble.jpg").write_bytes(b"x")
        found = card.find_media_files(self.dcim)
        self.assertEqual(found["OTHER"], [sidecar])


if __name__ == "__main__":
    unittest.main()
