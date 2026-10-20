"""Streaming reads of the mono 24-bit tracks. Never loads a whole track.

The top 16 bits of each sample are read straight off the map, which costs no
decode. That is plenty for the band, crowd and program feeds. NEVER use it on
the pilot: tiny negative samples truncate to -1 LSB (about -90 dBFS) and read as
open. The pilot goes through tacet.pilot.read_envelope, which is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import games
import numpy as np
from numpy.typing import NDArray

from tacet import wav

FRAME_S = 0.05
CHUNK_S = 60.0
INT16_SCALE = 32768.0
MIN_POWER = 1e-12
MIN_DB = -120.0
_SAMPLE_DTYPE = np.dtype([("lo", "u1"), ("hi", "<i2")])


@dataclass(frozen=True)
class Track:
    number: int
    path: Path
    layout: wav.WavLayout
    samples: np.memmap

    @property
    def rate(self) -> int:
        return self.layout.sample_rate

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def duration(self) -> float:
        return self.n_samples / self.rate


def mic_memmap(path: Path, layout: wav.WavLayout) -> np.memmap:
    count = layout.data_bytes // layout.frame_bytes
    return np.memmap(path, dtype=_SAMPLE_DTYPE, mode="r", offset=layout.data_offset, shape=(count,))


def open_track(game: games.Game, number: int) -> Track:
    path = games.track_path(game, number)
    layout = games.read_layout(path)
    return Track(number, path, layout, mic_memmap(path, layout))


def read_window(track: Track, start_s: float, end_s: float) -> NDArray[np.float32]:
    """Samples in [start_s, end_s) as float32 fractions of full scale (top 16 bits)."""
    first = max(0, round(start_s * track.rate))
    last = min(track.n_samples, round(end_s * track.rate))
    if last <= first:
        return np.zeros(0, dtype=np.float32)
    return np.asarray(track.samples["hi"][first:last], dtype=np.float32) / np.float32(INT16_SCALE)


def power_frames(track: Track) -> NDArray[np.float32]:
    """Mean square per FRAME_S, as linear power, for the whole track (chunked)."""
    frame = round(FRAME_S * track.rate)
    whole = track.n_samples // frame
    per_chunk = round(CHUNK_S / FRAME_S)
    out = np.empty(whole, dtype=np.float32)
    for first in range(0, whole, per_chunk):
        last = min(whole, first + per_chunk)
        x = np.asarray(track.samples["hi"][first * frame : last * frame], dtype=np.float32) / np.float32(INT16_SCALE)
        out[first:last] = np.mean(x.reshape(last - first, frame) ** 2, axis=1)
    return out


def to_db(power: NDArray[np.floating]) -> NDArray[np.float32]:
    return np.asarray(10 * np.log10(np.maximum(power, MIN_POWER)), dtype=np.float32)


def cached_power(game: games.Game, number: int, *, refresh: bool = False) -> NDArray[np.float32]:
    games.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = games.CACHE_DIR / f"{game.id}-power-{number:02d}.npy"
    if cache.exists() and not refresh:
        return np.load(cache)
    power = power_frames(open_track(game, number))
    np.save(cache, power)
    return power
