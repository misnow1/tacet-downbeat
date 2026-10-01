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

The page lists the presets in the order they are written, and an open goes
to the *default*: `default_db` when the site names one, and the first
preset otherwise. Until #139 the list did both jobs, which was right while
the default was an end of the range and wrong once the cap could exceed
unity - 0 dB suits most games and +3 dB a very loud crowd, so page order
and the default became two different requirements. They still cannot
disagree, because a default that is not one of the presets is refused
here: the page's control offers presets only, so a box booted anywhere
else is at a level the operator cannot get back to.

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
    """The levels the page offers, the ceiling they were checked against, and
    the one of them a fresh open goes to.

    `levels` are console units in page order; `default` is one of them, but
    not necessarily `levels[0]` (#139).
    """

    levels: tuple[int, ...]
    max_level: int
    default: int

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


def build(presets_db: Iterable[float], max_db: float, default_db: float | None = None) -> Targets:
    """Check a list of presets against the cap and turn it into `Targets`.

    Pure. Raises `TargetError` naming the offender for an empty list, a value
    the console cannot take, a duplicate, a preset above the cap, or a default
    that is not one of the presets.
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
    levels_tuple = tuple(levels)
    if default_db is None:
        default = levels_tuple[0]
    else:
        default = _level_in_range("the default target", default_db)
        if default not in levels_tuple:
            listed = ", ".join(repr(db) for db in values)
            raise TargetError(f"default target {default_db!r} dB is not one of the presets ({listed})")
    return Targets(levels=levels_tuple, max_level=max_level, default=default)


DEFAULT_TARGETS: Targets = build(DEFAULT_PRESETS_DB, DEFAULT_MAX_TARGET_DB)
