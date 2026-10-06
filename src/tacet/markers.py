"""Derive Reaper markers and regions from the annotation log.

The log is the source of truth; this is the derived view (design.md 5.9). It is
pure, so the whole mapping can be tested without Reaper running, and it is
regenerable, so a Reaper failure costs the view and not the data.

Instants become markers, spans become regions, and names follow the
`CATEGORY|key` convention so reading them back is a split rather than a regex.

Anything this cannot place is reported rather than dropped. Reaper has no
negative timeline, so entries made before recording started have nowhere to go -
but losing them quietly is the silent degradation CLAUDE.md forbids, so they
come back in `skipped_before_anchor` for the caller to surface. A
`recording-requested` entry always lands there: it is written before the
recording it asked for exists.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .annotations import (
    ANCHOR_EVENT,
    FOUND_EVENT,
    PHASE_END,
    PHASE_START,
    AnnotationError,
    Entry,
    is_anchor,
    marker_name,
    project_seconds,
)
from .taps import delay_of

CSV_FIELDS = ("name", "start", "end")

#: How far a later `recording-found` entry may disagree with the anchor's
#: arithmetic and still be the same take seen again. A separate take is at
#: least a stop, Reaper's prompt and a restart away, which is seconds; the
#: arithmetic holds to the second over a five hour game (#158).
TAKE_MATCH_TOLERANCE_SECONDS = 2.0


class NoAnchorError(AnnotationError):
    """The log never recorded the start of the recording, so nothing in it can
    be placed on a timeline."""


@dataclass(frozen=True)
class Marker:
    name: str
    start: float
    #: `None` for an instant marker; a position for a region.
    end: float | None = None

    @property
    def is_region(self) -> bool:
        return self.end is not None


@dataclass(frozen=True)
class DerivedMarkers:
    """The mapping's result, including everything it could not place."""

    markers: tuple[Marker, ...] = ()
    skipped_before_anchor: tuple[Entry, ...] = ()
    unclosed_spans: tuple[str, ...] = ()
    orphan_ends: tuple[str, ...] = ()
    #: Recordings after the first one in this log. The whole timeline is
    #: measured from the first anchor, so entries logged after a stop and
    #: restart are placed the length of the gap away from the audio they
    #: describe. They are not dropped and their positions are not negative, so
    #: nothing else here would notice: without this they are silently wrong,
    #: which is the one outcome CLAUDE.md rules out.
    extra_anchors: tuple[Entry, ...] = ()

    @property
    def is_clean(self) -> bool:
        """True when every entry landed somewhere, correctly. Show the operator otherwise."""
        return not (self.skipped_before_anchor or self.unclosed_spans or self.orphan_ends or self.extra_anchors)


def find_anchor(entries: Iterable[Entry]) -> Entry:
    """The first entry that is an anchor: a take seen starting, or a stamped
    take found rolling (`is_anchor`).

    First, not last: a restarted recording begins a new project, which is a
    different problem from placing marks in this one.
    """
    for entry in entries:
        if is_anchor(entry):
            return entry
    raise NoAnchorError(f"no {ANCHOR_EVENT!r} or stamped {FOUND_EVENT!r} entry; nothing can be placed")


def _origin_of(anchor: Entry) -> float:
    """The position the anchor itself stands at: Reaper's own number when it
    has one (a take found rolling), else zero (a take seen starting)."""
    return anchor.project_seconds if anchor.project_seconds is not None else 0.0


def _arithmetic(entry: Entry, anchor: Entry) -> float:
    return _origin_of(anchor) + project_seconds(entry, anchor)


def _is_extra_recording(entry: Entry, anchor: Entry) -> bool:
    """Whether `entry` is evidence of a recording other than the anchor's."""
    if entry.seq == anchor.seq:
        return False
    if entry.event == ANCHOR_EVENT:
        return True
    if entry.event == FOUND_EVENT and entry.project_seconds is not None:
        return abs(entry.project_seconds - _arithmetic(entry, anchor)) > TAKE_MATCH_TOLERANCE_SECONDS
    return False


def position_of(entry: Entry, anchor: Entry) -> float:
    """Where an entry sits on the timeline.

    A `project_seconds` recorded at the time wins: it came from Reaper's own
    playhead and beats arithmetic on our clock.

    Either one says where the entry *arrived*. When the tap that made it logged
    how late it was (#11), the entry goes back by that much, to where the
    operator tapped. A negative delay is the clock estimate's error, and a tap
    cannot follow its own arrival, so it moves nothing. An anchor or found
    entry is never moved: it marks a recording, not a tap.

    Arithmetic starts from the anchor's own position: zero for a take seen
    starting, Reaper's stamp for one found rolling.
    """
    arrived = entry.project_seconds if entry.project_seconds is not None else _arithmetic(entry, anchor)
    if entry.event in (ANCHOR_EVENT, FOUND_EVENT):
        # It marks a recording, never a tap: a tap delay would place it before
        # itself.
        return arrived
    delay = delay_of(entry.data)
    return arrived if delay is None else arrived - max(0.0, delay)


def derive(
    entries: Sequence[Entry],
    anchor: Entry,
    *,
    timeline_end: float | None = None,
) -> DerivedMarkers:
    """Map log entries onto markers and regions."""
    placed: list[Marker] = []
    skipped: list[Entry] = []
    orphans: list[str] = []
    open_spans: dict[str, float] = {}
    span_names: dict[str, str] = {}
    extra_anchors: list[Entry] = []
    latest = 0.0

    for entry in entries:
        # Every later recording in this log. Reported rather than placed on:
        # the arithmetic below is measured from `anchor`, so everything after a
        # restart is out by the length of the stop.
        if _is_extra_recording(entry, anchor):
            extra_anchors.append(entry)
        position = position_of(entry, anchor)
        if position < 0:
            skipped.append(entry)
            continue
        latest = max(latest, position)

        if entry.span_id is None:
            placed.append(Marker(name=marker_name(entry), start=position))
            continue

        if entry.phase == PHASE_START:
            open_spans[entry.span_id] = position
            span_names[entry.span_id] = marker_name(entry)
        elif entry.phase == PHASE_END:
            start = open_spans.pop(entry.span_id, None)
            if start is None:
                orphans.append(entry.span_id)
                continue
            placed.append(Marker(name=span_names[entry.span_id], start=start, end=position))

    # A game may legitimately end with spans open - the operator stopped tapping
    # before the quarter ended. Run them to the end rather than discarding them.
    end = timeline_end if timeline_end is not None else latest
    for span_id, start in open_spans.items():
        placed.append(Marker(name=span_names[span_id], start=start, end=max(end, start)))

    placed.sort(key=lambda marker: (marker.start, marker.name))
    return DerivedMarkers(
        markers=tuple(placed),
        skipped_before_anchor=tuple(skipped),
        unclosed_spans=tuple(open_spans),
        orphan_ends=tuple(orphans),
        extra_anchors=tuple(extra_anchors),
    )


def to_csv(markers: Iterable[Marker]) -> str:
    """Serialise for a Reaper-side script to apply. CSV so it stays inspectable
    by eye in a press box."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for marker in markers:
        writer.writerow([marker.name, f"{marker.start:.6f}", "" if marker.end is None else f"{marker.end:.6f}"])
    return buffer.getvalue()


def from_csv(text: str) -> list[Marker]:
    reader = csv.DictReader(io.StringIO(text))
    return [
        Marker(
            name=row["name"],
            start=float(row["start"]),
            end=float(row["end"]) if row["end"] else None,
        )
        for row in reader
    ]
