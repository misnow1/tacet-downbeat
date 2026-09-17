"""The operating state machine.

                     operator trigger: arms, then opens
         +---------------------------------------------------+
         |                                                   v
    STANDING DOWN <--(stand down)--- IDLE ---(trigger)---> OPEN
         |  ^                         ^  ^                   |
         |  |                         |  |          loss of consensus
         |  +-----------(arm)---------+  |                   v
         |                               +----------- RELEASING (2 s fade)
         |     operator release:            (fade lands)      |
         +---- fades, and the state                 any trigger snaps
               does not change                      back to OPEN

    READY sits beside IDLE, entered only by the operator ("something good just
    happened, band likely"), never by the detector:

        IDLE ---(ready)---> READY ---(any trigger, fast)---> OPEN
                               |
                 (score reversed, or stand down: the ordinary 2 s fade)
                               v
                           RELEASING

    A fade that lands while a stand-down is pending goes to STANDING DOWN
    instead of IDLE. A stand-down from READY gets that same fade rather than a
    snap-close: the fader is up with no band confirmed, and the box cannot be
    sure one has not quietly started (#6). A detector-sourced event is refused
    in every state until `allow_detector` is set, and refused in STANDING DOWN
    whatever it says.

    `handoff` sits across all of the above, orthogonal to `state` (#12): the
    box can be in any of them and also not trust its own belief about where
    the fader really is, because StageMix might be moving it. While handed
    off, RELEASE, READY and a gradual TRIGGER are queued rather than acted on -
    each one depends on knowing the real level, which is exactly what is not
    known - and STAND_DOWN goes straight to STANDING DOWN from wherever it
    was, sending nothing. A snap TRIGGER always passes straight through.
    TAKE_BACK_UP and TAKE_BACK_DOWN answer where the fader actually is, clear
    `handoff`, and run whatever was queued from that corrected belief.

Pure: `step` takes a machine and an event and returns a new machine plus what
the shell should do about it. No sockets, no clock, no fader. That keeps every
transition testable, and it keeps the one decision that matters - whether to
move the fader - in a single place that can be read at a glance.

**The fader does not move autonomously before Phase 2.** Events carry their
source, and a detector-sourced event is refused unless `allow_detector` has been
set, and always while standing down: a box that is not on duty is not put on
duty by a detector. Phase 0's buttons are operator-initiated and are not the
same thing; see CLAUDE.md. The gate is here rather than in a comment because a
comment does not fail a test.

**The operator drives in every state** (#89, principle 5). STANDING DOWN says
the band is not in the stands, which is what Phase 1's duty labels are made of
and what gates the detector. It is not a lock on the operator: refusing their
open there made a forgotten Arm into a missed downbeat, and a missed downbeat
is unrecoverable. A `READY` while standing down arms the box the same way a
`TRIGGER` does - a forgotten Arm must not cost a heads-up either.

The machine emits `OPEN`, `READY`, `FADE`, `TAKE_BACK_UP` and `TAKE_BACK_DOWN`,
and there is no other option. Faders only, never mutes: the band mics feed
other mixes pre-fader and post-mute, so there is deliberately no mute for
anything here to reach for. `READY` is still a fader write, to a hold level
short of target - not a mute and not silence. `TAKE_BACK_UP` is the one
exception that writes nothing at all - it corrects a belief, never the fader -
which is still not a mute, since nothing is silenced by it either.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class State(StrEnum):
    #: Boot state. Band not in the stands - pregame, halftime, exodus.
    STANDING_DOWN = "standing-down"
    #: Armed, band in the stands, DCA closed.
    IDLE = "idle"
    #: The fader is riding, or has ridden, to a hold level short of target -
    #: something good happened for the home team, the band is not yet playing.
    #: Any trigger takes it the rest of the way; a score reversed or a
    #: stand-down fades it like an ordinary close (#6).
    READY = "ready"
    #: DCA at unity.
    OPEN = "open"
    #: Fading. Any qualifying trigger returns to OPEN.
    RELEASING = "releasing"


class Source(StrEnum):
    OPERATOR = "operator"
    DETECTOR = "detector"


class Command(StrEnum):
    ARM = "arm"
    STAND_DOWN = "stand-down"
    #: Something says the band is playing: a whistle, a unison onset, a button.
    TRIGGER = "trigger"
    #: The operator rides up to a hold level short of target: something good
    #: happened for the home team, the band is not yet playing (#6).
    READY = "ready"
    #: Loss of consensus, or the operator reaching for the fade.
    RELEASE = "release"
    #: The fade reached the bottom.
    FADE_COMPLETE = "fade-complete"
    #: A send failed partway through a move, so the fader stopped short of
    #: where the state says it is going. Reported by the shell, never tapped.
    MOVE_FAILED = "move-failed"
    #: A ride-in reached the top - the ordinary open's or READY's hold level.
    #: Reported by the shell, never tapped.
    RIDE_IN_COMPLETE = "ride-in-complete"
    #: StageMix has the DCA now. Every ramp starts from `commanded_level`, and
    #: that number is fiction the moment another interface can move the fader
    #: (#12). A mode change, like ARM - no fader move of its own.
    HANDOFF = "handoff"
    #: Take-back answers: where the DCA actually is now that control is back.
    #: UP never sends anything by itself - forcing a nonzero level with
    #: nothing queued to justify it is exactly the surprise CLAUDE.md's
    #: fail-safe principle forbids. DOWN always confirms silence for real,
    #: because closing is always safe to do proactively.
    TAKE_BACK_UP = "take-back-up"
    TAKE_BACK_DOWN = "take-back-down"


class FaderCommand(StrEnum):
    OPEN = "open"
    #: Ride to the READY hold level, short of target. Still a fader write, not
    #: a mute (#6).
    READY = "ready"
    FADE = "fade"
    #: The take-back answers (#12). UP is belief only, never a packet: it
    #: names the level `Dm7Client.assume` should trust without sending
    #: anything. DOWN is one immediate packet to -inf, confirming silence for
    #: real - never the 2 s fade, which would leave the band audible for two
    #: more seconds if the belief it starts from turns out wrong anyway.
    TAKE_BACK_UP = "take-back-up"
    TAKE_BACK_DOWN = "take-back-down"


@dataclass(frozen=True)
class Event:
    command: Command
    source: Source = Source.OPERATOR
    #: Free text for the why line - "whistle", "drums", "operator button".
    detail: str = ""
    #: A trigger that rides the fader in rather than snapping it: `up-slow`.
    #: The machine still has no opinion about how long a move takes; it only
    #: needs to know that a slower one is under way, so a fast one can take
    #: over (#45).
    gradual: bool = False


@dataclass(frozen=True)
class Machine:
    state: State = State.STANDING_DOWN
    #: Set when the operator stands down mid-fade. Standing down while open
    #: fades rather than slamming, so the intent has to outlive the fade.
    pending_stand_down: bool = False
    #: The Phase 2 gate. False means the detector cannot move anything.
    allow_detector: bool = False
    #: The last move did not finish. While set, repeating the command that
    #: started it retries the move instead of being ignored as already done.
    #: Never set by a healthy move, so a panic tap on a fade that is still
    #: running does not restart it.
    stalled: bool = False
    #: The operator opened or readied the fader while the box was standing
    #: down, which armed it (#89). Says so on the why line for as long as that
    #: lasts: the tap did two things, and only one of them was asked for in
    #: words.
    armed_by_operator: bool = False
    #: A ride-in is on its way up: the slow-open ramp into OPEN, or READY's own
    #: ride to the hold level. While set and the state is OPEN, a fast trigger
    #: snaps the rest of the way: the whistle or the drums say the band is
    #: coming in now, and the downbeat wins over the gesture (#45). A slow
    #: trigger does not restart it. In READY any trigger already commits
    #: regardless of this flag (#6); it is kept there for the why line and so a
    #: failed ride can be retried.
    riding_in: bool = False
    #: StageMix has the DCA. Every ramp depends on knowing where the fader
    #: really is, which this box cannot while another interface might be
    #: moving it (#12). Orthogonal to `state`, like `allow_detector`: the box
    #: can be in any state and also not trust its own belief about the level.
    handoff: bool = False
    #: The one fader tap blocked while handed off, waiting on a take-back
    #: answer. A new blocked tap silently replaces whatever was queued, the
    #: same way a ride-in is already superseded by a fade elsewhere.
    queued: Event | None = None


@dataclass(frozen=True)
class Outcome:
    machine: Machine
    fader: FaderCommand | None = None
    #: Set when an event was declined, with the reason, for the why line.
    refusal: str | None = None
    #: False when the event was legal but changed nothing.
    changed: bool = True
    #: A take-back answer's own queued tap, now safe to run - only ever set by
    #: TAKE_BACK_UP/TAKE_BACK_DOWN, and only when one was queued (#12). The
    #: shell replays it through the ordinary command path, which is what makes
    #: it ramp from the just-corrected belief without this module needing to
    #: know any level, target or duration itself.
    replay: Event | None = None


def _unchanged(machine: Machine, refusal: str | None = None) -> Outcome:
    return Outcome(machine=machine, refusal=refusal, changed=False)


def step(machine: Machine, event: Event) -> Outcome:
    """Apply one event. Never raises: an illegal event is refused, not fatal."""
    if event.command is Command.READY and event.source is Source.DETECTOR:
        # Unconditional, and checked before the phase gate below: READY is
        # entered on a prediction that something is about to happen, which
        # only a watching human can judge (design.md 2, CLAUDE.md "never gate
        # on level alone"). That is not the Phase 1/2 line TRIGGER sits on -
        # a detector confirming sound is present is exactly its Phase 2 job,
        # but guessing that sound is about to start never becomes one (#6).
        return _unchanged(machine, "READY is operator-only; only a person can tell what is about to play")
    if machine.state is State.STANDING_DOWN and event.source is Source.DETECTOR:
        # Unconditional, whatever `allow_detector` says, and moved up here
        # (#12) so it covers HANDOFF and the take-back answers too, not only
        # the commands `_standing_down` itself dispatches: standing down is
        # what says the band is not in the stands, so there is nothing to
        # detect, and the detector does not get to change the mode.
        return _unchanged(machine, "standing down; the detector cannot arm the box")
    if event.source is Source.DETECTOR and not machine.allow_detector:
        return _unchanged(
            machine,
            "detector input is ignored until phase 2 is declared; the operator is driving",
        )
    if event.command is Command.HANDOFF:
        # A mode change, like ARM: no fader move, legal from any state. A
        # second HANDOFF while already handed off changes nothing.
        if machine.handoff:
            return _unchanged(machine)
        return Outcome(machine=replace(_settled_for_handoff(machine), handoff=True, queued=None))
    if event.command is Command.TAKE_BACK_UP:
        return _take_back(machine, fader=FaderCommand.TAKE_BACK_UP)
    if event.command is Command.TAKE_BACK_DOWN:
        return _take_back(machine, fader=FaderCommand.TAKE_BACK_DOWN)
    if machine.handoff:
        # Checked before dispatching by state, because both of these apply
        # identically whatever `state` currently is (#12).
        if event.command is Command.STAND_DOWN:
            # Changes state only and sends nothing: there is no real fader
            # move for RELEASING to be waiting on, so this goes straight to
            # STANDING_DOWN from wherever it was, discarding anything queued -
            # standing down means off duty, so a queued fader intention no
            # longer applies. `handoff` itself outlives this: the box still
            # does not know where the console really is.
            return Outcome(
                machine=replace(
                    machine,
                    state=State.STANDING_DOWN,
                    queued=None,
                    pending_stand_down=False,
                    stalled=False,
                    riding_in=False,
                    armed_by_operator=False,
                )
            )
        if _blocked_by_handoff(event):
            # Queued, not refused: the reason still gets logged by the shell
            # exactly as tapped, and the move itself runs once answered. A
            # second blocked tap silently replaces the first.
            return Outcome(machine=replace(machine, queued=event))

    handler = _HANDLERS[machine.state]
    return handler(machine, event)


def _settled_for_handoff(machine: Machine) -> Machine:
    """Presume whatever move is running has already landed, before handing
    off cancels it for real (#12). `state` describes what the operator wants,
    not where the fader physically is - handing off does not change that
    intent, so there is nothing to abandon, only a ramp already under way to
    stop racing against StageMix. A state that quietly stopped matching what
    the shell is actually doing until the next tap is exactly the drift
    CLAUDE.md's fail-visible principle warns about."""
    if machine.state is State.RELEASING:
        landing = State.STANDING_DOWN if machine.pending_stand_down else State.IDLE
        return replace(machine, state=landing, pending_stand_down=False, stalled=False, riding_in=False)
    if machine.riding_in:
        return replace(machine, riding_in=False, stalled=False)
    return machine


def _blocked_by_handoff(event: Event) -> bool:
    """Whether this command is a ramp that depends on knowing the console's
    real level - the hazard #12 exists to prevent. A snap TRIGGER always
    passes straight through, correct from any start; STAND_DOWN gets its own
    handling above, since it must still change state."""
    if event.command is Command.RELEASE:
        return True
    if event.command is Command.READY:
        return True
    return event.command is Command.TRIGGER and event.gradual


def _take_back(machine: Machine, *, fader: FaderCommand) -> Outcome:
    """UP and DOWN share everything except which `FaderCommand` they emit."""
    if not machine.handoff:
        if machine.stalled:
            # The confirming packet itself failed to send. Retry it - by now
            # `queued` is already empty, whichever answer it was: the shell
            # only replays a queued tap once its own confirming send actually
            # landed, so nothing was lost, only delayed (#12).
            return Outcome(machine=replace(machine, stalled=False), fader=fader)
        return _unchanged(machine)
    return Outcome(
        machine=replace(machine, handoff=False, queued=None),
        fader=fader,
        replay=machine.queued,
    )


def _standing_down(machine: Machine, event: Event) -> Outcome:
    # A detector-sourced event never reaches here: `step` refuses it first.
    if event.command is Command.ARM:
        # Deliberately no fader move. A mode change is not a fader move:
        # announce, do not surprise.
        return Outcome(machine=replace(machine, state=State.IDLE))
    if event.command is Command.STAND_DOWN:
        return _unchanged(machine)
    if event.command is Command.TRIGGER:
        # The operator has opened the fader, so the band is evidently playing
        # and the box is on duty. Announced: the state changes, the why line
        # says the open armed it, and `app.session_entries` logs the arming.
        return _opening(machine, event, armed_by_operator=True)
    if event.command is Command.READY:
        # Same reasoning as TRIGGER above: something good just happened for
        # the home team, so the box is evidently on duty, whether or not
        # anyone remembered to arm it first.
        return _readying(machine, armed_by_operator=True)
    if event.command is Command.RELEASE:
        # Moves the fader and changes nothing: a close says nothing about
        # whether the band is in the stands. Deliberately not RELEASING, which
        # would have the box arm itself on the way into the fade and stand
        # itself down again as it lands, in the log and on the page.
        return Outcome(machine=machine, fader=FaderCommand.FADE)
    # FADE_COMPLETE and RIDE_IN_COMPLETE are already true of a closed fader,
    # and MOVE_FAILED has no move here to have failed.
    return _unchanged(machine)


def _move_failed(machine: Machine) -> Outcome:
    return Outcome(machine=replace(machine, stalled=True, riding_in=False))


def _opening(machine: Machine, event: Event, *, armed_by_operator: bool = False) -> Outcome:
    """Every open the machine emits, fast or gradual, and nothing pending."""
    return Outcome(
        machine=replace(
            machine,
            state=State.OPEN,
            pending_stand_down=False,
            stalled=False,
            riding_in=event.gradual,
            armed_by_operator=armed_by_operator,
        ),
        fader=FaderCommand.OPEN,
    )


def _readying(machine: Machine, *, armed_by_operator: bool = False) -> Outcome:
    """Ride to the hold level short of target. Always gradual - there is no
    fast form of READY, only a fast commit out of it (#6)."""
    return Outcome(
        machine=replace(
            machine,
            state=State.READY,
            pending_stand_down=False,
            stalled=False,
            riding_in=True,
            armed_by_operator=armed_by_operator,
        ),
        fader=FaderCommand.READY,
    )


def _closing(machine: Machine, *, pending_stand_down: bool) -> Outcome:
    return Outcome(
        machine=replace(
            machine,
            state=State.RELEASING,
            pending_stand_down=pending_stand_down,
            stalled=False,
            riding_in=False,
            armed_by_operator=False,
        ),
        fader=FaderCommand.FADE,
    )


def _idle(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        return _opening(machine, event)
    if event.command is Command.READY:
        return _readying(machine)
    if event.command is Command.STAND_DOWN:
        return Outcome(machine=replace(machine, state=State.STANDING_DOWN))
    if event.command is Command.ARM:
        return _unchanged(machine)
    # RELEASE and FADE_COMPLETE are already true of a closed fader.
    return _unchanged(machine)


def _ready(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        # Committing: the ordinary up buttons, from wherever the ride to the
        # hold level got to - mid-ride or already settled makes no difference,
        # unlike OPEN's own `riding_in` snap (#6).
        return _opening(machine, event)
    if event.command is Command.RELEASE:
        # Score reversed, or a plain close: the same 2 s fade as any other
        # close. The band may have quietly started, so this is never a snap.
        return _closing(machine, pending_stand_down=False)
    if event.command is Command.STAND_DOWN:
        # Same caution as a stand-down from OPEN, and for the same reason: the
        # fader is up and the box cannot be sure the band has not started (#6).
        return _closing(machine, pending_stand_down=True)
    if event.command is Command.MOVE_FAILED:
        return _move_failed(machine)
    if event.command is Command.READY and machine.stalled:
        # The ride to the hold level never got there. Send it again.
        return _readying(machine)
    if event.command is Command.RIDE_IN_COMPLETE and machine.riding_in:
        return Outcome(machine=replace(machine, riding_in=False))
    # A second READY once the ride has landed only confirms what is already
    # true, same as a TRIGGER while already OPEN.
    return _unchanged(machine)


def _open(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.RELEASE:
        return _closing(machine, pending_stand_down=False)
    if event.command is Command.STAND_DOWN:
        # Fade out and stand down when it lands, rather than slamming shut.
        return _closing(machine, pending_stand_down=True)
    if event.command is Command.MOVE_FAILED:
        return _move_failed(machine)
    if event.command is Command.TRIGGER and machine.stalled:
        # The open never got there. Send it again, at the speed now asked for.
        return _opening(machine, event)
    if event.command is Command.TRIGGER and machine.riding_in and not event.gradual:
        # The band is coming in now; snap the rest of the way.
        return _opening(machine, event)
    if event.command is Command.RIDE_IN_COMPLETE and machine.riding_in:
        return Outcome(machine=replace(machine, riding_in=False))
    # A trigger while open only confirms what is already true.
    return _unchanged(machine)


def _releasing(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        # The snap back. A pending stand-down is cancelled: the band started
        # again, so standing down would now be wrong.
        return _opening(machine, event)
    if event.command is Command.FADE_COMPLETE:
        landing = State.STANDING_DOWN if machine.pending_stand_down else State.IDLE
        return Outcome(machine=replace(machine, state=landing, pending_stand_down=False, stalled=False))
    if event.command is Command.STAND_DOWN:
        return Outcome(machine=replace(machine, pending_stand_down=True))
    if event.command is Command.MOVE_FAILED:
        return _move_failed(machine)
    if event.command is Command.RELEASE and machine.stalled:
        # The fade died partway. Fade again, from wherever it stopped.
        return Outcome(machine=replace(machine, stalled=False), fader=FaderCommand.FADE)
    # Already fading.
    return _unchanged(machine)


_HANDLERS = {
    State.STANDING_DOWN: _standing_down,
    State.IDLE: _idle,
    State.READY: _ready,
    State.OPEN: _open,
    State.RELEASING: _releasing,
}


_DESCRIPTIONS = {
    State.STANDING_DOWN: "Standing down. You can still drive the fader; an open arms the box.",
    State.IDLE: "Armed and closed, waiting for the band.",
    State.READY: "Riding to the hold level. Any trigger takes it the rest of the way.",
    State.OPEN: "Open at unity.",
    State.RELEASING: "Fading out. Any trigger brings it straight back up.",
}


def describe(machine: Machine) -> str:
    """The plain-language why line for the UI (design.md 5.5)."""
    text = _DESCRIPTIONS[machine.state]
    if machine.handoff:
        text += " StageMix has the DCA; where it really is stays unknown until you say."
        if machine.queued is not None:
            text += " Your last tap is waiting on that answer."
    if machine.armed_by_operator:
        text += " Armed by that: the box was standing down."
    if machine.stalled:
        text += " The last fader move did not finish; tap it again to retry."
    if machine.pending_stand_down:
        text += " Standing down once the fade lands."
    if machine.allow_detector:
        text += " The detector is driving; you can override at any time."
    else:
        text += " You are driving; the detector cannot move the fader."
    return text
