"""Yamaha DM7 console control over OSC.

The protocol is write-only (design.md section 5.3): there is no get, no subscribe and
no notify, so nothing here reads console state. Every level this module reports
is what it last *commanded*, never what the console confirmed. Callers must
present it that way.

Only `MIXER:Current/DCA/Fader/Level` is ever sent. Never `Fader/On`, on the DCA
or on a channel: the band mics feed other mixes pre-fader and post-mute, so a
mute would pull the band out of those mixes too. A full close is `-32768` (-inf),
which costs nothing and needs no mute.

Nothing in this module decides *when* to move. Phase 0 ramps are started by the
operator; see CLAUDE.md on not moving the fader autonomously before Phase 2.
"""

from __future__ import annotations

import asyncio
import bisect
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass

from . import osc
from .net import ClosableSender, Sender, TransportError, UdpSender

DEFAULT_PORT = 49900

#: OSC address parts, from the DM7 spec. The protocol is write-only, so `set` is
#: the only action this project has any use for.
OSC_REQUEST_PREFIX = "/yosc:req"
ACTION_SET = "set"
PARAM_DCA_FADER_LEVEL = "MIXER:Current/DCA/Fader/Level"

#: The console expresses levels in hundredths of a dB - the spec's "scaling" of
#: 100. Every level in this module is in those units unless it says dB.
UNITS_PER_DB = 100

#: Complete close. A protocol sentinel rather than a dB value, which is why no
#: ramp may interpolate toward it.
MINUS_INF = -32768
#: Unity. The operator's normal open level (design.md 4).
UNITY = 0
LEVEL_MIN = MINUS_INF
LEVEL_MAX = 10 * UNITS_PER_DB

#: Level below which a fade may step the rest of the way to silence unheard.
DEFAULT_FADE_FLOOR = -60 * UNITS_PER_DB

DEFAULT_FADE_SECONDS = 2.0
#: A fast open is not instantaneous: design.md section 6.1 buys 10-20 ms of ramp, which
#: takes the edge off the step without being audible as a late entry.
DEFAULT_OPEN_SECONDS = 0.02
#: The ride-in for `up-slow`: the operator missed the first phrase and is
#: disguising the late entry rather than snapping the fader open under a
#: phrase already in progress (design.md section 4). Long enough to read as
#: deliberate, short enough that the band is audible within the phrase.
#: Retunable per site as `fader.slow_open_seconds`.
DEFAULT_SLOW_OPEN_SECONDS = 1.5
DEFAULT_TICK_HZ = 50.0
#: A ramp always emits at least one step, however short its duration.
MIN_RAMP_TICKS = 1

#: Table 1 of the OSC spec, in hundredths of a dB, ascending.
#:
#: The console at this site does NOT snap to these: `verify_dm7 --granularity`
#: sent -1550 and the DM7 displayed -15.50 (2026-09-12), so the spec's scaling
#: governs and Table 1 is a display convention. Kept as a contingency for a
#: different console or a firmware change - ramping through `quantize` makes both
#: cases behave identically, at the cost of a coarser fade below -10 dB.
TABLE_1 = (
    -32768,
    -13800,
    -12000,
    -10500,
    -9000,
    -7000,
    -6000,
    -5500,
    -5000,
    -4500,
    -4000,
    -3500,
    -3000,
    -2800,
    -2600,
    -2400,
    -2200,
    -2000,
    -1800,
    -1600,
    -1400,
    -1200,
    -1000,
    -900,
    -800,
    -700,
    -600,
    -500,
    -400,
    -300,
    -200,
    -100,
    0,
    100,
    200,
    300,
    400,
    500,
    600,
    700,
    800,
    900,
    1000,
)


@dataclass(frozen=True)
class Taper:
    """The shape of a ride-in: fast through the bottom, slow through the top.

    The ramp reaches `knee_level` at `knee_fraction` of its duration, then
    rises linearly in dB to the target over the rest. A hand on a fader does
    roughly this: the bottom of the travel is inaudible under a crowd, so it is
    got through quickly, and the time goes where the band can be heard (#8).
    """

    knee_level: int
    knee_fraction: float

    def __post_init__(self) -> None:
        if not 0.0 < self.knee_fraction < 1.0:
            raise ValueError(f"knee_fraction must be strictly between 0 and 1, got {self.knee_fraction}")

    def shapes(self, start: int, target: int) -> bool:
        """Only a rise that crosses the knee has a bottom to rush. Anything else,
        closes included, is left linear."""
        return start < self.knee_level < target

    def level_at(self, start: int, target: int, fraction: float) -> float:
        if fraction <= self.knee_fraction:
            return start + (self.knee_level - start) * (fraction / self.knee_fraction)
        above = (fraction - self.knee_fraction) / (1.0 - self.knee_fraction)
        return self.knee_level + (target - self.knee_level) * above


#: The ride-in shape, a guess until it is fitted against hand rides captured on
#: game 3's post-DCA reference channel (#8, #13). -20 dB is where the band starts
#: to read over a crowd; 15% of the duration is quick without being a snap.
RIDE_IN_KNEE_LEVEL = -20 * UNITS_PER_DB
RIDE_IN_KNEE_FRACTION = 0.15
RIDE_IN_TAPER = Taper(knee_level=RIDE_IN_KNEE_LEVEL, knee_fraction=RIDE_IN_KNEE_FRACTION)


def clamp(level: int) -> int:
    return max(LEVEL_MIN, min(LEVEL_MAX, int(level)))


def to_db(level: int) -> float:
    """Console units to dB. -inf comes back as -inf, which formats readably."""
    return float("-inf") if level == MINUS_INF else level / UNITS_PER_DB


def quantize(level: int) -> int:
    """Snap to the nearest Table 1 value."""
    return min(TABLE_1, key=lambda candidate: abs(candidate - level))


def latest_due(offsets: Sequence[float], first: int, elapsed: float) -> int:
    """The index of the newest step due by `elapsed`, never one before `first`.

    Pure. `first` is the next step not yet sent and must already be due. Every
    step between the two is stale: the console only ever needs to hear where
    the fader should be now, not the levels it was meant to pass through while
    the loop was stalled (#40).
    """
    return max(first, bisect.bisect_right(offsets, elapsed, lo=first) - 1)


@dataclass(frozen=True)
class MoveTiming:
    """How far a move fell behind its schedule.

    `worst_lateness` is in seconds, measured from when the oldest unsent step
    fell due to when a step was actually sent - so a stall reads as the whole of
    its length, however many steps it skipped. A drive that keeps up still
    shows the loop's own wake-up jitter, a millisecond or so. `skipped` counts
    steps passed over because a later one was already due.
    """

    worst_lateness: float = 0.0
    skipped: int = 0


def fader_address(dca: int) -> str:
    return f"{OSC_REQUEST_PREFIX}/{ACTION_SET}/{PARAM_DCA_FADER_LEVEL}/{dca}"


def ramp_steps(
    start: int,
    target: int,
    duration: float,
    *,
    tick_hz: float = DEFAULT_TICK_HZ,
    fade_floor: int = DEFAULT_FADE_FLOOR,
    quantized: bool = False,
    taper: Taper | None = None,
) -> Iterator[tuple[float, int]]:
    """Yield ``(offset_seconds, level)`` for one fader move.

    Pure, so the shape of a ramp can be tested without a socket or a clock.

    Interpolation is linear in dB, which is what a fade-out wants. A ride-in
    passes a `taper` instead, which only shapes a rise that crosses its knee.
    Both are starting guesses, not measured curves: the real ones come out of
    Phase 1, by comparing the post-DCA reference channel against the pre-fader
    mics. Tune them against captured games, never in a quiet room. A close to -inf ramps
    to `fade_floor` and then steps the rest of the way: -inf is not a dB value and
    cannot be interpolated toward, and the last few dB are inaudible anyway.

    Repeated levels are dropped, so a ramp finer than the console's resolution
    costs nothing extra on the wire.
    """
    start, target = clamp(start), clamp(target)
    if start == target:
        return  # already there; say nothing

    ramp_start, ramp_target, tail = start, target, None

    # -inf is not a dB value and cannot be interpolated through: a ramp toward it
    # would collapse into its first tick. Ramps enter and leave silence at the
    # floor and step the remaining, inaudible, distance.
    if target == MINUS_INF:
        if start <= fade_floor:
            yield duration, MINUS_INF
            return
        ramp_target, tail = fade_floor, MINUS_INF
    elif start == MINUS_INF:
        if target <= fade_floor:
            yield duration, target
            return
        ramp_start = fade_floor

    ticks = max(MIN_RAMP_TICKS, round(duration * tick_hz))
    emitted = start  # the console is already here; do not restate it
    shaped = taper if taper is not None and taper.shapes(ramp_start, ramp_target) else None

    for tick in range(1, ticks + 1):
        fraction = tick / ticks
        if shaped is not None:
            exact = shaped.level_at(ramp_start, ramp_target, fraction)
        else:
            exact = ramp_start + (ramp_target - ramp_start) * fraction
        level = clamp(round(exact))
        if quantized:
            level = quantize(level)
        if level != emitted:
            emitted = level
            yield duration * fraction, level

    if tail is not None and emitted != tail:
        yield duration, tail


class Dm7Client:
    """Commands one DCA fader.

    `commanded_level` is the last value written, not a console reading.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        dca: int = 1,
        initial_level: int = MINUS_INF,
        tick_hz: float = DEFAULT_TICK_HZ,
        fade_floor: int = DEFAULT_FADE_FLOOR,
        quantized: bool = False,
        sender: Sender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.dca = dca
        self.tick_hz = tick_hz
        self.fade_floor = fade_floor
        self.quantized = quantized
        self._sender = sender if sender is not None else UdpSender(host, port)
        self._monotonic = monotonic
        self._sleep = sleep
        self._address = fader_address(dca)

        self._commanded = clamp(initial_level)
        self._ramp: asyncio.Task[None] | None = None
        self._timing = MoveTiming()
        self.last_error: str | None = None
        self.sent_count = 0

    # -- state ------------------------------------------------------------

    @property
    def commanded_level(self) -> int:
        return self._commanded

    @property
    def commanded_db(self) -> float:
        return to_db(self._commanded)

    @property
    def is_ramping(self) -> bool:
        return self._ramp is not None and not self._ramp.done()

    @property
    def timing(self) -> MoveTiming:
        """The current move's timing, or the last one's once it has ended."""
        return self._timing

    @property
    def healthy(self) -> bool:
        return self.last_error is None

    # -- sending ----------------------------------------------------------

    def send_level(self, level: int) -> int:
        """Command one level immediately. Returns the clamped value sent."""
        level = clamp(level)
        packet = osc.encode_message(self._address, level, tags=osc.TypeTag.INT32)
        # Any sender, any error. The fade and ride-in tasks catch only
        # TransportError, so a sender that breaks its contract would otherwise
        # end a move silently, with the fader still reading healthy (#72).
        try:
            self._sender.send(packet)
        except TransportError as exc:
            self.last_error = str(exc)
            raise
        except Exception as exc:
            self.last_error = str(exc)
            raise TransportError(str(exc)) from exc
        self.last_error = None
        self.sent_count += 1
        self._commanded = level
        return level

    # -- moves ------------------------------------------------------------

    async def open(self, level: int = UNITY, seconds: float = DEFAULT_OPEN_SECONDS) -> None:
        """Open fast. Cancels any fade in progress, which is the snap-back to
        OPEN described in design.md section 6.3."""
        await self._move(level, seconds)

    async def fade_out(self, seconds: float = DEFAULT_FADE_SECONDS) -> None:
        """The ~2 s close. Same fade for every reason the music stopped."""
        await self._move(MINUS_INF, seconds)

    async def ride_in(self, level: int = UNITY, seconds: float = DEFAULT_SLOW_OPEN_SECONDS) -> None:
        """A deliberate, tapered open: fast through the inaudible bottom, slow
        through the top (#8). Superseded like any other move."""
        await self._move(level, seconds, taper=RIDE_IN_TAPER)

    async def _move(self, target: int, seconds: float, *, taper: Taper | None = None) -> None:
        self.cancel_ramp()
        steps = list(
            ramp_steps(
                self._commanded,
                target,
                seconds,
                tick_hz=self.tick_hz,
                fade_floor=self.fade_floor,
                quantized=self.quantized,
                taper=taper,
            )
        )
        self._timing = MoveTiming()
        ramp = asyncio.ensure_future(self._drive(steps))
        self._ramp = ramp
        try:
            await ramp
        except asyncio.CancelledError:
            # A newer move superseding this one cancels the ramp, and that is
            # normal, not a fault. A cancel aimed at whoever awaited this move
            # is not the same thing and has to reach them: swallowed, a
            # cancelled fade carried on as though it had finished (#34).
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise

    async def _drive(self, steps: Sequence[tuple[float, int]]) -> None:
        """Send each step when it falls due, or only the newest once behind.

        Catching up step by step after the loop stalled sent the console a
        burst of stale levels, and the fade jumped anyway (#40).
        """
        offsets = [offset for offset, _ in steps]
        started = self._monotonic()
        this = asyncio.current_task()
        worst, skipped = 0.0, 0
        pending = 0
        while pending < len(steps):
            elapsed = self._monotonic() - started
            remaining = offsets[pending] - elapsed
            if remaining > 0:
                await self._sleep(remaining)
                continue
            newest = latest_due(offsets, pending, elapsed)
            worst = max(worst, -remaining)
            # Steps sharing the sent step's instant were never meant to be
            # heard apart from it - a close's floor and its -inf - so passing
            # them over is not falling behind.
            skipped += bisect.bisect_left(offsets, offsets[newest], lo=pending) - pending
            if self._ramp is this:
                self._timing = MoveTiming(worst_lateness=worst, skipped=skipped)
            self.send_level(steps[newest][1])
            pending = newest + 1

    def cancel_ramp(self) -> None:
        ramp = self._ramp
        if ramp is not None and not ramp.done():
            ramp.cancel()
        self._ramp = None

    def close(self) -> None:
        self.cancel_ramp()
        if isinstance(self._sender, ClosableSender):
            self._sender.close()
