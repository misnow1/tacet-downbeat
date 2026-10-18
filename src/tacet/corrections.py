"""The corrections sidecar: what a later look at a game says its log got wrong (#205).

The annotation log is the source of truth and is never edited. The operator's
taps are late, a span's end is remembered rather than seen, a kind is a guess.
When something better than the tap turns up - the scoreboard's RTD stream, a
post-game review, later a speech recogniser - it is written beside the log, in
`<log stem>.corrections.jsonl`, and a reader lays it over the log. Applying it
is #21's; this module only defines, reads and writes it.

Schema v1: one JSON object per line, ASCII, UTF-8. Closed - an unknown key,
action or `v` is an error, not something to drop quietly.

Every line carries:

    v         1, required
    action    "supersede" or "add", required
    source    who says so: "rtd", "post-game review", later "asr"; non-empty, required
    reason    why, in words; non-empty, required
    evidence  an object, optional; carried through untouched

`supersede` replaces part of an existing entry, found by its `seq`:

    seq       the entry's sequence number, a whole number, required
    wall      the corrected time, ISO 8601 with a timezone, on the log's wall clock
    event     the corrected key, from `tacet.annotations.EVENTS`

At least one of `wall` and `event` is required. A span's kind is its start
entry's `event`, so supersede the start to reclassify a span and the end entry
to move its end. A reader places a superseding `wall` at the superseded entry's
arrival position plus the difference between the new wall and its own; the tap
delay recorded in its `data` does not apply to a time that was not tapped.

`add` records an entry the log lacks:

    event          a key from `EVENTS`, required
    wall           the time, ISO 8601 with a timezone, required
    phase          "start" or "end"; required for a span, forbidden for an instant
    span_id        the span's id; required for a span, forbidden for an instant
    project_seconds  a finite number, optional (the anchor a recording-started add carries, #159)
    data           an object, optional (the fields of an app move, #159)

`span_id` is the log's own id when adding a missing end. For a new span it must
not appear in the log; the code that applies the sidecar checks that.

A hand correction always wins. `unrecorded` drops any proposal whose target an
existing line already covers, whoever wrote that line, so a second `--write`
adds nothing and a reviewer's edit is never overwritten.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from . import annotations as ann

#: The schema this build writes, as `v` on every line.
CORRECTIONS_SCHEMA_VERSION = 1
#: Replaces the log's `.jsonl`: `2026-10-17-gt.jsonl` -> `2026-10-17-gt.corrections.jsonl`.
SIDECAR_SUFFIX = ".corrections.jsonl"

ACTION_SUPERSEDE = "supersede"
ACTION_ADD = "add"
ACTIONS = (ACTION_SUPERSEDE, ACTION_ADD)

_ENCODING = "utf-8"
_LINE_TERMINATOR = "\n"
_TERMINATOR_BYTE = _LINE_TERMINATOR.encode(_ENCODING)
_LAST_BYTE = -1


class CorrectionsError(ann.AnnotationError):
    """A line of the sidecar this build cannot read, or a file it cannot append to."""


def sidecar_path(log: Path) -> Path:
    return log.with_name(log.stem + SIDECAR_SUFFIX)


@dataclass(frozen=True)
class Supersede:
    seq: int
    source: str
    reason: str
    wall: str | None = None
    event: str | None = None
    evidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return _present(
            {
                "v": CORRECTIONS_SCHEMA_VERSION,
                "action": ACTION_SUPERSEDE,
                "seq": self.seq,
                "wall": self.wall,
                "event": self.event,
                "source": self.source,
                "reason": self.reason,
                "evidence": self.evidence,
            }
        )

    def to_json(self) -> str:
        return _dump(self.as_dict())

    @classmethod
    def from_json(cls, line: str) -> Self:
        raw = _object(line)
        if raw.get("action") != ACTION_SUPERSEDE:
            raise CorrectionsError(f"expected a supersede line, not action {raw.get('action')!r}")
        return cls(**_checked(raw, _SUPERSEDE_FIELDS, _check_supersede))


@dataclass(frozen=True)
class Add:
    event: str
    wall: str
    source: str
    reason: str
    phase: str | None = None
    span_id: str | None = None
    project_seconds: float | None = None
    data: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return _present(
            {
                "v": CORRECTIONS_SCHEMA_VERSION,
                "action": ACTION_ADD,
                "event": self.event,
                "wall": self.wall,
                "phase": self.phase,
                "span_id": self.span_id,
                "project_seconds": self.project_seconds,
                "data": self.data,
                "source": self.source,
                "reason": self.reason,
                "evidence": self.evidence,
            }
        )

    def to_json(self) -> str:
        return _dump(self.as_dict())

    @classmethod
    def from_json(cls, line: str) -> Self:
        raw = _object(line)
        if raw.get("action") != ACTION_ADD:
            raise CorrectionsError(f"expected an add line, not action {raw.get('action')!r}")
        return cls(**_checked(raw, _ADD_FIELDS, _check_add))


Correction = Supersede | Add

#: What a correction changes: the action, the thing, and the part of it.
Target = tuple[str, int | str, str]


def _present(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _dump(fields: Mapping[str, Any]) -> str:
    # No NaN or Infinity, for the same reason as the log: no other JSON reader would take them.
    return json.dumps(fields, separators=(",", ":"), sort_keys=True, allow_nan=False)


# -- reading ----------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


def _is_finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_aware_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _is_event(value: object) -> bool:
    return isinstance(value, str) and value in ann.EVENTS


def _is_phase(value: object) -> bool:
    return value in (ann.PHASE_START, ann.PHASE_END)


Check = Callable[[object], bool]
Fields = Mapping[str, tuple[bool, Check, str]]

_COMMON_FIELDS: Fields = {
    "v": (True, _is_int, "a whole number"),
    "action": (True, lambda value: value in ACTIONS, "supersede or add"),
    "source": (True, _is_text, "a non-empty string"),
    "reason": (True, _is_text, "a non-empty string"),
    "evidence": (False, _is_object, "an object"),
}
_SUPERSEDE_FIELDS: Fields = {
    **_COMMON_FIELDS,
    "seq": (True, _is_int, "a whole number"),
    "wall": (False, _is_aware_time, "an ISO 8601 time with a timezone"),
    "event": (False, _is_event, "a key in the event vocabulary"),
}
_ADD_FIELDS: Fields = {
    **_COMMON_FIELDS,
    "event": (True, _is_event, "a key in the event vocabulary"),
    "wall": (True, _is_aware_time, "an ISO 8601 time with a timezone"),
    "phase": (False, _is_phase, "start or end"),
    "span_id": (False, _is_text, "a non-empty string"),
    "project_seconds": (False, _is_finite, "a finite number"),
    "data": (False, _is_object, "an object"),
}


def _object(line: str) -> dict[str, Any]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise CorrectionsError(f"line is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CorrectionsError(f"line is not an object: {line[:60]!r}")
    return raw


def _checked(raw: dict[str, Any], fields: Fields, check_whole: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    if "v" not in raw:
        raise CorrectionsError("line has no schema version 'v'")
    if not _is_int(raw["v"]):
        raise CorrectionsError(f"schema version must be a whole number, not {raw['v']!r}")
    if raw["v"] != CORRECTIONS_SCHEMA_VERSION:
        raise CorrectionsError(
            f"line is schema v{raw['v']}, which this build cannot read (it reads v{CORRECTIONS_SCHEMA_VERSION})"
        )
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise CorrectionsError(f"line has fields this action does not: {', '.join(unknown)}")
    for name, (required, valid, expected) in fields.items():
        if name not in raw:
            if required:
                raise CorrectionsError(f"line is missing {name!r}")
        elif not valid(raw[name]):
            raise CorrectionsError(f"{name!r} must be {expected}, not {raw[name]!r}")
    check_whole(raw)
    return {key: value for key, value in raw.items() if key not in ("v", "action")}


def _check_supersede(raw: dict[str, Any]) -> None:
    if "wall" not in raw and "event" not in raw:
        raise CorrectionsError("a supersede needs a wall or an event to put in place of the entry's")


def _check_add(raw: dict[str, Any]) -> None:
    is_span = ann.EVENTS[raw["event"]].kind is ann.Kind.SPAN
    for name in ("phase", "span_id"):
        if is_span and name not in raw:
            raise CorrectionsError(f"{raw['event']!r} is a span, so an add needs {name!r}")
        if not is_span and name in raw:
            raise CorrectionsError(f"{raw['event']!r} is an instant, so an add must not carry {name!r}")


def correction_from_json(line: str) -> Correction:
    """One line, read as whichever action it names."""
    action = _object(line).get("action")
    if action == ACTION_SUPERSEDE:
        return Supersede.from_json(line)
    if action == ACTION_ADD:
        return Add.from_json(line)
    raise CorrectionsError(f"'action' must be {' or '.join(ACTIONS)}, not {action!r}")


def read_corrections(path: Path) -> list[Correction]:
    """The sidecar's lines, or an empty list if there is no sidecar.

    A malformed line raises `CorrectionsError` naming its number: a correction
    dropped quietly is a log that reads wrong without anyone knowing why.
    """
    if not path.exists():
        return []
    found: list[Correction] = []
    with path.open(encoding=_ENCODING) as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                found.append(correction_from_json(raw))
            except CorrectionsError as exc:
                raise CorrectionsError(f"line {number}: {exc}") from exc
    return found


# -- writing ----------------------------------------------------------------


def _ends_without_newline(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(_LAST_BYTE, os.SEEK_END)
        return handle.read(1) != _TERMINATOR_BYTE


def append_corrections(path: Path, items: Sequence[Correction]) -> None:
    """Add lines to the sidecar. No lines, no file; a torn last line is ended first."""
    if not items:
        return
    repair = _LINE_TERMINATOR if _ends_without_newline(path) else ""
    text = repair + "".join(item.to_json() + _LINE_TERMINATOR for item in items)
    with path.open("ab") as handle:
        handle.write(text.encode(_ENCODING))
        handle.flush()
        os.fsync(handle.fileno())


# -- who wins ---------------------------------------------------------------


def targets(correction: Correction) -> tuple[Target, ...]:
    """The parts of the log a correction speaks for."""
    if isinstance(correction, Add):
        which: int | str = correction.span_id if correction.span_id is not None else correction.event
        return (("add", which, correction.phase or correction.wall),)
    parts = (("wall", correction.wall), ("event", correction.event))
    return tuple(("supersede", correction.seq, name) for name, value in parts if value is not None)


def unrecorded(
    proposed: Sequence[Correction], existing: Sequence[Correction]
) -> tuple[list[Correction], list[Correction]]:
    """Split proposals into (fresh, already covered by a line in the sidecar)."""
    covered = {target for item in existing for target in targets(item)}
    fresh: list[Correction] = []
    already: list[Correction] = []
    for item in proposed:
        (already if covered.intersection(targets(item)) else fresh).append(item)
    return fresh, already
