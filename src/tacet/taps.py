"""When a tap happened, as distinct from when it arrived (#11).

On stadium wifi a POST can arrive seconds after the thumb left the glass: TCP
retransmits, and the request is executed as current. The box used to log only
receipt, so game 2's first-quarter annotations are delivery times with an
unknown lag, and a late tap looked exactly like a prompt one.

So the page stamps each tap with its own clock and with its estimate of how that
clock relates to the box's. The estimate comes from a round trip on the
websocket - see `clock_sample` in `static/app.js` - so it is approximate by
design and carries its uncertainty: half the round trip it was measured over.

Everything here is pure. `Tap` is what the page sent; `TapTiming` is that tap on
the box's monotonic clock, with how late it arrived.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .annotations import AnnotationError

#: The field a tap stamp arrives in, in every request body the page sends, and
#: the key its timing is logged under in an entry's `data`.
TAP_FIELD = "tap"


class TapError(AnnotationError):
    """A tap stamp that is present but malformed. Refused, not guessed at: a
    wrong stamp is worse than none, because none says it does not know."""


@dataclass(frozen=True)
class Tap:
    """What the page sent.

    `at` is the page's clock, in seconds. `offset` is its estimate of page clock
    minus box monotonic clock and `uncertainty` how far that estimate may be
    out; both are None until the page has measured one.
    """

    at: float
    offset: float | None = None
    uncertainty: float | None = None

    @property
    def estimated(self) -> bool:
        return self.offset is not None and self.uncertainty is not None


@dataclass(frozen=True)
class TapTiming:
    """A tap on the box's monotonic clock.

    `tapped`, `delay` and `uncertainty` are None when the page had no estimate:
    the tap is stamped as received, and says it cannot say more.
    """

    received: float
    tapped: float | None = None
    delay: float | None = None
    uncertainty: float | None = None

    def as_data(self) -> dict[str, float | None]:
        return {
            "tapped": self.tapped,
            "received": self.received,
            "delay": self.delay,
            "uncertainty": self.uncertainty,
        }


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TapError(f"tap {name} must be a finite number, not {value!r}")
    return float(value)


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def read_tap(body: object) -> Tap | None:
    """The tap stamp in a request body, or None if it carries none.

    None is not an error: a page cached from before #11, or a hand-typed curl,
    sends no stamp, and its taps are logged as received, as they always were.
    """
    if not isinstance(body, dict) or body.get(TAP_FIELD) is None:
        return None
    raw = body[TAP_FIELD]
    if not isinstance(raw, dict):
        raise TapError(f"tap must be an object, not {raw!r}")
    offset = _optional_number(raw.get("offset"), "offset")
    uncertainty = _optional_number(raw.get("uncertainty"), "uncertainty")
    if (offset is None) != (uncertainty is None):
        raise TapError("tap offset and uncertainty come together or not at all")
    if uncertainty is not None and uncertainty < 0:
        raise TapError(f"tap uncertainty cannot be negative, not {uncertainty!r}")
    return Tap(at=_number(raw.get("at"), "at"), offset=offset, uncertainty=uncertainty)


def timing(tap: Tap | None, received: float) -> TapTiming:
    """Place a tap on the box's clock, given when the box received it.

    The delay is not clamped. A small negative one is the estimate's error
    showing, and is within `uncertainty` of zero; hiding it would hide how good
    the estimate was.
    """
    if tap is None or tap.offset is None or tap.uncertainty is None:
        return TapTiming(received=received)
    tapped = tap.at - tap.offset
    return TapTiming(received=received, tapped=tapped, delay=received - tapped, uncertainty=tap.uncertainty)


def delay_of(data: Any) -> float | None:
    """The logged delay in an entry's `data`, or None if it has none or it is
    not a number. For reading logs back, which may have been written by
    anything."""
    tap = data.get(TAP_FIELD) if isinstance(data, dict) else None
    delay = tap.get("delay") if isinstance(tap, dict) else None
    if isinstance(delay, bool) or not isinstance(delay, int | float) or not math.isfinite(delay):
        return None
    return float(delay)
