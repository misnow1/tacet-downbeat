"""The game clock, period and running/stopped, read live from the scoreboard's RTD (#235).

Pure and standard library only. No socket, no asyncio, no clock, no files: the
box's receiver (`tacet.serve`) hands each datagram in with the box's own
monotonic time on it, and `tacet.app` decides what to log and show. What this
module yields is context for the operator, never an interlock (design.md 2, 8):
nothing here may reach the control path, and `tests/test_dependency_policy.py`
says so by reading the imports.

Liveness comes from arrivals, not from the clock's value. The time of day is
written every second whatever the game does, so a stopped game clock still
arrives, and a feed that has gone quiet is told from one that is merely stopped
by there being no valid write for `STALE_SECONDS`. Unless the feed is live the
view carries no clock, no period and no flag, so the page cannot show a frozen
time as if it were the game's.

The reader is meant to survive anything a UDP port can be sent. A datagram
over `MAX_DATAGRAM_BYTES` is counted and never decoded; decoding stops above
`DECODES_PER_SECOND`; a frame that does not decode is counted and applies
nothing and does not refresh liveness. `tacet.app` wraps each call in a
catch-all on top of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from . import rtd
from .datagram import Datagram

#: The time of day arrives every second whatever the game does. The longest
#: normal gap in 66,775 across Games 4 and 5 was 1.109 s, and every real
#: silence was 18 s or more.
STALE_SECONDS = 3.0
#: How old a snapshot of a running clock may be on the page before the page
#: blanks it (#235). A different question from `STALE_SECONDS`, with the same
#: answer: while the board says it is running, its clock changed at least every
#: 1.11 s in both captures, so the box pushes about once a second, and 3 s is
#: slack for the wifi without letting a frozen running clock look live for
#: longer than the box's own rule.
PAGE_FRESH_SECONDS = STALE_SECONDS
#: A full write is the largest datagram ever seen, 378 bytes on the wire: a
#: frame's minimum plus the whole buffer. Twice that is room to spare.
MAX_DATAGRAM_BYTES = 2 * (rtd.MIN_FRAME_LENGTH + rtd.BUFFER_SIZE)
#: Ten times the busiest second in either game (5).
DECODES_PER_SECOND = 50
#: The span `DECODES_PER_SECOND` is counted over.
BUDGET_WINDOW_SECONDS = 1.0

#: The values of the flags in the football field map.
FLAG_STOPPED = "s"
FLAG_RUNNING = " "
FLAG_AT_ZERO = "z"
FLAG_NOT_AT_ZERO = " "


class Status(StrEnum):
    #: Nothing has ever arrived.
    NEVER = "never"
    #: A valid write within the window, from one source, with no flood.
    LIVE = "live"
    #: It arrived once and has stopped.
    STALE = "stale"
    #: Datagrams are arriving and none decodes.
    UNDECODABLE = "undecodable"
    #: More than one source wrote within the window: whose clock is it?
    TWO_SOURCES = "two-sources"
    #: Decoding stopped above the budget within the window.
    FLOOD = "flood"
    #: The receiver itself is not working: bind failed, socket lost.
    DOWN = "down"


@dataclass(frozen=True)
class Counts:
    datagrams: int = 0
    writes: int = 0
    faults: int = 0
    oversize: int = 0
    dropped: int = 0

    def as_data(self) -> dict[str, int]:
        return {
            "datagrams": self.datagrams,
            "writes": self.writes,
            "faults": self.faults,
            "oversize": self.oversize,
            "dropped": self.dropped,
        }


@dataclass(frozen=True)
class ClockView:
    status: Status
    #: The last source a valid write came from, if any.
    source: str | None
    #: The sources with a valid write within the window.
    sources: tuple[str, ...]
    #: The latest valid write, on the box's monotonic clock. Only while the feed
    #: is not live: it is fixed while nothing valid arrives, where an age would
    #: churn the snapshot (#147).
    last_write_at: float | None
    detail: str | None
    #: The values below are None unless `status` is LIVE, and None also for a
    #: field no write has covered yet. A blank is "", never None.
    clock: str | None
    running: bool | None
    at_zero: bool | None
    quarter: str | None
    period: str | None

    @classmethod
    def down(cls, detail: str) -> ClockView:
        """A view that says the reader is not working, with nothing to show."""
        return cls(Status.DOWN, None, (), None, detail, None, None, None, None, None)

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "source": self.source,
            "sources": list(self.sources),
            "last_write_at": self.last_write_at,
            "detail": self.detail,
            "clock": self.clock,
            "running": self.running,
            "at_zero": self.at_zero,
            "quarter": self.quarter,
            "period": self.period,
        }


@dataclass(frozen=True)
class StatusChanged:
    at: float
    previous: Status
    view: ClockView
    counts: Counts


@dataclass(frozen=True)
class SourceChanged:
    at: float
    old: str | None
    new: str


Transition = StatusChanged | SourceChanged


@dataclass(frozen=True)
class Fed:
    #: Whether anything the page shows differs from before this datagram.
    shown_changed: bool
    transitions: tuple[Transition, ...]


def running_of(flag: str | None) -> bool | None:
    """`s` is stopped and a blank is running; anything else is not claimed."""
    if flag == FLAG_STOPPED:
        return False
    if flag == FLAG_RUNNING:
        return True
    return None


def at_zero_of(flag: str | None) -> bool | None:
    if flag == FLAG_AT_ZERO:
        return True
    if flag == FLAG_NOT_AT_ZERO:
        return False
    return None


def trimmed(value: str | None) -> str | None:
    """The field as text, without its padding. Never parsed: a value the board
    writes that is not a time still reaches the page as it was written."""
    return None if value is None else value.strip()


def status_at(
    *,
    now: float,
    stale_seconds: float,
    down: bool,
    last_drop_at: float | None,
    recent_sources: int,
    last_write_at: float | None,
    last_arrival_at: float | None,
) -> Status:
    """The precedence: DOWN, FLOOD, TWO_SOURCES, LIVE, UNDECODABLE, STALE, NEVER."""
    if down:
        return Status.DOWN
    if last_drop_at is not None and now - last_drop_at < stale_seconds:
        return Status.FLOOD
    if recent_sources > 1:
        return Status.TWO_SOURCES
    if last_write_at is not None and now - last_write_at < stale_seconds:
        return Status.LIVE
    if last_arrival_at is None:
        return Status.NEVER
    if now - last_arrival_at < stale_seconds:
        return Status.UNDECODABLE
    return Status.STALE


class ClockReader(Protocol):
    """What `tacet.app` needs of a reader, so a test can hand it one that fails."""

    @property
    def counts(self) -> Counts: ...

    def feed(self, datagram: Datagram) -> Fed: ...

    def poll(self, *, now: float) -> tuple[Transition, ...]: ...

    def view(self, *, now: float) -> ClockView: ...

    def fail(self, detail: str, *, now: float) -> tuple[Transition, ...]: ...

    def forget_display(self) -> None: ...


class Reader:
    def __init__(
        self,
        *,
        stale_seconds: float = STALE_SECONDS,
        max_datagram_bytes: int = MAX_DATAGRAM_BYTES,
        decodes_per_second: int = DECODES_PER_SECOND,
    ) -> None:
        self._stale_seconds = stale_seconds
        self._max_datagram_bytes = max_datagram_bytes
        self._decodes_per_second = decodes_per_second
        self._display = rtd.Display()
        self._down: str | None = None
        self._counts = Counts()
        self._last_write_at: float | None = None
        self._last_arrival_at: float | None = None
        self._last_drop_at: float | None = None
        #: Source -> time of its last valid write.
        self._writers: dict[str, float] = {}
        self._source: str | None = None
        self._window_start = 0.0
        self._window_decodes = 0
        self._reported = Status.NEVER

    @property
    def counts(self) -> Counts:
        return self._counts

    def feed(self, datagram: Datagram) -> Fed:
        """One datagram, stamped with the box's monotonic clock."""
        now = datagram.time
        self._count(datagrams=1)
        # The cheap refusals first: a flood costs a length check and a budget
        # check, never a settle. The tick's `poll` reports the edges they cause,
        # but for the drop that starts a FLOOD, which is reported at once.
        if len(datagram.payload) > self._max_datagram_bytes:
            self._last_arrival_at = now
            self._count(oversize=1)
            return Fed(shown_changed=False, transitions=())
        if self._over_budget(now):
            self._last_arrival_at = now
            starts = self._last_drop_at is None or now - self._last_drop_at >= self._stale_seconds
            self._count(dropped=1)
            self._last_drop_at = now
            if not starts:
                return Fed(shown_changed=False, transitions=())
            transitions = self._settle(now)
            return Fed(shown_changed=bool(transitions), transitions=transitions)
        # A silence nobody polled for is still an edge: say so before the datagram ends it.
        earlier = self._settle(now)
        before = self.view(now=now)
        self._last_arrival_at = now
        self._decode(datagram, now)
        transitions = (*earlier, *self._settle(now))
        return Fed(shown_changed=self.view(now=now) != before, transitions=transitions)

    def poll(self, *, now: float) -> tuple[Transition, ...]:
        """The edges that need no datagram: live to stale, a flood ending."""
        return self._settle(now)

    def fail(self, detail: str, *, now: float) -> tuple[Transition, ...]:
        """The receiver is not working. Sticky: nothing here clears it."""
        self._down = detail
        return self._settle(now)

    def forget_display(self) -> None:
        """After a caught exception: a fresh display, timing kept."""
        self._display = rtd.Display()

    def view(self, *, now: float) -> ClockView:
        status = self._status(now)
        live = status is Status.LIVE
        shown = self._display
        return ClockView(
            status=status,
            source=self._source,
            sources=self._recent(now),
            last_write_at=None if live else self._last_write_at,
            detail=self._down if status is Status.DOWN else None,
            clock=trimmed(shown.value(rtd.MAIN_CLOCK)) if live else None,
            running=running_of(shown.value(rtd.MAIN_CLOCK_STOPPED)) if live else None,
            at_zero=at_zero_of(shown.value(rtd.CLOCK_AT_ZERO)) if live else None,
            quarter=trimmed(shown.value(rtd.QUARTER)) if live else None,
            period=trimmed(shown.value(rtd.QUARTER_TEXT)) if live else None,
        )

    # -- internals --------------------------------------------------------

    def _count(self, **by: int) -> None:
        current = self._counts
        self._counts = Counts(
            datagrams=current.datagrams + by.get("datagrams", 0),
            writes=current.writes + by.get("writes", 0),
            faults=current.faults + by.get("faults", 0),
            oversize=current.oversize + by.get("oversize", 0),
            dropped=current.dropped + by.get("dropped", 0),
        )

    def _over_budget(self, now: float) -> bool:
        if now - self._window_start >= BUDGET_WINDOW_SECONDS or now < self._window_start:
            self._window_start = now
            self._window_decodes = 0
        if self._window_decodes >= self._decodes_per_second:
            return True
        self._window_decodes += 1
        return False

    def _decode(self, datagram: Datagram, now: float) -> None:
        wrote = False
        for found in rtd.parse_datagram(datagram.payload):
            if isinstance(found, rtd.FrameFault):
                self._count(faults=1)
                continue
            if not found.is_write:
                continue
            applied = self._display.apply(found)
            if isinstance(applied, rtd.FrameFault):
                self._count(faults=1)
                continue
            self._count(writes=1)
            wrote = True
        if wrote:
            self._last_write_at = now
            self._writers[datagram.source] = now

    def _recent(self, now: float) -> tuple[str, ...]:
        return tuple(sorted(s for s, at in self._writers.items() if now - at < self._stale_seconds))

    def _status(self, now: float) -> Status:
        return status_at(
            now=now,
            stale_seconds=self._stale_seconds,
            down=self._down is not None,
            last_drop_at=self._last_drop_at,
            recent_sources=len(self._recent(now)),
            last_write_at=self._last_write_at,
            last_arrival_at=self._last_arrival_at,
        )

    def _settle(self, now: float) -> tuple[Transition, ...]:
        """Report each edge once, and forget the sources that have gone."""
        recent = self._recent(now)
        self._writers = {s: at for s, at in self._writers.items() if s in recent}
        moved: SourceChanged | None = None
        if len(recent) == 1 and recent[0] != self._source:
            if self._source is not None:
                moved = SourceChanged(now, self._source, recent[0])
            self._source = recent[0]
        out: list[Transition] = []
        status = self._status(now)
        if status is not self._reported:
            out.append(StatusChanged(now, self._reported, self.view(now=now), self._counts))
            self._reported = status
        if moved is not None:
            out.append(moved)
        return tuple(out)
