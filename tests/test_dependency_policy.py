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
CONTROL_PATH = ("osc", "net", "dm7", "state", "targets")

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


class TestConfigStaysImportableFromTheControlPath(unittest.TestCase):
    """`tacet.config` is not control path, but it is deliberately free to become it.

    Nothing in `osc`, `net`, `dm7` or `state` reads the config today. Keeping it
    stdlib-only -- TOML is `tomllib` at 3.11, so this costs nothing -- means that
    if one of them ever needs a site value, the policy above is not what stops
    it. A dependency added here would close that door quietly.
    """

    def test_config_imports_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for module in _imported_top_level_modules(PACKAGE_ROOT / "config.py"):
            self.assertIn(module, allowed, f"tacet.config imports {module!r}, which is not in the standard library")


class TestOnlyServeAsksGit(unittest.TestCase):
    """What code the box runs is asked once, by `serve.main`, before the loop (#157, #41)."""

    def test_provenance_imports_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for module in _imported_top_level_modules(PACKAGE_ROOT / "provenance.py"):
            self.assertIn(module, allowed, f"tacet.provenance imports {module!r}, which is not in the standard library")

    def test_no_control_path_module_imports_subprocess_or_provenance(self):
        for name in CONTROL_PATH:
            source = (PACKAGE_ROOT / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("subprocess", source, name)
            self.assertNotIn("provenance", source, name)

    def test_only_serve_asks_git(self):
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            # reach asks the network, not git, and does it through asyncio's
            # subprocess support rather than the `subprocess` module (#73).
            if path.name in ("provenance.py", "serve.py", "reach.py"):
                continue
            self.assertNotIn("subprocess", _imported_top_level_modules(path), path.name)
            self.assertNotIn("probe(", path.read_text(encoding="utf-8"), path.name)


def _imported_tacet_modules(source: Path) -> set[str]:
    """Every tacet module a file imports, however spelled: `from . import osc`,
    `from .net import X`, `from tacet import osc` and `import tacet.osc`."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "tacet" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0 or module == "tacet":
                if module and node.level > 0:
                    found.add(module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif module.startswith("tacet."):
                found.add(module.split(".")[1])
    return found


class TestTheConsoleCheckCannotSendOsc(unittest.TestCase):
    """#73: a ping and a read of the neighbour table, never a packet of ours.

    `reach` is outside the control path and moves nothing. It cannot import the
    code that could put a UDP datagram on the wire, so it structurally cannot
    send the console an OSC message, let alone a get that might recall a scene.
    """

    def test_reach_imports_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for module in _imported_top_level_modules(PACKAGE_ROOT / "reach.py"):
            self.assertIn(module, allowed, f"tacet.reach imports {module!r}, which is not in the standard library")

    def test_reach_cannot_send_osc(self):
        self.assertNotIn("socket", _imported_top_level_modules(PACKAGE_ROOT / "reach.py"))
        tacet_modules = _imported_tacet_modules(PACKAGE_ROOT / "reach.py")
        for forbidden in ("osc", "net", "dm7"):
            self.assertNotIn(forbidden, tacet_modules)

    def test_the_helper_sees_every_spelling_of_an_import(self):
        # Proves the check above could fail.
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("from . import osc\nfrom .net import X\nfrom tacet.dm7 import Y\nimport tacet.state\n")
            self.assertEqual(_imported_tacet_modules(sample), {"osc", "net", "dm7", "state"})

    def test_no_control_path_module_imports_reach(self):
        for name in CONTROL_PATH:
            self.assertNotIn("reach", _imported_tacet_modules(PACKAGE_ROOT / f"{name}.py"), name)


if __name__ == "__main__":
    unittest.main()
