"""The Google Photos sync as separate processes: the real program, a fake rclone, a temp home.

These cover what only shows up across processes: two syncs at once, stopping a sync while
rclone runs, and pausing. Every path the program uses comes from ``HOME``, which points into
a temp dir, so nothing here can reach the real install or start a real upload.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, List

from tests import support

SYNC_PROGRAM = support.REPO_ROOT / "bin" / "photo-offloader-google-sync"

# Copies one listed file each time a gate opens: ``gate`` lets every file through,
# ``gate-N`` only the Nth file of each copy. Notes what it sent and whether two copies overlapped.
FAKE_RCLONE = r"""#!/bin/bash
d="$FAKE_RCLONE_DIR"
case "$1" in
  listremotes) echo "gphotos:"; exit 0 ;;
  copy) ;;
  *) exit 0 ;;
esac
list=""; log=""
while [ $# -gt 0 ]; do
  case "$1" in --files-from) list="$2"; shift ;; --log-file) log="$2"; shift ;; esac
  shift
done
mkdir "$d/copy-running" 2>/dev/null || echo overlap >> "$d/overlap"
echo $$ >> "$d/copy-pids"
trap 'rmdir "$d/copy-running" 2>/dev/null; exit 143' TERM
n=0
while IFS= read -r name; do
  [ -n "$name" ] || continue
  n=$((n + 1))
  until [ -e "$d/gate" ] || [ -e "$d/gate-$n" ]; do sleep 0.05; done
  echo "$(date '+%Y/%m/%d %H:%M:%S') INFO  : $name: Copied (new)" >> "$log"
  echo "$name" >> "$d/copied"
done < "$list"
rmdir "$d/copy-running" 2>/dev/null
exit 0
"""

WAIT_SECONDS = 30  # a busy CI runner is slow; a real failure never gets there at all


class SyncProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.install = self.home / "Library" / "Application Support" / "PhotoOffloader"
        self.data = self.home / "Library" / "Application Support" / "PhotoOffloaderData"
        self.photos = self.home / "Pictures" / "CameraImports" / "2026-11"
        self.photos.mkdir(parents=True)
        self.fake = self.root / "rclone-state"
        self.fake.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        rclone = bin_dir / "rclone"
        rclone.write_text(FAKE_RCLONE)
        rclone.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHOTO_OFFLOADER_")}
        env.update(
            HOME=str(self.home), PATH=f"{bin_dir}:/usr/bin:/bin", FAKE_RCLONE_DIR=str(self.fake)
        )
        self.env = env
        self.processes: List[subprocess.Popen] = []
        self.addCleanup(self.stop_everything)

    # ------------------------------------------------------------------ helpers
    def stop_everything(self):
        (self.fake / "gate").touch()
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        for pid in self.rclone_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def photo(self, name: str) -> Path:
        path = self.photos / name
        path.write_bytes(name.encode() * 100)
        return path

    def start_sync(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(SYNC_PROGRAM), "--sync"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # as the importer and the dashboard start it
        )
        self.processes.append(proc)
        return proc

    def wait_for(self, condition: Callable[[], object], what: str):
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_exit(self, proc: subprocess.Popen) -> int:
        try:
            return proc.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail("the sync did not exit")

    def lines(self, name: str) -> List[str]:
        path = self.fake / name
        return path.read_text().split() if path.exists() else []

    def rclone_pids(self) -> List[int]:
        return [int(pid) for pid in self.lines("copy-pids")]

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # A finished child of ours that nobody has reaped yet is not running.
        ps = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        return bool(ps.stdout.strip()) and not ps.stdout.strip().startswith("Z")

    def copying(self, count: int = 1) -> bool:
        return len(self.rclone_pids()) >= count

    def status(self) -> dict:
        path = self.install / "google_photos_status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def uploaded(self) -> List[str]:
        db = self.data / "catalog.sqlite3"
        if not db.exists():
            return []
        with sqlite3.connect(db) as con:
            return sorted(
                Path(r[0]).name for r in con.execute("SELECT path FROM google_photos_uploads")
            )

    def pause_flag(self) -> Path:
        return self.install / ".google-photos-paused"


class TestStoppingASync(SyncProcessTest):
    def test_stopping_the_sync_stops_rclone(self):
        self.photo("a.jpg")
        sync = self.start_sync()
        self.wait_for(self.copying, "rclone to start copying")
        rclone = self.rclone_pids()[0]

        sync.send_signal(signal.SIGTERM)
        self.wait_exit(sync)

        self.wait_for(lambda: not self.alive(rclone), "rclone to stop with the sync")

    def test_an_rclone_left_by_a_killed_sync_keeps_a_new_sync_out(self):
        # SIGKILL can't be caught, so rclone outlives the sync. A second rclone next to it
        # would send the same files twice.
        self.photo("a.jpg")
        first = self.start_sync()
        self.wait_for(self.copying, "rclone to start copying")
        first.kill()
        self.wait_exit(first)

        second = self.start_sync()
        time.sleep(1.5)  # long enough for the second sync to have started rclone, if it does
        self.assertEqual(len(self.rclone_pids()), 1, "a second rclone started")

        (self.fake / "gate").touch()
        self.wait_exit(second)
        self.assertEqual(self.lines("overlap"), [])

    def test_a_stopped_upload_keeps_what_got_through_and_says_interrupted(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.photo(name)
        (self.fake / "gate-1").touch()
        sync = self.start_sync()
        self.wait_for(lambda: self.lines("copied"), "the first file to be copied")

        sync.send_signal(signal.SIGTERM)
        self.wait_exit(sync)

        self.assertEqual(self.uploaded(), ["a.jpg"])
        status = self.status()
        self.assertEqual(status["state"], "interrupted")
        self.assertEqual(status.get("waiting"), 2)


class TestPausing(SyncProcessTest):
    def test_pausing_stops_the_upload_and_says_paused(self):
        for name in ("a.jpg", "b.jpg"):
            self.photo(name)
        (self.fake / "gate-1").touch()
        sync = self.start_sync()
        self.wait_for(lambda: self.lines("copied"), "the first file to be copied")

        self.pause_flag().touch()
        sync.send_signal(signal.SIGTERM)

        self.assertEqual(self.wait_exit(sync), 0)
        status = self.status()
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status.get("waiting"), 1)
        self.assertEqual(self.uploaded(), ["a.jpg"])

    def test_no_upload_starts_while_paused(self):
        self.photo("a.jpg")
        self.install.mkdir(parents=True)
        self.pause_flag().touch()
        (self.fake / "gate").touch()

        self.assertEqual(self.wait_exit(self.start_sync()), 0)

        self.assertEqual(self.rclone_pids(), [])
        self.assertEqual(self.status()["state"], "paused")


class TestASyncDuringAnUpload(SyncProcessTest):
    def test_a_card_imported_during_an_upload_is_sent_after_it(self):
        self.photo("first.jpg")
        running = self.start_sync()
        self.wait_for(self.copying, "the first upload to start")

        self.photo("second.jpg")  # the next import lands, and starts its own sync
        queued = self.start_sync()
        time.sleep(1)
        self.assertIsNone(queued.poll(), "the second sync gave up instead of waiting")
        queue_file = self.install / ".google-photos-sync.queue.lock"
        self.assertEqual(queue_file.read_text(), str(queued.pid), "the dashboard looks for it")

        (self.fake / "gate").touch()
        self.assertEqual(self.wait_exit(running), 0)
        self.assertEqual(self.wait_exit(queued), 0)
        self.assertEqual(self.lines("copied"), ["2026-11/first.jpg", "2026-11/second.jpg"])
        self.assertEqual(self.uploaded(), ["first.jpg", "second.jpg"])
        self.assertEqual(self.lines("overlap"), [])
        self.assertEqual(queue_file.read_text(), "", "nothing waits any more")

    def test_only_one_sync_waits(self):
        # Ten imports during one long upload must not stack ten syncs: the one waiting lists
        # every file once it starts.
        self.photo("first.jpg")
        self.start_sync()
        self.wait_for(self.copying, "the first upload to start")
        queued = self.start_sync()
        time.sleep(1)

        third = self.start_sync()
        self.assertEqual(self.wait_exit(third), 0)
        self.assertIsNone(queued.poll())

    def test_a_waiting_sync_does_nothing_if_uploads_were_paused_meanwhile(self):
        self.photo("first.jpg")
        running = self.start_sync()
        self.wait_for(self.copying, "the first upload to start")
        self.photo("second.jpg")
        queued = self.start_sync()
        time.sleep(1)

        self.pause_flag().touch()
        running.send_signal(signal.SIGTERM)

        self.wait_exit(running)
        self.assertEqual(self.wait_exit(queued), 0)
        self.assertEqual(len(self.rclone_pids()), 1)
        self.assertEqual(self.status()["state"], "paused")


if __name__ == "__main__":
    unittest.main()
