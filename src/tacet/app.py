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
    ann.Action.RELEASE: state.Command.RELEASE,
}

#: Keys the box writes itself when the machine moves.
ARMED = "armed"
STOOD_DOWN = "stood-down"
COMMANDED = "commanded"
RECORDING_STARTED = "recording-started"


class App:
    def __init__(
        self,
        *,
        console: dm7.Dm7Client,
        log: ann.AnnotationLog,
        recorder: ReaperClient | None = None,
        machine: state.Machine | None = None,
        fade_seconds: float = dm7.DEFAULT_FADE_SECONDS,
        open_level: int = dm7.UNITY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._log = log
        self._recorder = recorder
        self.machine = machine if machine is not None else state.Machine()
        self._fade_seconds = fade_seconds
        self._open_level = open_level
        self._monotonic = monotonic

        self._fade_task: asyncio.Task[None] | None = None
        self._last_refusal: str | None = None
        #: Whether `_last_refusal` came from the record button. That refusal is
        #: derived from Reaper's state, so it has to clear itself when the
        #: state moves on - otherwise the screen keeps saying "already
        #: recording" at a recorder that has since stopped.
        self._refused_recording = False
        self._listeners: list[Callable[[], None]] = []

    # -- operator actions -------------------------------------------------

    async def arm(self) -> state.Outcome:
        return await self._command(state.Command.ARM, annotation=ARMED)

    async def stand_down(self) -> state.Outcome:
        return await self._command(state.Command.STAND_DOWN, annotation=STOOD_DOWN)

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
        annotation: str | None = None,
    ) -> state.Outcome:
        outcome = state.step(self.machine, state.Event(command, source=source, detail=detail))
        self.machine = outcome.machine
        self._last_refusal = outcome.refusal

        if outcome.fader is not None:
            await self._move_fader(outcome.fader, source=source, detail=detail)
        if annotation is not None and outcome.changed:
            self._log.record(annotation, data={"state": self.machine.state.value})
        self._notify()
        return outcome

    async def _move_fader(self, command: state.FaderCommand, *, source: state.Source, detail: str) -> None:
        # A console that cannot be reached must not take the box down with it.
        # The fault is recorded and shown; the operator stays in control.
        try:
            if command is state.FaderCommand.OPEN:
                self._cancel_fade()
                await self._console.open(self._open_level)
            else:
                self._start_fade()
        except TransportError:
            pass
        # A fade is asynchronous, so `level` is where the fader was when the
        # command was issued. `target` is where it is going, which is the
        # unambiguous half when reading a log back.
        target = self._open_level if command is state.FaderCommand.OPEN else dm7.MINUS_INF
        self._log.record(
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
                "delivered": self._console.healthy,
            },
        )

    # -- the fade ---------------------------------------------------------

    def _start_fade(self) -> None:
        self._cancel_fade()
        self._fade_task = asyncio.ensure_future(self._run_fade())

    def _cancel_fade(self) -> None:
        if self._fade_task is not None and not self._fade_task.done():
            self._fade_task.cancel()
        self._fade_task = None

    async def _run_fade(self) -> None:
        try:
            await self._console.fade_out(self._fade_seconds)
        except TransportError:
            return
        except asyncio.CancelledError:
            return
        # Only complete the fade if nothing snapped back to OPEN meanwhile.
        if self.machine.state is state.State.RELEASING:
            await self._command(state.Command.FADE_COMPLETE)

    async def wait_for_fade(self) -> None:
        """Block until any fade in flight has finished. For tests and shutdown."""
        task = self._fade_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- annotation -------------------------------------------------------

    async def annotate(self, event_key: str, *, data: Mapping[str, Any] | None = None) -> ann.Entry:
        """Record what the operator saw, and act on it when it says to.

        The fader buttons do both. Asking for the move and the reason as two
        separate taps meant the reason was the one that got dropped when the
        night got busy - and it is the half nothing else can recover, since
        design.md 9 reconstructs the moves themselves from the post-DCA
        reference channel.
        """
        event = ann.lookup(event_key)
        if event.action is not None:
            # The move goes first. A missed downbeat is unrecoverable and must
            # not wait behind a log write.
            await self._command(_ACTIONS[event.action], detail=event_key)
        # Recorded whatever the machine did with it, including a refusal: the
        # operator saw what they saw, and a log that only kept the accepted
        # taps would misrepresent the night.
        entry = self._log.record(event_key, data=data)
        self._notify()
        return entry

    async def start_span(self, event_key: str) -> str:
        span_id = self._log.start_span(event_key)
        self._notify()
        return span_id

    async def end_span(self, span_id: str) -> ann.Entry:
        entry = self._log.end_span(span_id)
        self._notify()
        return entry

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
            self._recorder.start_recording()
        self._log.record(RECORDING_STARTED)
        self._last_refusal = None
        self._refused_recording = False
        self._notify()

    @property
    def record_refusal(self) -> str | None:
        """Why the record button will not fire, or None if it will."""
        if self._recorder is None:
            return None
        return record_refusal(self._recorder.state, self._monotonic())

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
            "open_spans": list(self._log.open_spans()),
            "log": str(self._log.path),
        }

    # -- change notification ----------------------------------------------

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in self._listeners:
            listener()


def _finite(value: float) -> float | None:
    """JSON has no -inf. A closed fader reads as null rather than a lie."""
    return None if value in (float("-inf"), float("inf")) else value
