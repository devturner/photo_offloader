#!/usr/bin/env python3
"""Benchmark cold and repeat Photo Offloader catalog refreshes.

This script reads the configured archive roots but uses a temporary SQLite
catalog, so it does not replace or modify the user's production catalog.
Run it on the Mac where the archives and ExifTool are available:
    python3 benchmark_catalog.py
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import camera_offloader_v2 as app


def main() -> int:
    original_db = app.CATALOG_DB_PATH
    original_initialized = app._CATALOG_INITIALIZED_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="photo-offloader-benchmark-") as temp:
            app.CATALOG_DB_PATH = Path(temp) / "benchmark.sqlite3"
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
        app.CATALOG_DB_PATH = original_db
        app._CATALOG_INITIALIZED_PATH = original_initialized
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
