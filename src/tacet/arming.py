"""Whether every track but Pilot is armed to record (#167).

An unarmed track records nothing, and nothing recovers it. Reaper's stock OSC
bank shows tracks 1-8 only, so `reaper/tacet_mirror.lua` lists every track's
number, arm state and name in the report `tacet.mirror_report` reads, and this
module judges them: every track but the one named Pilot must be armed.

The script reports facts and judges nothing. The Pilot rule lives here, so it
can change without reinstalling the Reaper script, and the script holds no
track name, number or count.

Warn only. Nothing here blocks a take, and nothing here is a reason to touch
the fader. "Armed" on the page always says "armed to record": the duty chip
already uses the bare word for the box's own state.

A pure core with no I/O. Standard library plus `tacet.mirror_report`, and
nothing in the control path imports it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from . import mirror_report

KEY_TRACKS = "tracks"
KEY_TRACK = "track"
ARMED_FLAG = "1"
UNARMED_FLAG = "0"
#: A `track` value is number, arm flag and name, tab-separated.
TRACK_PARTS = 3
#: The tone source (docs/reaper.md), the one track that records nothing. Matched
#: exactly, after stripping surrounding whitespace, case-sensitively. `Pilot
#: Reference` is a different, armed track.
PILOT_NAME = "Pilot"


@dataclass(frozen=True)
class Track:
    number: int
    armed: bool
    #: Raw: may be empty, and may contain a tab.
    name: str


class ArmStatus(StrEnum):
    ARMED = "armed"
    UNARMED = "unarmed"
    NO_TRACKS = "no-tracks"
    UNKNOWN = "unknown"


class ArmReason(StrEnum):
    """Why the status is unknown. The first three share their values with
    `mirror_report.Reason`."""

    NO_REPORT = "no-report"
    STOPPED = "stopped"
    UNREADABLE = "unreadable"
    #: A fresh report with no `tracks` key: a script from before #167, or a
    #: failed track API.
    NOT_REPORTED = "not-reported"
    TRACKS_UNREADABLE = "tracks-unreadable"


@dataclass(frozen=True)
class ArmCheck:
    status: ArmStatus
    reason: ArmReason | None
    #: Every armed track of any name, an armed Pilot included.
    armed: int | None
    tracks: int | None
    unarmed: tuple[Track, ...] = ()
    #: The numbers of the tracks named Pilot.
    pilots: tuple[int, ...] = ()
    #: `capture.channels`, what the disk check sized the game from.
    channels: int | None = None

    @property
    def pilot_ambiguous(self) -> bool:
        return len(self.pilots) > 1

    @property
    def channels_differ(self) -> bool:
        return (
            self.status is ArmStatus.ARMED
            and self.channels is not None
            and self.armed is not None
            and self.armed != self.channels
        )

    def as_snapshot(self) -> dict[str, Any]:
        """For the page. `seq` is deliberately absent: it changes every two
        seconds and would make every broadcast different (#147)."""
        return {
            "status": str(self.status),
            "reason": None if self.reason is None else str(self.reason),
            "armed": self.armed,
            "tracks": self.tracks,
            "unarmed": [{"number": track.number, "name": track.name} for track in self.unarmed],
            "pilot_ambiguous": self.pilot_ambiguous,
            "channels": self.channels,
            "channels_differ": self.channels_differ,
        }


def is_pilot(name: str) -> bool:
    return name.strip() == PILOT_NAME


def parse_tracks(fields: Sequence[tuple[str, str]]) -> tuple[Track, ...] | mirror_report.Unreadable | None:
    """The tracks in a report's pairs, in order. None when there is no `tracks`
    key; Unreadable when the lines contradict the count or each other."""
    counts = [value for key, value in fields if key == KEY_TRACKS]
    if not counts:
        return None
    try:
        count = int(counts[0])
    except ValueError:
        return mirror_report.Unreadable(f"tracks is not a number: {counts[0]!r}")
    values = [value for key, value in fields if key == KEY_TRACK]
    if count < 0 or len(values) != count:
        return mirror_report.Unreadable(f"tracks says {counts[0]} and there are {len(values)} track lines")
    parsed: list[Track] = []
    for expected, value in enumerate(values, start=1):
        one = _parse_track(value, expected)
        if isinstance(one, mirror_report.Unreadable):
            return one
        parsed.append(one)
    return tuple(parsed)


def _parse_track(value: str, expected: int) -> Track | mirror_report.Unreadable:
    parts = value.split(mirror_report.DELIMITER, TRACK_PARTS - 1)
    if len(parts) != TRACK_PARTS:
        return mirror_report.Unreadable(f"not a track line: {value!r}")
    number, flag, name = parts
    if number != str(expected):
        return mirror_report.Unreadable(f"track {number!r} where track {expected} should be")
    if flag not in (ARMED_FLAG, UNARMED_FLAG):
        return mirror_report.Unreadable(f"arm state is not 0 or 1: {flag!r}")
    return Track(expected, flag == ARMED_FLAG, name)


def judge(tracks: Sequence[Track], channels: int | None) -> ArmCheck:
    """Hold every track but Pilot to being armed. Pure.

    Exactly one Pilot is exempt. With none, or with two or more, nothing is:
    the box cannot tell which is the tone source."""
    pilots = tuple(track.number for track in tracks if is_pilot(track.name))
    exempt = pilots if len(pilots) == 1 else ()
    unarmed = tuple(track for track in tracks if not track.armed and track.number not in exempt)
    armed = sum(1 for track in tracks if track.armed)
    if not tracks:
        status = ArmStatus.NO_TRACKS
    elif unarmed:
        status = ArmStatus.UNARMED
    else:
        status = ArmStatus.ARMED
    return ArmCheck(status, None, armed, len(tracks), unarmed, pilots, channels)


def assess(observed: mirror_report.Observed, now: float, *, channels: int | None) -> ArmCheck:
    """What the box may say about track arming at `now`. Pure.

    Independent of the project status: a garbled tracks block never makes the
    project chip unreadable."""
    stale = mirror_report.freshness(observed, now)
    if stale is not None:
        return _unknown(ArmReason(str(stale)), channels)
    if observed.report is None:
        return _unknown(ArmReason.NO_REPORT, channels)
    parsed = parse_tracks(observed.report.fields)
    if parsed is None:
        return _unknown(ArmReason.NOT_REPORTED, channels)
    if isinstance(parsed, mirror_report.Unreadable):
        return _unknown(ArmReason.TRACKS_UNREADABLE, channels)
    return judge(parsed, channels)


def _unknown(reason: ArmReason, channels: int | None) -> ArmCheck:
    return ArmCheck(ArmStatus.UNKNOWN, reason, None, None, channels=channels)
