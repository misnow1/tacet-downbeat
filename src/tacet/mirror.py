"""The queue the Reaper-side script tails to place markers live.

Two paths put marks on the timeline, and they answer different questions.

*Live*, this module: the box appends one line per event, a Lua script inside
Reaper notices it and drops a marker at the current playhead. No clock
arithmetic is involved, because Reaper already knows where it is.

*Offline*, `tacet.markers`: positions are computed from the recording anchor and
the whole set is regenerated. That is the path that makes markers a derived view
rather than the only copy.

The queue is deliberately not JSON. Lua has no parser in the box, and hand-
rolling one inside Reaper to read a file we also control would be work for
nothing. Tab-separated, three fields, `-` for absent - which a Lua script reads
in about five lines and a human reads at a glance in a press box.

The queue is itself derived: `rebuild_queue` reproduces it from the log, and a
test pins that the rebuilt file matches what was written live.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .annotations import AnnotationError, AppendFile, Entry, Opener, Repair, marker_name, open_for_append

DELIMITER = "\t"
LINE_TERMINATOR = "\n"
#: Stands in for an empty field, so the Lua side can split on tabs without
#: having to reason about empty strings.
ABSENT = "-"

QUEUE_FIELDS = ("name", "phase", "span_id")


class QueueFormatError(AnnotationError):
    """A value that would corrupt the queue's framing."""


@dataclass(frozen=True)
class QueueItem:
    name: str
    phase: str | None = None
    span_id: str | None = None


def _field(value: str | None) -> str:
    if value is None:
        return ABSENT
    if DELIMITER in value or LINE_TERMINATOR in value:
        raise QueueFormatError(f"{value!r} contains the queue's framing characters")
    return value


def queue_line(entry: Entry) -> str:
    """One entry as a queue line, without its terminator."""
    return DELIMITER.join((_field(marker_name(entry)), _field(entry.phase), _field(entry.span_id)))


def parse_queue_line(line: str) -> QueueItem:
    parts = line.rstrip(LINE_TERMINATOR).split(DELIMITER)
    if len(parts) != len(QUEUE_FIELDS):
        raise QueueFormatError(f"expected {len(QUEUE_FIELDS)} fields: {line!r}")
    name, phase, span_id = parts
    return QueueItem(
        name=name,
        phase=None if phase == ABSENT else phase,
        span_id=None if span_id == ABSENT else span_id,
    )


def rebuild_queue(entries: Iterable[Entry]) -> str:
    """Regenerate the whole queue from a log."""
    return "".join(queue_line(entry) + LINE_TERMINATOR for entry in entries)


class MirrorQueue:
    """Append-only, flushed and fsynced per line like the log itself.

    Every line is terminated before it is flushed: the Lua tail only consumes
    complete lines, so an unterminated one would strand that event until the
    next arrived.
    """

    def __init__(self, path: Path | str, *, fsync: bool = True, opener: Opener = open_for_append) -> None:
        self.path = Path(path)
        # Nothing unterminated is trusted here, even a tail with three fields:
        # it may be a truncated span id, and the queue is regenerable. The Lua
        # tail never reads past the last terminator, so the cut cannot land
        # behind its stored position.
        self._file = AppendFile(self.path, keep_entries=False, fsync=fsync, opener=opener)
        self.repair: Repair | None = None

    def open(self) -> Self:
        self.repair = self._file.open()
        return self

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, entry: Entry) -> None:
        """Raises `WriteError` if the line did not reach the disk; the next
        append starts on a fresh line regardless."""
        if not self._file.is_open:
            raise QueueFormatError("mirror queue is not open")
        self._file.append_line(queue_line(entry))
