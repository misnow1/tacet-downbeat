"""The candidate heuristics, one proposer per class. Each returns candidates in preference order."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import zip_longest

import audio
import detect
import games
import model
import numpy as np
import rtd_game
import timeline
from model import Candidate
from numpy.typing import NDArray

from tacet.annotations import Entry

FRAME_S = audio.FRAME_S
TAP_CLUSTER_S = 5.0
PILOT_MARGIN_S = timeline.PILOT_MARGIN_S

BAND_LOUD_INSET_S = 10.0
BAND_LOUD_CLIP_S = 15.0
BAND_LOUD_END_GUARD_S = 5.0
BAND_LOUD_SHORT_INSET_S = 5.0
BAND_LOUD_SHORT_CLIP_S = 12.0

PAT_DIP_S = 8.0
PAT_MIN_DROP_DB = 6.0
PAT_CLIP_PAD_S = 6.0
PAT_ENVELOPE_S = 0.25
PAT_GAME3_WINDOW = (15.0, 50.0)
PAT_GAME4_WINDOW = (-30.0, 8.0)
PAT_FALLBACK_AFTER_TD = (15.0, 45.0)
LOUD_PERCENTILE = 75

CROWD_PLAY_PAD = (3.0, 5.0)
CROWD_TAP_WINDOW = (14.0, 1.0)
CROWD_NO_OPEN_AFTER_S = 20.0
CROWD_NO_OPEN_RELAXED_S = 0.0
CROWD_WANTED = 2
CROWD_END_BEFORE_OPEN_S = 3.0
PROGRAM_ACTIVE_ABOVE_FLOOR_DB = 20.0
PROGRAM_ALWAYS_ON_FRACTION = 0.5
ANNOUNCER_QUIET_ABOVE_FLOOR_DB = 10.0
FLOOR_PERCENTILE = 10
PA_SMOOTH_S = 1.0
PA_MIN_RUN_S = 15.0
PA_CLIP_S = 12.0
MIN_FLOOR_DB = -120.0

CANNON_SNAP_BEFORE_S = 2.0
CANNON_NO_SNAP_BEFORE_S = 15.0
CANNON_SNAP_MAX_AGO_S = 40.0
CANNON_AFTER_S = 30.0
CANNON_G3_BEFORE_S = 15.0
CANNON_FALLBACK_CLIP = (-2.0, 18.0)
SAFETY_EXCLUDE = (-15.0, 150.0)
CANNON_MIN_CLIP_S = 5.0
CANNON_CLIP_PAD_S = 6.0
SAFETY_SEARCH_S = 600.0
RELAXED_RISE_DB = 10.0
RELAXED_CHANNELS = 8
RELAXED_SHARP_DB = 6.0
SEGMENT_S = 100.0
PREROLL_S = 2.0

WHISTLE_CLIP_PAD_S = 4.0
WHISTLE_PLAY_WINDOW = (4.0, 0.5)
WHISTLE_STOP_WINDOW = (3.0, 0.0)
WHISTLE_G3_RUN_MIN_S = 20.0
WHISTLE_G3_MARGIN_S = 3.0
WHISTLE_BEFORE_OPEN_S = 15.0
WHISTLE_SPACING_S = 600.0
#: Game 4's own cluster (43 of 51 events in the 2500 Hz bin): rank by distance outside it.
WHISTLE_CLUSTER_HZ = (2450.0, 3000.0)
WHISTLE_G3_TAP_WINDOW = (14.0, 0.4)
WHISTLE_HIST_HZ = 500.0
WHISTLE_CHUNK_S = 60.0
G4_PLAY_PILOT_PAD_S = 1.0

ENTRANCE_PRE_SILENCE_S = 8.0
ENTRANCE_OPEN_WITHIN_S = 1.5
ENTRANCE_POST_S = 6.0
ENTRANCE_PERCENTILES = (10, 50, 90)
TAIL_PERCENTILES = (10, 50, 90, 95, 99)
TAP_PROXY_AFTER_S = (0.4, 13.0)
SLOW_FRAME_STEP = 1


@dataclass
class Ctx:
    game: games.Game
    entries: list[Entry]
    take_start: float
    pilot: timeline.PilotState
    rtd: rtd_game.Replayed | None
    refresh: bool = False
    _power: dict[int, NDArray[np.float32]] = field(default_factory=dict)
    _tracks: dict[int, audio.Track] = field(default_factory=dict)

    def power(self, number: int) -> NDArray[np.float32]:
        if number not in self._power:
            self._power[number] = audio.cached_power(self.game, number, refresh=self.refresh)
        return self._power[number]

    def track(self, number: int) -> audio.Track:
        if number not in self._tracks:
            self._tracks[number] = audio.open_track(self.game, number)
        return self._tracks[number]

    @property
    def take_end(self) -> float:
        return min(len(self.power(n)) for n in games.MV_TRACKS) * FRAME_S

    def mic_mean_power(self) -> NDArray[np.float32]:
        n = min(len(self.power(t)) for t in games.MV_TRACKS)
        return np.mean([self.power(t)[:n] for t in games.MV_TRACKS], axis=0).astype(np.float32)

    def taps(self, events: tuple[str, ...]) -> list[timeline.Tap]:
        return sorted(timeline.pick(self.entries, events), key=lambda t: t.t)

    def halftime(self) -> float:
        marks = self.taps(("halftime-exodus",))
        return marks[0].t if marks else self.take_end / 2


def hhmmss(ctx: Ctx, t: float) -> str:
    return datetime.fromtimestamp(ctx.take_start + t, UTC).strftime("%H:%M:%SZ")


def frames(t0: float, t1: float) -> slice:
    return slice(max(0, round(t0 / FRAME_S)), max(0, round(t1 / FRAME_S)))


def db_of(power: NDArray[np.float32]) -> NDArray[np.float32]:
    return audio.to_db(power)


def spaced(items: list[float], count: int) -> list[int]:
    """Indices of `count` items spread over the list's time order (farthest-point from the middle)."""
    if not items:
        return []
    chosen = [int(np.argmin([abs(v - float(np.median(items))) for v in items]))]
    while len(chosen) < min(count, len(items)):
        dist = [min(abs(v - items[c]) for c in chosen) for v in items]
        chosen.append(int(np.argmax(dist)))
    return chosen


def clusters(taps: list[timeline.Tap]) -> list[list[timeline.Tap]]:
    out: list[list[timeline.Tap]] = []
    for tap in taps:
        if out and tap.t - out[-1][-1].t <= TAP_CLUSTER_S:
            out[-1].append(tap)
        else:
            out.append([tap])
    return out


def pctl(values: list[float] | NDArray[np.float32], qs: tuple[int, ...] = ENTRANCE_PERCENTILES) -> str:
    if len(values) == 0:
        return "none"
    return "/".join(f"{np.percentile(values, q):.1f}" for q in qs)


# -- band-loud ---------------------------------------------------------------


def band_loud_spans(ctx: Ctx, inset: float, length: float) -> list[tuple[float, float, float]]:
    opens = clusters(ctx.taps(timeline.OPEN_TAP_EVENTS))
    closes = ctx.taps(timeline.CLOSE_EVENTS)
    commanded = [timeline.entry_position(e) for e in ctx.entries if e.event == timeline.COMMANDED]
    level = db_of(ctx.mic_mean_power())
    eligible: list[tuple[float, float, float]] = []
    for cluster in opens:
        opened = cluster[-1].t
        out = next((c.t for c in closes if c.t > opened), None)
        if out is None:
            continue
        start = opened + inset
        end = start + length
        if end > out - BAND_LOUD_END_GUARD_S:
            continue
        if not ctx.pilot.settled(start - PILOT_MARGIN_S, end + PILOT_MARGIN_S):
            continue
        if any(start - PILOT_MARGIN_S <= c <= end + PILOT_MARGIN_S for c in commanded):
            continue
        eligible.append((start, end, float(np.median(level[frames(start, end)]))))
    return eligible


def propose_band_loud(ctx: Ctx) -> list[Candidate]:
    half = ctx.halftime()
    eligible = band_loud_spans(ctx, BAND_LOUD_INSET_S, BAND_LOUD_CLIP_S)
    first = [e for e in eligible if e[0] < half]
    second = [e for e in eligible if e[0] >= half]
    print(
        f"band-loud: halftime at {hhmmss(ctx, half)}; {len(eligible)} eligible spans "
        f"({len(first)} first half, {len(second)} second half); "
        f"level p10/p50/p90 dB = {pctl([e[2] for e in eligible])}"
    )
    if not second:
        extra = [e for e in band_loud_spans(ctx, BAND_LOUD_SHORT_INSET_S, BAND_LOUD_SHORT_CLIP_S) if e[0] >= half]
        print(
            f"band-loud: no second-half span; retried with {BAND_LOUD_SHORT_INSET_S:.0f} s inset and "
            f"{BAND_LOUD_SHORT_CLIP_S:.0f} s clip: {len(extra)} second-half spans"
        )
        second = extra
    picks = [max(part, key=lambda e: e[2]) for part in (first, second) if part]
    rest_first = sorted((e for e in first if e not in picks), key=lambda e: -e[2])
    rest_second = sorted((e for e in second if e not in picks), key=lambda e: -e[2])
    # Alternate the halves so a pick lost to an overlap is replaced from the same half where possible.
    rest = [e for pair in zip_longest(rest_second, rest_first) for e in pair if e is not None]
    labels = {id(p): ("first" if p[0] < half else "second") + "-half loudest" for p in picks}
    return [
        Candidate(
            ctx.game.id,
            model.BAND_LOUD,
            s,
            e,
            "taps+pilot",
            f"pilot open and settled {hhmmss(ctx, s)}; 13-mic median {lv:.1f} dB, "
            f"{labels.get(id(span), 'next loudest')}",
            {"level_db": round(lv, 2), "half": 1 if s < half else 2},
        )
        for span in [*picks, *rest]
        for s, e, lv in [span]
    ]


# -- pat-quiet ----------------------------------------------------------------


def td_windows(ctx: Ctx) -> list[tuple[float, float, float, str]]:
    """(td, window start, window end, source) for each home touchdown."""
    if ctx.rtd is None:
        return [(t.t, t.t + PAT_GAME3_WINDOW[0], t.t + PAT_GAME3_WINDOW[1], "tap") for t in ctx.taps(("touchdown",))]
    scores = rtd_game.home_scores(ctx.rtd, ctx.take_start)
    pats = [s for s in scores if s.kind == "PAT"]
    found = []
    for td in (s for s in scores if s.kind == "TD"):
        pat = next((p for p in pats if 0 < p.t - td.t <= rtd_game.PAT_WITHIN_S), None)
        if pat is None:
            found.append((td.t, td.t + PAT_FALLBACK_AFTER_TD[0], td.t + PAT_FALLBACK_AFTER_TD[1], "rtd-no-pat"))
        else:
            found.append((td.t, pat.t + PAT_GAME4_WINDOW[0], pat.t + PAT_GAME4_WINDOW[1], "rtd"))
    pat_gaps = [round(p.t - td.t, 1) for td in scores if td.kind == "TD" for p in pats if 0 < p.t - td.t < 90]
    print(f"pat-quiet: TD-to-PAT intervals (s) from RTD: {pat_gaps}")
    return found


def coarse(power: NDArray[np.float32], t0: float, t1: float) -> NDArray[np.float32]:
    """dB of the 13-mic sum at 250 ms over [t0, t1)."""
    per = round(PAT_ENVELOPE_S / FRAME_S)
    chunk = power[frames(t0, t1)]
    whole = len(chunk) // per
    return db_of(chunk[: whole * per].reshape(whole, per).mean(axis=1))


def propose_pat_quiet(ctx: Ctx) -> list[Candidate]:
    total = ctx.mic_mean_power() * len(games.MV_TRACKS)
    width = round(PAT_DIP_S / PAT_ENVELOPE_S)
    found: list[Candidate] = []
    for td, w0, w1, source in td_windows(ctx):
        env = coarse(total, w0, w1)
        if len(env) < width:
            print(f"pat-quiet: TD {hhmmss(ctx, td)}: window too short")
            continue
        medians = np.median(np.lib.stride_tricks.sliding_window_view(env, width), axis=1)
        allowed = np.array(
            [
                ctx.pilot.open_on(
                    w0 + i * PAT_ENVELOPE_S - PAT_CLIP_PAD_S, w0 + (i + width) * PAT_ENVELOPE_S + PAT_CLIP_PAD_S
                )
                for i in range(len(medians))
            ]
        )
        closed_note = ""
        if not allowed.any():
            allowed = np.ones(len(medians), dtype=bool)
            closed_note = "PILOT CLOSED in window: is the band still playing?"
            print(f"pat-quiet: TD {hhmmss(ctx, td)}: pilot closed in the whole window; proposing flagged")
        best = int(np.argmin(np.where(allowed, medians, np.inf)))
        loud = float(np.percentile(env, LOUD_PERCENTILE))
        dip = float(medians[best])
        drop = loud - dip
        s = w0 + best * PAT_ENVELOPE_S
        e = s + PAT_DIP_S
        weak = "" if drop >= PAT_MIN_DROP_DB else f"; WEAK: drop under {PAT_MIN_DROP_DB:.0f} dB"
        flag = f"{closed_note}; " if closed_note else ""
        print(
            f"pat-quiet: TD {hhmmss(ctx, td)} ({source}): loud p75 {loud:.1f} dB, "
            f"dip {dip:.1f} dB, drop {drop:.1f} dB, "
            f"dip at {hhmmss(ctx, s)} (+{s - td:.0f} s){weak}"
        )
        found.append(
            Candidate(
                ctx.game.id,
                model.PAT_QUIET,
                s - PAT_CLIP_PAD_S,
                e + PAT_CLIP_PAD_S,
                f"{source}+pilot",
                f"{flag}quietest {PAT_DIP_S:.0f} s after TD {hhmmss(ctx, td)}: {drop:.1f} dB under window p75{weak}",
                {
                    "td": round(td, 2),
                    "loud_p75_db": round(loud, 2),
                    "dip_db": round(dip, 2),
                    "drop_db": round(drop, 2),
                    "pilot_closed": bool(closed_note),
                },
            )
        )
    return found


# -- crowd -------------------------------------------------------------------


def program_floors(ctx: Ctx, tracks: tuple[int, ...]) -> dict[int, tuple[NDArray[np.float32], float]]:
    out = {}
    for n in tracks:
        db = smoothed_db(ctx.power(n))
        out[n] = (db, max(MIN_FLOOR_DB, float(np.percentile(db, FLOOR_PERCENTILE))))
    return out


def smoothed_db(power: NDArray[np.float32]) -> NDArray[np.float32]:
    k = round(PA_SMOOTH_S / FRAME_S)
    cs = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    avg = (cs[k:] - cs[:-k]) / k
    padded = np.concatenate((np.full(k // 2, avg[0]), avg, np.full(k - k // 2 - 1, avg[-1])))
    return db_of(padded.astype(np.float32))


def crowd_windows(ctx: Ctx) -> list[tuple[float, float]]:
    if ctx.rtd is None:
        found = []
        for t in ctx.taps(("first-down", "defensive-stop")):
            found.append((t.t - CROWD_TAP_WINDOW[0], t.t - CROWD_TAP_WINDOW[1]))
        return found
    plays = rtd_game.plays(ctx.rtd, ctx.take_start)
    taps = ctx.taps(("first-down", "defensive-stop"))
    lo, hi = TAP_PROXY_AFTER_S
    matched = sum(1 for t in taps if any(lo <= t.t - p.dead <= hi for p in plays))
    print(
        f"crowd: proxy check: {matched}/{len(taps)} first-down/defensive-stop taps fall "
        f"{lo}-{hi} s after an RTD play end"
        f" ({len(plays)} RTD plays)"
    )
    return [(p.snap - CROWD_PLAY_PAD[0], p.dead + CROWD_PLAY_PAD[1]) for p in plays]


def usable_program_floors(ctx: Ctx) -> dict[int, tuple[NDArray[np.float32], float]]:
    """Program feeds that are a music indicator: a feed 20 dB over its own floor for half the take is not."""
    usable = {}
    for n, (db, floor) in program_floors(ctx, games.PROGRAM_TRACKS).items():
        on = float((db >= floor + PROGRAM_ACTIVE_ABOVE_FLOOR_DB).mean())
        print(
            f"program feed {n}: p10/p50/p90/p95/p99 dBFS = {pctl(db, TAIL_PERCENTILES)}, floor {floor:.1f}, "
            f"above floor+20 dB {on:.0%} of the take"
        )
        if on >= PROGRAM_ALWAYS_ON_FRACTION:
            print(f"program feed {n}: always on, not used as an indicator")
        else:
            usable[n] = (db, floor)
    return usable


def shift_before_open(a: float, b: float, opens: list[timeline.Tap]) -> tuple[float, float]:
    """End the clip at least CROWD_END_BEFORE_OPEN_S before the next open tap, keeping its length."""
    nxt = next((o.t for o in opens if o.t > a), None)
    if nxt is None or nxt - CROWD_END_BEFORE_OPEN_S >= b:
        return a, b
    end = nxt - CROWD_END_BEFORE_OPEN_S
    return end - (b - a), end


def propose_crowd(ctx: Ctx) -> list[Candidate]:
    floors = usable_program_floors(ctx)
    opens = ctx.taps(timeline.OPEN_TAP_EVENTS)
    level = db_of(ctx.mic_mean_power())
    windows = crowd_windows(ctx)
    pa = program_floors(ctx, (games.MAIN_PA, games.ANNOUNCER))
    pa_margin = {games.MAIN_PA: PROGRAM_ACTIVE_ABOVE_FLOOR_DB, games.ANNOUNCER: ANNOUNCER_QUIET_ABOVE_FLOOR_DB}
    for after in (CROWD_NO_OPEN_AFTER_S, CROWD_NO_OPEN_RELAXED_S):
        eligible: list[tuple[float, float, float, str]] = []
        reasons: Counter[str] = Counter()
        for a, b in windows:
            if after == CROWD_NO_OPEN_RELAXED_S and ctx.rtd is None:
                a, b = shift_before_open(a, b, opens)
            if not ctx.pilot.closed_on(a - PILOT_MARGIN_S, b + PILOT_MARGIN_S):
                reasons["pilot not closed"] += 1
            elif any(b <= o.t <= b + after for o in opens) and after != CROWD_NO_OPEN_RELAXED_S:
                reasons[f"band opens within {after:.0f} s after"] += 1
            elif any(
                float(np.percentile(db[frames(a, b)], 90)) >= floor + PROGRAM_ACTIVE_ABOVE_FLOOR_DB
                for db, floor in floors.values()
            ):
                reasons["program feed active"] += 1
            else:
                active = [
                    name
                    for n, name in ((games.MAIN_PA, "main PA"), (games.ANNOUNCER, "announcer"))
                    if float(np.percentile(pa[n][0][frames(a, b)], 90)) >= pa[n][1] + pa_margin[n]
                ]
                note = f"PA active: {', '.join(active)}" if active else "PA quiet"
                eligible.append((a, b, float(np.median(level[frames(a, b)])), note))
        print(
            f"crowd (no-open-after {after:.0f} s): {len(eligible)} of {len(windows)} windows eligible; "
            f"rejected: {dict(reasons)}; level p10/p50/p90 dB = {pctl([e[2] for e in eligible])}"
        )
        if len(eligible) >= CROWD_WANTED:
            break
    if not eligible:
        return []
    loudest = max(eligible, key=lambda e: e[2])
    half = ctx.halftime()
    others = [e for e in eligible if e is not loudest and (e[0] < half) != (loudest[0] < half)] or [
        e for e in eligible if e is not loudest
    ]
    med = float(np.median([e[2] for e in eligible]))
    ordinary = sorted(others, key=lambda e: abs(e[2] - med))
    ordered = [loudest, *ordinary]
    return [
        Candidate(
            ctx.game.id,
            model.CROWD,
            a,
            b,
            "rtd-play" if ctx.rtd else "taps",
            f"pilot closed, no program feed; 13-mic median {lv:.1f} dB ({'loudest' if i == 0 else 'ordinary'}); {note}",
            {"level_db": round(lv, 2), "pa_note": note},
        )
        for i, (a, b, lv, note) in enumerate(ordered)
    ]


# -- crowd + main PA music ---------------------------------------------------


def propose_crowd_pa(ctx: Ctx) -> list[Candidate]:
    usable = usable_program_floors(ctx)
    tracks = (games.ANNOUNCER, *usable, games.MAIN_PA)
    env = program_floors(ctx, (games.ANNOUNCER, games.MAIN_PA)) | usable
    for n, (db, floor) in env.items():
        print(f"crowd-pa-music: track {n} p10/p50/p90/p95/p99 dBFS = {pctl(db, TAIL_PERCENTILES)}  floor {floor:.1f}")
    n = min(len(db) for db, _ in env.values())
    over = {k: env[k][0][:n] - env[k][1] for k in tracks}
    music = np.max([over[k] for k in usable], axis=0) >= PROGRAM_ACTIVE_ABOVE_FLOOR_DB
    main_on = over[games.MAIN_PA] >= PROGRAM_ACTIVE_ABOVE_FLOOR_DB
    speech_off = over[games.ANNOUNCER] < ANNOUNCER_QUIET_ABOVE_FLOOR_DB
    per = round(FRAME_S / ctx.pilot.step)
    closed = ctx.pilot.closed
    closed_frames = closed[: len(closed) // per * per].reshape(-1, per).all(axis=1)
    m = min(n, len(closed_frames))
    ok = (music & main_on & speech_off)[:m] & closed_frames[:m]
    print(
        f"crowd-pa-music: music {music.mean():.1%}, main on {main_on.mean():.1%}, speech off {speech_off.mean():.1%}, "
        f"all with pilot closed {ok.mean():.2%} of the take"
    )
    runs = timeline.runs(ok, FRAME_S, PA_MIN_RUN_S)
    scored = []
    for a, b in runs:
        sl = frames(a, b)
        src = max(usable, key=lambda k: float(np.median(over[k][sl])))
        scored.append((a, b, src))
    print(f"crowd-pa-music: {len(runs)} runs >= {PA_MIN_RUN_S:.0f} s; sources {dict(Counter(s[2] for s in scored))}")
    ranked = sorted(scored, key=lambda r: r[0] - r[1])
    ordered: list[tuple[float, float, int]] = []
    seen: set[int] = set()
    for r in ranked:
        if r[2] not in seen:
            ordered.append(r)
            seen.add(r[2])
    ordered += [r for r in ranked if r not in ordered]
    out = []
    for a, b, src in ordered:
        mid = (a + b) / 2
        out.append(
            Candidate(
                ctx.game.id,
                model.CROWD_PA,
                mid - PA_CLIP_S / 2,
                mid + PA_CLIP_S / 2,
                "audio+pilot",
                f"pilot closed, track {src} and Main PA above floor, announcer quiet, run {b - a:.0f} s",
                {"run": [round(a, 1), round(b, 1)], "source_track": src},
            )
        )
    return out


# -- cannon ------------------------------------------------------------------


@dataclass(frozen=True)
class CannonHit:
    t: float
    ev: detect.CoincidenceEvent


def read_frames(ctx: Ctx, t0: float, t1: float) -> NDArray[np.float32]:
    rows = []
    frame = round(detect.CANNON_FRAME_S * ctx.track(games.MV_TRACKS[0]).rate)
    for n in games.MV_TRACKS:
        tr = ctx.track(n)
        x = audio.read_window(tr, t0, t1)
        rows.append(detect.frame_power_db(detect.cannon_filter(x, tr.rate), frame))
    width = min(len(r) for r in rows)
    return np.stack([r[:width] for r in rows])


def cannon_scan(
    ctx: Ctx,
    t0: float,
    t1: float,
    *,
    rise_db: float = detect.CANNON_RISE_DB,
    channels: int = detect.CANNON_MIN_CHANNELS,
    sharp_db: float = detect.CANNON_SHARP_RISE_DB,
) -> tuple[list[CannonHit], float]:
    """Coincidence events in [t0, t1) best first, and the time of the loudest frame."""
    t0 = max(0.0, t0)
    hits: list[CannonHit] = []
    loudest = (-math.inf, t0)
    start = t0
    while start < t1:
        read0 = max(0.0, start - PREROLL_S)
        fr = read_frames(ctx, read0, min(t1, start + SEGMENT_S))
        for ev in detect.onset_coincidence(fr, rise_threshold_db=rise_db, min_channels=channels, sharp_db=sharp_db):
            t = read0 + ev.frame * detect.CANNON_FRAME_S
            if t >= start:
                hits.append(CannonHit(t, ev))
        total = (10 ** (fr / 10)).sum(axis=0)
        k = int(np.argmax(total[round((start - read0) / detect.CANNON_FRAME_S) :])) + round(
            (start - read0) / detect.CANNON_FRAME_S
        )
        if total[k] > loudest[0]:
            loudest = (float(total[k]), read0 + k * detect.CANNON_FRAME_S)
        start += SEGMENT_S
    hits.sort(key=lambda h: (h.ev.channels, h.ev.median_rise_db), reverse=True)
    return hits, loudest[1]


def since(t: float, score: float | None) -> str:
    return "" if score is None else f" ({t - score:+.1f} s from score)"


def describe_hit(ctx: Ctx, h: CannonHit, score: float | None = None) -> str:
    offs = ",".join("-" if o is None else f"{o:g}" for o in h.ev.offsets_ms)
    return (
        f"{hhmmss(ctx, h.t)}{since(h.t, score)} ch {h.ev.channels}/{len(games.MV_TRACKS)} "
        f"long rise {h.ev.median_rise_db:.1f} dB, sharp rise {h.ev.median_sharp_db:.1f} dB "
        f"spread {h.ev.spread_ms:.0f} ms offsets_ms[{offs}]"
    )


def cannon_windows(ctx: Ctx) -> list[tuple[str, float, float, float]]:
    """(label, score time, start, end) for each home scoring play."""
    if ctx.rtd is not None:
        snaps = rtd_game.snaps(ctx.rtd, ctx.take_start)
        out = []
        for s in (s for s in rtd_game.home_scores(ctx.rtd, ctx.take_start) if s.kind in ("TD", "FG")):
            snap = max((x for x in snaps if s.t - CANNON_SNAP_MAX_AGO_S <= x < s.t), default=None)
            a = s.t - CANNON_NO_SNAP_BEFORE_S if snap is None else snap - CANNON_SNAP_BEFORE_S
            out.append((f"{s.kind} {hhmmss(ctx, s.t)}", s.t, a, s.t + CANNON_AFTER_S))
        return out
    return [
        (f"{t.event} {hhmmss(ctx, t.t)}", t.t, t.t - CANNON_G3_BEFORE_S, t.t + CANNON_AFTER_S)
        for t in ctx.taps(("touchdown", "field-goal"))
    ]


def safety_window(ctx: Ctx) -> tuple[float, float] | None:
    if ctx.rtd is not None:
        return None
    seq = SAFETY_SEQ.get(ctx.game.id)
    ref = next((e for e in ctx.entries if e.seq == seq), None)
    if ref is None:
        return None
    t = timeline.entry_position(ref)
    return t - SAFETY_SEARCH_S, t + SAFETY_SEARCH_S


SAFETY_SEQ = {"game-3": 712}


def hit_evidence(h: CannonHit, score: float | None) -> dict[str, object]:
    ev: dict[str, object] = {
        "hit": True,
        "hit_t": round(h.t, 3),
        "channels": h.ev.channels,
        "median_rise_db": round(h.ev.median_rise_db, 2),
        "median_sharp_db": round(h.ev.median_sharp_db, 2),
        "spread_ms": h.ev.spread_ms,
    }
    if score is not None:
        ev["from_score_s"] = round(h.t - score, 2)
    return ev


def propose_cannon(ctx: Ctx) -> list[Candidate]:
    found: list[Candidate] = []
    source = "rtd" if ctx.rtd else "taps"
    windows = cannon_windows(ctx)
    for label, score, a, b in windows:
        hits, _ = cannon_scan(ctx, a, b)
        print(f"cannon: window {label} [{a - score:+.1f}, {b - score:+.1f}] s: {len(hits)} coincidence events")
        for h in hits[:3]:
            print("    " + describe_hit(ctx, h, score))
        if hits:
            top = hits[0]
            why = (
                f"{top.ev.channels}/13 mics onset within {top.ev.spread_ms:.0f} ms, long rise "
                f"{top.ev.median_rise_db:.1f} dB, sharp {top.ev.median_sharp_db:.1f} dB, near {label}"
            )
            found.append(
                Candidate(
                    ctx.game.id,
                    model.CANNON,
                    top.t - CANNON_CLIP_PAD_S,
                    top.t + CANNON_CLIP_PAD_S,
                    source,
                    why,
                    hit_evidence(top, score),
                )
            )
            continue
        relaxed, _ = cannon_scan(
            ctx, a, b, rise_db=RELAXED_RISE_DB, channels=RELAXED_CHANNELS, sharp_db=RELAXED_SHARP_DB
        )
        for h in relaxed[:3]:
            print("    relaxed: " + describe_hit(ctx, h, score))
        if relaxed:
            ev = hit_evidence(relaxed[0], score) | {"hit": False, "relaxed_hit": True}
            why = (
                f"no strict hit; relaxed ({RELAXED_RISE_DB:.0f} dB, {RELAXED_SHARP_DB:.0f} dB sharp, "
                f"{RELAXED_CHANNELS} mics) hit {relaxed[0].ev.channels}/13 mics near {label}"
            )
            t0, t1 = relaxed[0].t - CANNON_CLIP_PAD_S, relaxed[0].t + CANNON_CLIP_PAD_S
        else:
            ev = {"hit": False}
            why = f"no coincidence hit, strict or relaxed; 20 s from the score at {label}"
            t0, t1 = score + CANNON_FALLBACK_CLIP[0], score + CANNON_FALLBACK_CLIP[1]
        found.append(Candidate(ctx.game.id, model.CANNON, t0, t1, source, why, ev))
    safety = safety_window(ctx)
    if safety is not None:
        scoring = [(sc + SAFETY_EXCLUDE[0], sc + SAFETY_EXCLUDE[1]) for _, sc, _, _ in windows]
        tier = "strict"
        hits, _ = cannon_scan(ctx, *safety)
        hits = [h for h in hits if not any(a <= h.t <= b for a, b in scoring)]
        if not hits:
            tier = "relaxed"
            loose, _ = cannon_scan(
                ctx, *safety, rise_db=RELAXED_RISE_DB, channels=RELAXED_CHANNELS, sharp_db=RELAXED_SHARP_DB
            )
            hits = [h for h in loose if not any(a <= h.t <= b for a, b in scoring)]
        print(
            f"cannon: safety search {hhmmss(ctx, safety[0])}..{hhmmss(ctx, safety[1])}: "
            f"{len(hits)} {tier} coincidence events outside score-15..+150 s"
        )
        for h in hits[:5]:
            print("    " + describe_hit(ctx, h))
        if hits:
            top = hits[0]
            found.append(
                Candidate(
                    ctx.game.id,
                    model.CANNON,
                    top.t - CANNON_CLIP_PAD_S,
                    top.t + CANNON_CLIP_PAD_S,
                    "safety-search",
                    f"top {tier} coincidence event in the safety search window: {top.ev.channels}/13 mics, "
                    f"long rise {top.ev.median_rise_db:.1f} dB, sharp {top.ev.median_sharp_db:.1f} dB",
                    hit_evidence(top, None) | {"safety_search": True},
                )
            )
        else:
            print("cannon: safety search: no coincidence hit, no candidate proposed")
    return found


# -- officials' whistle ------------------------------------------------------


def whistle_scan(ctx: Ctx, a: float, b: float) -> list[detect.WhistleEvent]:
    a = max(0.0, a)
    per_mic: list[list[detect.WhistleEvent]] = []
    for n in games.WHISTLE_TRACKS:
        tr = ctx.track(n)
        pieces = []
        t = a
        while t < b:
            end = min(b, t + WHISTLE_CHUNK_S)
            x = audio.read_window(tr, t, end + (detect.WHISTLE_NFFT - detect.WHISTLE_HOP) / tr.rate)
            pieces.append(detect.whistle_frames(x, tr.rate))
            t = end
        prom = np.concatenate([p.prominence_db for p in pieces]) if pieces else np.zeros(0, dtype=np.float32)
        peak = np.concatenate([p.peak_hz for p in pieces]) if pieces else np.zeros(0, dtype=np.float32)
        per_mic.append(detect.whistle_events(detect.WhistleFrames(prom, peak), tr.rate, a))
    return detect.agree_across_mics(per_mic)


def propose_whistle(ctx: Ctx) -> list[Candidate]:
    events: list[tuple[detect.WhistleEvent, str]] = []
    if ctx.rtd is not None:
        plays = rtd_game.plays(ctx.rtd, ctx.take_start)
        stops = rtd_game.clock_stops(ctx.rtd, ctx.take_start)
        windows = [(p.dead - WHISTLE_PLAY_WINDOW[0], p.dead + WHISTLE_PLAY_WINDOW[1], "play-end") for p in plays]
        windows += [(s - WHISTLE_STOP_WINDOW[0], s - WHISTLE_STOP_WINDOW[1], "clock-stop") for s in stops]
        searched = [w for w in windows if ctx.pilot.closed_on(w[0] - G4_PLAY_PILOT_PAD_S, w[1] + G4_PLAY_PILOT_PAD_S)]
        play_hits = 0
        play_searched = 0
        for a, b, kind in searched:
            evs = whistle_scan(ctx, a, b)
            events += [(e, kind) for e in evs]
            if kind == "play-end":
                play_searched += 1
                play_hits += bool(evs)
        stop_n = sum(1 for w in searched if w[2] == "clock-stop")
        print(
            f"whistle: searched {play_searched}/{len(plays)} play ends and {stop_n}/{len(stops)} clock stops "
            f"(pilot closed); "
            f"{play_hits} play ends with a hit ({play_hits / max(play_searched, 1):.0%}); {len(events)} events"
        )
    else:
        closed = ctx.pilot.closed
        opens = ctx.taps(timeline.OPEN_TAP_EVENTS)
        scanned = 0.0
        for a, b in timeline.runs(closed, ctx.pilot.step, WHISTLE_G3_RUN_MIN_S):
            a, b = a + WHISTLE_G3_MARGIN_S, min(b, ctx.take_end) - WHISTLE_G3_MARGIN_S
            if b - a <= 0:
                continue
            scanned += b - a
            for e in whistle_scan(ctx, a, b):
                if not any(0 <= o.t - e.start <= WHISTLE_BEFORE_OPEN_S for o in opens):
                    events.append((e, "closed-run"))
        print(
            f"whistle: scanned {scanned / 3600:.2f} h of pilot-closed time; {len(events)} events "
            f"({len(events) / max(scanned / 3600, 1e-9):.1f}/h)"
        )
    hist = Counter(int(e.peak_hz // WHISTLE_HIST_HZ * WHISTLE_HIST_HZ) for e, _ in events)
    print(f"whistle: peak-frequency histogram (Hz bin: count) {dict(sorted(hist.items()))}")
    play_taps = ctx.taps(("first-down", "defensive-stop")) if ctx.rtd is None else []

    def rank(p: tuple[detect.WhistleEvent, str]) -> tuple[bool, float, float]:
        e = p[0]
        near_tap = any(-WHISTLE_G3_TAP_WINDOW[0] <= e.start - t.t <= -WHISTLE_G3_TAP_WINDOW[1] for t in play_taps)
        away = max(WHISTLE_CLUSTER_HZ[0] - e.peak_hz, e.peak_hz - WHISTLE_CLUSTER_HZ[1], 0.0)
        return (not near_tap and ctx.rtd is None, away, -e.score)

    ranked = sorted(events, key=rank)
    chosen: list[tuple[detect.WhistleEvent, str]] = []
    for e, kind in ranked:
        if all(abs(e.start - c.start) >= WHISTLE_SPACING_S for c, _ in chosen):
            chosen.append((e, kind))
    return [
        Candidate(
            ctx.game.id,
            model.WHISTLE,
            e.start - WHISTLE_CLIP_PAD_S,
            e.end + WHISTLE_CLIP_PAD_S,
            f"audio+{kind}",
            f"narrowband {e.peak_hz:.0f} Hz, {e.end - e.start:.2f} s, "
            f"prominence {e.prominence_db:.1f} dB on {e.mics} mics, pilot closed",
            {"peak_hz": round(e.peak_hz, 1), "prominence_db": round(e.prominence_db, 2), "mics": e.mics, "kind": kind},
        )
        for e, kind in chosen
    ]


# -- entrances ---------------------------------------------------------------


def entrance_clusters(ctx: Ctx) -> list[tuple[str, list[timeline.Tap]]]:
    out: list[tuple[str, list[timeline.Tap]]] = []
    for cluster in clusters(ctx.taps(timeline.OPEN_TAP_EVENTS)):
        labelled = [t for t in cluster if t.event in ("up-drums", "up-whistle")]
        if not labelled:
            continue
        first, last = cluster[0].t, cluster[-1].t
        if not ctx.pilot.closed_on(first - ENTRANCE_PRE_SILENCE_S, first - 0.1):
            continue
        if not ctx.pilot.open_on(last + ENTRANCE_OPEN_WITHIN_S, last + ENTRANCE_OPEN_WITHIN_S):
            continue
        out.append((labelled[-1].event, cluster))
    return out


def propose_entrances(ctx: Ctx) -> list[Candidate]:
    found = entrance_clusters(ctx)
    counts = Counter(label for label, _ in found)
    print(f"entrances: {len(found)} eligible clusters {dict(counts)}")
    passed = relaxed_passed = 0
    for _, cluster in found:
        a, b = cluster[0].t - ENTRANCE_PRE_SILENCE_S, cluster[-1].t + ENTRANCE_POST_S
        hits, _ = cannon_scan(ctx, a, b)
        passed += bool(hits)
        if not hits:
            loose, _ = cannon_scan(
                ctx, a, b, rise_db=RELAXED_RISE_DB, channels=RELAXED_CHANNELS, sharp_db=RELAXED_SHARP_DB
            )
            relaxed_passed += bool(loose)
    print(
        f"entrances: cannon detector passes {passed}/{len(found)} entrance windows strict; "
        f"{passed + relaxed_passed}/{len(found)} at the relaxed tier"
    )
    out: list[Candidate] = []
    for label, cls in (("up-drums", model.ENTRANCE_DRUMS), ("up-whistle", model.ENTRANCE_WHISTLE)):
        pool = [c for lab, c in found if lab == label]
        corrected = [c for c in pool if label == "up-drums" and c[0].event == "up-whistle"]
        times = [c[0].t for c in pool]
        picks = spaced(times, len(pool))
        order = [pool[i] for i in picks]
        if corrected:
            order = [corrected[len(corrected) // 2], *[c for c in order if c is not corrected[len(corrected) // 2]]]
        for c in order:
            first, last = c[0].t, c[-1].t
            note = "corrected whistle->drums" if c in corrected else ("single tap" if len(c) == 1 else "multi-tap")
            out.append(
                Candidate(
                    ctx.game.id,
                    cls,
                    first - ENTRANCE_PRE_SILENCE_S,
                    last + ENTRANCE_POST_S,
                    "taps+pilot",
                    f"{label} at {hhmmss(ctx, last)} ({note}), pilot closed before, open after",
                    {"taps": [t.event for t in c], "first": round(first, 2), "last": round(last, 2)},
                )
            )
    return out


PROPOSERS: dict[str, Callable[[Ctx], list[Candidate]]] = {
    model.BAND_LOUD: propose_band_loud,
    model.PAT_QUIET: propose_pat_quiet,
    model.CROWD: propose_crowd,
    model.CROWD_PA: propose_crowd_pa,
    model.CANNON: propose_cannon,
    model.WHISTLE: propose_whistle,
    "entrances": propose_entrances,
}
