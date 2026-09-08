"""The annotation log: an append-only record of what happened during a game.

This log is the source of truth (design.md 5.9). Reaper markers are a derived
view of it and can be regenerated from it at any time, given the recording
anchor. If Reaper crashes or the mirroring script is not loaded, the convenient
view is lost and the data is not.

Most band state cannot be reconstructed afterwards (design.md 5.6). It is not in
the multitrack, not in RTD and not in the fader moves, so a game that is not
annotated live is permanently unlabelled. That is why every entry is flushed and
fsynced as it is written rather than buffered: an entry that is not on disk is
an entry lost.

Times are recorded twice. `wall` is for humans and survives a restart;
`monotonic` is what offsets are computed from, because an NTP correction
mid-game would otherwise corrupt every interval that spans it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

SCHEMA_VERSION = 1

#: Marker names are `CATEGORY|key`, so reading them back later is a split rather
#: than a regex (design.md 5.9).
MARKER_DELIMITER = "|"

PHASE_START = "start"
PHASE_END = "end"

_ENCODING = "utf-8"
_LINE_TERMINATOR = "\n"


class AnnotationError(RuntimeError):
    """Base for every fault this module raises."""


class UnknownEventError(AnnotationError):
    """An event key that is not in the vocabulary."""


class EventKindError(AnnotationError):
    """An instant event used as a span, or a span used as an instant."""


class UnknownSpanError(AnnotationError):
    """A span id that was never opened, or has already been closed."""


class CorruptLogError(AnnotationError):
    """A log line that cannot be read. Never skipped silently: see CLAUDE.md on
    failing visibly."""


class Category(StrEnum):
    SESSION = "SYS"
    BAND = "BAND"
    GAME = "GAME"
    FADER = "FDR"
    DETECTOR = "DET"
    NOTE = "NOTE"


class Kind(StrEnum):
    #: A point in time. Becomes a Reaper marker.
    INSTANT = "instant"
    #: An interval. Becomes a Reaper region, from its start entry to its end.
    SPAN = "span"


class Action(StrEnum):
    """A fader move a button makes as well as recording why it was made.

    Kept here rather than in `state` so the vocabulary stays a vocabulary: this
    records what the operator meant, and `tacet.app` decides what the machine
    does about it. Nothing here moves a fader on its own - these fire only when
    an operator taps them, which is what CLAUDE.md permits in Phase 0.
    """

    OPEN = "open"
    RELEASE = "release"


@dataclass(frozen=True)
class EventType:
    key: str
    label: str
    category: Category
    kind: Kind = Kind.INSTANT
    #: False for events the box writes itself. The operator has a handful of
    #: seconds and a small screen; only what a human taps belongs on it.
    button: bool = True
    #: Set when tapping this both moves the fader and says why. None means the
    #: event only records.
    action: Action | None = None


def _instant(
    key: str,
    label: str,
    category: Category,
    *,
    button: bool = True,
    action: Action | None = None,
) -> EventType:
    return EventType(key, label, category, Kind.INSTANT, button=button, action=action)


def _span(key: str, label: str, category: Category, *, button: bool = True) -> EventType:
    return EventType(key, label, category, Kind.SPAN, button=button)


#: The event vocabulary. Extend it freely - unknown-unknowns are the point
#: (design.md 5.6). Keys are stable and machine-facing; labels are what the
#: operator sees on a button and may be reworded without breaking old logs.
VOCABULARY: tuple[EventType, ...] = (
    # Session. Written by the box, not tapped by anyone.
    _instant("recording-started", "Recording started", Category.SESSION, button=False),
    _instant("recording-stopped", "Recording stopped", Category.SESSION, button=False),
    _instant("armed", "Armed", Category.SESSION, button=False),
    _instant("stood-down", "Stood down", Category.SESSION, button=False),
    # Band, from design.md 5.6
    _instant("band-enters-stadium", "Band enters stadium", Category.BAND),
    _instant("band-enters-stands", "Band enters stands", Category.BAND),
    _instant("band-exits-stands", "Band exits stands", Category.BAND),
    _instant("band-returns-to-stands", "Band returns to stands", Category.BAND),
    _instant("band-exits-stadium", "Band exits stadium", Category.BAND),
    _instant("other-band-on-field", "Other band on field", Category.BAND),
    _instant("other-band-off-field", "Other band off field", Category.BAND),
    _instant("drumline-cadence", "Drumline cadence started", Category.BAND),
    _instant("touchdown-sequence", "Touchdown sequence started", Category.BAND),
    _span("band-in-stands", "Band in stands", Category.BAND),
    # Game timing. Stands in for RTD until it exists (design.md 5.4).
    _span("q1", "Q1", Category.GAME),
    _span("q2", "Q2", Category.GAME),
    _span("q3", "Q3", Category.GAME),
    _span("q4", "Q4", Category.GAME),
    _span("halftime", "Halftime", Category.GAME),
    _span("halftime-exodus", "Halftime exodus", Category.GAME),
    _span("last-two-minutes", "Last two minutes", Category.GAME),
    # Timeouts are not one thing. The band plays through most of them, but not
    # an injury timeout (design.md 2, "Practical reading"), so a log that calls
    # them all "timeout" cannot answer the one question that matters when
    # tuning: was the band meant to be playing here. Whose timeout it is also
    # decides which band is playing at all - see other-band-on-field.
    _span("timeout-home", "Timeout: home", Category.GAME),
    _span("timeout-away", "Timeout: away", Category.GAME),
    _span("timeout-media", "Timeout: media", Category.GAME),
    _span("timeout-official", "Timeout: officials", Category.GAME),
    _span("timeout-injury", "Timeout: injury", Category.GAME),
    # Kept, and deliberately last. The operator is watching a field, and an
    # unclassified timeout beats one that went unmarked while they decided.
    # Also the key old logs already carry.
    _span("timeout", "Timeout: unspecified", Category.GAME),
    # Fader moves. `commanded` is written by the box itself; the rest both move
    # the fader and say why, in one tap.
    #
    # Splitting those cost two taps for one event, and the reason is the half
    # that cannot be recovered afterwards: design.md 9 gets the moves themselves
    # back from the post-DCA reference channel, but nothing gets back why. A
    # reason that depends on the operator finding a second button is a reason
    # that goes missing on a busy night, and Phase 1 exists to collect it.
    _instant("commanded", "Fader commanded", Category.FADER, button=False),
    _instant("up-whistle", "Up on whistle", Category.FADER, action=Action.OPEN),
    _instant("up-drums", "Up on drums", Category.FADER, action=Action.OPEN),
    _instant("up-slow", "Up slow, missed the start", Category.FADER, action=Action.OPEN),
    _instant("out", "Faded out", Category.FADER, action=Action.RELEASE),
    # Detector was wrong. Useful long before a detector exists, because the
    # operator can mark what one would have got wrong.
    _instant("false-open", "Detector wrong: false open", Category.DETECTOR),
    _instant("missed-entrance", "Detector wrong: missed entrance", Category.DETECTOR),
    # Free text. Two taps is the budget; typing is for when it matters.
    _instant("note", "Note", Category.NOTE),
)

EVENTS: Mapping[str, EventType] = {event.key: event for event in VOCABULARY}

#: What the operator UI offers. Two taps is the budget (design.md 5.6).
BUTTONS: tuple[EventType, ...] = tuple(e for e in VOCABULARY if e.button)


def lookup(key: str) -> EventType:
    try:
        return EVENTS[key]
    except KeyError:
        raise UnknownEventError(f"no such event: {key!r}") from None


class EntrySink(Protocol):
    """Anything that wants a copy of each entry as it is written, such as the
    Reaper mirror queue."""

    def append(self, entry: Entry) -> None: ...


class Clock(Protocol):
    """Injected so tests are deterministic and so replay can supply its own."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """The real clock. `monotonic` is immune to NTP steps; `now` is not, which
    is why offsets are never computed from it."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class Entry:
    seq: int
    event: str
    category: str
    kind: str
    label: str
    wall: str
    monotonic: float
    phase: str | None = None
    span_id: str | None = None
    project_seconds: float | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    v: int = SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        seq: int,
        event: EventType,
        clock: Clock,
        *,
        data: Mapping[str, Any] | None = None,
        phase: str | None = None,
        span_id: str | None = None,
        project_seconds: float | None = None,
    ) -> Self:
        return cls(
            seq=seq,
            event=event.key,
            category=event.category.value,
            kind=event.kind.value,
            label=event.label,
            wall=clock.now().isoformat(),
            monotonic=clock.monotonic(),
            phase=phase,
            span_id=span_id,
            project_seconds=project_seconds,
            data=dict(data or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event": self.event,
            "category": self.category,
            "kind": self.kind,
            "label": self.label,
            "wall": self.wall,
            "monotonic": self.monotonic,
            "phase": self.phase,
            "span_id": self.span_id,
            "project_seconds": self.project_seconds,
            "data": dict(self.data),
            "v": self.v,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> Self:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorruptLogError(f"line is not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise CorruptLogError(f"line is not an object: {line[:60]!r}")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise CorruptLogError(f"line is not an entry: {exc}") from exc


def marker_name(entry: Entry) -> str:
    """The Reaper marker or region name for an entry."""
    return f"{entry.category}{MARKER_DELIMITER}{entry.event}"


def project_seconds(entry: Entry, anchor: Entry) -> float:
    """Seconds from the recording anchor to `entry`.

    Computed from the monotonic reading, so a wall-clock step cannot corrupt it.
    """
    return entry.monotonic - anchor.monotonic


def span_id_for(event_key: str, seq: int) -> str:
    """Deterministic, so replaying a log reproduces the same regions."""
    return f"{event_key}-{seq}"


class AnnotationLog:
    """Append-only JSONL. One entry per line, flushed and fsynced as written."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        fsync: bool = True,
        mirror: EntrySink | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._fsync = fsync
        self._mirror = mirror
        self._handle: Any = None
        self._seq = 0
        self._open_spans: dict[str, Entry] = {}

    # -- lifecycle --------------------------------------------------------

    def open(self) -> Self:
        self._resume()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding=_ENCODING, newline=_LINE_TERMINATOR)
        return self

    def _resume(self) -> None:
        """Pick up sequence numbering and open spans from an existing log."""
        if not self.path.exists():
            return
        for entry in read_entries(self.path):
            self._seq = max(self._seq, entry.seq)
            if entry.span_id is None:
                continue
            if entry.phase == PHASE_START:
                self._open_spans[entry.span_id] = entry
            elif entry.phase == PHASE_END:
                self._open_spans.pop(entry.span_id, None)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- writing ----------------------------------------------------------

    def record(
        self,
        event_key: str,
        *,
        data: Mapping[str, Any] | None = None,
        project_seconds: float | None = None,
    ) -> Entry:
        """Append one instant event."""
        event = lookup(event_key)
        if event.kind is Kind.SPAN:
            raise EventKindError(f"{event_key!r} is a span; use start_span and end_span")
        return self._append(event, data=data, project_seconds=project_seconds)

    def start_span(
        self,
        event_key: str,
        *,
        data: Mapping[str, Any] | None = None,
        project_seconds: float | None = None,
    ) -> str:
        """Open a span. Returns its id, which `end_span` needs."""
        event = lookup(event_key)
        if event.kind is not Kind.SPAN:
            raise EventKindError(f"{event_key!r} is an instant; use record")
        span_id = span_id_for(event.key, self._seq + 1)
        entry = self._append(
            event,
            data=data,
            phase=PHASE_START,
            span_id=span_id,
            project_seconds=project_seconds,
        )
        self._open_spans[span_id] = entry
        return span_id

    def end_span(
        self,
        span_id: str,
        *,
        data: Mapping[str, Any] | None = None,
        project_seconds: float | None = None,
    ) -> Entry:
        start = self._open_spans.pop(span_id, None)
        if start is None:
            raise UnknownSpanError(f"no open span with id {span_id!r}")
        return self._append(
            lookup(start.event),
            data=data,
            phase=PHASE_END,
            span_id=span_id,
            project_seconds=project_seconds,
        )

    def open_spans(self) -> tuple[str, ...]:
        """Span ids still awaiting an end. A game may legitimately finish with
        some open; the mirror runs them to the end of the timeline."""
        return tuple(self._open_spans)

    def _append(
        self,
        event: EventType,
        *,
        data: Mapping[str, Any] | None = None,
        phase: str | None = None,
        span_id: str | None = None,
        project_seconds: float | None = None,
    ) -> Entry:
        if self._handle is None:
            raise AnnotationError("log is not open")
        self._seq += 1
        entry = Entry.build(
            self._seq,
            event,
            self._clock,
            data=data,
            phase=phase,
            span_id=span_id,
            project_seconds=project_seconds,
        )
        self._handle.write(entry.to_json() + _LINE_TERMINATOR)
        self._handle.flush()
        if self._fsync:
            os.fsync(self._handle.fileno())
        # Mirrored from the one place an entry is written, so the two files
        # cannot drift. The mirror is regenerable if it is lost.
        if self._mirror is not None:
            self._mirror.append(entry)
        return entry


def read_entries(path: Path | str) -> Iterator[Entry]:
    """Read a log back.

    A malformed line raises `CorruptLogError`, because dropping entries quietly is
    worse than stopping. The one exception is an unterminated final line, which
    is the ordinary result of losing power mid-write: that costs the last entry
    and nothing else.
    """
    path = Path(path)
    with path.open(encoding=_ENCODING) as handle:
        pending: str | None = None
        pending_terminated = False
        for raw in handle:
            terminated = raw.endswith(_LINE_TERMINATOR)
            line = raw.strip()
            if not line:
                continue
            if pending is not None:
                yield Entry.from_json(pending)
            pending, pending_terminated = line, terminated
        if pending is None:
            return
        if pending_terminated:
            yield Entry.from_json(pending)
            return
        try:
            entry = Entry.from_json(pending)
        except CorruptLogError:
            return  # torn final write
        yield entry
