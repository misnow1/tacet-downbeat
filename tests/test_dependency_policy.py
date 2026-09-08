"""The dependency policy, enforced rather than remembered.

CLAUDE.md draws the line at the control path: the code that moves the fader
during a game imports nothing but the standard library, because that is the part
running in a press box on a Saturday with nobody available to fix it. Everything
outside it may take dependencies.

A policy in a document is a policy someone will violate at 3pm on a Friday. This
reads the imports and fails.
"""

import ast
import sys
import unittest
from pathlib import Path

import tacet

#: The modules that move the fader. Nothing here may import a third party.
CONTROL_PATH = ("osc", "net", "dm7", "state")

PACKAGE_ROOT = Path(tacet.__file__).parent


def _imported_top_level_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        # A relative import (node.level > 0) is our own package by definition.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


class TestControlPathIsDependencyFree(unittest.TestCase):
    def test_every_control_path_module_exists(self):
        # Guards against the list silently going stale after a rename.
        for name in CONTROL_PATH:
            self.assertTrue((PACKAGE_ROOT / f"{name}.py").is_file(), f"{name}.py is missing")

    def test_control_path_imports_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for name in CONTROL_PATH:
            for module in _imported_top_level_modules(PACKAGE_ROOT / f"{name}.py"):
                self.assertIn(
                    module,
                    allowed,
                    f"tacet.{name} imports {module!r}, which is not in the standard "
                    f"library. The control path must not take dependencies; see "
                    f"CLAUDE.md.",
                )

    def test_the_detector_will_join_the_control_path(self):
        # A reminder rather than a check: when the detector lands it moves the
        # fader, but it needs numpy and scipy. The policy will need revisiting
        # then, deliberately, rather than by accident.
        self.assertNotIn("detector", CONTROL_PATH)


if __name__ == "__main__":
    unittest.main()
