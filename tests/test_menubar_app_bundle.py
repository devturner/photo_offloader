#!/usr/bin/env python3
"""The menu-bar app bundle: macOS names an app after the bundle its program sits in.

Started as `python3 photo-offloader-menubar`, the menu bar runs inside Homebrew's Python.app
and appears as "Python" in Login Items, Activity Monitor and the menu-bar settings.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "macos" / "build_app.sh"
UNSUPPORTED_PYTHON = 3  # build_app.sh: this Python has no shared library to load

PROBE = """\
import json, sys
with open(sys.argv[1], "w") as out:
    json.dump({"prefix": sys.prefix, "argv": sys.argv[1:]}, out)
"""


def build(
    app: Path, script: Path, python: str = sys.executable, kind: str = "menubar"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BUILD), kind, str(app), python, str(script), "9.8.7"],
        capture_output=True,
        text=True,
        timeout=120,
    )


@unittest.skipUnless(shutil.which("clang"), "the Command Line Tools aren't installed")
class TestMenubarAppBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.script = cls.tmp / "probe.py"
        cls.script.write_text(PROBE, encoding="utf-8")
        cls.app = cls.tmp / "Photo Offloader Menu Bar.app"
        result = build(cls.app, cls.script)
        if result.returncode == UNSUPPORTED_PYTHON:
            cls._tmp.cleanup()
            raise unittest.SkipTest(f"this Python can't be embedded: {result.stderr.strip()}")
        if result.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(f"build_app.sh failed ({result.returncode}): {result.stderr}")
        cls.info = plistlib.loads((cls.app / "Contents" / "Info.plist").read_bytes())
        cls.executable = cls.app / "Contents" / "MacOS" / cls.info["CFBundleExecutable"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_app(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.executable), *args], capture_output=True, text=True, timeout=60
        )

    def test_bundle_names_the_app_and_keeps_it_out_of_the_dock(self):
        self.assertEqual(self.info["CFBundleName"], "Photo Offloader")
        self.assertEqual(self.info["CFBundleIdentifier"], "com.user.photooffloader.menubar")
        self.assertEqual(self.info["CFBundleShortVersionString"], "9.8.7")
        self.assertIs(self.info["LSUIElement"], True)
        icon = self.app / "Contents" / "Resources" / f"{self.info['CFBundleIconFile']}.icns"
        self.assertGreater(icon.stat().st_size, 0)

    def test_program_runs_the_script_in_the_given_python_environment(self):
        out = self.tmp / "probe.json"
        result = self.run_app(str(out), "extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(seen["prefix"], sys.prefix)
        self.assertEqual(seen["argv"], [str(out), "extra"])

    def test_the_app_you_open_is_a_normal_app_with_its_own_identity(self):
        # It shows the dashboard window, so it needs a Dock icon; the menu bar mustn't have one.
        app = self.tmp / "Photo Offloader.app"
        result = build(app, self.script, kind="app")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleName"], "Photo Offloader")
        self.assertEqual(info["CFBundleIdentifier"], "com.user.photooffloader")
        self.assertEqual(info["CFBundleExecutable"], "photo-offloader")
        self.assertNotIn("LSUIElement", info)
        out = self.tmp / "app-probe.json"
        run = subprocess.run(
            [str(app / "Contents" / "MacOS" / "photo-offloader"), str(out)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["prefix"], sys.prefix)

    def test_an_unknown_kind_is_refused(self):
        result = build(self.tmp / "Odd.app", self.script, kind="widget")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.tmp / "Odd.app").exists())

    def test_a_python_that_cannot_be_embedded_is_reported(self):
        fake = self.tmp / "not-python"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        result = build(self.tmp / "Other.app", self.script, python=str(fake))
        self.assertEqual(result.returncode, UNSUPPORTED_PYTHON, result.stderr)
        self.assertFalse((self.tmp / "Other.app").exists())

    def test_program_explains_a_missing_python_library(self):
        app = self.tmp / "Broken.app"
        shutil.copytree(self.app, app)
        conf = app / "Contents" / "Resources" / "launcher.conf"
        lines = conf.read_text(encoding="utf-8").splitlines()
        conf.write_text("\n".join(["/nonexistent/libpython.dylib", *lines[1:]]) + "\n")
        result = subprocess.run(
            [str(app / "Contents" / "MacOS" / self.info["CFBundleExecutable"])],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/nonexistent/libpython.dylib", result.stderr)
        self.assertIn("install.sh", result.stderr)


class TestInstallUsesTheBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = (ROOT / "install.sh").read_text(encoding="utf-8")
        cls.uninstall = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    def test_install_builds_both_apps_and_falls_back_to_python(self):
        self.assertIn('"$SCRIPT_DIR/macos/build_app.sh" menubar', self.install)
        self.assertIn('"$SCRIPT_DIR/macos/build_app.sh" app', self.install)
        self.assertIn('MENUBAR_PROGRAM=("$VENV_DIR/bin/python3" "$MENUBAR_PATH")', self.install)
        self.assertIn('DASHBOARD_PROGRAM=("$VENV_DIR/bin/python3" "$DASHBOARD_PATH")', self.install)
        self.assertIn('for arg in "${MENUBAR_PROGRAM[@]}"', self.install)

    def test_install_never_replaces_another_app_or_a_desktop_item(self):
        self.assertIn('"$existing_id" != "$USER_APP_ID"', self.install)
        self.assertIn('[[ ! -e "$DESKTOP_LINK" && ! -L "$DESKTOP_LINK" ]]', self.install)

    def test_install_and_uninstall_agree_on_where_things_go(self):
        for line in (
            'USER_APP="$HOME/Applications/Photo Offloader.app"',
            'USER_APP_ID="com.user.photooffloader"',
            'DESKTOP_LINK="$HOME/Desktop/Photo Offloader"',
        ):
            self.assertIn(line, self.install)
            self.assertIn(line, self.uninstall)

    def test_uninstall_stops_both_apps_and_removes_only_what_install_made(self):
        self.assertIn(
            '"$INSTALL_DIR/Photo Offloader Menu Bar.app/Contents/MacOS/photo-offloader-menubar|',
            self.uninstall,
        )
        self.assertIn('"$USER_APP/Contents/MacOS/photo-offloader|', self.uninstall)
        self.assertIn('"$(readlink "$DESKTOP_LINK")" == "$USER_APP"', self.uninstall)
        self.assertIn(')" == "$USER_APP_ID" ]]; then', self.uninstall)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
