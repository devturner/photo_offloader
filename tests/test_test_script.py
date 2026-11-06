"""tools/test.sh: the fast level leaves out only the slow modules, so new tests are picked up."""

import subprocess
import unittest

from tests import support

SLOW = {"tests.test_trigger", "tests.test_service_control"}


class TestTestScript(unittest.TestCase):
    def listed(self, *args):
        return subprocess.run(
            ["bash", str(support.REPO_ROOT / "tools" / "test.sh"), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fast_is_every_top_level_test_module_except_the_slow_ones(self):
        expected = {
            f"tests.{path.stem}" for path in (support.REPO_ROOT / "tests").glob("test_*.py")
        } - SLOW
        result = self.listed("fast", "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.split()), expected)

    def test_an_unknown_level_is_refused(self):
        result = self.listed("everything")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fast", result.stderr)


if __name__ == "__main__":
    unittest.main()
