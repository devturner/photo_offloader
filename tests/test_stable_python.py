#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stable_python", REPO / "scripts/stable_python.py")
stable_python = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stable_python)


class TestStablePython(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.brew = Path(tmp.name).resolve()
        self.cellar = (
            self.brew
            / "Cellar/python@3.14/3.14.8_3/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
        )
        self.cellar.parent.mkdir(parents=True)
        self.cellar.touch()
        self.opt = self.brew / "opt/python@3.14/bin/python3.14"

    def add_opt(self):
        self.opt.parent.mkdir(parents=True)
        self.opt.touch()

    def test_a_homebrew_version_folder_becomes_the_path_that_survives_upgrades(self):
        # The install's Python was linked to .../Cellar/python@3.14/3.14.8_3/..., which a Homebrew
        # cleanup deletes. .../opt/python@3.14 always points at the current version.
        self.add_opt()
        self.assertEqual(stable_python.stable_interpreter(str(self.cellar)), str(self.opt))

    def test_a_cellar_python_stays_as_it_is_without_an_opt_link(self):
        self.assertEqual(stable_python.stable_interpreter(str(self.cellar)), str(self.cellar))

    def test_any_other_python_stays_as_it_is(self):
        other = self.brew / "pyenv/versions/3.13.1/bin/python3.13"
        self.assertEqual(stable_python.stable_interpreter(str(other)), str(other))

    def test_the_path_is_resolved_first_so_a_venvs_link_is_followed(self):
        self.add_opt()
        link = self.brew / "venv-python"
        link.symlink_to(self.cellar)
        self.assertEqual(stable_python.stable_interpreter(str(link)), str(self.opt))


if __name__ == "__main__":
    unittest.main()
