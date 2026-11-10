"""The installed Reaper script against the checkout's (#223).

Pure core first (path, compare, backup name, the answer, the wording), then
`startup` against a fake terminal and temp directories only. Nothing here reads
or writes the real REAPER folder: `home` is always a temp directory.
"""

import dataclasses
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tacet import mirror_report, reach, script_check

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_SCRIPT = REPO_ROOT / "reaper" / "tacet_mirror.lua"
LUA_TEST = REPO_ROOT / "reaper" / "test_tacet_mirror.lua"

NEW = b"-- the checkout's script\nprint('new')\n"
OLD = b"-- an old script\n"
MAC = "darwin"
LINUX = "linux"
WHEN = datetime(2026, 11, 14, 13, 5, 9)
BACKUP_NAME = "tacet_mirror.lua.bak-20261114-130509"


class FakeTerminal:
    """Answers from a list. An exception class in the list is raised instead."""

    def __init__(self, *answers: object, tty: bool = True) -> None:
        self.answers: list[object] = list(answers)
        self.tty = tty
        self.asked: list[str] = []
        self.said: list[str] = []
        self.on_ask: Callable[[], object] | None = None

    def can_answer(self) -> bool:
        return self.tty

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if self.on_ask is not None:
            self.on_ask()
        answer: object = self.answers.pop(0)
        if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
            raise answer
        assert isinstance(answer, str)
        return answer

    def say(self, line: str) -> None:
        self.said.append(line)


class Site(unittest.TestCase):
    """A temp home with a REAPER Scripts folder, a repo copy and a queue."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.scripts = self.home / "Library" / "Application Support" / "REAPER" / "Scripts"
        self.scripts.mkdir(parents=True)
        self.installed = self.scripts / "tacet_mirror.lua"
        self.repo = self.root / "repo" / "tacet_mirror.lua"
        self.repo.parent.mkdir()
        self.repo.write_bytes(NEW)

    def plan(self, **overrides: Any) -> script_check.Plan:
        base = script_check.Plan(queue=self.root / "queue.tsv", home=self.home, sys_platform=MAC, repo_script=self.repo)
        return dataclasses.replace(base, **overrides)

    def run_startup(self, terminal: FakeTerminal | None = None, **overrides: Any) -> script_check.ScriptStatus:
        return script_check.startup(self.plan(**overrides), terminal or FakeTerminal(), lambda: WHEN)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.scripts.iterdir())


class TestThePureCore(unittest.TestCase):
    def test_the_path_on_each_platform(self):
        home = Path("/h")
        self.assertEqual(
            script_check.installed_path(MAC, home),
            Path("/h/Library/Application Support/REAPER/Scripts/tacet_mirror.lua"),
        )
        self.assertEqual(script_check.installed_path(LINUX, home), Path("/h/.config/REAPER/Scripts/tacet_mirror.lua"))

    def test_the_platform_table_is_the_reach_one(self):
        # The path rule keys on the same names `reach` does.
        self.assertEqual({p.value for p in reach.Platform}, {MAC, LINUX})

    def test_an_unknown_platform_has_no_path(self):
        self.assertIsNone(script_check.installed_path("win32", Path("/h")))

    def test_compare(self):
        self.assertIs(script_check.compare(NEW, NEW), script_check.Match.CURRENT)
        self.assertIs(script_check.compare(NEW, OLD), script_check.Match.DIFFERS)
        self.assertIs(script_check.compare(NEW, None), script_check.Match.MISSING)

    def test_the_backup_name_is_pinned_and_is_not_a_lua_file(self):
        got = script_check.backup_path(Path("/s/tacet_mirror.lua"), WHEN)
        self.assertEqual(got, Path("/s") / BACKUP_NAME)
        self.assertNotEqual(got.suffix, ".lua")

    def test_only_y_and_yes_mean_yes(self):
        for yes in ("y", "Y", "yes", "YES", " Yes ", "y\n"):
            self.assertTrue(script_check.is_yes(yes), yes)
        for no in ("", "n", "no", "yy", "yep", "ok", "1", "y es", "sure"):
            self.assertFalse(script_check.is_yes(no), no)


class TestWhatItIsAndIsNot(Site):
    def test_current_never_asks_and_writes_nothing(self):
        self.installed.write_bytes(NEW)
        terminal = FakeTerminal()
        status = self.run_startup(terminal)
        self.assertEqual(status.lines, (script_check.ROW_CURRENT,))
        self.assertEqual((terminal.asked, terminal.said), ([], []))
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])

    def test_the_expected_checksum_is_the_checkouts(self):
        self.installed.write_bytes(OLD)
        self.assertEqual(self.run_startup(FakeTerminal("n")).expected, mirror_report.source_checksum(NEW))

    def test_missing_with_no_terminal(self):
        terminal = FakeTerminal(tty=False)
        status = self.run_startup(terminal)
        self.assertEqual(
            status.lines,
            (
                script_check.ROW_MISSING.format(path=self.installed),
                script_check.NOT_REPLACED_NO_TERMINAL,
            ),
        )
        self.assertEqual((terminal.asked, terminal.said), ([], []))
        self.assertFalse(self.installed.exists())

    def test_differs_with_no_terminal(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal(tty=False)
        status = self.run_startup(terminal)
        self.assertEqual(status.lines[0], script_check.ROW_DIFFERS.format(path=self.installed))
        self.assertEqual(terminal.asked, [])
        self.assertEqual(self.installed.read_bytes(), OLD)

    def test_a_terminal_is_told_what_is_wrong_then_asked(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal("n")
        self.run_startup(terminal)
        self.assertEqual(terminal.said, [script_check.INTRO_DIFFERS.format(path=self.installed)])
        self.assertEqual(terminal.asked, [script_check.QUESTION])

    def test_a_missing_script_has_its_own_intro(self):
        terminal = FakeTerminal("n")
        self.run_startup(terminal)
        self.assertEqual(terminal.said, [script_check.INTRO_MISSING.format(path=self.installed)])


class TestAnswering(Site):
    def test_yes_installs_the_repo_bytes_and_keeps_the_old_ones(self):
        self.installed.write_bytes(OLD)
        status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(self.installed.read_bytes(), NEW)
        backup = self.scripts / BACKUP_NAME
        self.assertEqual(backup.read_bytes(), OLD)
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua", BACKUP_NAME])
        self.assertEqual(
            status.lines,
            (
                script_check.ROW_REPLACED.format(path=self.installed),
                script_check.NOTE_BACKUP.format(path=backup),
                script_check.NOTE_RUN_AGAIN,
            ),
        )

    def test_yes_is_case_insensitive_and_yes_works(self):
        for answer in ("YES", "Y", "yes"):
            with self.subTest(answer):
                self.installed.write_bytes(OLD)
                self.run_startup(FakeTerminal(answer))
                self.assertEqual(self.installed.read_bytes(), NEW)

    def test_yes_with_nothing_installed_has_no_backup_note(self):
        status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(self.installed.read_bytes(), NEW)
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])
        self.assertEqual(
            status.lines,
            (script_check.ROW_REPLACED.format(path=self.installed), script_check.NOTE_RUN_AGAIN),
        )

    def test_the_repo_is_read_once_and_those_bytes_are_what_is_installed(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal("y")
        terminal.on_ask = lambda: self.repo.write_bytes(b"changed while the question was open\n")
        status = self.run_startup(terminal)
        self.assertEqual(self.installed.read_bytes(), NEW)
        self.assertEqual(status.expected, mirror_report.source_checksum(NEW))

    def test_no_and_blank_and_garbage_change_nothing(self):
        for answer in ("n", "N", "no", "", "maybe", "yy"):
            with self.subTest(answer):
                self.installed.write_bytes(OLD)
                status = self.run_startup(FakeTerminal(answer))
                self.assertEqual(self.installed.read_bytes(), OLD)
                self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])
                self.assertEqual(status.lines[1:], (script_check.NOT_REPLACED_NO,))

    def test_undecodable_input_is_no_answer(self):
        self.installed.write_bytes(OLD)
        status = self.run_startup(FakeTerminal(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(status.lines[1:], (script_check.NOT_REPLACED_NO_ANSWER,))

    def test_eof_is_no_and_carries_on(self):
        self.installed.write_bytes(OLD)
        status = self.run_startup(FakeTerminal(EOFError))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(status.lines[1:], (script_check.NOT_REPLACED_NO_ANSWER,))

    def test_ctrl_c_propagates_and_writes_nothing(self):
        self.installed.write_bytes(OLD)
        with self.assertRaises(KeyboardInterrupt):
            self.run_startup(FakeTerminal(KeyboardInterrupt))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])

    def test_check_only_never_asks_or_writes(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal, check_only=True)
        self.assertEqual((terminal.asked, terminal.said), ([], []))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(status.lines[1:], (script_check.NOT_REPLACED_CHECK,))

    def test_a_restart_of_an_existing_log_never_asks_or_writes(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal, restart=True)
        self.assertEqual((terminal.asked, terminal.said), ([], []))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(status.lines[1:], (script_check.NOT_REPLACED_RESTART,))

    def test_a_restart_with_a_current_script_says_current(self):
        self.installed.write_bytes(NEW)
        self.assertEqual(self.run_startup(restart=True).lines, (script_check.ROW_CURRENT,))


class TestWhenWritingFails(Site):
    def test_a_failed_replace_raises_nothing_and_leaves_the_old_file(self):
        self.installed.write_bytes(OLD)
        with mock.patch("tacet.script_check.os.replace", side_effect=PermissionError("denied")):
            status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertFalse([n for n in self.leftovers() if n.endswith(".tmp")])
        self.assertEqual(
            status.lines,
            (
                script_check.ROW_DIFFERS.format(path=self.installed),
                script_check.NOTE_COULD_NOT.format(error="PermissionError: denied"),
                script_check.NOTE_BY_HAND,
            ),
        )

    def test_a_failed_backup_replaces_nothing(self):
        self.installed.write_bytes(OLD)
        with mock.patch("tacet.script_check.shutil.copyfile", side_effect=OSError("no room")):
            status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])
        self.assertIn(script_check.NOTE_BY_HAND, status.lines)

    def test_a_failed_temp_write_is_reported_the_same_way(self):
        self.installed.write_bytes(OLD)
        with mock.patch("tacet.script_check.os.fsync", side_effect=OSError("io")):
            status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(self.installed.read_bytes(), OLD)
        self.assertEqual(self.leftovers(), ["tacet_mirror.lua"])
        self.assertIn(script_check.NOTE_BY_HAND, status.lines)

    def test_a_failed_missing_install_keeps_the_missing_row(self):
        with mock.patch("tacet.script_check.os.replace", side_effect=OSError("denied")):
            status = self.run_startup(FakeTerminal("y"))
        self.assertEqual(status.lines[0], script_check.ROW_MISSING.format(path=self.installed))
        self.assertFalse(self.installed.exists())


class TestNotChecked(Site):
    def assert_not_checked(self, status, row, *, terminal) -> None:
        self.assertEqual(status.lines, (row,))
        self.assertEqual(terminal.asked, [])

    def test_no_queue(self):
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal, queue=None)
        self.assert_not_checked(status, script_check.NOT_CHECKED_NO_QUEUE, terminal=terminal)
        self.assertIsNone(status.expected)

    def test_no_repo_copy(self):
        self.repo.unlink()
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal)
        self.assert_not_checked(status, script_check.NOT_CHECKED_NO_REPO_COPY, terminal=terminal)
        self.assertIsNone(status.expected)

    def test_an_unreadable_repo_copy(self):
        terminal = FakeTerminal("y")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            status = self.run_startup(terminal)
        self.assert_not_checked(
            status,
            script_check.NOT_CHECKED_CANNOT_READ.format(path=self.repo, error="PermissionError: denied"),
            terminal=terminal,
        )
        self.assertIsNone(status.expected)

    def test_no_scripts_folder(self):
        shutil.rmtree(self.scripts)
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal)
        self.assert_not_checked(status, script_check.NOT_CHECKED_NO_FOLDER.format(path=self.scripts), terminal=terminal)
        self.assertFalse(self.scripts.exists())
        # The checkout's copy is still known, so the chip can still compare.
        self.assertEqual(status.expected, mirror_report.source_checksum(NEW))

    def test_an_unreadable_scripts_folder(self):
        terminal = FakeTerminal("y")
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            status = self.run_startup(terminal)
        self.assert_not_checked(
            status,
            script_check.NOT_CHECKED_CANNOT_READ.format(path=self.scripts, error="PermissionError: denied"),
            terminal=terminal,
        )
        self.assertEqual(status.expected, mirror_report.source_checksum(NEW))

    def test_an_unknown_platform(self):
        terminal = FakeTerminal("y")
        status = self.run_startup(terminal, sys_platform="win32")
        self.assert_not_checked(status, script_check.NOT_CHECKED_PLATFORM.format(name="win32"), terminal=terminal)

    def test_an_unreadable_installed_script(self):
        self.installed.write_bytes(OLD)
        terminal = FakeTerminal("y")
        real = Path.read_bytes

        def read(path: Path) -> bytes:
            if path == self.installed:
                raise PermissionError("denied")
            return real(path)

        with mock.patch.object(Path, "read_bytes", read):
            status = self.run_startup(terminal)
        self.assert_not_checked(
            status,
            script_check.NOT_CHECKED_CANNOT_READ.format(path=self.installed, error="PermissionError: denied"),
            terminal=terminal,
        )


class TestWording(Site):
    def test_every_line_is_ascii(self):
        self.installed.write_bytes(OLD)
        said: list[str] = []
        outcomes = []
        for answers, overrides in (
            (("y",), {}),
            (("n",), {}),
            ((EOFError,), {}),
            ((), {"check_only": True}),
            ((), {"restart": True}),
            ((), {"queue": None}),
            ((), {"sys_platform": "win32"}),
        ):
            terminal = FakeTerminal(*answers)
            self.installed.write_bytes(OLD)
            outcomes.append(self.run_startup(terminal, **overrides))
            said.extend(terminal.said)
        lines = [line for status in outcomes for line in status.lines] + said + [script_check.QUESTION]
        for line in lines:
            self.assertTrue(line.isascii(), line)

    def test_the_wording_is_pinned(self):
        pinned = {
            script_check.ROW_CURRENT: "current: matches this checkout",
            script_check.ROW_MISSING: "missing: {path}",
            script_check.ROW_DIFFERS: "differs from this checkout: {path}",
            script_check.NOT_REPLACED_NO: "not replaced: answered no",
            script_check.NOT_REPLACED_NO_ANSWER: "not replaced: no answer",
            script_check.NOT_REPLACED_NO_TERMINAL: "not replaced: no terminal to ask",
            script_check.NOT_REPLACED_CHECK: "not replaced: --check only reports",
            script_check.NOT_REPLACED_RESTART: "not replaced: restart of an existing log",
            script_check.ROW_REPLACED: "replaced: {path}",
            script_check.NOTE_BACKUP: "old copy kept as {path}",
            script_check.NOTE_RUN_AGAIN: "run it again from Reaper: the running script is still the old one",
            script_check.NOTE_COULD_NOT: "could not replace it: {error}",
            script_check.NOTE_BY_HAND: "left as it was; copy reaper/tacet_mirror.lua there by hand",
            script_check.NOT_CHECKED_NO_QUEUE: "not checked: no queue",
            script_check.NOT_CHECKED_NO_REPO_COPY: "not checked: no reaper/tacet_mirror.lua in this install",
            script_check.NOT_CHECKED_NO_FOLDER: "not checked: no Reaper Scripts folder at {path}",
            script_check.NOT_CHECKED_PLATFORM: "not checked: unknown platform {name}",
            script_check.NOT_CHECKED_CANNOT_READ: "not checked: cannot read {path}: {error}",
            script_check.INTRO_DIFFERS: "The Reaper script differs from this checkout: {path}",
            script_check.INTRO_MISSING: "Reaper has no copy of the mirror script: {path}",
            script_check.QUESTION: "Replace the Reaper script? [y/N] ",
            script_check.CTRL_C_MESSAGE: "stopped at the script question; nothing was changed",
        }
        for constant, text in pinned.items():
            self.assertEqual(constant, text)

    def test_the_exit_code_for_ctrl_c_is_named(self):
        self.assertEqual(script_check.EXIT_INTERRUPTED, 130)


class TestTheStdinTerminal(unittest.TestCase):
    def test_it_asks_only_when_stdin_and_stdout_are_both_terminals(self):
        terminal = script_check.StdinTerminal()
        for stdin_tty, stdout_tty in ((True, True), (True, False), (False, True), (False, False)):
            with (
                mock.patch("tacet.script_check.sys.stdin") as stdin,
                mock.patch("tacet.script_check.sys.stdout") as stdout,
            ):
                stdin.isatty.return_value = stdin_tty
                stdout.isatty.return_value = stdout_tty
                with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                    self.assertEqual(terminal.can_answer(), stdin_tty and stdout_tty)

    def test_a_closed_stdin_cannot_answer(self):
        with mock.patch("tacet.script_check.sys.stdin", None):
            self.assertFalse(script_check.StdinTerminal().can_answer())


class TestTheLuaChecksumMatches(unittest.TestCase):
    """The pin the issue asks for: Lua and Python hash the same bytes alike."""

    def lua(self) -> str:
        found = shutil.which("lua5.4") or shutil.which("lua")
        if found is None:
            self.skipTest("no lua interpreter (brew install lua / apt install lua5.4)")
        return found

    def lua_checksum(self, path: Path) -> str:
        done = subprocess.run(
            [self.lua(), str(LUA_TEST), "--checksum-of", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return done.stdout.strip()

    def test_the_real_script(self):
        self.assertEqual(self.lua_checksum(REAL_SCRIPT), mirror_report.source_checksum(REAL_SCRIPT.read_bytes()))

    def test_every_byte_value(self):
        path = FIXTURES / "script-checksum-input.bin"
        self.assertEqual(self.lua_checksum(path), mirror_report.source_checksum(path.read_bytes()))

    def test_the_published_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data, want in ((b"", "811c9dc5"), (b"a", "e40c292c"), (b"foobar", "bf9cf968")):
                path = Path(tmp) / "v.bin"
                path.write_bytes(data)
                with self.subTest(data):
                    self.assertEqual(self.lua_checksum(path), want)
                    self.assertRegex(self.lua_checksum(path), re.compile(r"^[0-9a-f]{8}$"))
