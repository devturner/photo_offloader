#!/usr/bin/env python3
"""Benchmark cold and repeat catalog refreshes using a temporary database.

Run on the Mac where the configured archives and ExifTool are available:
    python3 benchmark_catalog.py

The production SQLite catalog, log, and dashboard status files are not modified.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import camera_offloader_v2 as app


def main() -> int:
    originals = {
        "CATALOG_DB_PATH": app.CATALOG_DB_PATH,
        "_CATALOG_INITIALIZED_PATH": app._CATALOG_INITIALIZED_PATH,
        "STATUS_FILE_PATH": app.STATUS_FILE_PATH,
        "LOG_FILE_PATH": app.LOG_FILE_PATH,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="photo-offloader-benchmark-") as temp:
            temp_root = Path(temp)
            app.CATALOG_DB_PATH = temp_root / "benchmark.sqlite3"
            app.STATUS_FILE_PATH = temp_root / "status.json"
            app.LOG_FILE_PATH = temp_root / "benchmark.log"
            app._CATALOG_INITIALIZED_PATH = None
            results = []
            for label in ("cold_build", "repeat_refresh"):
                started = time.perf_counter()
                stats = app.update_file_catalog()
                elapsed = time.perf_counter() - started
                results.append({
                    "run": label,
                    "seconds": round(elapsed, 3),
                    "files_scanned": stats.get("scanned"),
                    "metadata_refreshed": stats.get("metadata_refreshed"),
                    "roots": stats.get("roots"),
                })
            print(json.dumps({
                "catalog": "temporary; production catalog unchanged",
                "results": results,
                "repeat_speedup": (
                    round(results[0]["seconds"] / results[1]["seconds"], 2)
                    if results[1]["seconds"] else None
                ),
            }, indent=2))
    finally:
        for name, value in originals.items():
            setattr(app, name, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
