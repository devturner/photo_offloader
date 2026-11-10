#!/usr/bin/env python3
"""The dashboard header shows the app's own icon, drawn from the same shapes as AppIcon.png."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def draw_icon_module():
    spec = importlib.util.spec_from_file_location("draw_icon", ROOT / "macos" / "draw_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAppIcon(unittest.TestCase):
    def test_the_dashboard_header_shows_the_current_icon(self):
        # index.html carries a copy, since the page is one static file. After changing the
        # icon, paste the output of draw_icon.svg() into the header.
        page = (ROOT / "photo_offloader" / "dashboard" / "web" / "index.html").read_text("utf-8")
        self.assertIn(draw_icon_module().svg(), page)


if __name__ == "__main__":
    unittest.main()
