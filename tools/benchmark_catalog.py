#!/usr/bin/env python3
"""Benchmark cold and repeat catalog refreshes using a temporary database.

Run on the Mac where the configured archives and ExifTool are available:
    python3 benchmark_catalog.py

The production SQLite catalog, log, and dashboard status files are not modified.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photo_offloader import (
    catalog,
    catalog_refresh,
    config,  # noqa: E402
)


def main() -> int:
    originals = {
        "CATALOG_DB_PATH": config.CATALOG_DB_PATH,
        "STATUS_FILE_PATH": config.STATUS_FILE_PATH,
        "LOG_FILE_PATH": config.LOG_FILE_PATH,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="photo-offloader-benchmark-") as temp:
            temp_root = Path(temp)
            config.CATALOG_DB_PATH = temp_root / "benchmark.sqlite3"
            config.STATUS_FILE_PATH = temp_root / "status.json"
            config.LOG_FILE_PATH = temp_root / "benchmark.log"
            catalog._CATALOG_INITIALIZED_PATH = None
            results = []
            for label in ("cold_build", "repeat_refresh"):
                started = time.perf_counter()
                stats = catalog_refresh.update_file_catalog()
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
            setattr(config, name, value)
        catalog._CATALOG_INITIALIZED_PATH = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
