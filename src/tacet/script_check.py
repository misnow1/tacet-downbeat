"""Is the Reaper script the checkout's, and offer to fix it (#223).

Reaper runs its own copy of `reaper/tacet_mirror.lua` out of `REAPER/Scripts`.
`git pull` never touches that copy, so a box can be new and the mirror old
(game 4 found one dated September). At startup `tacet-serve` compares the
installed copy with the checkout's, byte for byte, says missing, differs or
current in the banner, and if it is not current and someone is at a terminal,
asks `Replace the Reaper script? [y/N]`.

Warn only. Nothing here blocks the box or a take: no terminal, a `--check`, a
restart of an existing log, an unreadable file, a failed write - each is a row
in the banner and the box carries on. The question comes only on a fresh log,
because an unanswered question has no timeout and a restart mid-game must never
wait on one. Ctrl-C at the question is the one exception and stops the box
without changing anything, the same as Ctrl-C anywhere else before it starts.

The checkout's copy is read once, before the question, and those same bytes are
compared, written and checksummed. The checksum goes to the page (`expected`),
which compares it with what the running script reports of itself.

Everything here is blocking disk I/O and one `input()`, so it runs in
`serve.main` before the event loop exists (#41) and never after. Standard
library only, and not part of the control path.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from . import mirror_report, provenance, reach

SCRIPT_NAME = "tacet_mirror.lua"
#: Where the checkout keeps it, relative to the checkout root.
REPO_SCRIPT = provenance.CHECKOUT_ROOT / "reaper" / SCRIPT_NAME
REPO_SCRIPT_RELATIVE = f"reaper/{SCRIPT_NAME}"
TEMP_SUFFIX = ".tmp"
#: Not `.lua`, so Reaper never lists it as a second script.
BACKUP_INFIX = ".bak-"
BACKUP_STAMP = "%Y%m%d-%H%M%S"

#: Reaper's resource folder on each platform, below the home directory. Reaper
#: lets the resource path be moved; this is the default, and a moved one reads
#: as "no Reaper Scripts folder", which is a row, not a guess.
SCRIPTS_PARTS = {
    reach.Platform.MACOS: ("Library", "Application Support", "REAPER", "Scripts"),
    reach.Platform.LINUX: (".config", "REAPER", "Scripts"),
}

YES_ANSWERS = frozenset({"y", "yes"})
#: What a shell reports for a process ended by SIGINT: 128 + 2.
EXIT_INTERRUPTED = 130

# Words. Pinned in tests; quoted in docs/box.md, docs/gameday.md and
# docs/troubleshooting.md.
INTRO_DIFFERS = "The Reaper script differs from this checkout: {path}"
INTRO_MISSING = "Reaper has no copy of the mirror script: {path}"
QUESTION = "Replace the Reaper script? [y/N] "
CTRL_C_MESSAGE = "stopped at the script question; nothing was changed"

ROW_CURRENT = "current: matches this checkout"
ROW_MISSING = "missing: {path}"
ROW_DIFFERS = "differs from this checkout: {path}"
ROW_REPLACED = "replaced: {path}"
NOT_REPLACED_NO = "not replaced: answered no"
NOT_REPLACED_NO_ANSWER = "not replaced: no answer"
NOT_REPLACED_NO_TERMINAL = "not replaced: no terminal to ask"
NOT_REPLACED_CHECK = "not replaced: --check only reports"
NOT_REPLACED_RESTART = "not replaced: restart of an existing log"
NOTE_BACKUP = "old copy kept as {path}"
NOTE_RUN_AGAIN = "run it again from Reaper: the running script is still the old one"
NOTE_COULD_NOT = "could not replace it: {error}"
NOTE_BY_HAND = f"left as it was; copy {REPO_SCRIPT_RELATIVE} there by hand"
NOT_CHECKED_NO_QUEUE = "not checked: no queue"
NOT_CHECKED_NO_REPO_COPY = f"not checked: no {REPO_SCRIPT_RELATIVE} in this install"
NOT_CHECKED_NO_FOLDER = "not checked: no Reaper Scripts folder at {path}"
NOT_CHECKED_PLATFORM = "not checked: unknown platform {name}"
NOT_CHECKED_CANNOT_READ = "not checked: cannot read {path}: {error}"


class Match(StrEnum):
    CURRENT = "current"
    MISSING = "missing"
    DIFFERS = "differs"


class Terminal(Protocol):
    """Whatever can be asked a question. Injected, so no test touches stdin."""

    def can_answer(self) -> bool: ...

    def ask(self, prompt: str) -> str: ...

    def say(self, line: str) -> None: ...


class StdinTerminal:
    def can_answer(self) -> bool:
        stdin = sys.stdin
        # Both: with stdout redirected the prompt would go to the file and the
        # box would wait on a question nobody can see.
        stdout = sys.stdout
        return stdin is not None and stdin.isatty() and stdout is not None and stdout.isatty()

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, line: str) -> None:
        print(line)


@dataclass(frozen=True)
class Plan:
    """What `startup` needs to know, none of it read from the machine here."""

    #: `--queue`, or None. Without one no mirror runs and there is nothing to check.
    queue: Path | str | None
    #: `--check`: report, never ask and never write.
    check_only: bool = False
    #: The `--log` already holds a game: a restart, which never waits on a question.
    restart: bool = False
    home: Path = field(default_factory=Path.home)
    sys_platform: str = sys.platform
    repo_script: Path = REPO_SCRIPT


@dataclass(frozen=True)
class ScriptStatus:
    #: The banner's `script` row first, then its notes.
    lines: tuple[str, ...]
    #: The checkout's checksum, or None when its copy could not be read.
    expected: str | None


# -- the pure core ----------------------------------------------------------


def installed_path(sys_platform: str, home: Path) -> Path | None:
    platform = reach.platform_of(sys_platform)
    if platform is None:
        return None
    return home.joinpath(*SCRIPTS_PARTS[platform], SCRIPT_NAME)


def compare(repo: bytes, installed: bytes | None) -> Match:
    if installed is None:
        return Match.MISSING
    return Match.CURRENT if installed == repo else Match.DIFFERS


def backup_path(installed: Path, when: datetime) -> Path:
    return installed.with_name(f"{installed.name}{BACKUP_INFIX}{when.strftime(BACKUP_STAMP)}")


def is_yes(answer: str) -> bool:
    return answer.strip().lower() in YES_ANSWERS


def _error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def status_lines(match: Match, installed: Path, notes: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The row for a script left as it was, then why."""
    if match is Match.CURRENT:
        return (ROW_CURRENT,)
    row = ROW_MISSING if match is Match.MISSING else ROW_DIFFERS
    return (row.format(path=installed), *notes)


def replaced_lines(installed: Path, backup: Path | None) -> tuple[str, ...]:
    kept = (NOTE_BACKUP.format(path=backup),) if backup is not None else ()
    return (ROW_REPLACED.format(path=installed), *kept, NOTE_RUN_AGAIN)


def _cannot_read(path: Path, error: OSError) -> str:
    return NOT_CHECKED_CANNOT_READ.format(path=path, error=_error(error))


# -- the thin shell ---------------------------------------------------------


def startup(plan: Plan, terminal: Terminal, clock: Callable[[], datetime]) -> ScriptStatus:
    """Compare, maybe ask, maybe replace. Never raises except KeyboardInterrupt."""
    if plan.queue is None:
        return ScriptStatus((NOT_CHECKED_NO_QUEUE,), None)
    try:
        repo = plan.repo_script.read_bytes()
    except FileNotFoundError:
        return ScriptStatus((NOT_CHECKED_NO_REPO_COPY,), None)
    except OSError as error:
        return ScriptStatus((_cannot_read(plan.repo_script, error),), None)
    expected = mirror_report.source_checksum(repo)
    target = installed_path(plan.sys_platform, plan.home)
    if target is None:
        return ScriptStatus((NOT_CHECKED_PLATFORM.format(name=plan.sys_platform),), expected)
    try:
        has_folder = target.parent.is_dir()
    except OSError as error:
        return ScriptStatus((_cannot_read(target.parent, error),), expected)
    if not has_folder:
        return ScriptStatus((NOT_CHECKED_NO_FOLDER.format(path=target.parent),), expected)
    try:
        installed: bytes | None = target.read_bytes()
    except FileNotFoundError:
        installed = None
    except OSError as error:
        return ScriptStatus((_cannot_read(target, error),), expected)
    match = compare(repo, installed)
    if match is Match.CURRENT:
        return ScriptStatus(status_lines(match, target), expected)
    return ScriptStatus(_not_current(plan, terminal, clock, match, repo, target), expected)


def _refusal(plan: Plan, terminal: Terminal) -> str | None:
    """Why there will be no question, or None if one may be asked."""
    if plan.check_only:
        return NOT_REPLACED_CHECK
    if plan.restart:
        return NOT_REPLACED_RESTART
    if not terminal.can_answer():
        return NOT_REPLACED_NO_TERMINAL
    return None


def _not_current(
    plan: Plan,
    terminal: Terminal,
    clock: Callable[[], datetime],
    match: Match,
    repo: bytes,
    target: Path,
) -> tuple[str, ...]:
    refusal = _refusal(plan, terminal)
    if refusal is not None:
        return status_lines(match, target, (refusal,))
    intro = INTRO_MISSING if match is Match.MISSING else INTRO_DIFFERS
    terminal.say(intro.format(path=target))
    try:
        answer = terminal.ask(QUESTION)
    except (EOFError, UnicodeError):
        return status_lines(match, target, (NOT_REPLACED_NO_ANSWER,))
    if not is_yes(answer):
        return status_lines(match, target, (NOT_REPLACED_NO,))
    backup = backup_path(target, clock()) if match is Match.DIFFERS else None
    try:
        _replace(repo, target, backup)
    except OSError as error:
        failed = (NOTE_COULD_NOT.format(error=_error(error)), NOTE_BY_HAND)
        return status_lines(match, target, failed)
    return replaced_lines(target, backup)


def _replace(repo: bytes, target: Path, backup: Path | None) -> None:
    """Write beside the target, keep the old copy, then swap. The swap is the
    only step that changes the script, and it is atomic: Reaper sees the old
    file or the whole new one."""
    temp = target.with_name(target.name + TEMP_SUFFIX)
    try:
        with temp.open("wb") as handle:
            handle.write(repo)
            handle.flush()
            os.fsync(handle.fileno())
        if backup is not None:
            shutil.copyfile(target, backup)
        temp.replace(target)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
