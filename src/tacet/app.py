"""Everything wired together, with no HTTP in sight.

The state machine decides, the console client moves the fader, the annotation
log records, the recorder is driven and read. This module is the only place
those four meet, and it is deliberately transport-free so the whole operator
flow can be tested without a server, a socket or a browser.

`snapshot` is what the UI renders. It draws one distinction carefully:
**fader position is commanded, recording state is confirmed.** The DM7 cannot
answer (design.md 5.3) while Reaper can (5.9), and presenting them alike would
be exactly the silent degradation CLAUDE.md forbids. So `fader.confirmed` is
always False, and `recording.confirmed` is true only while the feedback is fresh.

There is no stop. Stopping a recording is done deliberately, in Reaper.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import annotations as ann
from . import dm7, state
from .net import TransportError
from .reaper import Liveness, ReaperClient, record_refusal

#: What an operator button that also moves the fader asks the machine to do.
_ACTIONS: Mapping[ann.Action, state.Command] = {
    ann.Action.OPEN: state.Command.TRIGGER,
    # The same command: a ride-in reaches the same state by a slower route, and
    # the machine has no opinion about how long a move takes.
    ann.Action.OPEN_SLOW: state.Command.TRIGGER,
    ann.Action.RELEASE: state.Command.RELEASE,
}

#: How often the page is told where a fade has reached.
#:
#: The ramp ticks at 50 Hz, so pushing every step would be a hundred frames
#: across a link that is stadium wifi. Ten a second reads as movement and is an
#: order of magnitude less traffic; the exact number on the way down is not what
#: anyone is reading, only that it is going.
MOVE_PUSH_SECONDS = 0.1

#: Keys the box writes itself when the machine moves.
ARMED = "armed"
#: Written when the box reaches STANDING DOWN, not when it is asked to. Asked
#: while the fader is up, it fades first, and a trigger can snap it back before
#: the fade lands; the request and its cancellation are entries of their own so
#: the log never says the box stood down when it did not (#50).
STOOD_DOWN = "stood-down"
STAND_DOWN_REQUESTED = "stand-down-requested"
STAND_DOWN_CANCELLED = "stand-down-cancelled"
COMMANDED = "commanded"
#: How a move that ran in the background ended. A snap open is awaited, so its
#: `commanded` entry already knows whether it was delivered; a fade or ride-in
#: is still running when that entry is written, so `delivered` there is null
#: and one of these follows - unless a newer move replaced it, in which case
#: the newer `commanded` entry records where the fader had got to (#50).
MOVE_LANDED = "move-landed"
MOVE_FAILED = "move-failed"
#: Not a fourth spelling of the string: `tacet.markers` anchors the timeline to
#: this event and the box warns when a log already holds one, so all of them
#: have to agree or the warning goes quiet.
RECORDING_STARTED = ann.ANCHOR_EVENT

#: Shown when `/record` could not be sent. Quoted in docs/troubleshooting.md.
RECORD_SEND_FAILED = "Could not send the start to Reaper ({error}). Nothing started; tap again, or start it in Reaper."


def session_entries(before: state.Machine, after: state.Machine) -> tuple[str, ...]:
    """The session entries one transition earns, from the machines alone.

    Pure, so every path to and from STANDING DOWN is decided in one place
    rather than at each call site that happens to cause one.
    """
    down = state.State.STANDING_DOWN
    entries: list[str] = []
    if before.state is down and after.state is not down:
        entries.append(ARMED)
    if after.pending_stand_down and not before.pending_stand_down:
        entries.append(STAND_DOWN_REQUESTED)
    if before.pending_stand_down and not after.pending_stand_down and after.state is not down:
        entries.append(STAND_DOWN_CANCELLED)
    if after.state is down and before.state is not down:
        entries.append(STOOD_DOWN)
    return tuple(entries)


class App:
    def __init__(
        self,
        *,
        console: dm7.Dm7Client,
        log: ann.AnnotationLog,
        recorder: ReaperClient | None = None,
        machine: state.Machine | None = None,
        fade_seconds: float = dm7.DEFAULT_FADE_SECONDS,
        slow_open_seconds: float = dm7.DEFAULT_SLOW_OPEN_SECONDS,
        open_level: int = dm7.UNITY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._log = log
        self._recorder = recorder
        self.machine = machine if machine is not None else state.Machine()
        self._fade_seconds = fade_seconds
        self._slow_open_seconds = slow_open_seconds
        self._open_level = open_level
        self._monotonic = monotonic

        self._move_task: asyncio.Task[None] | None = None
        #: Where the move in flight is heading, or None when nothing is
        #: moving. Set when the tap lands rather than read back from the
        #: ramp, so the first frame the page gets already carries it.
        self._move_target: int | None = None
        self._move_push: asyncio.Task[None] | None = None
        self._last_refusal: str | None = None
        #: Whether `_last_refusal` came from the record button. That refusal is
        #: derived from Reaper's state, so it has to clear itself when the
        #: state moves on - otherwise the screen keeps saying "already
        #: recording" at a recorder that has since stopped.
        self._refused_recording = False
        self._listeners: list[Callable[[], None]] = []

    # -- operator actions -------------------------------------------------

    async def arm(self) -> state.Outcome:
        return await self._command(state.Command.ARM)

    async def stand_down(self) -> state.Outcome:
        return await self._command(state.Command.STAND_DOWN)

    async def trigger(self, *, source: state.Source = state.Source.OPERATOR, detail: str = "") -> state.Outcome:
        return await self._command(state.Command.TRIGGER, source=source, detail=detail)

    async def release(self, *, source: state.Source = state.Source.OPERATOR, detail: str = "") -> state.Outcome:
        return await self._command(state.Command.RELEASE, source=source, detail=detail)

    async def _command(
        self,
        command: state.Command,
        *,
        source: state.Source = state.Source.OPERATOR,
        detail: str = "",
        open_seconds: float | None = None,
    ) -> state.Outcome:
        before = self.machine
        event = state.Event(command, source=source, detail=detail, gradual=open_seconds is not None)
        outcome = state.step(before, event)
        self.machine = outcome.machine
        self._last_refusal = outcome.refusal

        if outcome.fader is not None:
            await self._move_fader(outcome.fader, source=source, detail=detail, open_seconds=open_seconds)
        for key in session_entries(before, outcome.machine):
            self._record(
                key,
                data={"state": self.machine.state.value},
                project_seconds=self._playhead(),
            )
        self._notify()
        return outcome

    async def _move_fader(
        self,
        command: state.FaderCommand,
        *,
        source: state.Source,
        detail: str,
        open_seconds: float | None = None,
    ) -> None:
        # A console that cannot be reached must not take the box down with it.
        # The fault is recorded and shown; the operator stays in control.
        failed = False
        # A move left running in the background has delivered nothing yet.
        background = command is state.FaderCommand.FADE or open_seconds is not None
        try:
            if command is state.FaderCommand.OPEN:
                self._cancel_move()
                if open_seconds is None:
                    # The ordinary open, awaited: it is one packet and 20 ms,
                    # and a missed downbeat is unrecoverable, so it goes out
                    # before anything else gets a turn.
                    await self._console.open(self._open_level)
                else:
                    # A ride-in takes over a second, and `annotate` deliberately
                    # moves the fader before writing the log. Awaiting it here
                    # would hold the annotation - and the playhead stamped on
                    # it - back by the whole length of the ramp, timestamping
                    # the tap where the ramp ended rather than where it began.
                    self._start_slow_open(open_seconds)
            else:
                self._start_fade()
        except TransportError:
            failed = True
        # A fade is asynchronous, so `level` is where the fader was when the
        # command was issued. `target` is where it is going, which is the
        # unambiguous half when reading a log back.
        target = self._open_level if command is state.FaderCommand.OPEN else dm7.MINUS_INF
        self._record(
            COMMANDED,
            data={
                "level": self._console.commanded_level,
                "db": _finite(dm7.to_db(self._console.commanded_level)),
                "target": target,
                "target_db": _finite(dm7.to_db(target)),
                "command": command.value,
                "source": source.value,
                "detail": detail,
                "state": self.machine.state.value,
                "delivered": None if background and not failed else self._console.healthy,
            },
            project_seconds=self._playhead(),
        )
        if failed:
            self._move_failed(target)

    def _move_failed(self, target: int) -> None:
        """A send failed partway through a move.

        The machine is told, so the command that started the move retries it
        rather than being ignored as already done (#27), and the log is told,
        because the `commanded` entry before this one describes a move that did
        not happen. Deliberately not a refusal: nothing was declined, and the
        why line already says what to do.
        """
        self.machine = state.step(self.machine, state.Event(state.Command.MOVE_FAILED)).machine
        self._record(
            MOVE_FAILED,
            data={**self._move_end(target), "error": self._console.last_error},
            project_seconds=self._playhead(),
        )

    def _move_landed(self, target: int) -> None:
        """A background move sent its last step. The `commanded` entry that
        started it could not say so, because it was written first."""
        self._record(MOVE_LANDED, data=self._move_end(target), project_seconds=self._playhead())

    def _ride_in_landed(self) -> None:
        """Tell the machine the ride-in is over, so a later trigger is a
        confirmation again rather than a snap (#45). Stepped directly, like a
        failed move: nothing was tapped, so nothing is refused or cleared."""
        self.machine = state.step(self.machine, state.Event(state.Command.RIDE_IN_COMPLETE)).machine
        self._move_landed(self._open_level)

    def _move_end(self, target: int) -> dict[str, Any]:
        """Where a move ended up against where it was going, for the log."""
        return {
            "level": self._console.commanded_level,
            "db": _finite(dm7.to_db(self._console.commanded_level)),
            "target": target,
            "target_db": _finite(dm7.to_db(target)),
            "state": self.machine.state.value,
        }

    # -- the fade ---------------------------------------------------------

    def _start_fade(self) -> None:
        self._cancel_move()
        # After the cancel, which is what clears the last destination.
        self._move_target = dm7.MINUS_INF
        self._move_task = asyncio.ensure_future(self._run_fade())
        self._move_push = asyncio.ensure_future(self._push_while_moving())

    def _start_slow_open(self, seconds: float) -> None:
        """Ride the fader up over `seconds` instead of snapping it.

        Runs as a task for the same reason the fade does: the operator has
        already tapped, and everything after the tap - the log entry, its
        playhead, the page - must not wait for the ramp to finish.
        """
        self._move_target = self._open_level
        self._move_task = asyncio.ensure_future(self._run_slow_open(seconds))
        self._move_push = asyncio.ensure_future(self._push_while_moving())

    async def _run_slow_open(self, seconds: float) -> None:
        this = asyncio.current_task()
        try:
            await self._console.open(self._open_level, seconds=seconds)
        except TransportError:
            if self._move_task is this:
                self._move_failed(self._open_level)
            return
        except asyncio.CancelledError:
            return
        else:
            if self._move_task is this:
                self._ride_in_landed()
        finally:
            # No command follows: the machine reached OPEN when the tap landed,
            # and only the gesture was still running.
            self._settle(this)

    def _settle(self, move: asyncio.Task[Any] | None) -> None:
        """However a move ended, stop sweeping and push the settled value once -
        if it is still the current move.

        Cancelling a move only asks. The cancelled task wakes a loop iteration
        later, after its replacement has started, and by then the push and the
        target belong to the replacement: clearing them stopped a ride-in's
        updates and blanked a close's destination (#34).
        """
        if self._move_task is not move:
            return
        self._stop_move_push()
        self._move_target = None
        self._notify()

    def _cancel_move(self) -> None:
        if self._move_task is not None and not self._move_task.done():
            self._move_task.cancel()
        self._move_task = None
        self._move_target = None
        self._stop_move_push()

    def _stop_move_push(self) -> None:
        if self._move_push is not None and not self._move_push.done():
            self._move_push.cancel()
        self._move_push = None

    async def _push_while_moving(self) -> None:
        """Send the sweeping level to the page while the close runs.

        The console client already moves `commanded_level` as it sends each ramp
        step, but nothing was telling the page, so it held the pre-fade number
        for the whole two seconds and then jumped to -inf. That number is the
        one the operator reads against the console, and a close is exactly when
        they are looking at it.

        Never the only thing that stops: the move's own task cancels this as it
        settles and `_cancel_move` cancels it when a newer move takes over, so it
        cannot outlive the move it is describing.
        """
        while True:
            await asyncio.sleep(MOVE_PUSH_SECONDS)
            self._notify()

    async def _run_fade(self) -> None:
        this = asyncio.current_task()
        try:
            await self._console.fade_out(self._fade_seconds)
        except TransportError:
            if self._move_task is this:
                self._move_failed(dm7.MINUS_INF)
            return
        except asyncio.CancelledError:
            return
        else:
            if self._move_task is this:
                self._move_landed(dm7.MINUS_INF)
        finally:
            self._settle(this)
        # Only complete the fade if nothing replaced it meanwhile. RELEASING on
        # its own is not enough: a snap back to OPEN and a second close leave
        # the machine RELEASING again, for a fade that is still running.
        if self._move_task is this and self.machine.state is state.State.RELEASING:
            await self._command(state.Command.FADE_COMPLETE)

    async def wait_for_fade(self) -> None:
        """Block until any fade in flight has finished. For tests and shutdown."""
        task = self._move_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- annotation -------------------------------------------------------

    async def annotate(self, event_key: str, *, data: Mapping[str, Any] | None = None) -> ann.Entry | None:
        """Record what the operator saw, and act on it when it says to.

        The fader buttons do both. Asking for the move and the reason as two
        separate taps meant the reason was the one that got dropped when the
        night got busy - and it is the half nothing else can recover, since
        design.md 9 reconstructs the moves themselves from the post-DCA
        reference channel.

        Returns None when the entry did not reach the disk. The move has still
        happened, and the snapshot's `log` says why the entry did not.

        The key and the data are checked before anything acts on them: a
        box-only event is refused, and data the log would not hold raises
        `DataError` here rather than inside the log after the fader moved.
        """
        event = ann.operator_event(event_key)
        data = ann.operator_data(data)
        if event.action is not None:
            # The move goes first. A missed downbeat is unrecoverable and must
            # not wait behind a log write.
            await self._command(
                _ACTIONS[event.action],
                detail=event_key,
                open_seconds=self._slow_open_seconds if event.action is ann.Action.OPEN_SLOW else None,
            )
        # Recorded whatever the machine did with it, including a refusal: the
        # operator saw what they saw, and a log that only kept the accepted
        # taps would misrepresent the night.
        entry = self._record(event_key, data=data, project_seconds=self._playhead())
        self._notify()
        return entry

    async def start_span(self, event_key: str) -> str | None:
        """The new span's id, or None when its start did not reach the disk -
        in which case it is not open, and the button still offers to start it."""
        ann.operator_event(event_key)
        span_id: str | None
        try:
            span_id = self._log.start_span(event_key, project_seconds=self._playhead())
        except ann.WriteError:
            span_id = None  # see _record
        self._notify()
        return span_id

    async def end_span(self, span_id: str) -> ann.Entry | None:
        """None when the end did not reach the disk. The span stays open, so
        the button still offers to end it and the next tap saves the end."""
        entry: ann.Entry | None
        try:
            entry = self._log.end_span(span_id, project_seconds=self._playhead())
        except ann.WriteError:
            entry = None  # see _record
        self._notify()
        return entry

    def _record(
        self,
        event_key: str,
        *,
        data: Mapping[str, Any] | None = None,
        project_seconds: float | None = None,
    ) -> ann.Entry | None:
        """Write one instant, or None if it did not reach the disk.

        Every log write in the box goes through here or the two span methods,
        and none of them lets a `WriteError` out. The failure has already been
        counted on the log's health, which the snapshot carries to the page.
        Raised out of a fader route it skipped the push after a move that had
        happened; raised inside a fade it ended the task before FADE_COMPLETE
        and left the machine in RELEASING. The fader, the machine and the page
        all carry on.
        """
        try:
            return self._log.record(event_key, data=data, project_seconds=project_seconds)
        except ann.WriteError:
            return None

    def _playhead(self) -> float | None:
        """Reaper's project position, or None when it cannot be trusted.

        Stamped onto every entry as it is written, so the log carries Reaper's
        own number rather than depending on arithmetic over our clock across a
        three-hour game. `markers.position_of` prefers it, which is also what
        makes a restarted recording merely untidy instead of wrong: entries
        placed by playhead do not care which recording the anchor came from.

        Freshness is the whole test. Reaper streams `/time` while the transport
        moves and stops when it parks, so a reading that arrived within the
        timeout is current by construction; one older than that is wherever the
        transport was last seen, and stamping it would place a marker at a
        confidently wrong point. Unstamped falls back to the arithmetic, which
        is what happened before this existed. The freshness is of `/time`
        itself, not of the link: see `TransportState.current_position`.

        The reported position is used as-is, never extrapolated forward by the
        time since it arrived. `/time` lands about eleven times a second while
        rolling, so the residual is tens of milliseconds - far below the
        operator's reaction time, which the annotation already carries - and
        inventing the difference would put our clock back in the answer and be
        wrong outright in the moment after the transport parks.
        """
        if self._recorder is None:
            return None
        return self._recorder.state.current_position(self._monotonic())

    # -- recorder ---------------------------------------------------------

    async def start_recording(self) -> None:
        """Roll. There is no counterpart here; stopping happens in Reaper.

        Reaper's `/record` is a toggle, so a second tap would stop the
        recording - the stop button design.md 5.9 keeps off this screen,
        reached by pressing start twice. The box refuses instead, and says why.
        """
        refusal = self.record_refusal
        if refusal is not None:
            self._last_refusal = refusal
            self._refused_recording = True
            self._notify()
            return
        if self._recorder is not None:
            try:
                self._recorder.start_recording()
            except TransportError as exc:
                # Nothing reached Reaper, so there is no start to log and
                # nothing a second tap could stop: the button stays live.
                self._last_refusal = RECORD_SEND_FAILED.format(error=exc)
                self._refused_recording = False
                self._notify()
                return
        # Deliberately not stamped with a playhead. The command has just gone
        # out and Reaper has not begun rolling, so whatever position it last
        # reported is where the transport was parked, not where this recording
        # starts. The anchor is the one entry whose position is genuinely not
        # known yet, and guessing it would misplace everything measured from it.
        self._record(RECORDING_STARTED)
        self._last_refusal = None
        self._refused_recording = False
        self._notify()

    @property
    def record_refusal(self) -> str | None:
        """Why the record button will not fire, or None if it will."""
        if self._recorder is None:
            return None
        return record_refusal(self._recorder.state, self._monotonic(), request=self._recorder.record_request)

    def handle_recorder_packet(self, packet: bytes) -> None:
        if self._recorder is not None:
            self._recorder.handle_packet(packet)
            self._notify()

    # -- what the UI renders ----------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        transport = self._recorder.state if self._recorder is not None else None
        liveness = transport.liveness(self._monotonic()) if transport is not None else Liveness.UNKNOWN
        # QUIET is silence from a Reaper that told us it had stopped, which is
        # all Reaper ever does when parked. Believing it is safe: that reading
        # can only under-claim, never show a dead recorder as rolling.
        believed = liveness in (Liveness.LIVE, Liveness.QUIET)
        refusal = self._last_refusal
        if self._refused_recording and self.record_refusal is None:
            refusal = None
        # Straight from the console client, so a ride-in points at where it is
        # going exactly as a close does. A settled fader has nothing to point
        # at: the commanded level already *is* the expectation.
        target = self._move_target
        return {
            "state": self.machine.state.value,
            "why": state.describe(self.machine),
            "refusal": refusal,
            "detector_enabled": self.machine.allow_detector,
            "fader": {
                "commanded": self._console.commanded_level,
                "db": _finite(self._console.commanded_db),
                # Always false. The protocol is write-only; see the module docstring.
                "confirmed": False,
                "healthy": self._console.healthy,
                "error": self._console.last_error,
                # Where a move in flight is heading, or None when nothing is
                # moving. Still expectation, not confirmation: it is where the
                # box intends to put the fader, which is the most the write-only
                # protocol can ever support.
                "target": target,
                "target_db": None if target is None else _finite(dm7.to_db(target)),
                "moving": self._console.is_ramping,
            },
            "recording": {
                "known": believed and transport is not None and transport.recording is not None,
                "recording": bool(transport.recording) if transport is not None else False,
                "position": transport.position if transport is not None else None,
                "confirmed": believed,
                "liveness": liveness.value,
                # False once Reaper is known to be rolling: /record is a toggle
                # and a second press would stop it (design.md 5.9).
                "can_start": self.record_refusal is None,
                "healthy": self._recorder.healthy if self._recorder is not None else True,
            },
            "buttons": [
                {
                    "key": event.key,
                    "label": event.label,
                    "category": event.category.value,
                    "kind": event.kind.value,
                    "action": event.action.value if event.action is not None else None,
                }
                for event in ann.BUTTONS
            ],
            # Each names its event, so the page matches a button to its span
            # without parsing an id (see AnnotationLog.open_spans).
            "open_spans": [{"span_id": span_id, "event": event} for span_id, event in self._log.open_spans().items()],
            "log": {"path": str(self._log.path), **_health(self._log.health)},
            # Only the live markers. They can be rebuilt from the log afterwards.
            "mirror": _health(self._log.mirror_health),
        }

    # -- change notification ----------------------------------------------

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in self._listeners:
            listener()


def _health(health: ann.WriteHealth) -> dict[str, Any]:
    return {"healthy": health.healthy, "error": health.error, "failures": health.failures}


def _finite(value: float) -> float | None:
    """JSON has no -inf. A closed fader reads as null rather than a lie."""
    return None if value in (float("-inf"), float("inf")) else value
