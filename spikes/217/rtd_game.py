"""Game 4's scoreboard (RTD) as plays, clock stops and scores, on the project timeline."""

from __future__ import annotations

import pickle
from dataclasses import dataclass

import games

from tacet import pcap, rtd, rtd_replay

#: The play clock ticks 40 -> 39 about this long after the dead ball.
PLAY_CLOCK_TICK_LAG_S = 1.0
PLAY_CLOCK_FULL = 40
PLAY_MIN_S = 1.5
PLAY_MAX_S = 15.0
PAT_WITHIN_S = 90.0
REVERSED_WITHIN_S = 30.0
POINTS_TD, POINTS_FG, POINTS_PAT = 6, 3, 1
STOPPED_FLAG = "s"


@dataclass(frozen=True)
class FieldAt:
    t: float
    name: str
    old: str | None
    new: str


@dataclass(frozen=True)
class Replayed:
    fields: list[FieldAt]
    outages: list[tuple[float, float]]
    sources: list[float]


@dataclass(frozen=True)
class Play:
    snap: float
    dead: float


@dataclass(frozen=True)
class Score:
    t: float
    kind: str
    delta: int


def replay(game: games.Game, *, refresh: bool = False) -> Replayed:
    assert game.pcap is not None
    cache = games.CACHE_DIR / f"{game.id}-rtd.pkl"
    games.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not refresh:
        loaded: Replayed = pickle.loads(cache.read_bytes())
        return loaded
    rep = rtd_replay.Replay()
    fields: list[FieldAt] = []
    outages: list[tuple[float, float]] = []
    sources: list[float] = []
    with game.pcap.open("rb") as handle:
        for item in pcap.Capture(handle):
            for ev in rep.feed(item):
                if isinstance(ev, rtd_replay.FieldEvent):
                    c = ev.change
                    fields.append(FieldAt(ev.time, c.field.name, c.old, c.new))
                elif isinstance(ev, rtd_replay.GapEvent):
                    outages.append((ev.since, ev.time))
                elif isinstance(ev, rtd_replay.SourceEvent):
                    sources.append(ev.time)
    result = Replayed(fields, outages, sources)
    cache.write_bytes(pickle.dumps(result))
    return result


def _clock(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _touches_break(a: float, b: float, rep: Replayed) -> bool:
    if any(a < end and b > start for start, end in rep.outages):
        return True
    return any(a <= t <= b for t in rep.sources)


def plays(rep: Replayed, take_start: float) -> list[Play]:
    """snap = play clock jumps to 40 from 1-39; dead = next 40 -> 39 tick, less the tick lag."""
    clock = [f for f in rep.fields if f.name == rtd.PLAY_CLOCK.name]
    found: list[Play] = []
    for i, f in enumerate(clock):
        old, new = _clock(f.old), _clock(f.new)
        if new != PLAY_CLOCK_FULL or old is None or not 1 <= old < PLAY_CLOCK_FULL:
            continue
        tick = next(
            (g for g in clock[i + 1 :] if _clock(g.old) == PLAY_CLOCK_FULL and _clock(g.new) == PLAY_CLOCK_FULL - 1),
            None,
        )
        if tick is None:
            continue
        dead = tick.t - PLAY_CLOCK_TICK_LAG_S
        if PLAY_MIN_S <= dead - f.t <= PLAY_MAX_S and not _touches_break(f.t, tick.t, rep):
            found.append(Play(f.t - take_start, dead - take_start))
    return found


def clock_stops(rep: Replayed, take_start: float) -> list[float]:
    return [
        f.t - take_start
        for f in rep.fields
        if f.name == rtd.MAIN_CLOCK_STOPPED.name and f.new.strip() == STOPPED_FLAG and (f.old or "").strip() == ""
    ]


def home_scores(rep: Replayed, take_start: float) -> list[Score]:
    changes: list[tuple[float, int, int]] = []
    for f in rep.fields:
        if f.name != rtd.HOME_SCORE.name:
            continue
        old, new = _clock(f.old), _clock(f.new)
        if old is not None and new is not None:
            changes.append((f.t, old, new))
    found: list[Score] = []
    last_td: float | None = None
    for i, (t, old, new) in enumerate(changes):
        delta = new - old
        reversed_ = any(t < u <= t + REVERSED_WITHIN_S and n <= old for u, _, n in changes[i + 1 :])
        if delta <= 0 or reversed_:
            continue
        if delta == POINTS_TD:
            kind, last_td = "TD", t
        elif delta == POINTS_FG:
            kind = "FG"
        elif delta == POINTS_PAT and last_td is not None and t - last_td <= PAT_WITHIN_S:
            kind = "PAT"
        else:
            kind = f"+{delta}"
        found.append(Score(t - take_start, kind, delta))
    return found
