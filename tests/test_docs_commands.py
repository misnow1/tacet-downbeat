"""The docs name each tool by its command, and every command they name exists (#201).

Each tool has one name to run it by: its `tacet-*` command in pyproject.toml's
`[project.scripts]`. The `python -m tacet.<module>` form ties an instruction to
a module path, which #199 wants to be free to move, and two names for one tool
is how the docs came to disagree with each other. Every box has run `make
install` since #193, so the command is always there.

Two checks, over every place a reader copies a command from: the Markdown in
`docs/`, CLAUDE.md and README.md, and each module's docstring, which is the
`--help` text of the tools. Nothing gives `python -m tacet.`, and nothing names a
`tacet-*` command that `[project.scripts]` does not declare. #48's page messages
are pinned in test_runbook.py, not here.
"""

import ast
import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS = REPO_ROOT / "docs"
PACKAGE = REPO_ROOT / "src" / "tacet"
#: The Markdown outside docs/ that a reader runs commands from.
TOP_LEVEL_DOCS = (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "README.md")

#: The module form, however the interpreter is spelled: `python`, `python3`,
#: `python3.12`, `.venv/bin/python`. `\s+`, because a wrapped line can split it.
MODULE_FORM = re.compile(r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+tacet\.")

#: A `tacet-` word that could be a command: lower case and hyphenated, and not
#: the tail of a longer word. `tacet.toml` and `tacet_mirror.lua` have no hyphen
#: after `tacet`, and `../tacet-<issue>` has no letter after it, so they do not
#: match.
COMMAND = re.compile(r"(?<![\w-])tacet-[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

#: `tacet-` words that are not commands, each with why.
NOT_COMMANDS = frozenset(
    {
        # The repository: the title of CLAUDE.md, the clone directory, and
        # every issue link.
        "tacet-downbeat",
    }
)

#: Files the scan must reach. A glob that silently matches nothing passes.
MUST_SCAN = (
    "CLAUDE.md",
    "README.md",
    "docs/box.md",
    "docs/gameday.md",
    "docs/handoff.md",
    "docs/reaper.md",
    "src/tacet/verify_dm7.py",
    "src/tacet/verify_reaper.py",
)


def scanned_docs() -> list[Path]:
    return [*TOP_LEVEL_DOCS, *sorted(DOCS.glob("*.md"))]


def module_docstrings() -> dict[str, str]:
    """Each module's docstring by its repo path. For a tool it is the `--help` text."""
    found: dict[str, str] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")), clean=False)
        if docstring:
            found[path.relative_to(REPO_ROOT).as_posix()] = docstring
    return found


def scanned_texts() -> dict[str, str]:
    texts = {path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8") for path in scanned_docs()}
    texts.update(module_docstrings())
    return texts


def module_form_lines(text: str) -> list[int]:
    """The line of each `python -m tacet.` in `text`, counting from 1."""
    return [text.count("\n", 0, match.start()) + 1 for match in MODULE_FORM.finditer(text)]


def named_commands(text: str) -> set[str]:
    return {match.group() for match in COMMAND.finditer(text)} - NOT_COMMANDS


def declared_commands() -> frozenset[str]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return frozenset(project["project"]["scripts"])


class TestTheDocsUseTheNamedCommands(unittest.TestCase):
    def test_nothing_runs_a_tool_as_a_module(self) -> None:
        for name, text in scanned_texts().items():
            with self.subTest(file=name):
                self.assertEqual(
                    module_form_lines(text),
                    [],
                    f"{name} gives `python -m tacet.` at these lines; name the command from [project.scripts]",
                )

    def test_every_command_named_is_declared(self) -> None:
        declared = declared_commands()
        for name, text in scanned_texts().items():
            with self.subTest(file=name):
                self.assertEqual(
                    named_commands(text) - declared,
                    set(),
                    f"{name} names a command that pyproject.toml's [project.scripts] does not declare",
                )


class TestTheScanReachesWhatItMeans(unittest.TestCase):
    def test_the_scan_reaches_the_runbooks_the_instructions_and_the_tool_help(self) -> None:
        texts = scanned_texts()
        for name in MUST_SCAN:
            with self.subTest(file=name):
                self.assertIn(name, texts)

    def test_every_declared_command_is_one_the_pattern_finds(self) -> None:
        # A command named any other way would never be checked at all.
        for command in declared_commands():
            with self.subTest(command=command):
                self.assertIsNotNone(COMMAND.fullmatch(command))
                self.assertNotIn(command, NOT_COMMANDS)


class TestTheChecksCanFail(unittest.TestCase):
    """The checks above pass on clean docs; these show they would see drift."""

    def test_the_module_form_is_seen_however_python_is_spelled(self) -> None:
        for text in (
            "python -m tacet.verify_dm7 --nudge",
            "python3 -m tacet.pilot_check",
            "python3.12 -m tacet.quarters",
            ".venv/bin/python -m tacet.timeouts",
            "Run `python\n-m tacet.rtd_replay` once.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(module_form_lines(text), [])

    def test_the_line_of_each_module_form_is_reported(self) -> None:
        self.assertEqual(module_form_lines("one\npython -m tacet.serve\nthree\npython -m tacet.rtd_replay\n"), [2, 4])

    def test_other_modules_and_the_named_commands_are_not_the_module_form(self) -> None:
        for text in (
            "tacet-verify-dm7 --nudge",
            "python -m tests.snapshots",
            "python -m pip install --upgrade pip",
            "when `rtd_replay` runs as `python -m`",
        ):
            with self.subTest(text=text):
                self.assertEqual(module_form_lines(text), [])

    def test_a_command_is_found_in_prose_code_and_paths(self) -> None:
        text = "Run `tacet-serve --log x`, then .venv/bin/tacet-pilot-check; read tacet-rtd's output."
        self.assertEqual(named_commands(text), {"tacet-serve", "tacet-pilot-check", "tacet-rtd"})

    def test_an_undeclared_command_is_caught(self) -> None:
        self.assertEqual(named_commands("tacet-verify-dm8 --nudge") - declared_commands(), {"tacet-verify-dm8"})

    def test_words_that_are_not_commands_are_not_found(self) -> None:
        text = (
            "# tacet-downbeat\n"
            "https://github.com/example/tacet-downbeat/issues/201\n"
            "cd tacet-downbeat\n"
            "`tacet.toml`, `tacet.config`, `tacet_mirror.lua`\n"
            "git worktree add ../tacet-<issue> <branch>\n"
            "branch 132-docs-wording\n"
        )
        self.assertEqual(named_commands(text), set())
