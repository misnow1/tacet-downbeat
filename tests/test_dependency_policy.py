"""The dependency policy, enforced rather than remembered.

CLAUDE.md draws the line at the control path: the code that moves the fader
during a game imports nothing but the standard library, because that is the part
running in a press box on a Saturday with nobody available to fix it. Everything
outside it may take dependencies.

A policy in a document is a policy someone will violate at 3pm on a Friday. This
reads the imports and fails.
"""

import ast
import re
import subprocess
import sys
import tomllib
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
            if path.name == "reach.py":
                # reach defines its own `probe(`, and asks the network, not git.
                # It runs children through asyncio's subprocess support, so it
                # still imports no `subprocess` and never mentions git (#73).
                self.assertNotIn("subprocess", _imported_top_level_modules(path))
                self.assertNotIn("git", re.findall(r"\w+", path.read_text(encoding="utf-8").lower()))
                continue
            if path.name in ("provenance.py", "serve.py"):
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

    `reach` is outside the control path and moves nothing. It imports nothing
    that can send to the console - no `socket`, no `osc`, `net` or `dm7` - and
    these tests read its source for the ways round that. They are a tripwire on
    what the module says, not a sandbox: what it proves is that no line of it
    opens a socket or an OSC encoder.
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

    #: Ways to reach a socket without importing one, or to import by string.
    FORBIDDEN_NAMES = frozenset({"importlib", "__import__"})
    FORBIDDEN_ATTRIBUTES = frozenset(
        {"create_datagram_endpoint", "create_connection", "open_connection", "sock_sendto", "sock_connect"}
    )

    def test_reach_has_no_way_round_the_import_check(self):
        tree = ast.parse((PACKAGE_ROOT / "reach.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN_NAMES)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self.FORBIDDEN_ATTRIBUTES)
            if isinstance(node, ast.alias | ast.ImportFrom):
                names = [node.name] if isinstance(node, ast.alias) else [node.module or ""]
                for name in names:
                    self.assertNotEqual(name.split(".")[0], "importlib")

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


class TestRtdStaysImportableFromTheLiveReader(unittest.TestCase):
    """`tacet.rtd` is not control path, but the live reader (#95) will import it.

    It is pure: bytes in, fields out. No socket, no file, no clock, and nothing
    outside the standard library, so the live reader can use it without
    reopening the dependency question. `rtd_replay`, the offline shell around it,
    is stdlib only too: dpkt is imported by `pcap` alone, and only when it runs.
    """

    #: Ways a decoder would acquire I/O or a clock.
    RTD_FORBIDDEN = frozenset(
        {"socket", "asyncio", "selectors", "os", "pathlib", "io", "time", "datetime", "subprocess"}
    )

    def test_rtd_and_replay_import_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for name in ("rtd", "rtd_replay", "datagram"):
            for module in _imported_top_level_modules(PACKAGE_ROOT / f"{name}.py"):
                self.assertIn(module, allowed, f"tacet.{name} imports {module!r}, which is not in the standard library")

    def test_rtd_does_no_io_and_keeps_no_clock(self):
        imported = _imported_top_level_modules(PACKAGE_ROOT / "rtd.py")
        self.assertEqual(imported & self.RTD_FORBIDDEN, set())

    def test_datagram_does_no_io_and_keeps_no_clock(self):
        imported = _imported_top_level_modules(PACKAGE_ROOT / "datagram.py")
        self.assertEqual(imported & self.RTD_FORBIDDEN, set())

    def test_pcap_does_not_import_the_replay(self):
        # The replay may run as __main__; importing it back makes a second copy and a second Skip.
        self.assertNotIn("rtd_replay", _imported_tacet_modules(PACKAGE_ROOT / "pcap.py"))

    def test_rtd_imports_no_other_tacet_module(self):
        self.assertEqual(_imported_tacet_modules(PACKAGE_ROOT / "rtd.py"), set())

    def test_rtd_is_not_on_the_control_path(self):
        self.assertNotIn("rtd", CONTROL_PATH)


#: The modules allowed to import numpy: offline analysis, never the box (#191).
NUMPY_MODULES = ("pilot",)

REPO_ROOT = PACKAGE_ROOT.parent.parent


class TestNumpyStaysOffTheBox(unittest.TestCase):
    """numpy serves the post-game pilot check, an optional extra (`.[analysis]`)."""

    def test_only_the_analysis_modules_import_numpy(self):
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            if path.stem in NUMPY_MODULES:
                continue
            self.assertNotIn("numpy", _imported_top_level_modules(path), f"tacet.{path.stem} imports numpy")

    def test_every_numpy_module_exists(self):
        for name in NUMPY_MODULES:
            self.assertTrue((PACKAGE_ROOT / f"{name}.py").is_file(), f"{name}.py is missing")

    def test_the_box_starts_without_importing_numpy(self):
        code = "import sys, tacet.serve; sys.exit(any(m in sys.modules for m in ('numpy', 'dpkt')))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, "importing tacet.serve pulled in numpy or dpkt")

    def test_numpy_is_an_extra_not_a_runtime_dependency(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertFalse([d for d in project["dependencies"] if d.lower().startswith("numpy")])
        extra = [d for d in project["optional-dependencies"]["analysis"] if d.lower().startswith("numpy")]
        self.assertEqual(len(extra), 1)
        self.assertIn("<", extra[0], "pin numpy with an upper bound")

    def test_wav_imports_only_the_standard_library(self):
        allowed = sys.stdlib_module_names | {"tacet"}
        for module in _imported_top_level_modules(PACKAGE_ROOT / "wav.py"):
            self.assertIn(module, allowed, f"tacet.wav imports {module!r}, which is not in the standard library")

    def test_the_cli_shell_imports_no_third_party_at_module_level(self):
        tree = ast.parse((PACKAGE_ROOT / "pilot_check.py").read_text(encoding="utf-8"))
        allowed = sys.stdlib_module_names | {"tacet"}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.assertIn(node.module.split(".")[0], allowed)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)


#: The modules allowed to import dpkt: reading a capture after the game, never the box (#210).
DPKT_MODULES = ("pcap",)


class TestDpkt(unittest.TestCase):
    """dpkt serves `tacet-rtd`, an optional extra (`.[analysis]`) like numpy."""

    def test_only_the_capture_reader_imports_dpkt(self):
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            if path.stem in DPKT_MODULES:
                continue
            self.assertNotIn("dpkt", _imported_top_level_modules(path), f"tacet.{path.stem} imports dpkt")

    def test_every_dpkt_module_exists(self):
        for name in DPKT_MODULES:
            self.assertTrue((PACKAGE_ROOT / f"{name}.py").is_file(), f"{name}.py is missing")

    def test_dpkt_is_an_extra_not_a_runtime_dependency(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertFalse([d for d in project["dependencies"] if d.lower().startswith("dpkt")])
        extra = [d for d in project["optional-dependencies"]["analysis"] if d.lower().startswith("dpkt")]
        self.assertEqual(len(extra), 1)
        self.assertIn("<", extra[0], "pin dpkt with an upper bound")

    def test_the_replay_imports_without_dpkt(self):
        code = "import sys, tacet.rtd_replay, tacet.rtd; sys.exit('dpkt' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, "importing tacet.rtd_replay pulled in dpkt")

    def test_no_control_path_module_imports_pcap(self):
        for name in CONTROL_PATH:
            self.assertNotIn("pcap", _imported_tacet_modules(PACKAGE_ROOT / f"{name}.py"), name)


if __name__ == "__main__":
    unittest.main()
