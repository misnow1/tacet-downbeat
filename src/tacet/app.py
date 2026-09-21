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
import contextvars
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from . import annotations as ann
from . import dm7, prompts, state, taps, targets
from .net import TransportError
from .reaper import Liveness, ReaperClient, record_refusal

#: What an operator button that also moves the fader asks the machine to do.
_ACTIONS: Mapping[ann.Action, state.Command] = {
    ann.Action.OPEN: state.Command.TRIGGER,
    # The same command: a ride-in reaches the same state by a slower route, and
    # the machine has no opinion about how long a move takes.
    ann.Action.OPEN_SLOW: state.Command.TRIGGER,
    ann.Action.READY: state.Command.READY,
    # score-reversed reuses this: the same 2 s fade as "out", one tap (#6).
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
#: A fader command that arrived too late to execute (#16).
STALE_TAP = "stale-tap"
#: `HANDED_OFF` is written for every accepted hand-off, including one made
#: while the level already reads unknown (#118) - a box that restarted while
#: StageMix had the DCA changes nothing, but the tap is still the operator
#: saying so, and the entry is the only record of it. The level going unknown
#: is no longer proof a hand-off happened, since a failed absolute send does
#: it too (#116); that one writes `move-failed`, never `handed-off`.
#: `TOOK_BACK` is written when the level starts being known again - the
#: control-authority timeline `docs/design.md` describes (#12). Since #107 it
#: also marks the first absolute tap after a cold boot, which never handed
#: anything off; the key is kept so older logs still read, and the log's own
#: `handed-off` is what says a handoff really happened.
HANDED_OFF = "handed-off"
TOOK_BACK = "took-back"
#: The return prompt's negative answer: a pure confirmation, nothing changes.
#: Still worth a log entry, since it is a positive record that the operator
#: was asked and confirmed nothing moved.
STILL_MINE = "still-mine"
#: The operator changed the standing target (#9). Written for a change that
#: took, never for a refused one, and it moves nothing: the entry is the only
#: record of which level an open was meant to go to.
TARGET_SET = "target-set"
#: The entries that say the box changed duty. The status the page shows,
#: ARMED or STOOD DOWN, is timed from whichever of them was written last.
_DUTY_ENTRIES = frozenset({ARMED, STOOD_DOWN})

#: What the page says about a fader tap that arrived too late. Quoted in
#: docs/troubleshooting.md, where a test holds it.
STALE_REFUSAL = (
    "That tap was not done: it took {delay:.1f}s to reach the box, and a fader tap later than "
    "{threshold:.1f}s is not acted on. Look at the band, and tap again if you still mean it."
)
#: What the page says about a target level that is not one of the presets.
#: Operator-facing, so it names the levels that do exist. Unreachable from the
#: page, whose control offers only presets; it is what a stale page or a hand-
#: made request meets.
TARGET_NOT_A_PRESET = "{db:g} dB is not one of the target levels ({presets}). Nothing was changed."
#: Not a fourth spelling of the string: `tacet.markers` anchors the timeline to
#: this event and the box warns when a log already holds one, so all of them
#: have to agree or the warning goes quiet.
RECORDING_STARTED = ann.ANCHOR_EVENT

#: Shown when `/record` could not be sent. Quoted in docs/troubleshooting.md.
RECORD_SEND_FAILED = "Could not send the start to Reaper ({error}). Nothing started; tap again, or start it in Reaper."


#: The tap being handled, while it is being handled (#11).
#:
#: A context variable rather than a parameter threaded through every private
#: method, because "every entry the tap produces" includes entries written
#: after the request was answered: the `move-landed` of a fade, the `stood-down`
#: it leads to. Those are written by a task the tap started, and a task copies
#: the context it was created in, so it carries its tap with it and a later tap
#: cannot overwrite it.
_TAP: contextvars.ContextVar[taps.TapTiming | None] = contextvars.ContextVar("tacet_tap", default=None)


@contextlib.contextmanager
def _tapped(tap: taps.TapTiming | None) -> Iterator[None]:
    token = _TAP.set(tap)
    try:
        yield
    finally:
        _TAP.reset(token)


def _stamped(data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`data` with the current tap's timing added, if there is a tap."""
    tap = _TAP.get()
    if tap is None:
        return None if data is None else dict(data)
    return {**(data or {}), taps.TAP_FIELD: tap.as_data()}


def session_entries(
    before: state.Machine, after: state.Machine, command: state.Command | None = None
) -> tuple[str, ...]:
    """The session entries one transition earns.

    Pure, so every path to and from STANDING DOWN is decided in one place
    rather than at each call site that happens to cause one.

    `command` is what the machine accepted, when the caller knows it, and None
    when it was refused or there is nothing to say. Almost everything here is
    read from the two machines; the one thing they cannot show is a hand-off
    that changed nothing, which is exactly the case #118 is about.
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
    # A hand-off while the level is already unknown leaves the machine alone -
    # the belief is already the right one - but it is still the operator saying
    # StageMix has the DCA, and that entry is what Phase 1's control-authority
    # labels are made of. After a restart under StageMix there is no diff to
    # read it from, so the command itself says so (#118). Every accepted
    # hand-off ends with the level unknown, so this and the diff never both
    # fire for one event.
    if (before.level_known and not after.level_known) or command is state.Command.HANDOFF:
        entries.append(HANDED_OFF)
    if after.level_known and not before.level_known:
        entries.append(TOOK_BACK)
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
        hold_below_db: float = dm7.DEFAULT_HOLD_BELOW_DB,
        ready_ride_seconds: float = dm7.DEFAULT_READY_RIDE_SECONDS,
        target_levels: targets.Targets = targets.DEFAULT_TARGETS,
        monotonic: Callable[[], float] = time.monotonic,
        stale_tap_seconds: float = taps.DEFAULT_STALE_TAP_SECONDS,
    ) -> None:
        self._console = console
        self._log = log
        self._recorder = recorder
        self.machine = machine if machine is not None else state.Machine()
        self._fade_seconds = fade_seconds
        self._slow_open_seconds = slow_open_seconds
        self._hold_below_db = hold_below_db
        self._ready_ride_seconds = ready_ride_seconds
        self._targets = target_levels
        #: The standing target, in console units: where every open goes and what
        #: READY's hold level is measured from (#9). Not `_move_target`, which is
        #: where a move already in flight is heading - the two differ for as long
        #: as a ride that started before a change is still running. Lives here
        #: rather than in `state.Machine`, which knows no console units. Starts
        #: at the first preset, and is only ever set to one of them.
        self._target = self._targets.default
        self._monotonic = monotonic
        self._stale_tap_seconds = stale_tap_seconds
        #: The last fader tap refused as stale, until the next command. Shown
        #: loudly: the operator tapped, nothing happened, and they have to
        #: decide again (#16).
        self._stale: dict[str, float] | None = None
        #: The arm / stand-down question on the page, if there is one (#19).
        #: Not part of the machine: a question about duty state, never a duty
        #: state, and asking it moves nothing.
        self._prompt: prompts.OpenPrompt | None = None
        #: The last seq a prompt took. Advanced only when one is raised, so a
        #: question that resolves directly or repeats does not burn one.
        self._prompt_seq = 0
        #: The seq of the prompt an accept is running the command for, or None.
        #: While set, `_settle_prompt` leaves that prompt alone: the accept
        #: decides whether it stays, and a refused accept must not show the
        #: page a prompt that vanished and came back.
        self._answering: int | None = None
        #: When the box last armed or stood down, on `_monotonic` - the clock
        #: `snapshot()["at"]` is on, so the page can convert it once. None
        #: until this run of the box has seen one: a restart has no duty
        #: history, and the box never invents one.
        self._duty_since: float | None = None

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
        #: The loop the writer thread wakes when a batch lands (#41). None when
        #: built outside one, in which case the page learns of a deferred
        #: write failure from the next snapshot instead - the state tick's, at
        #: worst.
        self._loop = _running_loop()
        log.when_written(self._log_written_elsewhere)

    # -- operator actions -------------------------------------------------

    async def arm(self, *, tap: taps.TapTiming | None = None) -> state.Outcome:
        with _tapped(tap):
            return await self._command(state.Command.ARM)

    async def stand_down(self, *, tap: taps.TapTiming | None = None) -> state.Outcome:
        with _tapped(tap):
            # A fader tap: standing down fades an open fader.
            if self._refuse_stale("stand_down", tap):
                return state.Outcome(machine=self.machine, refusal=self._last_refusal, changed=False)
            return await self._command(state.Command.STAND_DOWN)

    async def trigger(
        self,
        *,
        source: state.Source = state.Source.OPERATOR,
        detail: str = "",
        tap: taps.TapTiming | None = None,
    ) -> state.Outcome:
        with _tapped(tap):
            if self._refuse_stale("trigger", tap):
                return state.Outcome(machine=self.machine, refusal=self._last_refusal, changed=False)
            return await self._command(state.Command.TRIGGER, source=source, detail=detail)

    async def release(
        self,
        *,
        source: state.Source = state.Source.OPERATOR,
        detail: str = "",
        tap: taps.TapTiming | None = None,
    ) -> state.Outcome:
        with _tapped(tap):
            if self._refuse_stale("release", tap):
                return state.Outcome(machine=self.machine, refusal=self._last_refusal, changed=False)
            return await self._command(state.Command.RELEASE, source=source, detail=detail)

    async def handoff(self, *, tap: taps.TapTiming | None = None) -> state.Outcome:
        """StageMix has the DCA now (#12). A mode change, like `arm`: not
        stale-checked, since nothing here is timed against the music - the
        answer from the return prompt reaches this the same way as MORE's own
        button, since both mean exactly the same thing."""
        with _tapped(tap):
            return await self._command(state.Command.HANDOFF)

    async def close_now(self, *, tap: taps.TapTiming | None = None) -> state.Outcome:
        """One immediate packet to -inf, correct from any belief (#107). The
        one fader move always available, so it is never refused by state or by
        not knowing the level. Stale-checked like every other fader tap (#16)."""
        with _tapped(tap):
            if self._refuse_stale("close_now", tap):
                return state.Outcome(machine=self.machine, refusal=self._last_refusal, changed=False)
            return await self._command(state.Command.CLOSE_NOW, detail="close-now")

    async def report_ready(self, *, tap: taps.TapTiming | None = None) -> state.Outcome:
        """The operator says the fader is already at the hold level (#107).
        Belief only: nothing is sent. Stale-checked like every other fader tap
        (#16), for parity with `stand_down`, though it moves nothing."""
        with _tapped(tap):
            if self._refuse_stale("report_ready", tap):
                return state.Outcome(machine=self.machine, refusal=self._last_refusal, changed=False)
            return await self._command(state.Command.REPORT_READY, detail="report-ready")

    async def confirm_still_mine(self, *, tap: taps.TapTiming | None = None) -> None:
        """The return prompt's negative answer (#12): a pure log entry, never
        refused - the operator answered a question, and that answer is itself
        the half nothing else can recover, same reasoning as any annotation."""
        with _tapped(tap):
            self._record(STILL_MINE, data={"state": self.machine.state.value}, project_seconds=self._playhead())
            self._notify()

    async def set_target(self, db: float, *, tap: taps.TapTiming | None = None) -> None:
        """Change the standing target: the level the next open goes to (#9).

        Stores the value and nothing else, in every state - no packet, no
        machine step - so it is never refused by state, never held back while
        the level is unknown, and deliberately not stale-checked: there is no
        late move to guard, and a silently refused tap on a control that does
        nothing yet is a dead end. A ride already under way keeps its original
        destination and a fade still ends at -inf; READY's hold level and every
        later open read the new value. Turning it into a ride is #128.

        A level that is not one of the presets is refused, said on the page, and
        not logged: nothing happened.
        """
        level = targets.level_for(db)
        with _tapped(tap):
            if not self._targets.allows(level):
                self._last_refusal = TARGET_NOT_A_PRESET.format(db=db, presets=self._preset_names())
                self._notify()
                return
            previous = self._target
            self._target = level
            self._last_refusal = None
            self._record(
                TARGET_SET,
                data={
                    "level": level,
                    "db": _finite(dm7.to_db(level)),
                    "previous_level": previous,
                    "previous_db": _finite(dm7.to_db(previous)),
                    "default": level == self._targets.default,
                    "state": self.machine.state.value,
                },
                project_seconds=self._playhead(),
            )
            self._notify()

    def _preset_names(self) -> str:
        return ", ".join(f"{db:g}" for db in self._targets.db_values())

    def _stale_verdict(self, tap: taps.TapTiming | None) -> bool:
        """Whether this fader tap is too late to execute, noting it for the
        page if so. Every command clears the last one."""
        if tap is None or not taps.is_stale(tap, self._stale_tap_seconds):
            self._stale = None
            return False
        assert tap.delay is not None
        self._stale = {"delay": tap.delay, "threshold": self._stale_tap_seconds}
        self._last_refusal = STALE_REFUSAL.format(delay=tap.delay, threshold=self._stale_tap_seconds)
        return True

    def _refuse_stale(self, command: str, tap: taps.TapTiming | None) -> bool:
        """Refuse a bare fader command that arrived too late: logged with its
        true tap time, said on the page, and not executed (#16)."""
        if not self._stale_verdict(tap):
            return False
        self._record(
            STALE_TAP, data={"command": command, "state": self.machine.state.value}, project_seconds=self._playhead()
        )
        self._notify()
        return True

    def now(self) -> float:
        """The box's monotonic clock: what snapshots are stamped with, what the
        page's clock estimate is measured against, and what taps are placed on."""
        return self._monotonic()

    async def _command(
        self,
        command: state.Command,
        *,
        source: state.Source = state.Source.OPERATOR,
        detail: str = "",
        ride_seconds: float | None = None,
    ) -> state.Outcome:
        before = self.machine
        event = state.Event(command, source=source, detail=detail, gradual=ride_seconds is not None)
        outcome = state.step(before, event)
        self.machine = outcome.machine
        self._last_refusal = outcome.refusal

        if command is state.Command.HANDOFF and outcome.changed:
            # Whatever this box was in the middle of sending is no longer
            # trustworthy the instant StageMix might also touch the fader
            # (#12) - the same reasoning as any newer move superseding an
            # older one, just triggered by a mode change instead of a move.
            self._cancel_move()
        if outcome.fader is not None:
            await self._move_fader(
                outcome.fader,
                source=source,
                detail=detail,
                ride_seconds=ride_seconds,
                level_was_known=before.level_known,
            )
        # `outcome.machine`, captured before `_move_fader` ran, not
        # `self.machine`: a failed absolute move un-knows the level through
        # `_move_failed` after this point (#116), and reading `self.machine`
        # here would then see that as a hand-off and write a spurious
        # `handed-off` for a move that never touched StageMix. (Known,
        # accepted residual: a failed absolute command from an unknown level
        # still writes `took-back` for the belief it briefly held, immediately
        # followed by `move-failed`.)
        # The command only when the machine accepted it: a refused hand-off -
        # a detector's, since #117 - must not write the entry an operator's
        # does (#118).
        accepted = None if outcome.refusal is not None else command
        for key in session_entries(before, outcome.machine, accepted):
            if key in _DUTY_ENTRIES:
                self._duty_since = self._monotonic()
            self._record(
                key,
                data={"state": self.machine.state.value},
                project_seconds=self._playhead(),
            )
        self._settle_prompt()
        self._notify()
        return outcome

    def _hold_level(self) -> int:
        """The READY hold level: short of target by `hold_below_db` (#6)."""
        return dm7.clamp(self._target - round(self._hold_below_db * dm7.UNITS_PER_DB))

    def _fader_target(self, command: state.FaderCommand) -> int:
        if command is state.FaderCommand.OPEN:
            return self._target
        if command in (state.FaderCommand.READY, state.FaderCommand.REPORT_READY):
            return self._hold_level()
        # FADE and CLOSE_NOW.
        return dm7.MINUS_INF

    async def _move_fader(
        self,
        command: state.FaderCommand,
        *,
        source: state.Source,
        detail: str,
        ride_seconds: float | None = None,
        level_was_known: bool,
    ) -> None:
        """Carry out what the machine asked for. A failure is recorded and
        shown, never raised: the operator stays in control.

        `level_was_known` is required and has no default, so a new caller has
        to say it: forgotten, a snap open from a believed-unity fader would
        send nothing and still mark the level known (#107)."""
        # A console that cannot be reached must not take the box down with it.
        # The fault is recorded and shown; the operator stays in control.
        failed = False
        # A move left running in the background has delivered nothing yet.
        # FADE and READY are always background; an OPEN is only when ridden.
        # CLOSE_NOW and REPORT_READY are always synchronous, like a snap open:
        # one packet or none, never a ramp of their own.
        background = command in (state.FaderCommand.FADE, state.FaderCommand.READY) or (
            command is state.FaderCommand.OPEN and ride_seconds is not None
        )
        try:
            if command is state.FaderCommand.OPEN:
                self._cancel_move()
                if ride_seconds is None and not level_was_known and self._console.commanded_level == self._target:
                    # The box believes the fader is already there, and does not
                    # know that it is (#107): `open` ramps from the belief and
                    # says nothing to a target it is already at, which would
                    # mark the level known without the box ever having put the
                    # fader anywhere. Write it, so an open is absolute.
                    self._console.send_level(self._target)
                elif ride_seconds is None:
                    # The ordinary open, awaited: it is one packet and 20 ms,
                    # and a missed downbeat is unrecoverable, so it goes out
                    # before anything else gets a turn.
                    await self._console.open(self._target)
                else:
                    # A ride-in takes over a second, and `annotate` deliberately
                    # moves the fader before writing the log. Awaiting it here
                    # would hold the annotation - and the playhead stamped on
                    # it - back by the whole length of the ramp, timestamping
                    # the tap where the ramp ended rather than where it began.
                    self._start_ride_in(self._target, ride_seconds)
            elif command is state.FaderCommand.READY:
                # Always a ride - there is no fast form of READY (#6) - and
                # never held back for the same reason as the ride-in above.
                self._cancel_move()
                seconds = ride_seconds if ride_seconds is not None else self._ready_ride_seconds
                self._start_ride_in(self._hold_level(), seconds)
            elif command is state.FaderCommand.REPORT_READY:
                # Belief only - never a packet. The operator says the fader is
                # already at the hold level, and READY has no fast form to
                # drive it there with (#107). Forcing a nonzero level the
                # operator did not put there would be exactly the surprise
                # CLAUDE.md's fail-safe principle forbids.
                self._cancel_move()
                self._console.assume(self._hold_level())
            elif command is state.FaderCommand.CLOSE_NOW:
                # One immediate packet, never the 2 s fade: closing is always
                # safe to confirm for real, and a slow ramp from a belief that
                # might be wrong would leave the band audible for two more
                # seconds regardless (#12, #107).
                self._cancel_move()
                self._console.send_level(dm7.MINUS_INF)
            else:
                self._start_fade()
        except TransportError:
            failed = True
        # A fade is asynchronous, so `level` is where the fader was when the
        # command was issued. `target` is where it is going, which is the
        # unambiguous half when reading a log back.
        target = self._fader_target(command)
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
                # Null for the same reason as `delivered`: a move still running
                # has not been late yet, and `move-landed` or `move-failed`
                # carries its timing when it ends.
                **_timing(None if background and not failed else self._console.timing),
            },
            project_seconds=self._playhead(),
        )
        if failed:
            self._move_failed(target, absolute=_absolute(command, ride_seconds))

    def _move_failed(self, target: int, *, absolute: bool) -> None:
        """A send failed partway through a move.

        The machine is told, so the command that started the move retries it
        rather than being ignored as already done (#27), and the log is told,
        because the `commanded` entry before this one describes a move that did
        not happen. Deliberately not a refusal: nothing was declined, and the
        why line already says what to do.

        `absolute` is required and has no default, for the same reason
        `_move_fader`'s `level_was_known` is: a new caller has to say it out
        loud. An absolute move's failure also puts the belief back (#116) - the
        machine marked the level known before this send was even attempted,
        and it delivered nothing.
        """
        self.machine = state.step(self.machine, state.Event(state.Command.MOVE_FAILED, absolute=absolute)).machine
        self._record(
            MOVE_FAILED,
            data={**self._move_end(target), "error": self._console.last_error},
            project_seconds=self._playhead(),
        )

    def _move_landed(self, target: int) -> None:
        """A background move sent its last step. The `commanded` entry that
        started it could not say so, because it was written first."""
        self._record(MOVE_LANDED, data=self._move_end(target), project_seconds=self._playhead())

    def _ride_in_landed(self, target: int) -> None:
        """Tell the machine the ride-in is over, so a later trigger is a
        confirmation again rather than a snap (#45), or so a READY holds at
        the level it just reached rather than looking like it is still on its
        way. Stepped directly, like a failed move: nothing was tapped, so
        nothing is refused or cleared."""
        self.machine = state.step(self.machine, state.Event(state.Command.RIDE_IN_COMPLETE)).machine
        self._move_landed(target)

    def _move_end(self, target: int) -> dict[str, Any]:
        """Where a move ended up against where it was going, for the log."""
        return {
            "level": self._console.commanded_level,
            "db": _finite(dm7.to_db(self._console.commanded_level)),
            "target": target,
            "target_db": _finite(dm7.to_db(target)),
            "state": self.machine.state.value,
            **_timing(self._console.timing),
        }

    # -- the fade ---------------------------------------------------------

    def _start_fade(self) -> None:
        self._cancel_move()
        # After the cancel, which is what clears the last destination.
        self._move_target = dm7.MINUS_INF
        self._move_task = asyncio.ensure_future(self._run_fade())
        self._move_push = asyncio.ensure_future(self._push_while_moving())

    def _start_ride_in(self, level: int, seconds: float) -> None:
        """Ride the fader up to `level` over `seconds` instead of snapping it.

        Shared by the ordinary open's ride-in (`up-slow`) and READY's ride to
        the hold level (#6) - both are the same gesture to a different place.

        Runs as a task for the same reason the fade does: the operator has
        already tapped, and everything after the tap - the log entry, its
        playhead, the page - must not wait for the ramp to finish.
        """
        self._move_target = level
        self._move_task = asyncio.ensure_future(self._run_ride_in(level, seconds))
        self._move_push = asyncio.ensure_future(self._push_while_moving())

    async def _run_ride_in(self, level: int, seconds: float) -> None:
        this = asyncio.current_task()
        try:
            await self._console.ride_in(level, seconds=seconds)
        except TransportError:
            if self._move_task is this:
                # A ride is relative; its failure leaves the belief where it
                # was and #27's retry intact (#116).
                self._move_failed(level, absolute=False)
            return
        except asyncio.CancelledError:
            return
        else:
            if self._move_task is this:
                self._ride_in_landed(level)
        finally:
            # No command follows: the machine reached OPEN or READY when the
            # tap landed, and only the gesture was still running.
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
                # The fade is relative; its failure leaves the belief where it
                # was and #27's retry intact (#116).
                self._move_failed(dm7.MINUS_INF, absolute=False)
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

    async def annotate(
        self,
        event_key: str,
        *,
        data: Mapping[str, Any] | None = None,
        tap: taps.TapTiming | None = None,
    ) -> ann.Entry | None:
        """Record what the operator saw, and act on it when it says to.

        The fader buttons do both. Asking for the move and the reason as two
        separate taps meant the reason was the one that got dropped when the
        night got busy - and it is the half nothing else can recover, since
        design.md 9 reconstructs the moves themselves from the post-DCA
        reference channel.

        Returns None when the log refused the entry outright - its writer has
        stopped, say. An entry returned is accepted, not yet saved: it is
        written on the log's own thread (#41), and a failure there reaches the
        page through the snapshot's `log`. Either way the move has happened.

        The key and the data are checked before anything acts on them: a
        box-only event is refused, and data the log would not hold raises
        `DataError` here rather than inside the log after the fader moved.
        """
        event = ann.operator_event(event_key)
        data = ann.operator_data(data)
        if taps.TAP_FIELD in data:
            raise ann.DataError(f"data {taps.TAP_FIELD!r} is the box's own, and not the operator's to set")
        with _tapped(tap):
            return await self._annotate(event, data, tap)

    def _ride_seconds_for(self, action: ann.Action) -> float | None:
        """How long the gesture behind an action's move should take, or None
        for a snap. Both rides - `up-slow`'s and READY's - keep their own
        duration rather than sharing one (#6)."""
        if action is ann.Action.OPEN_SLOW:
            return self._slow_open_seconds
        if action is ann.Action.READY:
            return self._ready_ride_seconds
        return None

    async def _annotate(
        self, event: ann.EventType, data: dict[str, Any], tap: taps.TapTiming | None
    ) -> ann.Entry | None:
        event_key = event.key
        if event.action is not None and self._stale_verdict(tap):
            # The reason is kept - the operator saw what they saw - and marked
            # as a tap that was not acted on. Only annotation-only taps are
            # never refused (#16).
            entry = self._record(
                event_key, data={**data, "stale": True, "executed": False}, project_seconds=self._playhead()
            )
            self._notify()
            return entry
        if event.action is not None:
            # The move goes first. A missed downbeat is unrecoverable and must
            # not wait behind a log write.
            await self._command(
                _ACTIONS[event.action],
                detail=event_key,
                ride_seconds=self._ride_seconds_for(event.action),
            )
        # Recorded whatever the machine did with it, including a refusal: the
        # operator saw what they saw, and a log that only kept the accepted
        # taps would misrepresent the night.
        entry = self._record(event_key, data=data, project_seconds=self._playhead())
        # After the reason is written, and after the move above if there was
        # one: the question is asked about the machine as this tap left it.
        self._ask_prompt(event_key)
        self._notify()
        return entry

    async def start_span(self, event_key: str, *, tap: taps.TapTiming | None = None) -> str | None:
        """The new span's id, or None when the log refused its start. A start
        accepted and then not saved closes the span again once the writer says
        so, and the button offers to start it again."""
        ann.operator_event(event_key)
        span_id: str | None
        with _tapped(tap):
            try:
                span_id = self._log.start_span(event_key, data=_stamped(None), project_seconds=self._playhead())
            except ann.WriteError:
                span_id = None  # see _record
            # Asked whether or not the log kept the start: the question is
            # state on the box, not something the log holds.
            self._ask_prompt(event_key)
        self._notify()
        return span_id

    async def end_span(self, span_id: str, *, tap: taps.TapTiming | None = None) -> ann.Entry | None:
        """None when the log refused the end. An end accepted and then not saved
        reopens the span once the writer says so, so the button offers to end
        it again and the next tap saves the end."""
        entry: ann.Entry | None
        try:
            with _tapped(tap):
                entry = self._log.end_span(span_id, data=_stamped(None), project_seconds=self._playhead())
        except ann.WriteError:
            entry = None  # see _record
        self._notify()
        return entry

    # -- the arm / stand-down question (#19) -------------------------------

    def _ask_prompt(self, source: str) -> None:
        """Ask the question `source` raises, if it raises one. Writes log
        entries and sets `_prompt`; nothing else. In particular it never runs a
        command: the answer is the operator's tap on `accept_prompt`."""
        seq = self._prompt_seq + 1
        decision = prompts.ask(self._prompt, source=source, machine=self.machine, seq=seq)
        if decision.prompt is not None and decision.prompt.seq == seq:
            self._prompt_seq = seq
        self._apply_prompt(decision)

    def _settle_prompt(self) -> None:
        """Close the open prompt, logged as resolved, if the machine has moved
        to what it asks for. Called once per command, which covers every path
        that changes duty; the stalled and riding-in steps the box takes on its
        own touch nothing a prompt reads."""
        current = self._prompt
        if current is not None and current.seq == self._answering:
            return
        self._apply_prompt(prompts.settle(current, self.machine))

    def _apply_prompt(self, decision: prompts.Decision) -> None:
        for note in decision.notes:
            self._record(note.key, data=_prompt_data(note, self.machine), project_seconds=self._playhead())
        self._prompt = decision.prompt

    async def accept_prompt(self, seq: int, *, tap: taps.TapTiming | None = None) -> None:
        """The operator says yes to the open prompt `seq`.

        Runs the ordinary command - `stand_down` (stale-checked, fading an
        open fader) or `arm` (deliberately not, #16) - and then writes the
        answer, the way `annotate` moves first and gives the reason after. The
        prompt closes only if the command did something. A refusal, whether
        the box does not know the fader level (arming is refused then, #107)
        or the tap was late (#16), leaves the same prompt open under the same
        seq, with the refusal on the page, so the operator can fix it and
        answer again. Nothing is sent to make an accept work.

        An answer for a prompt that is not open, or was withdrawn or replaced,
        is a race - two browsers, or a tap that crossed a reply on stadium wifi
        - and is ignored, unlogged: acting on it could stand the box down long
        after the question stopped applying. So is an answer that arrives while
        another is being answered.

        Why the prompt is not simply cleared before the command runs:
        `_command` notifies, and the hub snapshots synchronously, so clearing
        first would push `prompt: null` and then push it back on a refused
        accept. Instead `_answering` holds the settle off, and the cost is that
        the one push made inside the command shows the new duty state with the
        question still open. Do not simplify it back."""
        with _tapped(tap):
            open_ = self._prompt
            if open_ is None or open_.seq != seq or self._answering is not None:
                return
            self._answering = seq
            try:
                if open_.prompt is prompts.Prompt.STAND_DOWN:
                    outcome = await self.stand_down(tap=tap)
                else:
                    outcome = await self.arm(tap=tap)
            finally:
                self._answering = None
            self._record(
                prompts.PROMPT_ACCEPTED,
                data={
                    **_prompt_data(_answer_note(prompts.PROMPT_ACCEPTED, open_), self.machine),
                    "executed": outcome.changed,
                    "refusal": outcome.refusal,
                    # Only stand_down judges the tap, and it does so afresh
                    # each time. An arm is never judged, so it says null -
                    # `_stale` after one is whatever an earlier fader tap left,
                    # and is no news about this answer.
                    "stale": self._stale is not None if open_.prompt is prompts.Prompt.STAND_DOWN else None,
                },
                project_seconds=self._playhead(),
            )
            if outcome.changed:
                if self._prompt is open_:
                    self._prompt = None
            else:
                # Still open, unless it turns out to be moot as well.
                self._settle_prompt()
            self._notify()

    async def dismiss_prompt(self, seq: int, *, tap: taps.TapTiming | None = None) -> None:
        """The operator says "not yet": logged, and the prompt closed.

        It does not come back by itself; tapping the event again raises a
        fresh one. Never refused, and never stale-checked: it changes nothing,
        so nothing about it can be dangerous."""
        with _tapped(tap):
            open_ = self._prompt
            if open_ is None or open_.seq != seq or self._answering is not None:
                return
            self._record(
                prompts.PROMPT_DISMISSED,
                data=_prompt_data(_answer_note(prompts.PROMPT_DISMISSED, open_), self.machine),
                project_seconds=self._playhead(),
            )
            self._prompt = None
            self._notify()

    def _record(
        self,
        event_key: str,
        *,
        data: Mapping[str, Any] | None = None,
        project_seconds: float | None = None,
    ) -> ann.Entry | None:
        """Write one instant, or None if the log refused it.

        Every log write in the box goes through here or the two span methods,
        and none of them lets a `WriteError` out. The failure has already been
        counted on the log's health, which the snapshot carries to the page.
        A failure on the writer thread is counted there too, later.
        Raised out of a fader route it skipped the push after a move that had
        happened; raised inside a fade it ended the task before FADE_COMPLETE
        and left the machine in RELEASING. The fader, the machine and the page
        all carry on.
        """
        try:
            return self._log.record(event_key, data=_stamped(data), project_seconds=project_seconds)
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

    async def start_recording(self, *, tap: taps.TapTiming | None = None) -> None:
        with _tapped(tap):
            await self._start_recording()

    async def _start_recording(self) -> None:
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
        # Whatever the writer has reported, including a stopped writer, is on
        # this snapshot however it was asked for.
        self._log.settle()
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
        age = None if self._console.last_sent_at is None else self._monotonic() - self._console.last_sent_at
        return {
            # When this was taken, on the box's clock. The page keeps the newest
            # it has seen: a POST response held up on the wifi used to paint an
            # older state over the pushes that overtook it (#11).
            "at": self._monotonic(),
            "state": self.machine.state.value,
            "why": state.describe(self.machine),
            "refusal": refusal,
            # The last fader tap refused as too late, or None. The page shows
            # the refusal loudly while this is set (#16).
            "stale_tap": self._stale,
            "detector_enabled": self.machine.allow_detector,
            # The arm / stand-down question, or None (#19). Only what it is:
            # the words are the page's, and it carries no copy.
            "prompt": None
            if self._prompt is None
            else {"seq": self._prompt.seq, "kind": self._prompt.prompt.value, "source": self._prompt.source},
            # Whether the box is on duty, and since when on the box's own
            # monotonic clock - the one `at` is on, so the page converts it
            # once. Null after a restart, which has no duty history. Fixed at
            # the change, not an age, so it does not churn the snapshot.
            "duty": {
                "armed": self.machine.state is not state.State.STANDING_DOWN,
                "since": self._duty_since,
            },
            # The STANDING target: the level the next open goes to, set from MORE
            # (#9). Not `fader.target`, which is where a move in flight is
            # heading and is null when nothing is moving - the two differ while
            # a ride that began before a change is still running. Commanded, like
            # everything here, and never read back from the console.
            "target": {
                "level": self._target,
                "db": _finite(dm7.to_db(self._target)),
                "default_db": _finite(dm7.to_db(self._targets.default)),
                "presets_db": list(self._targets.db_values()),
            },
            "fader": {
                "commanded": self._console.commanded_level,
                "db": _finite(self._console.commanded_db),
                # Always false. The protocol is write-only; see the module docstring.
                "confirmed": False,
                # False at cold boot and after a hand-off (#107, #12):
                # `commanded_level` is a belief nothing has confirmed, and it
                # is not to be trusted until an absolute command says where the
                # fader is. Not a claim the last such packet was delivered;
                # `healthy` and `stalled` carry that.
                "level_known": self.machine.level_known,
                # Seconds since the console was last actually told something -
                # a snap, a ramp step, or a close. None before anything has
                # ever been sent. Shown in every mode, known or not.
                "age": age,
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
            # The label as well as the key: a span left open by an older log can
            # belong to an event that is no longer a button (#14), and the page
            # still has to offer a way to end it.
            "open_spans": [
                {"span_id": span_id, "event": event, "label": ann.lookup(event).label}
                for span_id, event in self._log.open_spans().items()
            ],
            "log": {"path": str(self._log.path), **_health(self._log.health)},
            # Only the live markers. They can be rebuilt from the log afterwards.
            "mirror": _health(self._log.mirror_health),
        }

    # -- change notification ----------------------------------------------

    def _log_written_elsewhere(self) -> None:
        """On the log's writer thread. Hands over to the loop and returns."""
        loop = self._loop
        if loop is None:
            return
        # A loop already closed at shutdown has nobody left to tell.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._log_written)

    def _log_written(self) -> None:
        """A batch landed. Only a write that failed, or recovered, or a span it
        reopened, is news to the page; a successful write already was."""
        if self._log.settle():
            self._notify()

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in self._listeners:
            listener()


def _prompt_data(note: prompts.Note, machine: state.Machine) -> dict[str, Any]:
    """The entry a prompt note becomes. Flat, so it fits the log."""
    data: dict[str, Any] = {
        "prompt": note.prompt.value,
        "seq": note.seq,
        "source": note.source,
        "state": machine.state.value,
    }
    if note.replaced_by is not None:
        data["replaced_by"] = note.replaced_by
    return data


def _answer_note(key: str, answered: prompts.OpenPrompt) -> prompts.Note:
    return prompts.Note(key=key, prompt=answered.prompt, seq=answered.seq, source=answered.source)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _health(health: ann.WriteHealth) -> dict[str, Any]:
    return {"healthy": health.healthy, "error": health.error, "failures": health.failures}


def _timing(timing: dm7.MoveTiming | None) -> dict[str, Any]:
    """How far a move fell behind, for the log (#40). A stall on the event loop
    shows here, so Phase 1 logs can tell it from console behaviour."""
    return {
        "worst_lateness": None if timing is None else timing.worst_lateness,
        "skipped_steps": None if timing is None else timing.skipped,
    }


def _finite(value: float) -> float | None:
    """JSON has no -inf. A closed fader reads as null rather than a lie."""
    return None if value in (float("-inf"), float("inf")) else value


def _absolute(command: state.FaderCommand, ride_seconds: float | None) -> bool:
    """Whether this move puts the fader in one known place whatever the box
    believed (design.md 5.3): the snap open, and the instant close. Those are
    the moves that mark the level known, so those are the ones whose failure
    un-knows it again (#116). A ridden open is relative - it ramps from the
    belief, and `step` refuses it outright while the level is unknown - and so
    are the fade and READY's ride. REPORT_READY writes no packet, so it has no
    send to fail."""
    if command is state.FaderCommand.CLOSE_NOW:
        return True
    return command is state.FaderCommand.OPEN and ride_seconds is None
