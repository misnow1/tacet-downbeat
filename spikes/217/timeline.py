"""Where things are on the project timeline: log entries, the take start, the pilot."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import games
import numpy as np
from numpy.typing import NDArray

from tacet import annotations as ann
from tacet import pilot, taps
from tacet.annotations import Entry

#: An entry whose wall-minus-stamp is further than this from the median came from another playhead.
TAKE_OFFSET_TOLERANCE_S = 1.0
PILOT_MARGIN_S = 2.0
#: A fader that is not moving reads within this many dB.
PILOT_SETTLED_RANGE_DB = 1.0
OPEN_TAP_EVENTS = ("up-whistle", "up-drums", "up-slow")
CLOSE_EVENTS = ("out", "score-reversed")
COMMANDED = "commanded"


def wall_epoch(entry: Entry) -> float:
    return datetime.fromisoformat(entry.wall).timestamp()


def entry_position(entry: Entry) -> float:
    """Where the operator tapped, in project seconds (mirrors markers.position_of)."""
    assert entry.project_seconds is not None
    delay = taps.delay_of(entry.data)
    return entry.project_seconds - max(0.0, delay if delay is not None else 0.0)


@dataclass(frozen=True)
class TakeTiming:
    take_start_wall: float
    spread_s: float
    dropped: tuple[int, ...]


def trusted_entries(entries: list[Entry]) -> tuple[list[Entry], TakeTiming]:
    stamped = [e for e in entries if e.project_seconds is not None]
    offsets = {e.seq: wall_epoch(e) - float(e.project_seconds or 0.0) for e in stamped}
    median = statistics.median(offsets.values())
    kept = [e for e in stamped if abs(offsets[e.seq] - median) <= TAKE_OFFSET_TOLERANCE_S]
    kept_offsets = [offsets[e.seq] for e in kept]
    dropped = tuple(e.seq for e in stamped if e not in kept)
    unstamped = tuple(e.seq for e in entries if e.project_seconds is None)
    timing = TakeTiming(
        take_start_wall=statistics.median(kept_offsets),
        spread_s=max(kept_offsets) - min(kept_offsets),
        dropped=dropped + unstamped,
    )
    return kept, timing


@dataclass(frozen=True)
class Tap:
    seq: int
    event: str
    t: float
    entry: Entry


def pick(entries: list[Entry], events: tuple[str, ...]) -> list[Tap]:
    return [Tap(e.seq, e.event, entry_position(e), e) for e in entries if e.event in events]


@dataclass(frozen=True)
class PilotState:
    dbfs: NDArray[np.float32]
    step: float

    @property
    def closed(self) -> NDArray[np.bool_]:
        return self.dbfs < pilot.CLOSED_BELOW_DBFS

    @property
    def duration(self) -> float:
        return len(self.dbfs) * self.step

    def _span(self, start: float, end: float) -> tuple[int, int] | None:
        first, last = round(start / self.step), round(end / self.step) + 1
        if first < 0 or last > len(self.dbfs) or last <= first:
            return None
        return first, last

    def closed_on(self, start: float, end: float) -> bool:
        span = self._span(start, end)
        return span is not None and bool(self.closed[span[0] : span[1]].all())

    def open_on(self, start: float, end: float) -> bool:
        span = self._span(start, end)
        return span is not None and not bool(self.closed[span[0] : span[1]].any())

    def settled(self, start: float, end: float) -> bool:
        span = self._span(start, end)
        if span is None or not self.open_on(start, end):
            return False
        window = self.dbfs[span[0] : span[1]]
        return float(window.max() - window.min()) <= PILOT_SETTLED_RANGE_DB

    def level(self, start: float, end: float) -> float:
        span = self._span(start, end)
        return float("nan") if span is None else float(np.median(self.dbfs[span[0] : span[1]]))


def pilot_state(game: games.Game, *, refresh: bool = False) -> PilotState:
    games.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = games.CACHE_DIR / f"{game.id}-pilot.npz"
    if cache.exists() and not refresh:
        data = np.load(cache)
        return PilotState(data["dbfs"], float(data["step"]))
    _, env = pilot.read_envelope(games.track_path(game, games.PILOT))
    np.savez(cache, dbfs=env.dbfs, step=env.window_seconds)
    return PilotState(env.dbfs, env.window_seconds)


def runs(mask: NDArray[np.bool_], step: float, min_s: float) -> list[tuple[float, float]]:
    """(start, end) seconds of each True run at least min_s long."""
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    pairs = zip(edges[0::2], edges[1::2], strict=True)
    return [(a * step, b * step) for a, b in pairs if (b - a) * step >= min_s]


def read_log(game: games.Game) -> list[Entry]:
    return list(ann.read_entries(Path(game.log)))
