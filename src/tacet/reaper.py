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

The addresses below are the stock `Default.ReaperOSC` pattern names, verified
on 2026-09-08 against Reaper on the development Mac: `/play`, `/record` and
`/stop` all arrive carrying 1.0 or 0.0, and `/time` carries seconds as a float.
They remain configuration rather than constants, because the pattern file is
user-editable; verify them against the installed one before a game.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from . import osc
from .net import Sender, TransportError, UdpSender

#: Reaper and the box share a machine for now (design.md 5.9).
DEFAULT_HOST = "127.0.0.1"
#: The port Reaper listens on: "local listen port" in its OSC device settings.
DEFAULT_SEND_PORT = 8000
#: The port Reaper sends feedback to: its "device port".
DEFAULT_RECEIVE_PORT = 9000

#: How long we wait for the `/time` stream before calling the link lost.
#:
#: Reaper only feeds back while the transport is *moving* - about 11 Hz of
#: `/time` while rolling, and a single burst on each transport change. Parked
#: and stopped it sends nothing at all. So this is not a general staleness
#: timeout: it only means anything while Reaper should be streaming. See
#: `Liveness`.
DEFAULT_FEEDBACK_TIMEOUT = 2.0

_ACTION_PREFIX = "/action"


@dataclass(frozen=True)
class AddressMap:
    """Reaper's OSC addresses. Configuration, not constants: users can edit
    `Default.ReaperOSC`. These defaults match the stock pattern file."""

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


class Liveness(StrEnum):
    """What silence from Reaper means right now.

    Reaper's OSC feedback is edge-driven: it streams `/time` while the
    transport moves, sends one burst per transport change, and is otherwise
    completely silent. There is no heartbeat and no way to ask - probing
    `/device/track/count` draws a reply only when the value actually changes,
    so it cannot be used as a ping (measured 2026-09-08).

    That makes a single "is it fresh" flag wrong in both directions. Silence
    while Reaper should be streaming is a fault; the same silence while it sits
    parked is just Reaper sitting parked. Interpreting the second as a fault is
    what made the UI cry wolf through the whole pre-game window.
    """

    #: Reaper has never said anything, or has never said what it was doing.
    UNKNOWN = "unknown"
    #: Heard from within the timeout. The reading is current.
    LIVE = "live"
    #: Silent, but the last thing it said was that it had stopped. Expected.
    QUIET = "quiet"
    #: Silent while it should have been streaming `/time`. The link is gone.
    LOST = "lost"


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
    #: How many `/record` reports have arrived, whichever way they went. A
    #: count rather than a time, so "answered after the send" cannot be fooled
    #: by two readings of a coarse clock that happen to be equal.
    record_reports: int = 0

    def is_fresh(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> bool:
        """Whether Reaper has spoken within the timeout.

        Says nothing about whether the link is healthy: Reaper is silent
        whenever it is parked. Use `liveness` to tell those apart.
        """
        if self.last_packet is None:
            return False
        return (now - self.last_packet) <= timeout

    def liveness(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> Liveness:
        """Read silence in the light of what Reaper was last doing.

        The safety property: QUIET is only reachable when the last thing heard
        was that the transport had stopped, so believing a stale reading can
        only ever under-claim. A recorder that dies while parked keeps
        rendering as "stopped", which remains true. Nothing here can render a
        dead recorder as rolling - that path is LOST.
        """
        if self.last_packet is None:
            return Liveness.UNKNOWN
        if (now - self.last_packet) <= timeout:
            return Liveness.LIVE
        if self.playing or self.recording:
            return Liveness.LOST
        if self.playing is None and self.recording is None:
            return Liveness.UNKNOWN
        return Liveness.QUIET


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
                state = replace(state, recording=recording, record_reports=state.record_reports + 1)
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


@dataclass(frozen=True)
class RecordRequest:
    """A `/record` the box sent, and what Reaper had reported before it.

    Any `/record` report after the send answers it, whichever way it went: an
    explicit answer means the record state is known again.
    """

    sent_at: float
    reports_before: int

    def answered_by(self, state: TransportState) -> bool:
        return state.record_reports > self.reports_before


def record_refusal(
    state: TransportState,
    now: float,
    *,
    timeout: float = DEFAULT_FEEDBACK_TIMEOUT,
    request: RecordRequest | None = None,
) -> str | None:
    """Why the box will not send a record command, or None if it will.

    Reaper's `/record` is a toggle, not a start. Sent at a recorder that is
    already rolling it stops the recording - which is the stop button design.md
    5.9 refuses to put on this screen, reached by tapping "start" twice. Each
    home game is a single irreplaceable sample, so the box declines rather than
    risk it.

    Silence is what makes the safe cases decidable. Reaper streams `/time`
    while the transport moves and says nothing at all when it is parked, so
    having heard nothing recently means the transport is stopped and `/record`
    can only start it. When the transport *is* moving and Reaper has not said
    whether it is recording - a box restarted mid-game, since transport state
    is announced only when it changes - there is no way to tell a safe send
    from one that would end the recording, and the box says so instead of
    guessing.

    Nor will it send while its own last `/record` is unanswered (#28). Two taps
    that both leave before Reaper's confirmation comes back are a start and a
    stop, and stalled wifi delivers exactly that. The latch deliberately has no
    timeout: a timed release is what would let a lost confirmation turn the
    next tap into a stop, whereas holding it costs only a start done by hand in
    Reaper, which is recoverable.
    """
    liveness = state.liveness(now, timeout=timeout)
    if liveness is Liveness.LOST:
        return "Reaper has stopped answering. Start the recording in Reaper."
    if request is not None and not request.answered_by(state):
        return (
            f"Reaper has not confirmed the start sent {now - request.sent_at:.0f}s ago, "
            "so another tap could stop it. Check Reaper, and start it in Reaper if "
            "it is not recording."
        )
    if liveness is Liveness.LIVE:
        if state.recording:
            return "Reaper is already recording."
        if state.recording is None:
            return (
                "Reaper's transport is moving but it has not said whether it is "
                "recording. Start the recording in Reaper."
            )
    return None


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
        #: The last start sent. Never cleared: whether it has been answered is
        #: read from `state`, so there is no second copy of the truth to drift.
        self.record_request: RecordRequest | None = None

    @property
    def healthy(self) -> bool:
        return self.last_error is None

    # -- commands ---------------------------------------------------------

    def start_recording(self) -> None:
        """Arm and roll. There is no counterpart; stopping happens in Reaper.

        The request is remembered only once the send succeeded: a start that
        never reached Reaper has nothing a second tap could undo.
        """
        reports_before = self.state.record_reports
        self._send(self.addresses.record)
        self.record_request = RecordRequest(sent_at=self._monotonic(), reports_before=reports_before)

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
