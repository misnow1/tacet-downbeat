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

import math
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

#: How long we wait for a packet, or for the `/time` stream, before calling it
#: silent.
#:
#: What Reaper sends on the game rig (bench, 2026-10-04, #163): meters, about
#: 11 packets a second, whenever its audio device runs - parked or rolling.
#: `/time`, about 12 a second, only while the transport moves. `/record`,
#: `/play` and `/stop` only when they change, never in a launch or project-load
#: dump. Quitting is silent. So a packet means Reaper is there, `/time` means
#: it is moving, and this timeout is how long either may be missing. See
#: `Liveness`.
DEFAULT_FEEDBACK_TIMEOUT = 2.0

#: Why the box will not send a record command. Each is shown on the page beside
#: the grey button (#163) and quoted verbatim in docs/troubleshooting.md.
#:
#: Reaper says it is recording but its playhead has stopped (link lost).
RECORD_REFUSED_LOST = (
    "Reaper has stopped answering: it says it is recording but its playhead is not moving. "
    "Check Reaper, and start the recording there if it is not rolling."
)
#: The box's own last `/record` has had no answer (#28).
RECORD_REFUSED_UNANSWERED = (
    "Reaper has not confirmed the start the box sent, so another tap could stop it. "
    "Check Reaper, and start it there if it is not recording."
)
#: Nothing has been heard from Reaper lately: closed, or not sending feedback.
RECORD_REFUSED_SILENT = (
    "Reaper is not answering. Open it, with its audio device running, or start the recording in Reaper."
)
#: Reaper has said it is recording.
RECORD_REFUSED_ROLLING = "Reaper is already recording."
#: The transport is moving and Reaper has not said whether it is recording.
RECORD_REFUSED_MOVING = (
    "Reaper's transport is moving but it has not said whether it is recording. "
    "Check Reaper, and start the recording there if it is not."
)
#: The log already holds a recording and Reaper has not said whether it rolls.
RECORD_REFUSED_PRIOR = (
    "This log already holds a recording, and Reaper has not said whether it is still rolling. "
    "Check Reaper, and start it there if it is not."
)

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

    On the game rig Reaper streams meters whenever its audio device runs,
    `/time` only while the transport moves, and transport state only on change
    - never in a launch or project-load dump - and quitting is silent (bench,
    2026-10-04, #163). There is no heartbeat and no way to ask - probing
    `/device/track/count` draws a reply only when the value actually changes,
    so it cannot be used as a ping (measured 2026-09-08).

    That makes a single "is it fresh" flag wrong in both directions. Silence
    while Reaper should be streaming `/time` is a fault; the same silence while
    it sits parked is just Reaper sitting parked. Interpreting the second as a
    fault is what made the UI cry wolf through the whole pre-game window.
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
    #: Monotonic time `position` itself last arrived. Not `last_packet`: any
    #: packet proves the link, but only `/time` says where the playhead is.
    position_at: float | None = None
    #: Monotonic time of the last `/play` or `/record` report, whichever way it
    #: went. A transport change is announced before the first `/time` of a
    #: roll, so this is what keeps a recording that has just started from
    #: reading LOST in the moment before its playhead arrives.
    transport_at: float | None = None
    #: How many `/record` reports have arrived, whichever way they went. A
    #: count rather than a time, so "answered after the send" cannot be fooled
    #: by two readings of a coarse clock that happen to be equal.
    record_reports: int = 0

    def is_fresh(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> bool:
        """Whether any packet has arrived within the timeout.

        About any packet and nothing more: it is not liveness (see `liveness`,
        which reads a rolling Reaper's silence differently) and it says nothing
        about whether the transport is moving (`clock_running`).
        """
        if self.last_packet is None:
            return False
        return (now - self.last_packet) <= timeout

    def clock_running(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> bool:
        """Whether `/time` has arrived within the timeout: the transport is moving.

        On `/time` alone - meters flow parked or rolling on this rig (#35, #163).
        """
        if self.position_at is None:
            return False
        return (now - self.position_at) <= timeout

    @property
    def rolling(self) -> bool:
        """Whether Reaper's last word was that the transport is playing or recording."""
        return self.playing is True or self.recording is True

    def current_position(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> float | None:
        """Where the playhead is now, or None if that is not known.

        Reaper streams `/time` while the transport moves and stops when it
        parks, so a position that arrived within the timeout is current and one
        older than that is only where the transport was last seen. Judged on
        `/time` alone rather than on liveness: this rig streams meter data
        continuously while parked, which kept a link reading live and a
        position from a finished take looking current for as long as it sat
        there (#35).
        """
        if self.position is None or not self.clock_running(now, timeout=timeout):
            return None
        return self.position

    def liveness(self, now: float, *, timeout: float = DEFAULT_FEEDBACK_TIMEOUT) -> Liveness:
        """Read silence in the light of what Reaper was last doing.

        The safety property: QUIET is only reachable when the last thing heard
        was that the transport had stopped, so believing a stale reading can
        only ever under-claim. A recorder that dies while parked keeps
        rendering as "stopped", which remains true. Nothing here can render a
        dead recorder as rolling - that path is LOST.

        While Reaper's last word is that it is rolling, LIVE needs the clock,
        not just any packet: meters flow whenever its audio device runs, so a
        Reaper relaunched parked after quitting mid-take would otherwise read
        LIVE with a stale "recording" (#163). The clock is `/time`, or a
        `/play` or `/record` report, which a roll announces just before its
        first `/time`.
        """
        if self.last_packet is None:
            return Liveness.UNKNOWN
        if self.rolling:
            heard = max(
                (t for t in (self.position_at, self.transport_at) if t is not None),
                default=self.last_packet,
            )
            return Liveness.LIVE if (now - heard) <= timeout else Liveness.LOST
        if (now - self.last_packet) <= timeout:
            return Liveness.LIVE
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
    if isinstance(value, int | float) and math.isfinite(value):
        # Finite only: a position is stamped onto log entries, and JSON has no
        # NaN or Infinity to write it as.
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
                state = replace(
                    state,
                    recording=recording,
                    record_reports=state.record_reports + 1,
                    transport_at=now,
                )
        elif message.address == addresses.playing:
            playing = _as_bool(value)
            if playing is not None:
                state = replace(state, playing=playing, transport_at=now)
        elif message.address == addresses.position:
            position = _as_float(value)
            if position is not None:
                state = replace(state, position=position, position_at=now)
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
    prior_recording: bool = False,
) -> str | None:
    """Why the box will not send a record command, or None if it will.

    Reaper's `/record` is a toggle, not a start. Sent at a recorder that is
    already rolling it stops the recording - which is the stop button design.md
    5.9 refuses to put on this screen, reached by tapping "start" twice. Each
    home game is a single irreplaceable sample, so the box sends only on
    positive evidence that `/record` can only start. A start done by hand in
    Reaper is recoverable; a stop is not.

    The evidence is presence from any packet and motion from `/time` alone.
    Reaper streams meters whenever its audio device runs, parked or rolling, so
    a packet proves Reaper is there but not that it is parked; `/time` arrives
    only while the transport moves. Transport state is announced only when it
    changes, never in a launch or project-load dump, so after a box restart the
    record state is unknown while Reaper may be rolling. Two backstops cover a
    `/time` pattern that is missing: a Reaper that says it is rolling and sends
    no clock reads LINK LOST (see `TransportState.liveness`), and a log that
    already holds a recording, with the record state unknown, is refused as
    well - the one case where the box may have been restarted mid-take.

    Nor will it send while its own last `/record` is unanswered (#28). Two taps
    that both leave before Reaper's confirmation comes back are a start and a
    stop, and stalled wifi delivers exactly that. The latch deliberately has no
    timeout: a timed release is what would let a lost confirmation turn the
    next tap into a stop, whereas holding it costs only a start done by hand in
    Reaper, which is recoverable.
    """
    liveness = state.liveness(now, timeout=timeout)
    if liveness is Liveness.LOST:
        return RECORD_REFUSED_LOST
    if request is not None and not request.answered_by(state):
        return RECORD_REFUSED_UNANSWERED
    if liveness is not Liveness.LIVE:
        return RECORD_REFUSED_SILENT
    if state.recording is True:
        return RECORD_REFUSED_ROLLING
    if state.recording is None:
        if state.clock_running(now, timeout=timeout):
            return RECORD_REFUSED_MOVING
        if prior_recording:
            return RECORD_REFUSED_PRIOR
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
