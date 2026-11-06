"""The test isolation helper must cover every path in config, including future ones."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_offloader import config
from tests import support


class TestIsolateConfig(unittest.TestCase):
    def test_every_configured_path_is_redirected_into_the_test_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            support.isolate_config(self, root)
            for name, value in vars(config).items():
                if name.isupper() and isinstance(value, Path) and name not in support.CODE_PATHS:
                    with self.subTest(name=name):
                        self.assertTrue(value == root or root in value.parents, f"{name} = {value}")
            self.doCleanups()

    def test_a_new_config_path_without_a_test_location_is_refused(self):
        with patch.object(config, "NEW_THING_PATH", Path("/real/place"), create=True):
            with self.assertRaisesRegex(AssertionError, "NEW_THING_PATH"):
                support.isolate_config(self, Path("/tmp/x"))


if __name__ == "__main__":
    unittest.main()
