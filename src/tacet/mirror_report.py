"""What Reaper's mirror script says about its project (#204).

`reaper/tacet_mirror.lua` writes `<queue path>.report` every few seconds: a few
`key<TAB>value` lines saying which project file Reaper has open and where a
recording would land. The box reads it and the page shows a chip, so a project
that was never saved - which sends the take to `~/Documents/REAPER Media` - is
seen before kickoff, not after the take (game 4).

Warn only. Nothing here blocks recording, and nothing here is a reason to touch
the fader.

The file, version 1:

    version<TAB>1
    seq<TAB>42
    project<TAB>/Users/x/Reaper Media/20261114 X/20261114 X.RPP
    record_path<TAB>/Users/x/Reaper Media/20261114 X/Media
    script_checksum<TAB>0badf00d
    tracks<TAB>4
    track<TAB>1<TAB>1<TAB>MV Conductor
    track<TAB>2<TAB>0<TAB>MV Spare

An empty `project` is the only thing that means unsaved. A missing `project`
key means the mirror could not say, which is unknown. Unknown keys are ignored
and kept, so a later field needs no version bump; `version` changes only for a
breaking change to the format.

`script_checksum` (#223) is the FNV-1a 32-bit hash of the script's own source, as
eight lowercase hex digits, read once when the script starts. The box compares
it with the checkout's copy: a different value, or no key at all (a script from
before this field), means Reaper is still running older code than the file on
disk. An empty value means the script could not read itself, which says nothing.

`track` (#167) repeats, once per track: number, arm state and raw name. `fields`
keeps every pair in order, and `tacet.arming` reads them; this module only
keeps them.

Freshness has no clock in it. A report counts only if this run of the box saw
its `seq` change: the first read is a baseline, so last game's file left on disk
reads as unknown, and a mirror that stops (or a file that vanishes) ages into
stale after `STALE_SECONDS` whatever the file still says.

A pure core (`parse_report`, `observe`, `assess`) and a thin shell (`read_report`,
`Watch`). Standard library only, no socket, and nothing in the control path
imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Appended to the queue path, so the report sits beside the queue the mirror
#: already has the path of.
REPORT_SUFFIX = ".report"
DELIMITER = "\t"
KEY_VERSION = "version"
KEY_SEQ = "seq"
KEY_PROJECT = "project"
KEY_RECORD_PATH = "record_path"
KEY_SCRIPT_CHECKSUM = "script_checksum"
#: The newest format this box can read. A later one is refused, not guessed at.
SUPPORTED_VERSION = 1

#: The mirror writes every 2 s (`REPORT_SECONDS` in the script); the box looks
#: every second and calls the report stale after 10 s without a change.
POLL_SECONDS = 1.0
STALE_SECONDS = 10.0

#: FNV-1a, 32 bit. Small enough to write in Lua with no library, and it is a
#: fingerprint against a stale copy, not a defence against anyone.
FNV_OFFSET_BASIS = 0x811C9DC5
FNV_PRIME = 0x01000193
FNV_MASK = 0xFFFFFFFF
CHECKSUM_DIGITS = 8

HOME_MARK = "~"
PATH_SEPARATOR = "/"


class ReportError(ValueError):
    """The text is not a report this box can read."""


class Status(StrEnum):
    SAVED = "saved"
    UNSAVED = "unsaved"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    """Why the status is unknown. Only set when it is."""

    NO_REPORT = "no-report"
    STOPPED = "stopped"
    UNREADABLE = "unreadable"


class ScriptAge(StrEnum):
    """Whether the running script is the checkout's. Only asked when the project
    status is known; otherwise there is no report to ask it of."""

    CURRENT = "current"
    OLDER = "older"
    UNCHECKED = "unchecked"


def source_checksum(data: bytes) -> str:
    """FNV-1a 32-bit of `data`, eight lowercase hex digits. The Lua script
    computes the same value over its own bytes; a test holds the two equal."""
    value = FNV_OFFSET_BASIS
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & FNV_MASK
    return f"{value:0{CHECKSUM_DIGITS}x}"


@dataclass(frozen=True)
class Report:
    version: int
    seq: int
    #: None: no `project` key. "": the project has never been saved.
    project: str | None
    record_path: str | None
    #: Every pair in file order, known keys and unknown.
    fields: tuple[tuple[str, str], ...]
    #: None: no key (a script from before #223). "": the script could not read itself.
    script_checksum: str | None = None


@dataclass(frozen=True)
class Unreadable:
    """A file that exists and cannot be used, with why."""

    detail: str


#: One read of the file: a report, nothing there, or a file that cannot be used.
Read = Report | Unreadable | None


@dataclass(frozen=True)
class Observed:
    """What this run of the box has seen of the file. Immutable."""

    #: The last seq seen, or None before any report was read.
    seq: int | None
    report: Report | None
    #: When a seq change was last witnessed, on the caller's clock. None until
    #: one has been: the baseline read is not a witnessed change.
    changed_at: float | None
    #: Whether the latest read was a file that could not be used.
    unreadable: bool

    @classmethod
    def empty(cls) -> Observed:
        return cls(seq=None, report=None, changed_at=None, unreadable=False)


@dataclass(frozen=True)
class ProjectCheck:
    status: Status
    reason: Reason | None
    record_path: str | None
    project: str | None
    #: What `as_snapshot` shortens to `~`. Empty shortens nothing.
    home: str = ""
    script: ScriptAge = ScriptAge.UNCHECKED

    def as_snapshot(self) -> dict[str, Any]:
        """For the page. `seq` is deliberately absent: it changes every two
        seconds and would make every broadcast different (#147)."""
        return {
            "status": str(self.status),
            "reason": None if self.reason is None else str(self.reason),
            "record_path": _shorten(self.record_path, self.home),
            "project": _shorten(self.project, self.home),
            "script": str(self.script),
        }


def report_path(queue: Path | str) -> Path:
    return Path(str(queue) + REPORT_SUFFIX)


def _shorten(path: str | None, home: str) -> str | None:
    if path is None or not home:
        return path
    if path == home:
        return HOME_MARK
    prefix = home.rstrip(PATH_SEPARATOR) + PATH_SEPARATOR
    if path.startswith(prefix):
        return HOME_MARK + PATH_SEPARATOR + path[len(prefix) :]
    return path


def parse_report(text: str) -> Report:
    pairs: list[tuple[str, str]] = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        key, separator, value = line.partition(DELIMITER)
        if not separator:
            raise ReportError(f"not a key and a value: {line!r}")
        pairs.append((key, value))
    values = dict(pairs)
    version = _integer(values, KEY_VERSION)
    if version < 1 or version > SUPPORTED_VERSION:
        raise ReportError(f"version {version} is not one this box reads")
    seq = _integer(values, KEY_SEQ)
    return Report(
        version=version,
        seq=seq,
        project=values.get(KEY_PROJECT),
        record_path=values.get(KEY_RECORD_PATH) or None,
        fields=tuple(pairs),
        script_checksum=values.get(KEY_SCRIPT_CHECKSUM),
    )


def _integer(values: dict[str, str], key: str) -> int:
    if key not in values:
        raise ReportError(f"no {key}")
    try:
        return int(values[key])
    except ValueError:
        raise ReportError(f"{key} is not a number: {values[key]!r}") from None


def observe(prior: Observed | None, read: Read, now: float) -> Observed:
    """Fold one read into what has been seen. Pure.

    A missing file changes nothing, so it ages into stale; a file that cannot
    be used is remembered as such until a good one replaces it.
    """
    before = prior if prior is not None else Observed.empty()
    if read is None:
        return before
    if isinstance(read, Unreadable):
        return Observed(before.seq, before.report, before.changed_at, unreadable=True)
    if before.seq is None:
        return Observed(read.seq, read, None, unreadable=False)
    changed_at = now if read.seq != before.seq else before.changed_at
    return Observed(read.seq, read, changed_at, unreadable=False)


def freshness(observed: Observed, now: float) -> Reason | None:
    """Why the report is not to be believed at `now`, or None if it is. Pure.

    Shared by every reader of the report, so they all agree on what unknown means."""
    if observed.unreadable:
        return Reason.UNREADABLE
    if observed.report is None or observed.changed_at is None:
        return Reason.NO_REPORT
    if now - observed.changed_at >= STALE_SECONDS:
        return Reason.STOPPED
    return None


def assess(observed: Observed, now: float, home: str, *, expected_script: str | None = None) -> ProjectCheck:
    """What the box may say about the project at `now`. Pure.

    `expected_script` is the checksum of the checkout's copy of the script, or
    None when the box could not read it."""
    stale = freshness(observed, now)
    report = observed.report
    if stale is not None or report is None:
        return _unknown(stale or Reason.NO_REPORT, home)
    if report.project is None:
        return _unknown(Reason.UNREADABLE, home)
    record_path = report.record_path or None
    if report.project == "":
        return ProjectCheck(Status.UNSAVED, None, record_path, None, home, _script_age(report, expected_script))
    if record_path is None:
        return _unknown(Reason.UNREADABLE, home)
    return ProjectCheck(Status.SAVED, None, record_path, report.project, home, _script_age(report, expected_script))


def _script_age(report: Report, expected: str | None) -> ScriptAge:
    if expected is None or report.script_checksum == "":
        return ScriptAge.UNCHECKED
    return ScriptAge.CURRENT if report.script_checksum == expected else ScriptAge.OLDER


def _unknown(reason: Reason, home: str) -> ProjectCheck:
    return ProjectCheck(Status.UNKNOWN, reason, None, None, home)


def read_report(path: Path) -> Read:
    """Blocking. Run it off the event loop: `Watch` does."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        return Unreadable(f"{type(error).__name__}: {error}")
    try:
        return parse_report(text)
    except ReportError as error:
        return Unreadable(str(error))


class Watch:
    """Reads the report every `POLL_SECONDS` and hands what it has seen on.

    One task, so reads never overlap. There is no `wait_for` on the read: a
    read that hangs only ages the report into stale, and only one thread is
    ever stuck. Never raises (cancellation excepted): a failed read counts as
    an unreadable file, and a failing callback is dropped.
    """

    def __init__(
        self,
        read: Callable[[], Read],
        on_observed: Callable[[Observed], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._read = read
        self._on_observed = on_observed
        self._clock = clock
        self._sleep = sleep
        self._observed: Observed | None = None

    async def run(self) -> None:
        while True:
            await self._once()
            await self._sleep(POLL_SECONDS)

    async def _once(self) -> None:
        try:
            read: Read = await asyncio.to_thread(self._read)
        except Exception as error:
            read = Unreadable(f"{type(error).__name__}: {error}")
        self._observed = observe(self._observed, read, self._clock())
        with contextlib.suppress(Exception):
            self._on_observed(self._observed)
