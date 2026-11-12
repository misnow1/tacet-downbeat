"""Find the game's periods from the scoreboard's clock-at-zero flag and quarter field, offline (#212).

The pure core of `tacet-quarters`: RTD events in (from `tacet.rtd_replay`),
period spans out. No file, no socket and no clock beyond the capture's own, so
every rule here is tested with events built by hand.

A period ends when the board's clock reaches zero: the flag `z` rises. Game 4
showed that the quarter field follows 2 to 16 s later, so the flag is the
boundary and the field is the fallback. Each period starts where the one before
it ended, so the periods tile the game, and the first quarter and the third
start at the pregame countdown's zero and at halftime's.

Nothing here reads the main clock: its value slipped to 20:00 at Game 4 and
says nothing about where a period ends (design.md 8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import groupby, pairwise

from . import rtd
from .rtd_replay import Event, FieldEvent, GapEvent, SourceEvent
from .stoppages import count_of

#: The only fields read. The main clock is not among them.
WATCHED: tuple[rtd.Field, ...] = (rtd.CLOCK_AT_ZERO, rtd.QUARTER, rtd.QUARTER_TEXT)
#: What CLOCK_AT_ZERO holds at the end of a period and of the pregame countdown (Game 4).
AT_ZERO = "z"
#: The console rewrites the whole buffer about every 10 s, never more than 10.07 s at Game 4
#: (design.md 8), so a change an outage or a new source hid surfaces within this of the stream
#: coming back. Rounded up.
REWRITE_SECONDS = 11.0


class Period(StrEnum):
    PREGAME = "pregame"
    Q1 = "q1"
    Q2 = "q2"
    HALFTIME = "halftime"
    Q3 = "q3"
    Q4 = "q4"
    OTHER = "other"


QUARTER_NUMBERS: Mapping[int, Period] = {1: Period.Q1, 2: Period.Q2, 3: Period.Q3, 4: Period.Q4}
#: QUARTER_TEXT, stripped, when the quarter field is blank (Game 4).
BLANK_QUARTER_TEXTS: Mapping[str, Period] = {"PRE": Period.PREGAME, "HALF": Period.HALFTIME}
#: Game order. OTHER (overtime, FINAL, a typo) is outside it.
ORDER: tuple[Period, ...] = (Period.PREGAME, Period.Q1, Period.Q2, Period.HALFTIME, Period.Q3, Period.Q4)


class Edge(StrEnum):
    """What a boundary was read from."""

    CLOCK_AT_ZERO = "clock-at-zero"
    QUARTER_FIELD = "quarter"


class Doubt(StrEnum):
    """A reason the capture cannot vouch for a period's start or end."""

    #: The same strings as `stoppages.Doubt` for the shared two; a test pins that.
    INTERRUPTED = "interrupted"
    SOURCE_CHANGED = "source-changed"
    OUT_OF_ORDER = "out-of-order"
    REPEATED = "repeated"


@dataclass(frozen=True)
class Boundary:
    time: float
    edge: Edge
    doubts: tuple[Doubt, ...] = ()


@dataclass(frozen=True)
class PeriodSpan:
    period: Period
    #: QUARTER and QUARTER_TEXT as the board showed them, for the report.
    shown: str
    #: When the quarter field began showing it (the capture's first look, for the first).
    shown_from: float
    #: When the quarter field moved on; None if it never did in the capture.
    shown_to: float | None
    #: None: the capture began in it.
    start: Boundary | None
    #: None: the capture ended in it.
    end: Boundary | None
    #: Clock-at-zero rises while it was shown, in order.
    zeros: tuple[float, ...] = ()
    #: Doubts about the span as a whole (`repeated`).
    own_doubts: tuple[Doubt, ...] = ()

    @property
    def doubts(self) -> tuple[Doubt, ...]:
        found = set(self.own_doubts)
        for boundary in (self.start, self.end):
            if boundary is not None:
                found.update(boundary.doubts)
        return tuple(sorted(found))

    @property
    def trusted(self) -> bool:
        return self.start is not None and self.end is not None and not self.doubts


@dataclass(frozen=True)
class Board:
    #: Every showing in order, PREGAME and OTHER included.
    spans: tuple[PeriodSpan, ...]
    #: Every rise seen.
    zeros: tuple[float, ...]


def period_of(quarter: str | None, text: str | None) -> Period | None:
    """The period the two fields name together, or None while either was never written."""
    if quarter is None or text is None:
        return None
    number = count_of(quarter)
    if number is not None:
        return QUARTER_NUMBERS.get(number, Period.OTHER)
    if not quarter.strip():
        return BLANK_QUARTER_TEXTS.get(text.strip(), Period.OTHER)
    return Period.OTHER


def is_rise(old: str | None, new: str) -> bool:
    """The flag went up. A first write over a never-written field is not a rise."""
    return old is not None and old != AT_ZERO and new == AT_ZERO


def _window_doubts(lo: float, hi: float, gaps: Sequence[GapEvent], sources: Sequence[SourceEvent]) -> set[Doubt]:
    """What a gap or a new source in [lo - REWRITE_SECONDS, hi] could have hidden."""
    lo -= REWRITE_SECONDS
    found: set[Doubt] = set()
    if any(gap.since <= hi and gap.time >= lo for gap in gaps):
        found.add(Doubt.INTERRUPTED)
    if any(s.old is not None and lo <= s.time <= hi for s in sources):
        found.add(Doubt.SOURCE_CHANGED)
    return found


def _out_of_order(a: Period, b: Period) -> bool:
    return a in ORDER and b in ORDER and ORDER.index(b) != ORDER.index(a) + 1


@dataclass
class _Showing:
    period: Period
    shown: str
    since: float
    until: float | None = None
    zeros: list[float] = field(default_factory=list)


class _Board:
    """The quarter fields and the flag, fed one datagram's changes at a time."""

    def __init__(self) -> None:
        self.showings: list[_Showing] = []
        self.zeros: list[float] = []
        self._quarter: str | None = None
        self._text: str | None = None
        self._rises: list[float] = []

    def change(self, time: float, change: rtd.FieldChange) -> None:
        if change.field == rtd.QUARTER:
            self._quarter = change.new
        elif change.field == rtd.QUARTER_TEXT:
            self._text = change.new
        elif change.field == rtd.CLOCK_AT_ZERO and is_rise(change.old, change.new):
            self._rises.append(time)

    def settle(self, time: float) -> None:
        """The datagram is done: its rises belong to the showing that was open before it."""
        if self.showings and self._rises:
            self.showings[-1].zeros.extend(self._rises)
        self.zeros.extend(self._rises)
        self._rises = []
        now = period_of(self._quarter, self._text)
        if now is None or (self.showings and self.showings[-1].period is now):
            return
        if self.showings:
            self.showings[-1].until = time
        shown = f"{self._quarter}{self._text}"
        self.showings.append(_Showing(now, shown, time))


def _between(a: _Showing, b: _Showing, gaps: Sequence[GapEvent], sources: Sequence[SourceEvent]) -> Boundary:
    """Where showing `a` ended and `b` began."""
    doubts: set[Doubt]
    if a.zeros:
        time, edge = a.zeros[-1], Edge.CLOCK_AT_ZERO
        doubts = _window_doubts(time, b.since, gaps, sources)
    else:
        time, edge = b.since, Edge.QUARTER_FIELD
        doubts = _window_doubts(a.since, b.since, gaps, sources)
    if _out_of_order(a.period, b.period):
        doubts.add(Doubt.OUT_OF_ORDER)
    return Boundary(time, edge, tuple(sorted(doubts)))


def _last_end(last: _Showing, gaps: Sequence[GapEvent], sources: Sequence[SourceEvent]) -> Boundary | None:
    if not last.zeros:
        return None
    time = last.zeros[-1]
    return Boundary(time, Edge.CLOCK_AT_ZERO, tuple(sorted(_window_doubts(time, time, gaps, sources))))


def _spans(
    showings: Sequence[_Showing], gaps: Sequence[GapEvent], sources: Sequence[SourceEvent]
) -> tuple[PeriodSpan, ...]:
    ends: list[Boundary | None] = [_between(a, b, gaps, sources) for a, b in pairwise(showings)]
    if showings:
        ends.append(_last_end(showings[-1], gaps, sources))
    starts: list[Boundary | None] = [None, *ends[:-1]]
    shown_count = {p: sum(1 for s in showings if s.period is p) for p in ORDER}
    return tuple(
        PeriodSpan(
            s.period,
            s.shown,
            s.since,
            s.until,
            starts[i],
            ends[i],
            tuple(s.zeros),
            (Doubt.REPEATED,) if shown_count.get(s.period, 0) > 1 else (),
        )
        for i, s in enumerate(showings)
    )


def scan(events: Iterable[Event]) -> Board:
    board = _Board()
    gaps: list[GapEvent] = []
    sources: list[SourceEvent] = []
    watched: list[FieldEvent] = []
    for event in events:
        match event:
            case GapEvent():
                gaps.append(event)
            case SourceEvent():
                sources.append(event)
            case FieldEvent() if event.change.field in WATCHED:
                watched.append(event)
            case _:
                pass
    for time, group in groupby(watched, key=lambda e: e.time):
        for item in group:
            board.change(time, item.change)
        board.settle(time)
    return Board(_spans(board.showings, gaps, sources), tuple(board.zeros))
