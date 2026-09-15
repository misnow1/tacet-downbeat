"""The annotation log: an append-only record of what happened during a game.

This log is the source of truth (design.md 5.9). Reaper markers are a derived
view of it and can be regenerated from it at any time, given the recording
anchor. If Reaper crashes or the mirroring script is not loaded, the convenient
view is lost and the data is not.

Most band state cannot be reconstructed afterwards (design.md 5.6). It is not in
the multitrack, not in RTD and not in the fader moves, so a game that is not
annotated live is permanently unlabelled. That is why every entry is written,
flushed and fsynced as soon as it can be.

As soon as it can be, not before the tap is answered. Writing happens on one
thread of its own (#41): a stalled disk must never stall the event loop that
moves the fader, and a missed downbeat costs far more than the last few entries
lost with a process that died. Sequence numbers and spans are decided on the
loop; the thread only writes, and says afterwards what did not save.

Times are recorded twice. `wall` is for humans and survives a restart;
`monotonic` is what offsets are computed from, because an NTP correction
mid-game would otherwise corrupt every interval that spans it.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import json
import math
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

#: The schema this build writes, as `v` on every line.
#:
#: A log is read for years after the game that wrote it, by tools that did not
#: exist yet, so the schema changes deliberately or not at all. A new fact about
#: an entry goes under `data`, which every reader of every version carries
#: through untouched. A change to the top-level fields - a new one, a rename, a
#: changed type - is a new version: bump this, add a reader for the new shape
#: beside the old one in `_READERS`, and check in a real log written under it
#: as `tests/fixtures/log-v<n>.jsonl`. `tests/test_game_2_log.py` holds this
#: build to every fixture there.
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


class NotAButtonError(AnnotationError):
    """An event only the box writes, asked for from outside it."""


class DataError(AnnotationError):
    """Data attached to an operator's entry that the log will not hold."""


class WriteError(AnnotationError):
    """A line that did not reach the disk: a full disk, a mount that dropped, a
    permission. Raised, never swallowed, and counted on the writer's
    `WriteHealth` so the page can say so."""


class CorruptLogError(AnnotationError):
    """A log line that cannot be read. Never skipped silently: see CLAUDE.md on
    failing visibly."""


class SchemaVersionError(CorruptLogError):
    """A line written under a schema this build does not read: by a newer tacet,
    typically, read by an older one after a rollback. A kind of unreadable line,
    so everything that stops on a corrupt log stops on this too, but it says
    which version it found."""


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
    #: Open, but ridden up rather than snapped. The operator missed the first
    #: phrase and is disguising the late entry, which is existing practice the
    #: box is meant to reproduce (design.md section 4). The same state
    #: transition as OPEN - only the gesture differs, which is why this lives
    #: here and not in `state`.
    OPEN_SLOW = "open-slow"
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


#: The entry every other position in the log is measured from.
#:
#: Named once, here, because three things now depend on it meaning the same
#: string: the box writes it, `tacet.markers` anchors the whole timeline to the
#: first one, and the box warns at startup when a log already contains one. A
#: second copy of this string that drifted would break the warning silently,
#: which is the exact failure the warning exists to prevent.
ANCHOR_EVENT = "recording-started"

#: The event vocabulary. Extend it freely - unknown-unknowns are the point
#: (design.md 5.6). Keys are stable and machine-facing; labels are what the
#: operator sees on a button and may be reworded without breaking old logs.
VOCABULARY: tuple[EventType, ...] = (
    # Session. Written by the box, not tapped by anyone.
    _instant(ANCHOR_EVENT, "Recording started", Category.SESSION, button=False),
    _instant("recording-stopped", "Recording stopped", Category.SESSION, button=False),
    _instant("armed", "Armed", Category.SESSION, button=False),
    _instant("stood-down", "Stood down", Category.SESSION, button=False),
    _instant("stand-down-requested", "Stand down requested", Category.SESSION, button=False),
    _instant("stand-down-cancelled", "Stand down cancelled", Category.SESSION, button=False),
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
    # Written by the box when a send fails partway through a move, so a
    # `commanded` entry is never the only word on a move that did not happen.
    _instant("move-failed", "Fader move failed", Category.FADER, button=False),
    # Written by the box when a fade or ride-in sends its last step. Those run
    # on after their `commanded` entry, whose `delivered` is therefore null.
    _instant("move-landed", "Fader move landed", Category.FADER, button=False),
    # Written by the box when a fader tap arrived too late to execute (#16). The
    # fader buttons in the grid log their own entry instead, marked stale.
    _instant("stale-tap", "Fader tap arrived late, not executed", Category.FADER, button=False),
    _instant("up-whistle", "Up on whistle", Category.FADER, action=Action.OPEN),
    _instant("up-drums", "Up on drums", Category.FADER, action=Action.OPEN),
    _instant("up-slow", "Up slow, missed the start", Category.FADER, action=Action.OPEN_SLOW),
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


#: The longest string an operator's entry may carry. A note is typed with a
#: thumb in a press box; this is generous for that and small next to a log that
#: fsyncs every line.
MAX_DATA_TEXT = 1000


def operator_event(key: str) -> EventType:
    """An event an operator may record, or raise.

    Box-only events describe what the box did, and the markers depend on them
    meaning exactly that: a posted `recording-started` would become the anchor
    the whole timeline is measured from (#36).
    """
    event = lookup(key)
    if not event.button:
        raise NotAButtonError(f"{key!r} is written by the box, not by an operator")
    return event


def operator_data(data: object) -> dict[str, Any]:
    """Check data an operator attached to an entry, and return a copy of it.

    A flat JSON object of strings, finite numbers, booleans and nulls. Checked
    before anything acts on the tap: data that failed inside the log used to do
    so after a fader button had already moved the fader, losing the reason.
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DataError(f"data must be an object, not {type(data).__name__}")
    for key, value in data.items():
        if not isinstance(key, str):
            raise DataError(f"data keys must be strings, not {key!r}")
        _check_value(key, value)
    return dict(data)


def _check_value(key: str, value: object) -> None:
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataError(f"data {key!r} is {value}, which JSON cannot carry")
        return
    if isinstance(value, str):
        if len(value) > MAX_DATA_TEXT:
            raise DataError(f"data {key!r} is {len(value)} characters; the most is {MAX_DATA_TEXT}")
        return
    raise DataError(f"data {key!r} must be a string, number, true, false or null")


class EntrySink(Protocol):
    """Anything that wants a copy of each entry once it is saved, such as the
    Reaper mirror queue. Called on the log's writer thread, never the loop.

    `write` lands one entry without syncing it; `sync` then covers every entry
    written since the last, so a burst of taps costs one fsync."""

    def write(self, entry: Entry) -> None: ...

    def sync(self) -> None: ...


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
        # No NaN or Infinity: Python would write them, and no other JSON reader
        # of the log - Lua, jq, a browser - would read the line back.
        return json.dumps(self.as_dict(), separators=(",", ":"), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, line: str) -> Self:
        """Read one line, or raise `CorruptLogError` saying what is wrong with it.

        Dispatched on `v`. Every field is type-checked here, so a line that
        reads is an entry the rest of the code can trust: a `"seq": "7"` that
        got through used to surface much later as a bare TypeError from `max()`.
        """
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorruptLogError(f"line is not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise CorruptLogError(f"line is not an object: {line[:60]!r}")
        if "v" not in raw:
            raise CorruptLogError("line has no schema version 'v'")
        version = raw["v"]
        if not _is_int(version):
            raise CorruptLogError(f"schema version must be a whole number, not {version!r}")
        reader = _READERS.get(version)
        if reader is None:
            readable = ", ".join(f"v{v}" for v in READABLE_SCHEMAS)
            raise SchemaVersionError(
                f"line is schema v{version}, which this build cannot read (it reads {readable}); "
                "it was written by a different version of tacet"
            )
        return cls(**reader(raw))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_optional_number(value: object) -> bool:
    return value is None or _is_number(value)


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


#: Schema v1: each field, whether a line must carry it, and what it must be.
#: The optional ones have defaults on `Entry` and were always written anyway.
_V1_FIELDS: Mapping[str, tuple[bool, Callable[[object], bool], str]] = {
    "seq": (True, _is_int, "a whole number"),
    "event": (True, _is_str, "a string"),
    "category": (True, _is_str, "a string"),
    "kind": (True, _is_str, "a string"),
    "label": (True, _is_str, "a string"),
    "wall": (True, _is_str, "a string"),
    "monotonic": (True, _is_number, "a finite number"),
    "phase": (False, _is_optional_str, "a string or null"),
    "span_id": (False, _is_optional_str, "a string or null"),
    "project_seconds": (False, _is_optional_number, "a finite number or null"),
    "data": (False, _is_object, "an object"),
    "v": (True, _is_int, "a whole number"),
}


def _read_v1(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a v1 line, checked. v1 is closed: an unknown field is a
    line this reader does not understand, not one it may quietly drop."""
    unknown = sorted(set(raw) - set(_V1_FIELDS))
    if unknown:
        raise CorruptLogError(f"line has fields schema v1 does not: {', '.join(unknown)}")
    for name, (required, valid, expected) in _V1_FIELDS.items():
        if name not in raw:
            if required:
                raise CorruptLogError(f"line is missing {name!r}")
            continue
        if not valid(raw[name]):
            raise CorruptLogError(f"{name!r} must be {expected}, not {raw[name]!r}")
    return dict(raw)


#: A reader for every schema this build can read, by version. A reader returns
#: the keyword arguments for `Entry`, so a future one maps an older shape onto
#: the current fields.
_READERS: Mapping[int, Callable[[Mapping[str, Any]], dict[str, Any]]] = {1: _read_v1}

READABLE_SCHEMAS: tuple[int, ...] = tuple(sorted(_READERS))


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


# -- torn final writes ------------------------------------------------------

#: Appended to a file's name for the bytes cut from its torn end. Accumulates
#: across repairs: nothing a crash left behind is ever deleted.
TORN_SUFFIX = ".torn"

_TERMINATOR_BYTES = _LINE_TERMINATOR.encode(_ENCODING)


@dataclass(frozen=True)
class TornTail:
    """The bytes after a file's last line terminator.

    `offset` is where the complete lines end, which is where the file is cut.
    """

    offset: int
    tail: bytes

    def is_entry(self) -> bool:
        """Whether the tail is a whole entry that lost only its terminator."""
        try:
            Entry.from_json(self.tail.decode(_ENCODING))
        except SchemaVersionError:
            # A whole line, just not one this build reads. Refused rather than
            # judged a fragment and set aside from a log this build cannot read.
            raise
        except (CorruptLogError, UnicodeDecodeError):
            return False
        return True


@dataclass(frozen=True)
class Repair:
    """What opening a file did about a torn end. `set_aside` is None when the
    tail was kept, by giving it the terminator it was missing."""

    tail: bytes
    set_aside: Path | None


def torn_tail(data: bytes) -> TornTail | None:
    """The unterminated end of a file's contents, or None if it ends cleanly.

    Pure, and bytes rather than text: a write torn inside a multi-byte character
    has to be found before anything tries to decode it.
    """
    if not data or data.endswith(_TERMINATOR_BYTES):
        return None
    offset = data.rfind(_TERMINATOR_BYTES) + len(_TERMINATOR_BYTES)
    return TornTail(offset=offset, tail=data[offset:])


def find_torn_tail(path: Path | str) -> TornTail | None:
    """Look without touching. A missing file has nothing torn in it."""
    path = Path(path)
    if not path.exists():
        return None
    return torn_tail(path.read_bytes())


def set_aside_path(path: Path | str) -> Path:
    path = Path(path)
    return path.with_name(path.name + TORN_SUFFIX)


def repair_torn_tail(path: Path | str, *, keep_entries: bool, fsync: bool = True) -> Repair | None:
    """Make a file safe to append to again.

    Appending straight after an unterminated line glues the next line onto it,
    and the fragment is then in the middle of the file, where a reader has to
    treat it as corruption. So the tail is dealt with before anything is
    appended: kept, if `keep_entries` and it is a whole entry missing only its
    terminator; otherwise copied to the `.torn` file and cut off.
    """
    path = Path(path)
    torn = find_torn_tail(path)
    if torn is None:
        return None
    if keep_entries and torn.is_entry():
        _append_bytes(path, _TERMINATOR_BYTES, fsync=fsync)
        return Repair(tail=torn.tail, set_aside=None)
    aside = set_aside_path(path)
    # Copied before the cut, so a crash between the two leaves the bytes in both
    # places rather than neither.
    _append_bytes(aside, torn.tail + _TERMINATOR_BYTES, fsync=fsync)
    with path.open("r+b") as handle:
        handle.truncate(torn.offset)
        if fsync:
            os.fsync(handle.fileno())
    return Repair(tail=torn.tail, set_aside=aside)


def _append_bytes(path: Path, data: bytes, *, fsync: bool) -> None:
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())


class LineHandle(Protocol):
    """The part of an open text file that appending lines needs."""

    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...

    def fileno(self) -> int: ...

    def close(self) -> None: ...


Opener = Callable[[Path], LineHandle]
#: Makes a file's written data durable. Injected so a test can make it slow.
Syncer = Callable[[int], None]


def open_for_append(path: Path) -> LineHandle:
    return path.open("a", encoding=_ENCODING, newline=_LINE_TERMINATOR)


@dataclass
class WriteHealth:
    """How a file's writes are going.

    `error` is why the last write failed, and clears when one succeeds.
    `failures` counts every failed write this run and never clears: the
    entries those cost are gone whether or not the disk has recovered since.
    """

    error: str | None = None
    failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.error is None

    def failed(self, error: str) -> None:
        self.error = error
        self.failures += 1

    def succeeded(self) -> None:
        self.error = None


class AppendFile:
    """Lines appended to a file, flushed as written and fsynced on request, that
    survives a write failing partway.

    A failed write can leave part of a line on disk with the handle still open,
    so the next line would be glued onto the fragment within the same run - the
    in-run version of the torn end `repair_torn_tail` deals with at open (#26).
    So a failure abandons the handle, and the next append repairs the end of the
    file and reopens it before writing. The repair then keeps nothing
    unterminated, since the caller was told that write failed; a write whose
    line landed whole and only its fsync failed stays, because it is complete.

    A file that has gone away is not recreated. An empty file where the game's
    log was - on a mount that dropped, say - would take the rest of the night's
    entries somewhere nobody would look, and look healthy doing it.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        keep_entries: bool,
        fsync: bool = True,
        opener: Opener = open_for_append,
        sync: Syncer = os.fsync,
    ) -> None:
        self.path = Path(path)
        self._keep_entries = keep_entries
        self._fsync = fsync
        self._opener = opener
        self._sync = sync
        self._handle: LineHandle | None = None
        #: Opened, then a write failed. Still open as far as callers are
        #: concerned; the handle is reopened by the next append.
        self._broken = False

    @property
    def is_open(self) -> bool:
        return self._handle is not None or self._broken

    def open(self) -> Repair | None:
        """Repair a torn end left by a previous run, then open for appending."""
        repair = repair_torn_tail(self.path, keep_entries=self._keep_entries, fsync=self._fsync)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._opener(self.path)
        self._broken = False
        return repair

    def close(self) -> None:
        handle, self._handle = self._handle, None
        self._broken = False
        if handle is not None:
            handle.close()

    def append_line(self, line: str) -> None:
        """Write one line and its terminator and sync it, or raise `WriteError`."""
        self.write_line(line)
        self.sync()

    def write_line(self, line: str) -> None:
        """Write and flush one line and its terminator, or raise `WriteError`.
        Not durable until `sync`."""
        if not self.is_open:
            raise AnnotationError(f"{self.path.name} is not open")
        try:
            handle = self._handle if self._handle is not None else self._reopen()
            handle.write(line + _LINE_TERMINATOR)
            handle.flush()
        except OSError as exc:
            self.abandon()
            raise WriteError(f"could not write {self.path.name}: {exc.strerror or exc}") from exc

    def sync(self) -> None:
        """Make every line written since the last sync durable, or raise
        `WriteError`. A file whose last write failed has nothing to sync: that
        write abandoned its handle, and the next one reopens it."""
        if not self._fsync or self._handle is None:
            return
        try:
            self._sync(self._handle.fileno())
        except OSError as exc:
            self.abandon()
            raise WriteError(f"could not sync {self.path.name}: {exc.strerror or exc}") from exc

    def _reopen(self) -> LineHandle:
        if not self.path.exists():
            raise FileNotFoundError(errno.ENOENT, "it has gone away, and is not recreated", str(self.path))
        repair_torn_tail(self.path, keep_entries=False, fsync=self._fsync)
        self._handle = self._opener(self.path)
        self._broken = False
        return self._handle

    def abandon(self) -> None:
        """Give up on the handle after a failure; the next write reopens it."""
        handle, self._handle = self._handle, None
        self._broken = True
        if handle is not None:
            # Closing flushes whatever the failed write left buffered, which
            # fails the same way on a full disk. The repair on reopen is what
            # deals with anything it does manage to land.
            with contextlib.suppress(OSError):
                handle.close()


# -- the writer thread ------------------------------------------------------

#: The log's health once its writer thread has ended. Quoted on the page and in
#: docs/troubleshooting.md. It should never happen - the thread catches
#: everything it can - which is exactly why it must not happen quietly.
WRITER_STOPPED = "the log writer has stopped; nothing more will be saved until the box is restarted"

#: How long closing a log waits for the writer to finish what it was given. A
#: disk that has hung - a dropped NAS mount, say - must not hang shutdown too.
WRITER_STOP_SECONDS = 5.0

#: How often a wait on the writer checks that it is still alive, so a thread
#: that dies with the wait queued behind it cannot hang the waiter.
WRITER_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Written:
    """What became of one entry on the writer thread.

    `error` is None when the entry saved. `mirrored` says whether the mirror
    took its copy, and `mirror_error` why not; neither is set when there is no
    mirror, or when the entry did not save and so was never offered to it.
    """

    entry: Entry
    error: str | None = None
    mirrored: bool = False
    mirror_error: str | None = None


@dataclass(frozen=True)
class _Pending:
    entry: Entry
    line: str


@dataclass(frozen=True)
class _Mark:
    """Queued behind everything submitted so far; set once that is written."""

    done: threading.Event = field(default_factory=threading.Event)
    stop: bool = False


class LogWriter:
    """The one thread that touches the disk for a log and its mirror (#41).

    One queue, drained in order by one thread, so entries land in the order they
    were accepted, and each batch is written to the log and synced before any of
    it is offered to the mirror: the queue never gets ahead of the log. Every
    line queued while a sync was running is covered by the next one.

    The thread must not die while the process lives, because lines would pile
    up behind it unwritten while everything looked healthy. Every failure it can
    catch becomes a `Written` with an error, and the thread carries on. One it
    cannot catch ends it, and `alive` is how the log finds out and says so.
    """

    def __init__(
        self,
        file: AppendFile,
        mirror: EntrySink | None = None,
        *,
        on_written: Callable[[], None] | None = None,
    ) -> None:
        self._file = file
        self._mirror = mirror
        self._on_written = on_written
        self._queue: queue.SimpleQueue[_Pending | _Mark] = queue.SimpleQueue()
        self._outcomes: queue.SimpleQueue[Written] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=f"tacet-writer:{file.path.name}", daemon=True)
        #: Set by the thread as it ends, before it wakes anyone: a woken owner
        #: can ask before `Thread.is_alive` has caught up, and must not be told
        #: a dying writer is fine.
        self._finished = threading.Event()

    # -- the loop's side --------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._finished.is_set()

    def when_written(self, callback: Callable[[], None] | None) -> None:
        """Called on the writer thread after every batch. It must be safe to
        call from there, and must not block."""
        self._on_written = callback

    def submit(self, entry: Entry, line: str) -> None:
        self._queue.put(_Pending(entry, line))

    def outcomes(self) -> list[Written]:
        """Everything written since the last call, in order."""
        taken: list[Written] = []
        with contextlib.suppress(queue.Empty):
            while True:
                taken.append(self._outcomes.get_nowait())
        return taken

    def flush(self, timeout: float | None = None) -> bool:
        """Block until everything submitted so far is written. False if the
        writer is dead or did not finish in time."""
        return self._wait(_Mark(), timeout)

    def stop(self, timeout: float | None = None) -> bool:
        """Write what was submitted, then end the thread. False if it did not
        finish in time, in which case it is still running and still owns the
        file."""
        if not self._wait(_Mark(stop=True), timeout):
            return False
        self._thread.join(timeout)
        return not self.alive

    def _wait(self, mark: _Mark, timeout: float | None) -> bool:
        if not self.alive:
            return False
        self._queue.put(mark)
        deadline = None if timeout is None else time.monotonic() + timeout
        while not mark.done.wait(WRITER_POLL_SECONDS):
            # Asked afresh each time: the thread can die while this waits.
            if self._finished.is_set():
                return mark.done.is_set()
            if deadline is not None and time.monotonic() >= deadline:
                return False
        return True

    # -- the thread's side ------------------------------------------------

    def _run(self) -> None:
        try:
            while True:
                batch = self._take()
                pending = [item for item in batch if isinstance(item, _Pending)]
                marks = [item for item in batch if isinstance(item, _Mark)]
                if pending:
                    self._write_batch(pending)
                for mark in marks:
                    mark.done.set()
                if any(mark.stop for mark in marks):
                    return
        finally:
            # However the thread ends - including the way it must not - the
            # owner is woken to look, rather than finding out at the next tap.
            self._finished.set()
            self._wake()

    def _take(self) -> list[_Pending | _Mark]:
        """Wait for one item, then take everything else already queued."""
        batch = [self._queue.get()]
        with contextlib.suppress(queue.Empty):
            while True:
                batch.append(self._queue.get_nowait())
        return batch

    def _write_batch(self, batch: Sequence[_Pending]) -> None:
        try:
            outcomes = self._save(batch)
        except Exception as exc:  # never lets the thread end
            # Where it went wrong is unknown, so nothing in the batch is claimed
            # as saved and the handle is not trusted; the next write reopens it.
            self._file.abandon()
            error = f"could not write {self._file.path.name}: unexpected {type(exc).__name__}: {exc}"
            outcomes = [Written(item.entry, error=error) for item in batch]
        for outcome in outcomes:
            self._outcomes.put(outcome)
        self._wake()

    def _wake(self) -> None:
        if self._on_written is not None:
            with contextlib.suppress(Exception):
                self._on_written()

    def _save(self, batch: Sequence[_Pending]) -> list[Written]:
        errors: dict[int, str] = {}
        for item in batch:
            try:
                self._file.write_line(item.line)
            except WriteError as exc:
                errors[item.entry.seq] = str(exc)
        landed = [item.entry for item in batch if item.entry.seq not in errors]
        if landed:
            try:
                self._file.sync()
            except WriteError as exc:
                errors.update({entry.seq: str(exc) for entry in landed})
                landed = []
        mirror_errors = self._mirror_batch(landed)
        mirrored = self._mirror is not None
        return [
            Written(item.entry, error=errors[item.entry.seq])
            if item.entry.seq in errors
            else Written(
                item.entry,
                mirrored=mirrored and item.entry.seq not in mirror_errors,
                mirror_error=mirror_errors.get(item.entry.seq),
            )
            for item in batch
        ]

    def _mirror_batch(self, entries: Sequence[Entry]) -> dict[int, str]:
        """Offer saved entries to the mirror. Why each one it did not take was
        refused, by sequence number.

        Any exception at all is the mirror's failure and goes no further. The
        queue is a view the log can regenerate; a broken mirror must not cost
        the log its health, and must not end this thread.
        """
        if self._mirror is None or not entries:
            return {}
        errors: dict[int, str] = {}
        taken: list[Entry] = []
        for entry in entries:
            try:
                self._mirror.write(entry)
            except Exception as exc:
                errors[entry.seq] = str(exc)
            else:
                taken.append(entry)
        if taken:
            try:
                self._mirror.sync()
            except Exception as exc:
                errors.update({entry.seq: str(exc) for entry in taken})
        return errors


class AnnotationLog:
    """Append-only JSONL. One entry per line, written on a thread of its own.

    Everything that decides what an entry *is* - its sequence number, its span,
    its encoding - happens here, on the caller's thread. Only the disk is
    deferred, to a `LogWriter`. What it reports back is applied by `settle`:
    health, and undoing the span bookkeeping for a start or end that did not
    save, so the page offers that tap again.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        fsync: bool = True,
        mirror: EntrySink | None = None,
        opener: Opener = open_for_append,
        sync: Syncer = os.fsync,
    ) -> None:
        self.path = Path(path)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._file = AppendFile(self.path, keep_entries=True, fsync=fsync, opener=opener, sync=sync)
        self._writer = LogWriter(self._file, mirror)
        #: Whether entries are reaching the disk. Shown on the page: an
        #: annotation that is not saved is gone, and the operator is the only
        #: one who can do anything about it.
        self.health = WriteHealth()
        #: Whether the mirror is taking its copies. Only ever affects the live
        #: markers, which can be rebuilt from the log.
        self.mirror_health = WriteHealth()
        self._seq = 0
        self._open_spans: dict[str, Entry] = {}
        #: The start of each span whose end has been accepted but not yet
        #: reported saved, so an end that fails can reopen it.
        self._closing: dict[str, Entry] = {}
        #: Entries handed to the writer and not yet reported on.
        self._unsettled = 0
        self._writer_stop_reported = False
        #: The first `recording-started` already in the file when it was opened,
        #: or None for a log this run started. Not a fault -- appending is what
        #: an append-only log is for -- but it means `tacet.markers` will anchor
        #: today's entries to an earlier recording, so the box says so.
        self.prior_anchor: Entry | None = None
        #: The last entry already in the file, when its `monotonic` is ahead of
        #: this machine's clock - which restarts from near zero on a reboot.
        #: Offsets measured across that are wrong for any entry placed by
        #: arithmetic rather than by Reaper's playhead, so the box says so.
        self.clock_reset: Entry | None = None
        #: What opening did about a torn final write left by the last run, or
        #: None if the file ended cleanly.
        self.repair: Repair | None = None

    # -- lifecycle --------------------------------------------------------

    def open(self) -> Self:
        # The repair comes before resuming, so the entries counted are the
        # entries appended after. Both before the writer starts, so the thread
        # is the only thing touching the file from then until close.
        self.repair = self._file.open()
        try:
            self._resume()
        except BaseException:
            self._file.close()
            raise
        self._writer.start()
        return self

    def _resume(self) -> None:
        """Pick up sequence numbering and open spans from an existing log."""
        if not self.path.exists():
            return
        last: Entry | None = None
        for entry in read_entries(self.path):
            last = entry
            self._seq = max(self._seq, entry.seq)
            if entry.event == ANCHOR_EVENT and self.prior_anchor is None:
                self.prior_anchor = entry
            follow_span(self._open_spans, entry)
        if last is not None:
            self.clock_reset = clock_reset(last, now=self._clock.monotonic())

    def close(self, timeout: float = WRITER_STOP_SECONDS) -> None:
        """Write what was accepted, then close. A writer that does not finish in
        time keeps the file - closing it under a write in progress would be
        worse - and dies with the process, which is what the thread's deferral
        already accepted."""
        stopped = self._writer.stop(timeout)
        if stopped or not self._writer.alive:
            self._file.close()
        # After the file is closed, so a writer stopped on purpose is not
        # mistaken for one that died.
        self.settle()

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- what the writer reports ------------------------------------------

    def when_written(self, callback: Callable[[], None] | None) -> None:
        """Have `callback` called on the writer thread after each batch, so the
        owner can wake up and `settle`. It must be thread-safe and not block."""
        self._writer.when_written(callback)

    def flush(self, timeout: float | None = None) -> bool:
        """Block until everything accepted so far is written, then settle.
        False if the writer is dead or did not finish in time. For tests,
        tools and shutdown; never the event loop."""
        written = self._writer.flush(timeout)
        self.settle()
        return written

    def settle(self) -> bool:
        """Apply what the writer has reported since the last call. True if that
        changed anything the page shows."""
        before = self._shown()
        for outcome in self._writer.outcomes():
            self._unsettled -= 1
            self._apply(outcome)
        if not self._writer.alive and self._file.is_open and not self._writer_stop_reported:
            self._writer_stopped()
        return self._shown() != before

    def _shown(self) -> tuple[object, ...]:
        return (
            self.health.error,
            self.health.failures,
            self.mirror_health.error,
            self.mirror_health.failures,
            tuple(self._open_spans),
        )

    def _apply(self, outcome: Written) -> None:
        entry = outcome.entry
        if outcome.error is not None:
            self.health.failed(outcome.error)
            self._unsave_span(entry)
            return
        self.health.succeeded()
        if entry.phase == PHASE_END and entry.span_id is not None:
            self._closing.pop(entry.span_id, None)
        if outcome.mirror_error is not None:
            self.mirror_health.failed(outcome.mirror_error)
        elif outcome.mirrored:
            self.mirror_health.succeeded()

    def _unsave_span(self, entry: Entry) -> None:
        """Undo what accepting a span entry did, now that it did not save: a
        start that is not in the file is not open, and an end that is not in
        the file has not closed anything, so the button offers it again."""
        if entry.span_id is None:
            return
        if entry.phase == PHASE_START:
            self._open_spans.pop(entry.span_id, None)
        elif entry.phase == PHASE_END:
            start = self._closing.pop(entry.span_id, None)
            if start is not None:
                self._open_spans[entry.span_id] = start

    def _writer_stopped(self) -> None:
        """Once, however it is noticed. Everything accepted and never reported
        on is counted as lost, because it was."""
        self._writer_stop_reported = True
        # Not `failed`: the stop is not itself a lost entry, and `failures`
        # counts entries.
        self.health.error = WRITER_STOPPED
        self.health.failures += self._unsettled
        self._unsettled = 0

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
        self.settle()
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
        # First, so an end that failed to save is open again to be retapped.
        self.settle()
        start = self._open_spans.get(span_id)
        if start is None:
            raise UnknownSpanError(f"no open span with id {span_id!r}")
        entry = self._append(
            lookup(start.event),
            data=data,
            phase=PHASE_END,
            span_id=span_id,
            project_seconds=project_seconds,
        )
        # Closed as soon as the end is accepted, and reopened by `settle` if the
        # writer reports that it did not save.
        del self._open_spans[span_id]
        self._closing[span_id] = start
        return entry

    def open_spans(self) -> dict[str, str]:
        """Span ids still awaiting an end, each mapped to its event key. A game
        may legitimately finish with some open; the mirror runs them to the end
        of the timeline.

        The key is given rather than left in the id: ids are `<key>-<seq>` and
        keys contain hyphens, so "timeout-home-12" cannot say whether it belongs
        to "timeout" or "timeout-home"."""
        return {span_id: start.event for span_id, start in self._open_spans.items()}

    def _append(
        self,
        event: EventType,
        *,
        data: Mapping[str, Any] | None = None,
        phase: str | None = None,
        span_id: str | None = None,
        project_seconds: float | None = None,
    ) -> Entry:
        """Accept one entry and hand it to the writer.

        Raises `WriteError` only for what is known before the disk is involved:
        an entry that cannot be encoded, or a writer that has stopped. Either
        way the sequence number stays spent: a gap in the log marks where an
        entry was lost. A disk fault arrives later, through `settle`.
        """
        if not self._file.is_open:
            raise AnnotationError("log is not open")
        self.settle()
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
        try:
            line = entry.to_json()
        except ValueError as exc:
            # Not a disk fault, but the same outcome, and the same place to say
            # so: an entry that is not saved, counted on the page.
            self.health.failed(f"could not encode {event.key}: {exc}")
            raise WriteError(f"could not encode {event.key}: {exc}") from exc
        if not self._writer.alive:
            # Refused rather than queued behind a thread that will never write it.
            self.health.failures += 1
            raise WriteError(WRITER_STOPPED)
        self._unsettled += 1
        self._writer.submit(entry, line)
        return entry


def find_prior_anchor(path: Path | str) -> Entry | None:
    """The first `recording-started` already in a log, or None.

    Answered before the log is opened for writing, so the box can say at startup
    that today's entries will be positioned against an earlier recording. A
    missing file is not a problem -- that is the ordinary case, a fresh game.
    """
    path = Path(path)
    if not path.exists():
        return None
    for entry in read_entries(path):
        if entry.event == ANCHOR_EVENT:
            return entry
    return None


def follow_span(open_spans: dict[str, Entry], entry: Entry) -> None:
    """Update `open_spans` (span id to its start) for one entry read in order.

    The one rule for which spans a log leaves open, shared by resuming a log and
    by reporting on one before it is opened, so the two cannot disagree.
    """
    if entry.span_id is None:
        return
    if entry.phase == PHASE_START:
        open_spans[entry.span_id] = entry
    elif entry.phase == PHASE_END:
        open_spans.pop(entry.span_id, None)


def find_open_spans(path: Path | str) -> list[Entry]:
    """The start of every span a log leaves open, in the order they started.

    Answered before the log is opened for writing, like `find_prior_anchor`.
    Opening resumes these, which is right for a box restarted mid-quarter and
    wrong-looking for a log meant to be fresh: last game's `q4` reads "(end)".
    """
    path = Path(path)
    open_spans: dict[str, Entry] = {}
    if path.exists():
        for entry in read_entries(path):
            follow_span(open_spans, entry)
    return list(open_spans.values())


def clock_reset(last: Entry, *, now: float) -> Entry | None:
    """`last` if this machine's monotonic clock is behind it, else None.

    Pure. Monotonic time never goes backwards within a boot, so a clock behind
    the last entry is a clock that has restarted since it was written.
    """
    return last if last.monotonic > now else None


def find_clock_reset(path: Path | str, *, now: float) -> Entry | None:
    """`clock_reset` for a log on disk, before it is opened. A missing or empty
    log has no clock to compare."""
    path = Path(path)
    if not path.exists():
        return None
    # Every line is read, not just the last: a log with a corrupt line in it
    # is refused here as it would be on open.
    last = collections.deque(read_entries(path), maxlen=1)
    return clock_reset(last[0], now=now) if last else None


def read_entries(path: Path | str) -> Iterator[Entry]:
    """Read a log back.

    A malformed line raises `CorruptLogError` naming its line number, because
    dropping entries quietly is worse than stopping. The one exception is an
    unterminated final line, which is the ordinary result of losing power
    mid-write: that costs the last entry and nothing else. A final line that is
    whole but of a schema this build cannot read is not that, and still raises.
    """
    path = Path(path)
    with path.open(encoding=_ENCODING) as handle:
        pending: str | None = None
        pending_number = 0
        pending_terminated = False
        for number, raw in enumerate(handle, start=1):
            terminated = raw.endswith(_LINE_TERMINATOR)
            line = raw.strip()
            if not line:
                continue
            if pending is not None:
                yield _entry_at(pending, pending_number)
            pending, pending_number, pending_terminated = line, number, terminated
        if pending is None:
            return
        if pending_terminated:
            yield _entry_at(pending, pending_number)
            return
        try:
            entry = _entry_at(pending, pending_number)
        except SchemaVersionError:
            raise
        except CorruptLogError:
            return  # torn final write
        yield entry


def _entry_at(line: str, number: int) -> Entry:
    try:
        return Entry.from_json(line)
    except CorruptLogError as exc:
        raise type(exc)(f"line {number}: {exc}") from exc
