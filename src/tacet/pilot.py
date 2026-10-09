"""Post-game check of the box's ramps against the pilot recording (#191).

The DCA reference channel carries a pilot tone the console scales with the
fader, so the recording shows where the fader really was. The box cannot read
that back (the OSC is write-only), but afterwards the pilot and the log can be
laid side by side. A ramp the box starts from the wrong belief shows up as a
jump at its first moment: a fade that opens the fader a fraction (the blast), a
ride that drops it, or a belief the pilot simply disagrees with.

Offline analysis only. This module takes numpy, so it is not control path and
the box never imports it (tests/test_dependency_policy.py). The core is pure:
arrays and entries in, a `Report` out. `read_envelope` is the one reader.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from . import annotations as ann
from . import dm7, markers, wav
from .annotations import Entry
from .state import FaderCommand

#: One envelope window. The pilot moves 40-230 ms after the stamp (Game 3), so
#: 10 ms resolves the ramp's start well.
WINDOW_SECONDS = 0.01
#: A sine's mean square is half its peak squared; doubling gives peak power.
SINE_PEAK_POWER_RATIO = 2.0
#: What digital silence reads as, so log10 never sees zero.
SILENCE_FLOOR_DBFS = -200.0
#: A pilot window below this is the fader closed.
CLOSED_BELOW_DBFS = -100.0
ENVELOPE_BLOCK_SECONDS = 60.0
FULL_SCALE_24 = 1 << 23
BYTES_PER_SAMPLE = 3
_BYTE_BITS = 8
_SIGN_BIT = 1 << 23
_WORD_24 = 1 << 24

#: Where to look relative to a ramp's stamp: the belief window ends before the
#: pilot has moved, and the after window runs past the slowest Game 3 lag.
RAMP_BEFORE_WINDOW = (-0.5, 0.03)
RAMP_AFTER_SECONDS = 0.4
#: A snap open from silence: quiet before, settled after.
CALIBRATION_SILENCE_WINDOW = (-1.0, -0.3)
CALIBRATION_SETTLED_WINDOW = (0.5, 1.5)
JUMP_TOLERANCE_DB = 1.5
MISMATCH_TOLERANCE_DB = JUMP_TOLERANCE_DB
#: Measured stamp-to-pilot alignment on Game 3, kept so a test can pin that the
#: windows above allow for it.
GAME_3_LAG_RANGE = (0.04, 0.23)

COMMANDED_EVENT = "commanded"
DATA_COMMAND = "command"
DATA_DETAIL = "detail"
DATA_LEVEL = "level"
DATA_TARGET = "target"
DATA_TARGET_DB = "target_db"
DATA_DELIVERED = "delivered"
RAMP_COMMANDS = frozenset({FaderCommand.FADE.value, FaderCommand.READY.value, FaderCommand.RETARGET.value})

KIND_FADE = "fade"
KIND_READY = "ready"
KIND_UP_SLOW = "up-slow ride"
KIND_RETARGET = "retarget"
KINDS = (KIND_FADE, KIND_READY, KIND_UP_SLOW, KIND_RETARGET)

_NEG_INF = float("-inf")
_TIME_FORMAT = "%H:%M:%S"


class PilotCheckError(RuntimeError):
    """The check cannot run, or cannot be trusted to. Never a finding."""


class NoCalibrationError(PilotCheckError):
    """No snap open from silence settled in the pilot, so there is no offset."""


class AnchorConflictError(PilotCheckError):
    """The log already has an anchor and `--take-start` was given as well."""


class Direction(StrEnum):
    FALLING = "falling"
    RISING = "rising"
    LEVEL = "level"


class Finding(StrEnum):
    BLAST = "jumped up at the start of a falling ramp"
    DROPOUT = "dropped at the start of a rising ramp"
    MISMATCH = "pilot disagrees with the box's belief"
    UNCOVERED = "the pilot does not cover it"
    UNSTAMPED = "no project position was logged for it"


_FLAGS = (Finding.BLAST, Finding.DROPOUT, Finding.MISMATCH)
_UNCHECKED = (Finding.UNCOVERED, Finding.UNSTAMPED)


@dataclass(frozen=True)
class Envelope:
    """The pilot as equivalent sine-peak dBFS, one value per window."""

    dbfs: NDArray[np.float32]
    window_seconds: float
    #: Where window 0 starts, in project seconds.
    start_seconds: float

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + len(self.dbfs) * self.window_seconds

    def window(self, start: float, end: float) -> NDArray[np.float32] | None:
        """The windows between two project times, or None unless wholly covered."""
        first = round((start - self.start_seconds) / self.window_seconds)
        last = round((end - self.start_seconds) / self.window_seconds)
        if first < 0 or last > len(self.dbfs) or last <= first:
            return None
        return self.dbfs[first:last]


@dataclass(frozen=True)
class Anchor:
    wall: datetime
    entry: Entry | None
    description: str


@dataclass(frozen=True)
class Take:
    anchor: Anchor
    entries: tuple[Entry, ...]
    ended_by: Entry | None


@dataclass(frozen=True)
class Ramp:
    entry: Entry
    believed_db: float
    target_db: float
    direction: Direction


@dataclass(frozen=True)
class RampCheck:
    ramp: Ramp
    before_db: float | None
    after_db: float | None
    findings: tuple[Finding, ...]

    @property
    def flagged(self) -> bool:
        return any(f in _FLAGS for f in self.findings)

    @property
    def unchecked(self) -> bool:
        return any(f in _UNCHECKED for f in self.findings)


@dataclass(frozen=True)
class Calibration:
    """Pilot dBFS minus DCA dB, measured on snap opens from silence."""

    offset_db: float
    #: (seq, offset) for each open that was used.
    used: tuple[tuple[int, float], ...]
    #: Opens that came from silence and then showed nothing: the pilot never moved.
    silent_opens: tuple[int, ...]
    #: Opens whose windows the pilot does not cover.
    uncovered: int

    @property
    def count(self) -> int:
        return len(self.used)

    @property
    def lowest(self) -> tuple[int, float]:
        return min(self.used, key=lambda item: item[1])

    @property
    def highest(self) -> tuple[int, float]:
        return max(self.used, key=lambda item: item[1])


@dataclass(frozen=True)
class Report:
    take: Take
    layout: wav.WavLayout
    calibration: Calibration
    checks: tuple[RampCheck, ...]

    @property
    def flagged(self) -> tuple[RampCheck, ...]:
        return tuple(c for c in self.checks if c.flagged)

    @property
    def unchecked(self) -> tuple[RampCheck, ...]:
        return tuple(c for c in self.checks if c.unchecked)

    @property
    def counts_by_kind(self) -> dict[str, int]:
        counts = dict.fromkeys(KINDS, 0)
        for check in self.checks:
            counts[kind_of(check.ramp.entry)] += 1
        return counts


# -- envelope ---------------------------------------------------------------


def decode_pcm24(raw: NDArray[np.uint8]) -> NDArray[np.float64]:
    """Signed little-endian 24-bit samples as fractions of full scale."""
    triples = raw[: len(raw) - len(raw) % BYTES_PER_SAMPLE].reshape(-1, BYTES_PER_SAMPLE).astype(np.int64)
    value = triples[:, 0] | (triples[:, 1] << _BYTE_BITS) | (triples[:, 2] << (2 * _BYTE_BITS))
    value = np.where(value >= _SIGN_BIT, value - _WORD_24, value)
    return np.asarray(value / FULL_SCALE_24, dtype=np.float64)


def envelope_dbfs(samples: NDArray[np.float64], window_samples: int) -> NDArray[np.float32]:
    """Equivalent sine-peak dBFS per whole window; a partial tail is dropped."""
    whole = len(samples) // window_samples
    blocks = samples[: whole * window_samples].reshape(whole, window_samples)
    power = SINE_PEAK_POWER_RATIO * np.mean(blocks * blocks, axis=1)
    floor_power = 10 ** (SILENCE_FLOOR_DBFS / 10)
    return np.asarray(10 * np.log10(np.maximum(power, floor_power)), dtype=np.float32)


def envelope_of(
    raw: NDArray[np.uint8],
    *,
    sample_rate: int,
    start_seconds: float,
    block_seconds: float = ENVELOPE_BLOCK_SECONDS,
) -> Envelope:
    """The envelope of 24-bit mono PCM bytes, decoded a block at a time so a
    memory-mapped game never has to fit in memory as floats."""
    window_samples = round(sample_rate * WINDOW_SECONDS)
    windows = len(raw) // BYTES_PER_SAMPLE // window_samples
    per_block = max(1, round(block_seconds * sample_rate / window_samples))
    window_bytes = window_samples * BYTES_PER_SAMPLE
    parts: list[NDArray[np.float32]] = []
    for first in range(0, windows, per_block):
        last = min(windows, first + per_block)
        parts.append(envelope_dbfs(decode_pcm24(raw[first * window_bytes : last * window_bytes]), window_samples))
    dbfs = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return Envelope(dbfs=dbfs, window_seconds=window_samples / sample_rate, start_seconds=start_seconds)


def read_envelope(path: Path) -> tuple[wav.WavLayout, Envelope]:
    """Map the file and compute its envelope, placed by its BWF TimeReference."""
    with path.open("rb") as handle:
        layout = wav.read_layout(handle, path.stat().st_size)
    raw: NDArray[np.uint8]
    if layout.data_bytes:
        raw = np.memmap(path, dtype=np.uint8, mode="r", offset=layout.data_offset, shape=(layout.data_bytes,))
    else:
        raw = np.zeros(0, dtype=np.uint8)
    return layout, envelope_of(raw, sample_rate=layout.sample_rate, start_seconds=layout.start_seconds)


# -- entries ----------------------------------------------------------------


def _wall(entry: Entry) -> datetime:
    when = datetime.fromisoformat(entry.wall)
    if when.tzinfo is None:
        raise PilotCheckError(f"entry {entry.seq} has a wall time without a timezone: {entry.wall!r}")
    return when


def resolve_anchor(entries: Sequence[Entry], take_start: datetime | None) -> Anchor:
    """Where the take starts: the log's own anchor, or `--take-start`, never both."""
    try:
        logged: Entry | None = markers.find_anchor(entries)
    except markers.NoAnchorError:
        logged = None
    if logged is not None and take_start is not None:
        raise AnchorConflictError(
            f"the log already has an anchor (seq {logged.seq}, {logged.event}); "
            "drop --take-start, which is for logs without one"
        )
    if logged is not None:
        return Anchor(wall=_wall(logged), entry=logged, description=f"logged {logged.event} (seq {logged.seq})")
    if take_start is None:
        raise markers.NoAnchorError(
            "the log has no recording anchor, so the take cannot be told from earlier ones; "
            "pass --take-start with the wall time the recording started"
        )
    return Anchor(wall=take_start, entry=None, description=f"--take-start {take_start.isoformat()}")


def take_of(entries: Sequence[Entry], anchor: Anchor) -> Take:
    """The entries written during the take.

    With a logged anchor the take ends at the first recording the log later
    shows (`markers.derive`'s extra anchors). With a supplied start there is
    no arithmetic to compare against, so any later anchor entry ends it; that
    is conservative, and a restart inside a take cuts the take short.
    """
    if anchor.entry is not None:
        first = anchor.entry.seq
        extra = markers.derive(entries, anchor.entry).extra_anchors
        ended_by = extra[0] if extra else None
        inside = [e for e in entries if e.seq > first]
    else:
        ended_by = next((e for e in entries if _wall(e) > anchor.wall and ann.is_anchor(e)), None)
        inside = [e for e in entries if _wall(e) >= anchor.wall]
    if ended_by is not None:
        inside = [e for e in inside if e.seq < ended_by.seq]
    return Take(anchor=anchor, entries=tuple(inside), ended_by=ended_by)


def _is_ride_in(entry: Entry) -> bool:
    detail = entry.data.get(DATA_DETAIL)
    event = ann.EVENTS.get(detail) if isinstance(detail, str) else None
    return event is not None and event.action is ann.Action.OPEN_SLOW


def _command_of(entry: Entry) -> str | None:
    if entry.event != COMMANDED_EVENT:
        return None
    command = entry.data.get(DATA_COMMAND)
    return command if isinstance(command, str) else None


def is_ramp(entry: Entry) -> bool:
    command = _command_of(entry)
    if command is None:
        return False
    return command in RAMP_COMMANDS or (command == FaderCommand.OPEN.value and _is_ride_in(entry))


def is_snap_open(entry: Entry) -> bool:
    return _command_of(entry) == FaderCommand.OPEN.value and not _is_ride_in(entry)


def kind_of(entry: Entry) -> str:
    command = _command_of(entry)
    if command == FaderCommand.FADE.value:
        return KIND_FADE
    if command == FaderCommand.READY.value:
        return KIND_READY
    if command == FaderCommand.RETARGET.value:
        return KIND_RETARGET
    return KIND_UP_SLOW


def _level_db(entry: Entry, key: str) -> float:
    value = entry.data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PilotCheckError(f"commanded entry {entry.seq} has no usable {key!r}: {value!r}")
    return dm7.to_db(value)


def ramp_of(entry: Entry) -> Ramp:
    believed = _level_db(entry, DATA_LEVEL)
    target = _level_db(entry, DATA_TARGET)
    if target < believed:
        direction = Direction.FALLING
    elif target > believed:
        direction = Direction.RISING
    else:
        direction = Direction.LEVEL
    return Ramp(entry=entry, believed_db=believed, target_db=target, direction=direction)


# -- calibration and checks -------------------------------------------------


def _median(values: NDArray[np.float32]) -> float:
    return float(np.median(values))


def _commanded_between(take: Take, entry: Entry, after: float, before: float) -> bool:
    for other in take.entries:
        if other.seq == entry.seq or other.event != COMMANDED_EVENT or other.project_seconds is None:
            continue
        if after < other.project_seconds < before:
            return True
    return False


def _calibration_offset(envelope: Envelope, take: Take, entry: Entry) -> tuple[str, float]:
    """Classify one snap open: ("used", offset), "silent", "uncovered" or "skip"."""
    stamp = entry.project_seconds
    target_db = entry.data.get(DATA_TARGET_DB)
    if stamp is None or not isinstance(target_db, int | float) or entry.data.get(DATA_DELIVERED) is not True:
        return "skip", 0.0
    before = envelope.window(stamp + CALIBRATION_SILENCE_WINDOW[0], stamp + CALIBRATION_SILENCE_WINDOW[1])
    settled = envelope.window(stamp + CALIBRATION_SETTLED_WINDOW[0], stamp + CALIBRATION_SETTLED_WINDOW[1])
    if before is None or settled is None:
        return "uncovered", 0.0
    if _median(before) >= CLOSED_BELOW_DBFS:
        return "skip", 0.0
    if _commanded_between(take, entry, stamp, stamp + CALIBRATION_SETTLED_WINDOW[1]):
        return "skip", 0.0
    level = _median(settled)
    if level < CLOSED_BELOW_DBFS:
        return "silent", 0.0
    return "used", level - float(target_db)


def calibrate(envelope: Envelope, take: Take) -> Calibration:
    used: list[tuple[int, float]] = []
    silent: list[int] = []
    uncovered = 0
    for entry in take.entries:
        if not is_snap_open(entry):
            continue
        verdict, offset = _calibration_offset(envelope, take, entry)
        if verdict == "used":
            used.append((entry.seq, offset))
        elif verdict == "silent":
            silent.append(entry.seq)
        elif verdict == "uncovered":
            uncovered += 1
    if not used:
        raise NoCalibrationError(
            "no snap open from silence settled in the pilot, so the pilot level cannot be turned into DCA dB"
            f" ({len(silent)} showed nothing, {uncovered} fell outside the file)"
        )
    return Calibration(
        offset_db=statistics.median(offset for _, offset in used),
        used=tuple(used),
        silent_opens=tuple(silent),
        uncovered=uncovered,
    )


def _as_dca(window: NDArray[np.float32], calibration: Calibration) -> NDArray[np.float64]:
    dca = window.astype(np.float64) - calibration.offset_db
    return np.asarray(np.where(window < CLOSED_BELOW_DBFS, _NEG_INF, dca), dtype=np.float64)


def _jump_up(before: float, peak: float) -> bool:
    if math.isinf(before):
        return not math.isinf(peak)
    return peak - before > JUMP_TOLERANCE_DB


def _jump_down(before: float, low: float) -> bool:
    if math.isinf(before):
        return False
    return math.isinf(low) or before - low > JUMP_TOLERANCE_DB


def _disagrees(believed: float, before: float) -> bool:
    if math.isinf(believed) or math.isinf(before):
        return believed != before
    return abs(believed - before) > MISMATCH_TOLERANCE_DB


def check_ramp(envelope: Envelope, ramp: Ramp, calibration: Calibration) -> RampCheck:
    stamp = ramp.entry.project_seconds
    if stamp is None:
        return RampCheck(ramp, None, None, (Finding.UNSTAMPED,))
    split = stamp + RAMP_BEFORE_WINDOW[1]
    before_window = envelope.window(stamp + RAMP_BEFORE_WINDOW[0], split)
    after_window = envelope.window(split, stamp + RAMP_AFTER_SECONDS)
    if before_window is None or after_window is None:
        return RampCheck(ramp, None, None, (Finding.UNCOVERED,))
    before = float(np.median(_as_dca(before_window, calibration)))
    after = _as_dca(after_window, calibration)
    peak, low = float(np.max(after)), float(np.min(after))
    findings: list[Finding] = []
    if ramp.direction is not Direction.RISING and _jump_up(before, peak):
        findings.append(Finding.BLAST)
    if ramp.direction is not Direction.FALLING and _jump_down(before, low):
        findings.append(Finding.DROPOUT)
    if _disagrees(ramp.believed_db, before):
        findings.append(Finding.MISMATCH)
    shown = low if ramp.direction is Direction.RISING else peak
    return RampCheck(ramp, before, shown, tuple(findings))


def check(envelope: Envelope, take: Take, layout: wav.WavLayout) -> Report:
    calibration = calibrate(envelope, take)
    checks = tuple(check_ramp(envelope, ramp_of(e), calibration) for e in take.entries if is_ramp(e))
    return Report(take=take, layout=layout, calibration=calibration, checks=checks)


# -- report -----------------------------------------------------------------


def _db(value: float | None) -> str:
    if value is None:
        return "-"
    return "-inf" if math.isinf(value) else f"{value:+.1f}"


def _clock(entry: Entry, tz: tzinfo | None) -> str:
    return _wall(entry).astimezone(tz).strftime(_TIME_FORMAT)


def _utc_clock(entry: Entry) -> str:
    return _wall(entry).astimezone(UTC).strftime(_TIME_FORMAT)


def _what(entry: Entry) -> str:
    detail = entry.data.get(DATA_DETAIL)
    command = _command_of(entry) or entry.event
    return f"{command} ({detail})" if detail else command


def _row(check_: RampCheck, tz: tzinfo | None) -> list[str]:
    entry = check_.ramp.entry
    stamp = entry.project_seconds
    return [
        str(entry.seq),
        _clock(entry, tz),
        _utc_clock(entry),
        "-" if stamp is None else f"{stamp:.2f}",
        _what(entry),
        _db(check_.ramp.believed_db),
        _db(check_.before_db),
        _db(check_.after_db),
        "; ".join(f.value for f in check_.findings),
    ]


_HEADERS = ["seq", "local", "UTC", "project s", "command", "believed", "pilot before", "after", "findings"]


def _table(rows: list[list[str]]) -> Iterator[str]:
    widths = [max(len(r[i]) for r in [_HEADERS, *rows]) for i in range(len(_HEADERS))]
    for row in [_HEADERS, *rows]:
        yield "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()


def _opposite_jump(check_: RampCheck) -> float | None:
    before, after = check_.before_db, check_.after_db
    if before is None or after is None or math.isinf(before) or math.isinf(after):
        return None
    return before - after if check_.ramp.direction is Direction.RISING else after - before


def _calibration_lines(calibration: Calibration, tz: tzinfo | None, take: Take) -> list[str]:
    by_seq = {e.seq: e for e in take.entries}

    def at(item: tuple[int, float]) -> str:
        return f"{item[1]:+.1f} dB (seq {item[0]}, {_clock(by_seq[item[0]], tz)})"

    lines = [
        f"calibration: pilot = DCA {calibration.offset_db:+.1f} dB, from {calibration.count} snap opens from silence",
        f"  lowest {at(calibration.lowest)}, highest {at(calibration.highest)}",
    ]
    if calibration.silent_opens:
        seqs = ", ".join(str(s) for s in calibration.silent_opens)
        lines.append(f"  opens the pilot never showed: seq {seqs}")
    if calibration.uncovered:
        lines.append(f"  opens outside the file: {calibration.uncovered}")
    return lines


def _summary_line(unflagged: list[RampCheck]) -> str:
    gaps = [
        abs(c.ramp.believed_db - c.before_db)
        for c in unflagged
        if c.before_db is not None and not math.isinf(c.before_db) and not math.isinf(c.ramp.believed_db)
    ]
    jumps = [j for j in (_opposite_jump(c) for c in unflagged) if j is not None]
    if not gaps:
        return f"{len(unflagged)} ramps not flagged"
    largest = f"{max(jumps):+.2f} dB" if jumps else "none measured"
    return (
        f"{len(unflagged)} ramps not flagged: belief minus pilot median {statistics.median(gaps):.2f} dB, "
        f"max {max(gaps):.2f} dB; largest opposite-direction jump {largest}"
    )


def _verdict(report: Report) -> str:
    flagged, unchecked = report.flagged, report.unchecked
    if flagged:
        seqs = ", ".join(str(c.ramp.entry.seq) for c in flagged)
        return f"VERDICT: {len(flagged)} flagged (seq {seqs}); {len(unchecked)} unchecked"
    if unchecked:
        return f"VERDICT: none flagged, but {len(unchecked)} could not be checked"
    return f"VERDICT: all {len(report.checks)} ramps checked, none flagged"


def render(report: Report, *, tz: tzinfo | None, source: str | None = None) -> str:
    """The report as plain ASCII text."""
    layout = report.layout
    lines = [] if source is None else [f"pilot file: {source}"]
    lines.append(
        f"pilot covers project s {layout.start_seconds:.2f} to {layout.start_seconds + layout.duration_seconds:.2f}"
        + (" (TRUNCATED: the file ends early)" if layout.truncated else "")
    )
    lines.append(f"take: {report.take.anchor.description}; {len(report.take.entries)} entries")
    lines.extend(_calibration_lines(report.calibration, tz, report.take))
    counts = report.counts_by_kind
    lines.append("ramps: " + ", ".join(f"{counts[k]} {k}" for k in KINDS))
    rows = [_row(c, tz) for c in report.checks if c.flagged or c.unchecked]
    if rows:
        lines.append("")
        lines.extend(_table(rows))
        lines.append("")
    unflagged = [c for c in report.checks if not c.flagged and not c.unchecked]
    if unflagged:
        lines.append(_summary_line(unflagged))
    lines.append(_verdict(report))
    return "\n".join(lines) + "\n"
