"""The standing target level: which levels the operator may choose, and the cap.

The band DCA opens to a *target*, and until #9 that was `open_level`, a constant
of unity. This module is the rule for what the target may be. It is the safety
half of the feature: +3 dB costs 3 dB of feedback margin against a band PA that
sits just behind the mics, facing the same way, and the number that is safe is
set by an on-site ring-out with the stands empty, not guessed. So the cap ships
at unity, and a preset above it is refused when the configuration is read,
which keeps `+3` from existing by accident.

Pure functions over a frozen dataclass, and nothing else. There is no state
here: `App` owns the one mutable target, because `state.Machine` knows no
console units. This module is on the control path (`tests/test_dependency_
policy.py`) since the level it decides is the one the fader is sent to, so it
imports the standard library and `dm7` only.

The default target is the first preset. One list rather than a list and a
separate default, so the two cannot disagree: a site that rings out to -2 writes
`presets = [-2.0, -5.0, -8.0]` and the default follows with no second edit.

Everything is compared in console units. The DM7 resolves 0.01 dB
(`dm7.UNITS_PER_DB`), so two spellings of one level are one level - a duplicate
- and the page's `-3` and the file's `-3.0` are the same preset.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from . import dm7

#: What ships. Unity first, so unity is the default, then two quieter steps.
DEFAULT_PRESETS_DB: tuple[float, ...] = (0.0, -3.0, -6.0)

#: Ships at unity until the on-site ring-out has been done (#9), so a preset
#: louder than the band PA has been proven to tolerate cannot exist by accident.
DEFAULT_MAX_TARGET_DB: float = 0.0


class TargetError(Exception):
    """A set of presets that cannot be trusted. Names the offending value."""


@dataclass(frozen=True)
class Targets:
    """The levels the page offers and the ceiling they were checked against.

    `levels` are console units in page order, the first being the default.
    """

    levels: tuple[int, ...]
    max_level: int

    @property
    def default(self) -> int:
        """The level a fresh box opens to: the first preset."""
        return self.levels[0]

    def allows(self, level: int) -> bool:
        """Whether `level` is one of the presets, in console units."""
        return level in self.levels

    def db_values(self) -> tuple[float, ...]:
        """The presets as dB, in page order, for the page to label."""
        return tuple(level / dm7.UNITS_PER_DB for level in self.levels)


def level_for(db: float) -> int:
    """A dB value as console units, rounded to the console's resolution."""
    return round(db * dm7.UNITS_PER_DB)


def _level_in_range(name: str, db: float) -> int:
    """`db` as console units, or `TargetError` if it is not a level the console
    can be told: not a finite number, or outside the range it accepts. -inf is
    a close, never a target, so the sentinel itself is out too."""
    if not math.isfinite(db):
        raise TargetError(f"{name} {db!r} is not a finite number of dB")
    level = level_for(db)
    if not dm7.LEVEL_MIN < level <= dm7.LEVEL_MAX:
        low = (dm7.LEVEL_MIN + 1) / dm7.UNITS_PER_DB
        high = dm7.LEVEL_MAX / dm7.UNITS_PER_DB
        raise TargetError(f"{name} {db!r} dB is outside what the console accepts ({low:.2f} to {high:.2f} dB)")
    return level


def build(presets_db: Iterable[float], max_db: float) -> Targets:
    """Check a list of presets against the cap and turn it into `Targets`.

    Pure. Raises `TargetError` naming the offender for an empty list, a value
    the console cannot take, a duplicate, or a preset above the cap.
    """
    values = tuple(presets_db)
    if not values:
        raise TargetError("presets is empty: at least one target level is needed")
    max_level = _level_in_range("the cap", max_db)
    levels: list[int] = []
    for db in values:
        level = _level_in_range("preset", db)
        if level > max_level:
            raise TargetError(f"preset {db!r} dB is above the cap of {max_db!r} dB")
        if level in levels:
            raise TargetError(f"preset {db!r} dB appears twice")
        levels.append(level)
    return Targets(levels=tuple(levels), max_level=max_level)


DEFAULT_TARGETS: Targets = build(DEFAULT_PRESETS_DB, DEFAULT_MAX_TARGET_DB)
