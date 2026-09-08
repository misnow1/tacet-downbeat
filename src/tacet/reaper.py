"""Reaper transport control and feedback over OSC.

Reaper records the game (design.md 5.9) with DVS as its audio device, and for
now runs on the same machine as the box, so the default host is loopback. Treat
the endpoint as configuration: the Phase 2 Linux move separates them.

Unlike the console, Reaper's OSC is genuinely bidirectional, so recording state
here is *confirmed* rather than merely commanded. That distinction is the whole
reason this module tracks freshness: a state that has gone stale is unknown, not
"stopped", and the UI has to be able to tell the difference.

There is no stop. Each home game is a single irreplaceable sample, and a stop
button does not belong on a screen being tapped by someone watching a field.
Stopping is done deliberately, in Reaper.

**The addresses below are the stock `Default.ReaperOSC` pattern names and have
not yet been checked against a live console.** They are configuration rather
than constants precisely so a mismatch is a config change and not a rewrite;
verify them against the installed pattern file before a game.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from . import osc
from .net import Sender, TransportError, UdpSender

#: Reaper and the box share a machine for now (design.md 5.9).
DEFAULT_HOST = "127.0.0.1"
#: The port Reaper listens on: "local listen port" in its OSC device settings.
DEFAULT_SEND_PORT = 8000
#: The port Reaper sends feedback to: its "device port".
DEFAULT_RECEIVE_PORT = 9000

#: How long a transport reading stays trustworthy. Reaper feeds back
#: continuously, so silence for longer than this means the link is gone.
DEFAULT_FEEDBACK_TIMEOUT = 2.0

_ACTION_PREFIX = "/action"


@dataclass(frozen=True)
class AddressMap:
    """Reaper's OSC addresses. Configuration, not constants: users can edit
    `Default.ReaperOSC`, and these defaults are unverified."""

    play: str = "/play"
    pause: str = "/pause"
    record: str = "/record"
    #: Never sent. Held here so the "no stop" rule can be asserted against it.
    stop: str = "/stop"
    #: Feedback.
    playing: str = "/play"
    recording: str = "/record"
    position: str = "/time"


DEFAULT_ADDRESSES = AddressMap()


@dataclass(frozen=True)
class TransportState:
    """What Reaper last told us.

    Every field starts `None`, meaning *not heard from*, which is a different
    thing from stopped. Rendering unknown as "not recording" would be exactly
    the silent degradation CLAUDE.md forbids.
    """

    playing: bool | None = None
    recording: bool | None = None
    position: float | None = None
    #: Monotonic time of the last valid packet, whatever it contained.
    last_packet: float | None = None

    def is_fresh(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> bool:
        """Whether this reading can still be believed."""
        if self.last_packet is None:
            return False
        return (now - self.last_packet) <= timeout


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def apply_feedback(
    packet: bytes,
    state: TransportState,
    *,
    now: float,
    addresses: AddressMap = DEFAULT_ADDRESSES,
) -> TransportState:
    """Fold one received packet into the transport state.

    Pure, so every Reaper message this project cares about can be tested without
    Reaper running.

    Unrecognised addresses are ignored rather than rejected: Reaper feeds back a
    great deal that is none of our business. A malformed packet is ignored
    entirely - a half-received datagram must not take down a box that is
    mid-game - and notably does not refresh liveness.
    """
    try:
        decoded = osc.decode_packet(packet)
    except osc.OscError:
        return state

    messages: list[osc.Message] = []
    _collect(decoded, messages)

    state = replace(state, last_packet=now)
    for message in messages:
        if not message.args:
            continue
        value = message.args[0]
        if message.address == addresses.recording:
            recording = _as_bool(value)
            if recording is not None:
                state = replace(state, recording=recording)
        elif message.address == addresses.playing:
            playing = _as_bool(value)
            if playing is not None:
                state = replace(state, playing=playing)
        elif message.address == addresses.position:
            position = _as_float(value)
            if position is not None:
                state = replace(state, position=position)
    return state


def _collect(packet: osc.Message | osc.Bundle, into: list[osc.Message]) -> None:
    if isinstance(packet, osc.Message):
        into.append(packet)
        return
    for element in packet.elements:
        _collect(element, into)


class ReaperClient:
    """Commands Reaper's transport and tracks what it reports back.

    Deliberately has no stop; see the module docstring.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SEND_PORT,
        *,
        addresses: AddressMap = DEFAULT_ADDRESSES,
        sender: Sender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.addresses = addresses
        self._sender = sender if sender is not None else UdpSender(host, port)
        self._monotonic = monotonic
        self.state = TransportState()
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.last_error is None

    # -- commands ---------------------------------------------------------

    def start_recording(self) -> None:
        """Arm and roll. There is no counterpart; stopping happens in Reaper."""
        self._send(self.addresses.record)

    def play(self) -> None:
        self._send(self.addresses.play)

    def pause(self) -> None:
        self._send(self.addresses.pause)

    def run_action(self, command_id: int) -> None:
        """Trigger a Reaper action by command id - the generic escape hatch for
        anything the pattern file does not name."""
        self._send(f"{_ACTION_PREFIX}/{command_id}")

    def _send(self, address: str) -> None:
        try:
            self._sender.send(osc.encode_message(address))
        except TransportError as exc:
            self.last_error = str(exc)
            raise
        self.last_error = None

    # -- feedback ---------------------------------------------------------

    def handle_packet(self, packet: bytes) -> TransportState:
        self.state = apply_feedback(packet, self.state, now=self._monotonic(), addresses=self.addresses)
        return self.state
