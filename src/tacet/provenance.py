"""What code the box is running, asked of git once at startup (#157).

What ran has to be recorded by the box itself: Game 3 had to be reconstructed
from commit times. So `tacet-serve` asks git once, in `serve.main`, before the
event loop exists (#41), and carries the answer as a frozen `Provenance`.

One git subprocess. The filesystem decides "is this a checkout at all" before
git runs, so an installed copy never shells out and a git refusal can never be
mistaken for "not a repository". Every failure becomes a visible `unknown`,
never a crash and never a silent "clean". Standard library only, and not part
of the control path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

#: src layout: <root>/src/tacet/provenance.py
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
GIT_DIR_NAME = ".git"
GIT_EXECUTABLE = "git"
GIT_TIMEOUT_SECONDS = 5.0
#: The tree the box runs from. An untracked file here is new code; elsewhere it is not.
RUNNING_TREE = "src/"
SHORT_COMMIT_LENGTH = 7

# Porcelain v2 vocabulary, named once.
_RECORD_END = "\0"
_HEADER = "# "
_OID = "# branch.oid "
_HEAD = "# branch.head "
_INITIAL = "(initial)"
_DETACHED_HEAD = "(detached)"
_ORDINARY = "1 "
_RENAMED = "2 "
_UNMERGED = "u "
_UNTRACKED = "? "
_RECORD_PREVIEW = 40

# Words (pinned in tests; quoted in docs/box.md and docs/troubleshooting.md).
NOT_A_CHECKOUT = "not a git checkout"
DETACHED = "detached HEAD"
NO_COMMITS = "no commits yet"
WORKTREE = "(worktree)"
DIRTY = "uncommitted changes"
UNKNOWN_PREFIX = "unknown - "
GIT_NOT_FOUND = "git is not installed or not on PATH"
GIT_TIMED_OUT = "git did not answer within {seconds:g}s"
GIT_COULD_NOT_RUN = "git could not run: {error}"
GIT_FAILED = "git status failed (exit {code}): {detail}"
GIT_UNREADABLE = "git status said something this box cannot read: {detail}"
PROBE_FAILED = "the git check itself failed: {error}"


def status_args(root: Path) -> list[str]:
    """Read-only: `--no-optional-locks` keeps status from touching the index."""
    return [
        "--no-optional-locks",
        "-C",
        str(root),
        "status",
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=normal",
    ]


class Source(StrEnum):
    CHECKOUT = "checkout"
    NOT_A_CHECKOUT = "not-a-checkout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GitOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitUnavailableError(Exception):
    """git could not be asked at all: missing, timed out, or would not start."""


class GitRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout: float) -> GitOutput: ...


class SubprocessGit:
    def __init__(self, executable: str = GIT_EXECUTABLE) -> None:
        self._executable = executable

    def run(self, args: Sequence[str], *, timeout: float) -> GitOutput:
        try:
            done = subprocess.run(
                [self._executable, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitUnavailableError(GIT_NOT_FOUND) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitUnavailableError(GIT_TIMED_OUT.format(seconds=timeout)) from exc
        except OSError as exc:
            raise GitUnavailableError(GIT_COULD_NOT_RUN.format(error=exc)) from exc
        return GitOutput(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Status:
    """What `parse_status` reads out of git."""

    commit: str | None  # None for a repository with no commits
    branch: str | None  # None when detached
    detached: bool
    dirty: bool
    untracked: int


def parse_status(stdout: bytes) -> Status:
    """Pure. Raises ValueError on anything it does not recognise."""
    fields = stdout.decode("utf-8", errors="replace").split(_RECORD_END)
    if fields and fields[-1] == "":
        fields.pop()
    oid: str | None = None
    head: str | None = None
    dirty = False
    untracked = 0
    skip = False
    for field in fields:
        if skip:
            # A rename's original path: a path, not a record.
            skip = False
            continue
        if field.startswith(_OID):
            oid = field[len(_OID) :]
        elif field.startswith(_HEAD):
            head = field[len(_HEAD) :]
        elif field.startswith(_HEADER):
            continue
        elif field.startswith((_ORDINARY, _UNMERGED)):
            dirty = True
        elif field.startswith(_RENAMED):
            dirty = True
            skip = True
        elif field.startswith(_UNTRACKED):
            untracked += 1
            if field[len(_UNTRACKED) :].startswith(RUNNING_TREE):
                dirty = True
        else:
            raise ValueError(f"unrecognised record {field[:_RECORD_PREVIEW]!r}")
    if oid is None or head is None:
        raise ValueError("no branch headers")
    detached = head == _DETACHED_HEAD
    return Status(
        commit=None if oid == _INITIAL else oid,
        branch=None if detached else head,
        detached=detached,
        dirty=dirty,
        untracked=untracked,
    )


@dataclass(frozen=True)
class Provenance:
    source: Source
    commit: str | None = None
    branch: str | None = None
    detached: bool | None = None
    dirty: bool | None = None
    untracked: int | None = None
    worktree: bool | None = None
    path: str | None = None
    error: str | None = None

    @classmethod
    def not_a_checkout(cls) -> Provenance:
        return cls(source=Source.NOT_A_CHECKOUT)

    @classmethod
    def unknown(cls, error: str, *, path: Path, worktree: bool) -> Provenance:
        return cls(source=Source.UNKNOWN, worktree=worktree, path=str(path), error=error)

    @classmethod
    def from_status(cls, status: Status, *, path: Path, worktree: bool) -> Provenance:
        return cls(
            source=Source.CHECKOUT,
            commit=status.commit,
            branch=status.branch,
            detached=status.detached,
            dirty=status.dirty,
            untracked=status.untracked,
            worktree=worktree,
            path=str(path),
        )

    @property
    def short_commit(self) -> str | None:
        return None if self.commit is None else self.commit[:SHORT_COMMIT_LENGTH]

    def where(self) -> str | None:
        """'<branch or detached> @ <short commit>', plus the worktree mark."""
        if self.source is not Source.CHECKOUT:
            return None
        name = DETACHED if self.detached else self.branch
        text = f"{name} @ {self.short_commit or NO_COMMITS}"
        return f"{text} {WORKTREE}" if self.worktree else text

    def summary(self) -> str:
        """The banner row."""
        if self.source is Source.NOT_A_CHECKOUT:
            return NOT_A_CHECKOUT
        if self.source is Source.UNKNOWN:
            return f"{UNKNOWN_PREFIX}{self.error}"
        return f"{self.where()}, {DIRTY}" if self.dirty else str(self.where())

    def as_data(self) -> dict[str, Any]:
        """The box-started entry's data. Flat and JSON-safe."""
        return {
            "source": str(self.source),
            "commit": self.commit,
            "branch": self.branch,
            "detached": self.detached,
            "dirty": self.dirty,
            "untracked": self.untracked,
            "worktree": self.worktree,
            "path": self.path,
            "error": self.error,
        }

    def as_snapshot(self) -> dict[str, Any]:
        """What the page needs, nothing more."""
        return {
            "source": str(self.source),
            "dirty": self.dirty,
            "where": self.where(),
            "error": self.error,
        }


def _first_line(raw: bytes) -> str:
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    return ""


def probe(
    root: Path = CHECKOUT_ROOT,
    *,
    runner: GitRunner | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Provenance:
    """Once, at startup, before the loop (#41). Never raises.

    The filesystem decides whether this is a checkout; git is only asked after.
    Any failure after that point is `unknown`, never `not a checkout`: the tree
    may be dirty, and saying otherwise would hide it.
    """
    dot_git = root / GIT_DIR_NAME
    if not dot_git.exists():
        return Provenance.not_a_checkout()
    worktree = dot_git.is_file()
    try:
        git = SubprocessGit() if runner is None else runner
        output = git.run(status_args(root), timeout=timeout)
        if output.returncode != 0:
            detail = GIT_FAILED.format(code=output.returncode, detail=_first_line(output.stderr))
            return Provenance.unknown(detail, path=root, worktree=worktree)
        return Provenance.from_status(parse_status(output.stdout), path=root, worktree=worktree)
    except GitUnavailableError as exc:
        return Provenance.unknown(str(exc), path=root, worktree=worktree)
    except ValueError as exc:
        return Provenance.unknown(GIT_UNREADABLE.format(detail=exc), path=root, worktree=worktree)
    except Exception as exc:
        # The box must start: a forensic record is never worth a refusal to
        # start, and an unknown is shown on the page rather than swallowed.
        error = PROBE_FAILED.format(error=f"{type(exc).__name__}: {exc}")
        return Provenance.unknown(error, path=root, worktree=worktree)
