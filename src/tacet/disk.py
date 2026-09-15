"""Is there room for the game? Checked once, before the box starts (#53).

Running out of disk mid-game loses the recording, which is irreplaceable, and
can tear the annotation log (#26). Nothing noticed until it happened, so the box
now refuses to start without room for a whole game.

The box cannot ask Reaper where it records - Reaper's OSC does not say - so it
is told, as `capture.audio_path`, along with how many channels the game records.
A game's size is arithmetic on those, pinned against game 2's measured size.

Refusing is a deliberate departure from the rest of the startup checks, which
warn. A refusal the night before (`tacet-serve --check`) is a forcing function,
and `--no-disk-check` exists for the day the operator knows better.

A pure `assess` decides from measurements; `measure` and `check` are the thin
shell that takes them. An unmounted share is not measured as the directory
underneath it - that would be the local disk, reported as the NAS - so a path
that does not exist is refused. On macOS an unmounted share's `/Volumes/<name>`
goes away. On Linux an empty mountpoint directory would still pass: a known gap
for Phase 2, not handled here.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path

#: What Reaper records: uncompressed 48 kHz, 24-bit, one mono file per track.
SAMPLE_RATE = 48_000
BITS_PER_SAMPLE = 24
BITS_PER_BYTE = 8
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // BITS_PER_BYTE
BYTES_PER_CHANNEL_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE

SECONDS_PER_HOUR = 3600
#: Gates to the band leaving, with room for overtime.
DEFAULT_GAME_HOURS = 5.0
#: On top of the audio. Reaper's peak files were about 1% of game 2's media;
#: the rest is margin for a long night.
OVERHEAD = 0.10

#: Decimal, as a drive's label and `df -H` count them.
BYTES_PER_GB = 10**9

#: The log and the queue are small - a game's log is well under a megabyte - so
#: their volume only has to be not full. Generous, because a nearly full system
#: disk has other things filling it too.
MIN_LOG_FREE_BYTES = 100 * 10**6

#: The one way past every refusal here. A flag, never a config key: a file that
#: quietly turned the check off would be wrong every game after (#20's reasoning).
OVERRIDE_FLAG = "--no-disk-check"

_OVERRIDE = f"or pass {OVERRIDE_FLAG} to start anyway"

#: Refusals. Quoted in docs/troubleshooting.md, and pinned there by a test.
NOT_SET = (
    "capture.audio_path is not set, so there is no telling whether the recording fits: "
    f"set it and capture.channels in tacet.toml (or --audio-path and --channels), {_OVERRIDE}"
)
CHANNELS_REQUIRED = (
    f"capture.channels is required with capture.audio_path: how many tracks this game records (--channels), {_OVERRIDE}"
)
BAD_CHANNELS = f"capture.channels must be at least 1, not {{channels}}; {_OVERRIDE}"
BAD_HOURS = f"capture.game_hours must be more than 0, not {{hours}}; {_OVERRIDE}"
NOT_MOUNTED = (
    f"capture.audio_path {{path}} does not exist or is not a directory - is the NAS mounted? Mount it, {_OVERRIDE}"
)
SHORT = f"only {{free}} free on {{path}}, and a full game needs ~{{need}} ({{shape}}): free some space, {_OVERRIDE}"
LOG_SHORT = f"only {{free}} free on {{path}}, where the log or queue is written: free some space, {_OVERRIDE}"


def required_bytes(channels: int, hours: float) -> int:
    """A whole game's recording, with the overhead. Pure."""
    raw = channels * BYTES_PER_CHANNEL_SECOND * hours * SECONDS_PER_HOUR
    return math.ceil(raw * (1 + OVERHEAD))


def gigabytes(size: float) -> str:
    return f"{size / BYTES_PER_GB:.1f} GB"


@dataclass(frozen=True)
class Plan:
    """What the box was told about where the game goes."""

    audio_path: Path | None
    channels: int | None
    hours: float
    log_path: Path
    queue_path: Path | None
    override: bool


@dataclass(frozen=True)
class Volume:
    """A directory, measured. `device` says which ones share a disk."""

    path: Path
    free: int
    device: int


@dataclass(frozen=True)
class Verdict:
    """The banner's `disk` row, anything under it, and why not to start."""

    summary: str
    notes: tuple[str, ...] = ()
    refusal: str | None = None


def _shape(channels: int, hours: float) -> str:
    return f"{channels} ch x {hours:.1f} h"


def assess(plan: Plan, audio: Volume | None, others: list[Volume]) -> Verdict:
    """Decide from measurements already taken. Pure.

    `audio` is the recording directory, or None if it could not be measured;
    `others` are the directories the log and queue are written in.
    """
    refusal, summary = _assess_audio(plan, audio)
    notes: list[str] = []
    for other in others:
        if audio is not None and other.device == audio.device:
            continue  # one disk, already reported
        notes.append(f"log and queue: {gigabytes(other.free)} free on {other.path}")
        if refusal is None and other.free < MIN_LOG_FREE_BYTES:
            refusal = LOG_SHORT.format(free=gigabytes(other.free), path=other.path)
    if plan.override:
        if refusal is not None:
            notes.append(f"not enforced: {OVERRIDE_FLAG}")
        return Verdict(summary=summary, notes=tuple(notes))
    return Verdict(summary=summary, notes=tuple(notes), refusal=refusal)


def _assess_audio(plan: Plan, audio: Volume | None) -> tuple[str | None, str]:
    """The refusal, if any, and the summary, for the recording directory."""
    not_checked = f"not checked ({OVERRIDE_FLAG})" if plan.override else "not checked"
    if plan.audio_path is None:
        return NOT_SET, not_checked
    if plan.channels is None:
        return CHANNELS_REQUIRED, not_checked
    if plan.channels < 1:
        return BAD_CHANNELS.format(channels=plan.channels), not_checked
    if plan.hours <= 0:
        return BAD_HOURS.format(hours=plan.hours), not_checked
    if audio is None:
        return NOT_MOUNTED.format(path=plan.audio_path), not_checked
    need = required_bytes(plan.channels, plan.hours)
    shape = _shape(plan.channels, plan.hours)
    summary = f"{gigabytes(audio.free)} free on {audio.path}, need ~{gigabytes(need)} for {shape}"
    if audio.free < need:
        return SHORT.format(free=gigabytes(audio.free), path=audio.path, need=gigabytes(need), shape=shape), summary
    return None, summary


def measure(path: Path) -> Volume | None:
    """A directory's free space, or None if it is not an existing directory.

    Never the nearest parent: for a recording path that is how an unmounted NAS
    gets measured as the local disk underneath it.
    """
    if not path.is_dir():
        return None
    return Volume(path=path, free=shutil.disk_usage(path).free, device=path.stat().st_dev)


def measure_for_file(path: Path) -> Volume:
    """The volume a file will be written to: the nearest directory that exists.

    Right for the log and queue, which the box creates, directories included.
    """
    directory = path.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    volume = measure(directory)
    assert volume is not None, f"no existing directory above {path}"
    return volume


def check(plan: Plan) -> Verdict:
    """Measure what `plan` names, and assess it."""
    audio = measure(plan.audio_path) if plan.audio_path is not None else None
    files = [plan.log_path] if plan.queue_path is None else [plan.log_path, plan.queue_path]
    others: dict[int, Volume] = {}
    for file in files:
        volume = measure_for_file(file.expanduser().absolute())
        others.setdefault(volume.device, volume)
    return assess(plan, audio, list(others.values()))
