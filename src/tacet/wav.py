"""Find where the samples are in a WAV file, without reading them.

Standard library only. A Reaper take is a BWF file: `fmt `, then a `bext`
chunk, then a `junk` pad, then `data`, but the offsets are not assumed. A game
is about 2.5 GB per track, so this walks the chunk headers and stops at `data`;
the caller maps the samples itself.

The BWF `bext` chunk's TimeReference is the file's first sample counted from
the project's zero. That is what places a recording on the same timeline as the
log's `project_seconds` (#191), so a file without one is refused rather than
guessed at.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

RIFF_ID = b"RIFF"
RF64_ID = b"RF64"
WAVE_ID = b"WAVE"
FMT_ID = b"fmt "
BEXT_ID = b"bext"
DATA_ID = b"data"

#: Offset of the 64-bit TimeReference inside the bext chunk's data (EBU Tech
#: 3285): after Description 256, Originator 32, OriginatorReference 32,
#: OriginationDate 10 and OriginationTime 8.
BEXT_TIME_REFERENCE_OFFSET = 256 + 32 + 32 + 10 + 8
_BEXT_TIME_REFERENCE_FORMAT = "<II"

PCM_FORMAT = 1
REQUIRED_CHANNELS = 1
REQUIRED_BITS = 24

_FILE_HEADER = struct.Struct("<4sI4s")
_CHUNK_HEADER = struct.Struct("<4sI")
_FMT_BODY = struct.Struct("<HHIIHH")
_WORD_BITS = 32
_BITS_PER_BYTE = 8


class WavFormatError(ValueError):
    """The file is not one this tool can place on the project timeline."""


@dataclass(frozen=True)
class WavLayout:
    sample_rate: int
    channels: int
    bits: int
    data_offset: int
    data_bytes: int
    #: First sample's position on the project timeline, in samples (bext).
    time_reference: int
    #: The data chunk claimed more bytes than the file holds (a take still being
    #: written, or cut short); `data_bytes` is what is actually there.
    truncated: bool

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.bits // _BITS_PER_BYTE

    @property
    def start_seconds(self) -> float:
        return self.time_reference / self.sample_rate

    @property
    def duration_seconds(self) -> float:
        return (self.data_bytes // self.frame_bytes) / self.sample_rate


@dataclass(frozen=True)
class _Fmt:
    tag: int
    channels: int
    sample_rate: int
    bits: int


def _parse_fmt(body: bytes) -> _Fmt:
    if len(body) < _FMT_BODY.size:
        raise WavFormatError(f"fmt chunk is {len(body)} bytes, too short to read")
    tag, channels, rate, _byte_rate, _align, bits = _FMT_BODY.unpack_from(body)
    return _Fmt(tag=tag, channels=channels, sample_rate=rate, bits=bits)


def _parse_time_reference(body: bytes) -> int:
    end = BEXT_TIME_REFERENCE_OFFSET + struct.calcsize(_BEXT_TIME_REFERENCE_FORMAT)
    if len(body) < end:
        raise WavFormatError(f"bext chunk is {len(body)} bytes, too short to hold a TimeReference")
    low, high = struct.unpack_from(_BEXT_TIME_REFERENCE_FORMAT, body, BEXT_TIME_REFERENCE_OFFSET)
    return int(low) | (int(high) << _WORD_BITS)


def _check_header(handle: BinaryIO) -> None:
    head = handle.read(_FILE_HEADER.size)
    if len(head) < _FILE_HEADER.size:
        raise WavFormatError("file is too short to be a WAV")
    riff, _size, wave = _FILE_HEADER.unpack(head)
    if riff == RF64_ID:
        raise WavFormatError("RF64 files are not supported; the take is over 4 GB")
    if riff != RIFF_ID or wave != WAVE_ID:
        raise WavFormatError("not a RIFF/WAVE file")


def _check_format(fmt: _Fmt) -> None:
    if fmt.tag != PCM_FORMAT:
        raise WavFormatError(f"format tag {fmt.tag}, not PCM ({PCM_FORMAT})")
    if fmt.channels != REQUIRED_CHANNELS:
        raise WavFormatError(f"{fmt.channels} channels, not mono; the pilot is one channel")
    if fmt.bits != REQUIRED_BITS:
        raise WavFormatError(f"{fmt.bits}-bit, not {REQUIRED_BITS}-bit")
    if fmt.sample_rate <= 0:
        raise WavFormatError(f"sample rate {fmt.sample_rate} is not usable")


def read_layout(handle: BinaryIO, file_size: int) -> WavLayout:
    """Walk the chunks up to `data` and say where the samples are."""
    _check_header(handle)
    fmt: _Fmt | None = None
    time_reference: int | None = None
    position = _FILE_HEADER.size
    while True:
        handle.seek(position)
        head = handle.read(_CHUNK_HEADER.size)
        if len(head) < _CHUNK_HEADER.size:
            raise WavFormatError("no data chunk")
        chunk_id, size = _CHUNK_HEADER.unpack(head)
        body_at = position + _CHUNK_HEADER.size
        if chunk_id == DATA_ID:
            break
        if chunk_id == FMT_ID:
            fmt = _parse_fmt(handle.read(size))
        elif chunk_id == BEXT_ID:
            time_reference = _parse_time_reference(handle.read(size))
        position = body_at + size + (size & 1)
    if fmt is None:
        raise WavFormatError("no fmt chunk before the data")
    _check_format(fmt)
    if time_reference is None:
        raise WavFormatError("no bext chunk, so the file cannot be placed on the project timeline")
    available = max(0, file_size - body_at)
    truncated = size > available
    data_bytes = size
    if truncated:
        frame = fmt.channels * fmt.bits // _BITS_PER_BYTE
        data_bytes = available - available % frame
    return WavLayout(
        sample_rate=fmt.sample_rate,
        channels=fmt.channels,
        bits=fmt.bits,
        data_offset=body_at,
        data_bytes=data_bytes,
        time_reference=time_reference,
        truncated=truncated,
    )
