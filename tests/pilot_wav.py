"""Synthetic pilot recordings and log entries for the pilot check's tests.

Nothing here is a recording. A game is gigabytes and never goes in git
(CLAUDE.md, Data), so the WAV files are built in memory from a few seconds of
sine, and the envelopes are painted straight onto the 10 ms grid.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any

import numpy as np

from tacet import dm7
from tacet.annotations import Entry
from tacet.pilot import SILENCE_FLOOR_DBFS, WINDOW_SECONDS, Envelope

#: bext's fixed part, before any coding history (EBU Tech 3285).
BEXT_BYTES = 602
_TIME_REFERENCE_AT = 338
_FULL_SCALE = 1 << 23
_WORD = 32


def pcm24(samples: Sequence[int]) -> bytes:
    return b"".join(int(s).to_bytes(3, "little", signed=True) for s in samples)


def _chunk(chunk_id: bytes, body: bytes) -> bytes:
    pad = b"\x00" if len(body) % 2 else b""
    return chunk_id + struct.pack("<I", len(body)) + body + pad


def wav_bytes(
    pcm: bytes,
    *,
    rate: int,
    channels: int = 1,
    bits: int = 24,
    time_reference: int | None = 0,
    chunks_before_data: Sequence[tuple[bytes, bytes]] = (),
    format_tag: int = 1,
    declared_data_bytes: int | None = None,
) -> bytes:
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", format_tag, channels, rate, rate * block, block, bits)
    body = _chunk(b"fmt ", fmt)
    if time_reference is not None:
        bext = bytearray(BEXT_BYTES)
        bext[_TIME_REFERENCE_AT : _TIME_REFERENCE_AT + 8] = struct.pack(
            "<II", time_reference & 0xFFFFFFFF, time_reference >> _WORD
        )
        body += _chunk(b"bext", bytes(bext))
    for chunk_id, payload in chunks_before_data:
        body += _chunk(chunk_id, payload)
    size = len(pcm) if declared_data_bytes is None else declared_data_bytes
    body += b"data" + struct.pack("<I", size) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def tone(segments: Sequence[tuple[float, float | None]], *, rate: int, freq: float = 1000.0) -> bytes:
    """24-bit PCM: each segment is (seconds, sine-peak dBFS), None for silence."""
    out: list[np.ndarray] = []
    phase = 0
    for seconds, dbfs in segments:
        count = round(seconds * rate)
        if dbfs is None:
            out.append(np.zeros(count, dtype=np.int64))
        else:
            amplitude = 10 ** (dbfs / 20) * (_FULL_SCALE - 1)
            t = (np.arange(count) + phase) / rate
            out.append(np.round(amplitude * np.sin(2 * math.pi * freq * t)).astype(np.int64))
        phase += count
    return pcm24(np.concatenate(out).tolist())


def paint_envelope(
    segments: Sequence[tuple[float, float | None]],
    *,
    dt: float = WINDOW_SECONDS,
    start_seconds: float = 0.0,
) -> Envelope:
    """An envelope with each segment's (seconds, dBFS) laid on the window grid."""
    values: list[float] = []
    for seconds, dbfs in segments:
        values.extend([SILENCE_FLOOR_DBFS if dbfs is None else dbfs] * round(seconds / dt))
    return Envelope(dbfs=np.array(values, dtype=np.float32), window_seconds=dt, start_seconds=start_seconds)


def commanded(
    seq: int,
    wall: str,
    ps: float | None,
    command: str,
    detail: str | None,
    level: int,
    target: int,
    delivered: bool | None = True,
) -> Entry:
    """A `commanded` entry in the shape the box writes (app.py)."""
    return Entry(
        seq=seq,
        event="commanded",
        category="fader",
        kind="instant",
        label="Commanded",
        wall=wall,
        monotonic=float(seq),
        project_seconds=ps,
        data={
            "level": level,
            "db": None if level == dm7.MINUS_INF else dm7.to_db(level),
            "target": target,
            "target_db": None if target == dm7.MINUS_INF else dm7.to_db(target),
            "command": command,
            "source": "operator",
            "detail": detail,
            "state": "open",
            "delivered": delivered,
            "skipped_steps": 0,
            "worst_lateness": None,
        },
    )


def plain(seq: int, wall: str, event: str, ps: float | None = None, **data: Any) -> Entry:
    return Entry(
        seq=seq,
        event=event,
        category="recorder",
        kind="instant",
        label=event,
        wall=wall,
        monotonic=float(seq),
        project_seconds=ps,
        data=dict(data),
    )


class Canvas:
    """An envelope painted by hand: hold a level, ramp between levels.

    Levels are pilot dBFS; `None` is silence. Times are project seconds.
    """

    def __init__(self, seconds: float, *, dt: float = WINDOW_SECONDS) -> None:
        self.dt = dt
        self.dbfs = np.full(round(seconds / dt), SILENCE_FLOOR_DBFS, dtype=np.float64)

    def _index(self, t: float) -> int:
        return round(t / self.dt)

    def hold(self, start: float, end: float, dbfs: float | None) -> None:
        self.dbfs[self._index(start) : self._index(end)] = SILENCE_FLOOR_DBFS if dbfs is None else dbfs

    def ramp(self, start: float, end: float, from_dbfs: float, to_dbfs: float) -> None:
        first, last = self._index(start), self._index(end)
        self.dbfs[first:last] = np.linspace(from_dbfs, to_dbfs, last - first, endpoint=False)

    def envelope(self) -> Envelope:
        return Envelope(dbfs=self.dbfs.astype(np.float32), window_seconds=self.dt, start_seconds=0.0)
