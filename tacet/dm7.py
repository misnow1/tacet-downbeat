"""Yamaha DM7 console control over OSC.

The protocol is write-only (design.md §5.3): there is no get, no subscribe and
no notify, so nothing here reads console state. Every level this module reports
is what it last *commanded*, never what the console confirmed. Callers must
present it that way.

Only `MIXER:Current/DCA/Fader/Level` is ever sent. Never `Fader/On`, on the DCA
or on a channel: the band mics feed other mixes pre-fader and post-mute, so a
mute would pull the band out of those mixes too. A full close is `-32768` (−∞),
which costs nothing and needs no mute.

Nothing in this module decides *when* to move. Phase 0 ramps are started by the
operator; see CLAUDE.md on not moving the fader autonomously before Phase 2.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Callable, Iterable, Iterator, Optional, Tuple

from . import osc

DEFAULT_PORT = 49900

#: Complete close. −∞ on the console's scale.
MINUS_INF = -32768
#: Unity. The operator's normal open level (design.md §4).
UNITY = 0
LEVEL_MIN = -32768
LEVEL_MAX = 1000

#: Level in hundredths of a dB below which a fade may jump straight to −∞.
DEFAULT_FADE_FLOOR = -6000

DEFAULT_FADE_SECONDS = 2.0
#: A fast open is not instantaneous: design.md §6.1 buys 10–20 ms of ramp, which
#: takes the edge off the step without being audible as a late entry.
DEFAULT_OPEN_SECONDS = 0.02
DEFAULT_TICK_HZ = 50.0

#: Table 1 of the OSC spec, in hundredths of a dB, ascending.
#:
#: Whether the console accepts arbitrary values or snaps to these is an open
#: question (design.md §7). Ramping through `quantize` makes the two cases
#: behave identically, at the cost of a coarser fade below −10 dB.
TABLE_1 = (
    -32768, -13800, -12000, -10500, -9000, -7000, -6000, -5500, -5000, -4500,
    -4000, -3500, -3000, -2800, -2600, -2400, -2200, -2000, -1800, -1600,
    -1400, -1200, -1000, -900, -800, -700, -600, -500, -400, -300, -200, -100,
    0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
)


class Dm7Error(RuntimeError):
    """The console could not be reached. Surface this; never swallow it."""


def clamp(level: int) -> int:
    return max(LEVEL_MIN, min(LEVEL_MAX, int(level)))


def to_db(level: int) -> float:
    """Console units to dB. −∞ comes back as -inf, which formats readably."""
    return float("-inf") if level == MINUS_INF else level / 100.0


def quantize(level: int) -> int:
    """Snap to the nearest Table 1 value."""
    return min(TABLE_1, key=lambda candidate: abs(candidate - level))


def fader_address(dca: int) -> str:
    return f"/yosc:req/set/MIXER:Current/DCA/Fader/Level/{dca}"


def ramp_steps(
    start: int,
    target: int,
    duration: float,
    *,
    tick_hz: float = DEFAULT_TICK_HZ,
    fade_floor: int = DEFAULT_FADE_FLOOR,
    quantized: bool = False,
) -> Iterator[Tuple[float, int]]:
    """Yield ``(offset_seconds, level)`` for one fader move.

    Pure, so the shape of a ramp can be tested without a socket or a clock.

    Interpolation is linear in dB, which is what a fade-out wants and roughly
    what a hand on a fader does over the top of its travel. It is a starting
    guess, not a measured curve: the real one comes out of Phase 1, by comparing
    the post-DCA reference channel against the pre-fader mics. Tune it against
    captured games, never in a quiet room. A close to −∞ ramps
    to `fade_floor` and then steps the rest of the way: −∞ is not a dB value and
    cannot be interpolated toward, and the last few dB are inaudible anyway.

    Repeated levels are dropped, so a ramp finer than the console's resolution
    costs nothing extra on the wire.
    """
    start, target = clamp(start), clamp(target)
    if start == target:
        return  # already there; say nothing

    ramp_start, ramp_target, tail = start, target, None

    # −∞ is not a dB value and cannot be interpolated through: a ramp toward it
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

    ticks = max(1, int(round(duration * tick_hz)))
    emitted = start  # the console is already here; do not restate it

    for tick in range(1, ticks + 1):
        fraction = tick / ticks
        level = clamp(round(ramp_start + (ramp_target - ramp_start) * fraction))
        if quantized:
            level = quantize(level)
        if level != emitted:
            emitted = level
            yield duration * fraction, level

    if tail is not None and emitted != tail:
        yield duration, tail


class UdpSender:
    """Fire-and-forget UDP. Nothing comes back; see the module docstring."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, packet: bytes) -> None:
        try:
            self._socket.sendto(packet, (self.host, self.port))
        except OSError as exc:
            raise Dm7Error(f"could not send to {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        self._socket.close()


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
        sender: Optional[object] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dca = dca
        self.tick_hz = tick_hz
        self.fade_floor = fade_floor
        self.quantized = quantized
        self._sender = sender if sender is not None else UdpSender(host, port)
        self._monotonic = monotonic
        self._address = fader_address(dca)

        self._commanded = clamp(initial_level)
        self._ramp: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
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
    def healthy(self) -> bool:
        return self.last_error is None

    # -- sending ----------------------------------------------------------

    def send_level(self, level: int) -> int:
        """Command one level immediately. Returns the clamped value sent."""
        level = clamp(level)
        packet = osc.encode_message(self._address, level, tags="i")
        try:
            self._sender.send(packet)
        except Dm7Error as exc:
            self.last_error = str(exc)
            raise
        self.last_error = None
        self.sent_count += 1
        self._commanded = level
        return level

    # -- moves ------------------------------------------------------------

    async def open(self, level: int = UNITY, seconds: float = DEFAULT_OPEN_SECONDS) -> None:
        """Open fast. Cancels any fade in progress, which is the snap-back to
        OPEN described in design.md §6.3."""
        await self._move(level, seconds)

    async def fade_out(self, seconds: float = DEFAULT_FADE_SECONDS) -> None:
        """The ~2 s close. Same fade for every reason the music stopped."""
        await self._move(MINUS_INF, seconds)

    async def _move(self, target: int, seconds: float) -> None:
        self.cancel_ramp()
        steps = list(
            ramp_steps(
                self._commanded,
                target,
                seconds,
                tick_hz=self.tick_hz,
                fade_floor=self.fade_floor,
                quantized=self.quantized,
            )
        )
        self._ramp = asyncio.ensure_future(self._drive(steps))
        try:
            await self._ramp
        except asyncio.CancelledError:
            # A newer move superseded this one. That is normal, not a fault.
            pass

    async def _drive(self, steps: Iterable[Tuple[float, int]]) -> None:
        started = self._monotonic()
        for offset, level in steps:
            remaining = offset - (self._monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.send_level(level)

    def cancel_ramp(self) -> None:
        if self.is_ramping:
            self._ramp.cancel()
        self._ramp = None

    def close(self) -> None:
        self.cancel_ramp()
        closer = getattr(self._sender, "close", None)
        if closer is not None:
            closer()
