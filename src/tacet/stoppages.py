"""Find the stoppages in a game from the scoreboard's play clock, offline (#205).

The pure core of `tacet-timeouts`: RTD events in (from `tacet.rtd_replay`),
stoppages out. No file, no socket and no clock beyond the capture's own, so
every rule here is tested with events built by hand.

A stoppage is the time the play clock is not counting down. Game 4 showed that
this is not one value: an injury timeout reads as the clock reset to 40 and
held, a team timeout as 40 held and then 25 held, and the away timeout and
halftime as a countdown that froze at 25 by ticking 26 -> 25 with no reset. So
any value counts, and what ends the hold is the same everywhere: the clock
counting down again, which is ready-for-play.

After every play the board resets to 40 and waits, up to 14.5 s in game 4,
before it ticks. That is ordinary and is shorter than `MIN_HOLD_SECONDS`.

Nothing here reads the main clock: it stops for every incomplete pass, so it
says nothing about a timeout.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from . import rtd
from .rtd_replay import Event, FieldEvent, GapEvent, SourceEvent

#: Game 4's longest ordinary post-play hold was 14.5 s; its shortest admin holds were 18.5 and
#: 19.1 s (both untapped); the shortest hold under a tapped timeout was 40 s.
MIN_HOLD_SECONDS = 20.0
#: Game 4's pregame hold ran 3856 s and halftime 1235 s; its longest tapped stoppage was 241 s.
#: A longer hold is reported but never used to correct an end.
LONGEST_STOPPAGE_SECONDS = 600.0
#: The play clock counts down one second at a time.
COUNTDOWN_STEP = 1


class Team(StrEnum):
    """RTD's names for the two sides; the log calls the guest team `away`."""

    HOME = "home"
    GUEST = "guest"


class Doubt(StrEnum):
    """A reason the capture cannot vouch for a stoppage's start or end."""

    #: The first change of the hold had no earlier value: the capture began inside it.
    CAPTURE_START = "capture-start"
    #: The sending address changed during the hold.
    SOURCE_CHANGED = "source-changed"
    #: A gap in the capture touches the hold, so it may have begun or ended unseen.
    INTERRUPTED = "interrupted"
    TOO_LONG = "too-long"


@dataclass(frozen=True)
class TeamTimeout:
    """A team's timeouts-left value went down."""

    time: float
    team: Team


@dataclass(frozen=True)
class Stoppage:
    start: float
    #: The first tick of the countdown: ready-for-play, at most one tick late.
    end: float
    doubts: tuple[Doubt, ...] = ()
    #: Team timeouts that fell inside the hold, in time order.
    marks: tuple[TeamTimeout, ...] = ()

    @property
    def held(self) -> float:
        return self.end - self.start

    @property
    def trusted(self) -> bool:
        return not self.doubts


@dataclass(frozen=True)
class Scan:
    stoppages: tuple[Stoppage, ...]
    #: Every team timeout seen, attached to a stoppage or not.
    team_timeouts: tuple[TeamTimeout, ...]
    #: When a hold that was still going at the end of the capture began, if one was.
    open_hold_since: float | None


def play_clock_seconds(raw: str | None) -> int | None:
    """The play clock's number, or None for a blank or anything that is not a count."""
    if raw is None:
        return None
    text = raw.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _is_tick(old: str | None, new: str) -> bool:
    before, after = play_clock_seconds(old), play_clock_seconds(new)
    return before is not None and after is not None and after == before - COUNTDOWN_STEP


def _team_of(change: rtd.FieldChange) -> Team | None:
    """Which team's timeouts-left this changes, if it went down between two numbers."""
    teams = {rtd.HOME_TIMEOUTS: Team.HOME, rtd.GUEST_TIMEOUTS: Team.GUEST}
    team = teams.get(change.field)
    before, after = play_clock_seconds(change.old), play_clock_seconds(change.new)
    if team is None or before is None or after is None or after >= before:
        return None
    return team


@dataclass(frozen=True)
class _Hold:
    start: float
    end: float
    from_null: bool


class _Holds:
    """The hold rule, fed one play clock change at a time.

    `last_tick` is when the clock last counted down. `first_still` is the first
    change since that was not a count: the end-of-play reset or an admin set. A
    hold starts there when there is one, so a play's own frozen time is never
    counted, and at the last tick when there is not (a countdown that froze by
    ticking 26 -> 25).
    """

    def __init__(self, min_hold: float) -> None:
        self._min_hold = min_hold
        self._last_tick: float | None = None
        self._first_still: float | None = None
        self._from_null = False

    def change(self, time: float, old: str | None, new: str) -> _Hold | None:
        if not _is_tick(old, new):
            if self._first_still is None:
                self._first_still, self._from_null = time, old is None
            return None
        found = self._closed_by(time)
        self._last_tick, self._first_still, self._from_null = time, None, False
        return found

    def _closed_by(self, time: float) -> _Hold | None:
        start = self._first_still if self._first_still is not None else self._last_tick
        if start is None or time - start < self._min_hold:
            return None
        return _Hold(start, time, self._from_null and self._first_still is not None)

    def open_since(self, latest: float) -> float | None:
        start = self._first_still if self._first_still is not None else self._last_tick
        return start if start is not None and latest - start >= self._min_hold else None


def _doubts(
    hold: _Hold,
    gaps: list[GapEvent],
    changes: list[SourceEvent],
    longest: float,
) -> tuple[Doubt, ...]:
    found: set[Doubt] = set()
    if hold.from_null:
        found.add(Doubt.CAPTURE_START)
    if any(hold.start <= source.time <= hold.end and source.old is not None for source in changes):
        found.add(Doubt.SOURCE_CHANGED)
    # Inclusive: a hold whose first change is the first after a gap may have begun unseen.
    if any(gap.since <= hold.end and gap.time >= hold.start for gap in gaps):
        found.add(Doubt.INTERRUPTED)
    if hold.end - hold.start > longest:
        found.add(Doubt.TOO_LONG)
    return tuple(sorted(found))


def scan(
    events: Iterable[Event],
    *,
    min_hold: float = MIN_HOLD_SECONDS,
    longest: float = LONGEST_STOPPAGE_SECONDS,
) -> Scan:
    holds = _Holds(min_hold)
    found: list[_Hold] = []
    gaps: list[GapEvent] = []
    sources: list[SourceEvent] = []
    marks: list[TeamTimeout] = []
    latest = 0.0
    for event in events:
        latest = max(latest, event.time)
        match event:
            case GapEvent():
                gaps.append(event)
            case SourceEvent():
                sources.append(event)
            case FieldEvent() if event.change.field == rtd.PLAY_CLOCK:
                hold = holds.change(event.time, event.change.old, event.change.new)
                if hold is not None:
                    found.append(hold)
            case FieldEvent():
                team = _team_of(event.change)
                if team is not None:
                    marks.append(TeamTimeout(event.time, team))
    stoppages = tuple(
        Stoppage(
            hold.start,
            hold.end,
            _doubts(hold, gaps, sources, longest),
            tuple(m for m in marks if hold.start <= m.time <= hold.end),
        )
        for hold in found
    )
    return Scan(stoppages, tuple(marks), holds.open_since(latest))
