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

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .annotations import (
    PHASE_END,
    PHASE_START,
    AnnotationError,
    AppendFile,
    Entry,
    Opener,
    Repair,
    Syncer,
    marker_name,
    open_for_append,
)

DELIMITER = "\t"
LINE_TERMINATOR = "\n"
#: Stands in for an empty field, so the Lua side can split on tabs without
#: having to reason about empty strings.
ABSENT = "-"

QUEUE_FIELDS = ("name", "phase", "span_id")

#: What a name must look like: `CATEGORY|key`, as `annotations.marker_name`
#: makes it. Checked on both sides - `reaper/tacet_mirror.lua` holds the same
#: rule as a Lua pattern - so a fragment of a torn write with a whole line
#: glued after it reads as the garbage it is, rather than as an event.
NAME_PATTERN = re.compile(r"^[A-Z]+\|[a-z0-9-]+$")

#: The phases a span line may carry. An instant carries `ABSENT` instead.
PHASES = (PHASE_START, PHASE_END)


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
    line = DELIMITER.join((_field(marker_name(entry)), _field(entry.phase), _field(entry.span_id)))
    # Refused here, where the log can count it, rather than written for the
    # Lua mirror to refuse where only Reaper's console would say so.
    parse_queue_line(line)
    return line


def parse_queue_line(line: str) -> QueueItem:
    """Read a queue line the way the Lua mirror does, and as strictly.

    The contract between the two is `tests/fixtures/queue-*.tsv`, which this
    side writes and `reaper/test_tacet_mirror.lua` replays.
    """
    parts = line.rstrip(LINE_TERMINATOR).split(DELIMITER)
    if len(parts) != len(QUEUE_FIELDS):
        raise QueueFormatError(f"expected {len(QUEUE_FIELDS)} fields: {line!r}")
    name, phase, span_id = parts
    if not NAME_PATTERN.match(name):
        raise QueueFormatError(f"not a marker name: {name!r}")
    if phase == ABSENT:
        if span_id != ABSENT:
            raise QueueFormatError(f"an instant carries no span id: {line!r}")
        return QueueItem(name=name)
    if phase not in PHASES:
        raise QueueFormatError(f"unknown phase {phase!r}: {line!r}")
    if span_id == ABSENT:
        raise QueueFormatError(f"a span line needs its span id: {line!r}")
    return QueueItem(name=name, phase=phase, span_id=span_id)


def rebuild_queue(entries: Iterable[Entry]) -> str:
    """Regenerate the whole queue from a log."""
    return "".join(queue_line(entry) + LINE_TERMINATOR for entry in entries)


class MirrorQueue:
    """Append-only, flushed per line and fsynced per batch, like the log itself.

    Every line is terminated before it is flushed: the Lua tail only consumes
    complete lines, so an unterminated one would strand that event until the
    next arrived.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        fsync: bool = True,
        opener: Opener = open_for_append,
        sync: Syncer = os.fsync,
    ) -> None:
        self.path = Path(path)
        # Nothing unterminated is trusted here, even a tail with three fields:
        # it may be a truncated span id, and the queue is regenerable. The Lua
        # tail never reads past the last terminator, so the cut cannot land
        # behind its stored position.
        self._file = AppendFile(self.path, keep_entries=False, fsync=fsync, opener=opener, sync=sync)
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
        """`write` and `sync` one entry."""
        self.write(entry)
        self.sync()

    def write(self, entry: Entry) -> None:
        """Raises `WriteError` if the line did not reach the file; the next
        write starts on a fresh line regardless. Flushed, so the Lua tail sees
        it, but not durable until `sync`."""
        if not self._file.is_open:
            raise QueueFormatError("mirror queue is not open")
        self._file.write_line(queue_line(entry))

    def sync(self) -> None:
        self._file.sync()
