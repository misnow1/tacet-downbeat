"""The queue contract between the box and the Reaper mirror, as files both read.

`tacet.mirror` writes the queue in Python and `reaper/tacet_mirror.lua` reads it
in Lua, and each used to be tested only against itself: Python round-tripped its
own lines, and Lua read lines written by hand. A change to either passed both
suites while markers stopped appearing (#39).

So the contract is two pairs of files in `tests/fixtures/`:

- `queue-game-2.tsv`, the queue rebuilt from the game 2 log, and
  `queue-vocabulary.tsv`, one instant or span for every event there is;
- beside each, a `.marks` file: the markers and regions a mirror watching from
  the first line places, in order, one per line as `marker<TAB>name` or
  `region<TAB>name`.

Python pins the queues byte for byte and checks the game 2 marks against
`markers.derive`; the Lua tests replay each queue and must place exactly its
marks.

    python -m tests.queue_fixtures      rewrites the fixtures; review the diff
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from tacet import annotations as ann
from tacet import mirror

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAME_2_LOG = FIXTURES / "log-v1.jsonl"
GAME_2 = "game-2"
VOCABULARY = "vocabulary"
MARKER = "marker"
REGION = "region"
MARKS_SUFFIX = ".marks"
QUEUE_SUFFIX = ".tsv"


class _Clock:
    """Fixed, so the vocabulary queue does not depend on when it was made.
    Nothing in a queue line carries a time anyway."""

    def now(self) -> datetime:
        return datetime(2026, 9, 12, 19, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


def queue_path(name: str) -> Path:
    return FIXTURES / f"queue-{name}{QUEUE_SUFFIX}"


def marks_path(name: str) -> Path:
    return FIXTURES / f"queue-{name}{MARKS_SUFFIX}"


def vocabulary_entries() -> list[ann.Entry]:
    """Every event once: an instant as itself, a span as a start and its end."""
    clock = _Clock()
    entries: list[ann.Entry] = []
    for event in ann.VOCABULARY:
        seq = len(entries) + 1
        if event.kind is ann.Kind.SPAN:
            span_id = ann.span_id_for(event.key, seq)
            entries.append(ann.Entry.build(seq, event, clock, phase=ann.PHASE_START, span_id=span_id))
            entries.append(ann.Entry.build(seq + 1, event, clock, phase=ann.PHASE_END, span_id=span_id))
        else:
            entries.append(ann.Entry.build(seq, event, clock))
    return entries


def placements(queue: str) -> list[tuple[str, str]]:
    """What a mirror watching from the first line places, read with Python's
    parser: an instant is a marker when it arrives, a span is a region when it
    ends."""
    placed: list[tuple[str, str]] = []
    for line in queue.splitlines():
        item = mirror.parse_queue_line(line)
        if item.phase is None:
            placed.append((MARKER, item.name))
        elif item.phase == ann.PHASE_END:
            placed.append((REGION, item.name))
    return placed


def marks_text(placed: Iterable[tuple[str, str]]) -> str:
    return "".join(f"{kind}{mirror.DELIMITER}{name}\n" for kind, name in placed)


def generate() -> dict[Path, str]:
    """Every contract file, as the current code writes it."""
    game_2 = mirror.rebuild_queue(ann.read_entries(GAME_2_LOG))
    vocabulary = mirror.rebuild_queue(vocabulary_entries())
    return {
        queue_path(GAME_2): game_2,
        marks_path(GAME_2): marks_text(placements(game_2)),
        queue_path(VOCABULARY): vocabulary,
        marks_path(VOCABULARY): marks_text(placements(vocabulary)),
    }


def main() -> int:
    for path, text in generate().items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
