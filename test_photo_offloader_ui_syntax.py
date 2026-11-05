"""Regression guard: dashboard source must remain valid Python syntax."""
import ast
import unittest
from pathlib import Path


class DashboardSyntaxTests(unittest.TestCase):
    def test_dashboard_source_parses(self):
        source_path = Path(__file__).with_name("photo_offloader_ui.py")
        source = source_path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(source_path))


if __name__ == "__main__":
    unittest.main()
