#!/usr/bin/env python3
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from photo_offloader import catalog, config, fileops
from tests import support


class TestCatalog(support.CardFixtureTestCase):
    @patch("photo_offloader.catalog.catalog_connection")
    def test_catalog_db_closes_connection_after_transaction(self, connect):
        connection = MagicMock()
        connect.return_value = connection

        with catalog.catalog_db() as db:
            self.assertIs(db, connection)

        connection.__enter__.assert_called_once_with()
        connection.__exit__.assert_called_once()
        connection.close.assert_called_once_with()

    def test_catalog_migrates_from_legacy_install_directory(self):
        config.LEGACY_CATALOG_DB_PATH.parent.mkdir(parents=True)
        with sqlite3.connect(config.LEGACY_CATALOG_DB_PATH) as legacy:
            legacy.execute("CREATE TABLE legacy_marker (value TEXT)")
            legacy.execute("INSERT INTO legacy_marker VALUES ('preserve-me')")
        with catalog.catalog_connection() as db:
            self.assertEqual(
                db.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserve-me"
            )
        self.assertTrue(config.CATALOG_DB_PATH.is_file())

    def test_google_photos_upload_record_tracks_current_fingerprint(self):
        path = self.local / "uploaded.jpg"
        path.write_bytes(b"photo")
        catalog.mark_google_photos_uploaded(path, "gphotos", "Photo Offloader")
        record = catalog.google_photos_upload_record(path)
        self.assertIsNotNone(record)
        self.assertEqual(record["remote"], "gphotos")
        self.assertEqual(record["album"], "Photo Offloader")
        path.write_bytes(b"changed photo")
        self.assertIsNone(catalog.google_photos_upload_record(path))

    def test_catalog_records_file_locations(self):
        path = self.root / "catalog-photo.jpg"
        path.write_bytes(b"photo")
        catalog.catalog_record(
            path,
            sha256=fileops.sha256_file(path),
            capture_month="2026-10",
            camera_model="NIKON-Z-8",
            media_folder="Photos",
        )
        row = catalog.catalog_row(path)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], fileops.sha256_file(path))
        self.assertEqual(row[4:7], ("2026-10", "NIKON-Z-8", "Photos"))

    def test_catalog_uses_write_ahead_logging(self):
        with catalog.catalog_db() as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_batch_upload_marking_uses_one_connection(self):
        paths = []
        for index in range(25):
            path = self.local / f"p{index}.jpg"
            path.write_bytes(b"x" * (index + 1))
            paths.append(path)
        opened = []
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            opened.append(args)
            return real_connect(*args, **kwargs)

        catalog.catalog_connection().close()  # schema/WAL setup is a one-time cost
        with patch.object(catalog.sqlite3, "connect", counting_connect):
            catalog.mark_google_photos_uploaded_many(paths, "gphotos", "Album")
        self.assertEqual(len(opened), 1)
        self.assertTrue(all(catalog.google_photos_upload_record(p) for p in paths))

    def imported(self, finished_at=1000.0, outcome="complete", kind="import", details=None):
        catalog.record_import_run(
            kind=kind,
            outcome=outcome,
            started_at=finished_at - 10,
            finished_at=finished_at,
            details=details or {"new_bytes": 5},
        )

    def saved_upload(self, started_at=1001.0):
        catalog.record_upload_on_import(
            {"outcome": "complete", "files": 2}, started_at=started_at, within_seconds=60
        )
        return catalog.list_import_history()[0]["details"]

    def test_an_upload_is_saved_on_the_import_it_followed(self):
        self.imported()
        self.assertEqual(
            self.saved_upload(), {"new_bytes": 5, "upload": {"outcome": "complete", "files": 2}}
        )

    def test_an_upload_can_start_before_its_import_is_recorded(self):
        # The importer starts the sync, then writes its history row.
        self.imported(finished_at=1005.0)
        self.assertIn("upload", self.saved_upload(started_at=1001.0))

    def test_an_upload_long_after_the_import_is_not_saved_on_it(self):
        self.imported()
        self.assertNotIn("upload", self.saved_upload(started_at=2000.0))

    def test_a_failed_import_has_no_upload_to_save(self):
        self.imported(outcome="failed")
        self.assertNotIn("upload", self.saved_upload())

    def test_a_usb_backup_since_the_import_does_not_take_its_upload(self):
        self.imported()
        self.imported(kind="usb_backup")
        self.saved_upload()
        usb, run = catalog.list_import_history()
        self.assertNotIn("upload", usb["details"])
        self.assertIn("upload", run["details"])

    def test_a_retry_replaces_a_failed_upload_but_not_a_finished_one(self):
        self.imported(details={"upload": {"outcome": "failed"}})
        self.assertEqual(self.saved_upload()["upload"]["outcome"], "complete")
        catalog.record_upload_on_import({"outcome": "failed"}, started_at=1002.0, within_seconds=60)
        self.assertEqual(
            catalog.list_import_history()[0]["details"]["upload"]["outcome"], "complete"
        )

    def test_import_history_records_and_lists_newest_first(self):
        catalog.record_import_run(
            kind="import",
            source="NIKON Z 8",
            outcome="complete",
            files=10,
            copied=10,
            skipped=0,
            failed=0,
            bytes=1234,
            started_at=100.0,
            finished_at=160.0,
            message="ok",
        )
        catalog.record_import_run(
            kind="usb_backup",
            source="",
            outcome="failed",
            failed=2,
            started_at=200.0,
            finished_at=205.0,
            message="USB error",
        )
        runs = catalog.list_import_history(limit=10)
        self.assertEqual([r["kind"] for r in runs], ["usb_backup", "import"])
        self.assertEqual(runs[1]["source"], "NIKON Z 8")
        self.assertEqual(runs[1]["files"], 10)
        self.assertEqual(runs[1]["bytes"], 1234)
        self.assertAlmostEqual(runs[1]["duration_seconds"], 60.0)

    def test_import_history_survives_reopening_the_catalog(self):
        catalog.record_import_run(
            kind="import", source="card", outcome="complete", started_at=1.0, finished_at=2.0
        )
        catalog._CATALOG_INITIALIZED_PATH = None  # what a fresh process looks like
        self.assertEqual(len(catalog.list_import_history()), 1)

    def test_import_history_can_be_cleared(self):
        for index in range(3):
            catalog.record_import_run(
                kind="import",
                source=f"c{index}",
                outcome="complete",
                started_at=float(index),
                finished_at=float(index) + 1,
            )
        self.assertEqual(catalog.clear_import_history(), 3)
        self.assertEqual(catalog.list_import_history(), [])

    def test_import_history_is_bounded(self):
        with patch.object(catalog, "IMPORT_HISTORY_MAX_ROWS", 5):
            for index in range(8):
                catalog.record_import_run(
                    kind="import",
                    source=f"c{index}",
                    outcome="complete",
                    started_at=float(index),
                    finished_at=float(index) + 1,
                )
        runs = catalog.list_import_history(limit=50)
        self.assertEqual([r["source"] for r in runs], ["c7", "c6", "c5", "c4", "c3"])

    def test_recording_history_never_breaks_an_import(self):
        with patch.object(catalog, "catalog_db", side_effect=sqlite3.OperationalError("locked")):
            catalog.record_import_run(
                kind="import", source="x", outcome="complete", started_at=1.0, finished_at=2.0
            )  # must not raise

    def test_old_history_table_gains_the_details_column(self):
        config.CATALOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(config.CATALOG_DB_PATH) as db:
            db.execute("""CREATE TABLE import_history (id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL, finished_at REAL NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL,
                files INTEGER NOT NULL DEFAULT 0, copied INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER NOT NULL DEFAULT 0, usb INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '')""")
            db.execute(
                "INSERT INTO import_history (started_at, finished_at, kind, outcome) VALUES (1, 2, 'import', 'complete')"
            )
        catalog.record_import_run(
            kind="import",
            outcome="complete",
            started_at=3,
            finished_at=4,
            details={"new": {"RAW": 2}},
        )
        newer, older = catalog.list_import_history()
        self.assertEqual(newer["details"], {"new": {"RAW": 2}})
        self.assertEqual(older["details"], {})


if __name__ == "__main__":
    unittest.main()
