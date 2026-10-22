import unittest

from photo_offloader_ui import format_clock, parse_display_bytes


class DashboardMetricTests(unittest.TestCase):
    def test_elapsed_clock_formats_hours_minutes_and_seconds(self):
        self.assertEqual(format_clock(0), "00:00:00")
        self.assertEqual(format_clock(65), "00:01:05")
        self.assertEqual(format_clock(3661), "01:01:01")

    def test_elapsed_clock_clamps_negative_values(self):
        self.assertEqual(format_clock(-5), "00:00:00")

    def test_parse_display_bytes_supports_rclone_units(self):
        self.assertEqual(parse_display_bytes("1.0 KB"), 1024)
        self.assertEqual(parse_display_bytes("2 MB"), 2 * 1024**2)
        self.assertEqual(parse_display_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(parse_display_bytes("512 B"), 512)

    def test_parse_display_bytes_rejects_unknown_values(self):
        self.assertEqual(parse_display_bytes("unknown"), 0.0)
        self.assertEqual(parse_display_bytes(""), 0.0)


if __name__ == "__main__":
    unittest.main()
