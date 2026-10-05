"""What the page is told about a fader move, once, so it can animate it (#154).

A fade or a ride used to be shown by pushing a fresh snapshot to the page every
100 ms, ~3.9 KB each, across stadium wifi. The box now describes the move once -
where it starts, where it ends, how long it takes, when it began on the box's
own monotonic clock, and the shape of the curve - and the page draws the sweep
from that. `static/app.js` mirrors `dm7.ramp_steps` from these numbers, and
`tests/fixtures/move-curves.json` pins the two together.

Display only. Nothing here is sent to the console and nothing reads the page's
animation back: the box finishes a move without the page, and the page's
in-flight state ends only when the box says so. Pure, standard library plus
`dm7`, and not on the control path: it moves nothing.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from . import dm7


class MoveKind(enum.StrEnum):
    FADE = "fade"  # to -inf
    RIDE = "ride"  # up-slow, READY, and any retarget ride


@dataclass(frozen=True)
class MoveDescription:
    seq: int  # a new one for every move, so the page can tell a replacement
    kind: MoveKind
    by: str | None  # the vocabulary key that started it; None for a bare command
    start: int  # console units
    end: int
    seconds: float
    started_at: float  # box monotonic: the clock the snapshot's `at` is on
    floor: int  # the console's fade floor; -inf is entered and left here
    taper: dm7.Taper | None

    def as_data(self) -> dict[str, Any]:
        """The nine keys the page reads, in dB, with -inf as None (JSON has no -inf)."""
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "by": self.by or None,
            "from_db": _db(self.start),
            "to_db": _db(self.end),
            "seconds": self.seconds,
            "started_at": self.started_at,
            "floor_db": _db(self.floor),
            "knee": None
            if self.taper is None
            else {"db": _db(self.taper.knee_level), "fraction": self.taper.knee_fraction},
        }


def _db(level: int) -> float | None:
    """Console units to dB; -inf has no JSON spelling and reads as None."""
    return None if level == dm7.MINUS_INF else dm7.to_db(level)
