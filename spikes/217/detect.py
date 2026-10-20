"""Pure measures: arrays in, events out. No files, no game knowledge."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy import signal

CANNON_HIGHPASS_HZ = 40.0
CANNON_HIGHPASS_ORDER = 4
CANNON_FRAME_S = 0.01
CANNON_RISE_DB = 15.0
CANNON_MIN_CHANNELS = 10
CANNON_COINCIDENCE_S = 0.1
#: Rise is measured against frames [n - BASE_FAR, n - BASE_NEAR].
BASELINE_FAR_FRAMES = 60
BASELINE_NEAR_FRAMES = 10
EVENT_MERGE_S = 1.0
MIN_POWER = 1e-12

WHISTLE_NFFT = 1024
WHISTLE_HOP = 480
WHISTLE_BAND_HZ = (1500.0, 5000.0)
WHISTLE_PROMINENCE_DB = 15.0
WHISTLE_MIN_S = 0.15
WHISTLE_MAX_S = 3.0
WHISTLE_PEAK_SPREAD_HZ = 200.0
WHISTLE_STEP_HZ = 100.0
WHISTLE_MIC_START_S = 0.05
WHISTLE_MIC_FREQ_HZ = 100.0
WHISTLE_MIN_MICS = 2
MAG_FLOOR = 1e-9


def cannon_filter(x: NDArray[np.float32], rate: int) -> NDArray[np.float32]:
    sos = signal.butter(CANNON_HIGHPASS_ORDER, CANNON_HIGHPASS_HZ, btype="highpass", fs=rate, output="sos")
    return np.asarray(signal.sosfilt(sos, x), dtype=np.float32)


def frame_power_db(x: NDArray[np.float32], frame: int) -> NDArray[np.float32]:
    whole = len(x) // frame
    power = np.mean(x[: whole * frame].reshape(whole, frame) ** 2, axis=1)
    return np.asarray(10 * np.log10(np.maximum(power, MIN_POWER)), dtype=np.float32)


def rise_db(frames: NDArray[np.float32]) -> NDArray[np.float32]:
    """Each frame minus the median of frames [n-60, n-10]; zero where there is no baseline yet."""
    width = BASELINE_FAR_FRAMES - BASELINE_NEAR_FRAMES + 1
    windows = np.lib.stride_tricks.sliding_window_view(frames, width)
    base = np.median(windows, axis=1)
    out = np.zeros_like(frames)
    out[BASELINE_FAR_FRAMES:] = frames[BASELINE_FAR_FRAMES:] - base[: len(frames) - BASELINE_FAR_FRAMES]
    return out


@dataclass(frozen=True)
class CoincidenceEvent:
    frame: int
    channels: int
    median_rise_db: float
    spread_ms: float
    offsets_ms: list[float | None] = field(default_factory=list)


def onset_coincidence(
    frames_db: NDArray[np.float32],
    frame_s: float = CANNON_FRAME_S,
    *,
    rise_threshold_db: float = CANNON_RISE_DB,
    min_channels: int = CANNON_MIN_CHANNELS,
) -> list[CoincidenceEvent]:
    """frames_db[ch, n]: events where >= CANNON_MIN_CHANNELS channels start a >=15 dB rise within 0.1 s.

    Best first. Events closer than EVENT_MERGE_S keep only the better one.
    """
    rise = np.stack([rise_db(row) for row in frames_db])
    hot = rise >= rise_threshold_db
    edge = hot.copy()
    edge[:, 1:] &= ~hot[:, :-1]
    span = round(CANNON_COINCIDENCE_S / frame_s)
    cum = np.concatenate(
        [np.zeros((edge.shape[0], 1), dtype=np.int32), np.cumsum(edge, axis=1, dtype=np.int32)], axis=1
    )
    n = edge.shape[1]
    ahead = cum[:, np.minimum(np.arange(n) + span + 1, n)] - cum[:, :n]
    count = (ahead > 0).sum(axis=0)
    events: list[CoincidenceEvent] = []
    for start in np.flatnonzero((count >= min_channels) & (np.r_[0, count[:-1]] < min_channels)):
        events.append(_describe(rise, edge, int(start), span, frame_s, int(count[start:][: span + 1].max())))
    return _merge(events, frame_s)


def _describe(
    rise: NDArray[np.float32], edge: NDArray[np.bool_], start: int, span: int, frame_s: float, channels: int
) -> CoincidenceEvent:
    firsts: list[int | None] = []
    rises: list[float] = []
    for ch in range(edge.shape[0]):
        hits = np.flatnonzero(edge[ch, start : start + span + 1])
        firsts.append(start + int(hits[0]) if len(hits) else None)
        if len(hits):
            rises.append(float(rise[ch, start + int(hits[0])]))
    earliest = min(m for m in firsts if m is not None)
    offsets = [None if m is None else round((m - earliest) * frame_s * 1000, 1) for m in firsts]
    spread = max(o for o in offsets if o is not None)
    return CoincidenceEvent(earliest, channels, float(np.median(rises)), spread, offsets)


def _merge(events: list[CoincidenceEvent], frame_s: float) -> list[CoincidenceEvent]:
    ranked = sorted(events, key=lambda e: (e.channels, e.median_rise_db), reverse=True)
    kept: list[CoincidenceEvent] = []
    for ev in ranked:
        if all(abs(ev.frame - k.frame) * frame_s >= EVENT_MERGE_S for k in kept):
            kept.append(ev)
    return kept


def lowest_median_window(env_db: NDArray[np.float32], width: int) -> tuple[int, float]:
    """Start index and median of the window of `width` frames with the lowest median."""
    medians = np.median(np.lib.stride_tricks.sliding_window_view(env_db, width), axis=1)
    i = int(np.argmin(medians))
    return i, float(medians[i])


# -- whistle ---------------------------------------------------------------


@dataclass(frozen=True)
class WhistleFrames:
    prominence_db: NDArray[np.float32]
    peak_hz: NDArray[np.float32]


def whistle_frames(x: NDArray[np.float32], rate: int) -> WhistleFrames:
    """Per STFT frame: peak minus band median in 1.5-5 kHz, and where the peak is."""
    if len(x) < WHISTLE_NFFT:
        return WhistleFrames(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32))
    width = rate / WHISTLE_NFFT
    lo, hi = math.ceil(WHISTLE_BAND_HZ[0] / width), math.floor(WHISTLE_BAND_HZ[1] / width) + 1
    frames = np.lib.stride_tricks.sliding_window_view(x, WHISTLE_NFFT)[::WHISTLE_HOP]
    window = signal.windows.hann(WHISTLE_NFFT, sym=False).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * window, axis=1)[:, lo:hi])
    db = 20 * np.log10(mag + MAG_FLOOR)
    peak = db.max(axis=1)
    prominence = peak - np.median(db, axis=1)
    peak_hz = (np.argmax(db, axis=1) + lo) * width
    return WhistleFrames(prominence.astype(np.float32), peak_hz.astype(np.float32))


@dataclass(frozen=True)
class WhistleEvent:
    start: float
    end: float
    peak_hz: float
    prominence_db: float
    mics: int = 1

    @property
    def score(self) -> float:
        return self.prominence_db * math.sqrt(max(self.end - self.start, 0.0))


def whistle_events(frames: WhistleFrames, rate: int, t0: float) -> list[WhistleEvent]:
    """Runs of qualifying frames with a steady peak, 0.15 to 3 s long. t0 is frame 0's start."""
    hop_s = WHISTLE_HOP / rate
    ok = frames.prominence_db >= WHISTLE_PROMINENCE_DB
    found: list[WhistleEvent] = []
    i, n = 0, len(ok)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i + 1
        while j < n and ok[j] and abs(float(frames.peak_hz[j] - frames.peak_hz[j - 1])) <= WHISTLE_STEP_HZ:
            j += 1
        run = frames.peak_hz[i:j]
        dur = (j - i) * hop_s
        if WHISTLE_MIN_S <= dur <= WHISTLE_MAX_S and float(run.max() - run.min()) <= 2 * WHISTLE_PEAK_SPREAD_HZ:
            found.append(
                WhistleEvent(
                    t0 + i * hop_s, t0 + j * hop_s, float(np.median(run)), float(frames.prominence_db[i:j].mean())
                )
            )
        i = j
    return found


def agree_across_mics(per_mic: list[list[WhistleEvent]]) -> list[WhistleEvent]:
    """Events on >= 2 mics starting within 50 ms and within 100 Hz of each other."""
    pool = sorted(((ev, m) for m, evs in enumerate(per_mic) for ev in evs), key=lambda p: p[0].start)
    merged: list[WhistleEvent] = []
    used: set[int] = set()
    for a, (ev, m) in enumerate(pool):
        if a in used:
            continue
        group = {m: ev}
        members = [a]
        for b in range(a + 1, len(pool)):
            other, om = pool[b]
            if other.start - ev.start > WHISTLE_MIC_START_S:
                break
            if om not in group and abs(other.peak_hz - ev.peak_hz) <= WHISTLE_MIC_FREQ_HZ:
                group[om] = other
                members.append(b)
        if len(group) >= WHISTLE_MIN_MICS:
            used.update(members)
            evs = list(group.values())
            merged.append(
                WhistleEvent(
                    min(e.start for e in evs),
                    max(e.end for e in evs),
                    float(np.median([e.peak_hz for e in evs])),
                    float(np.mean([e.prominence_db for e in evs])),
                    len(evs),
                )
            )
    return merged
