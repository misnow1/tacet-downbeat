"""The operating state machine.

    STANDING DOWN --(arm)--> IDLE --(trigger)--> OPEN
        |                                          ^
        +----(operator trigger: arms, then opens)--+
                              ^                   |
                              |          loss of consensus
                              |                   v
                              +------------- RELEASING (2 s fade)
                                                  |
                                    any trigger snaps back to OPEN

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
is unrecoverable.

The machine emits `OPEN` and `FADE` and there is no third option. Faders only,
never mutes: the band mics feed other mixes pre-fader and post-mute, so there is
deliberately no mute for anything here to reach for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class State(StrEnum):
    #: Boot state. Band not in the stands - pregame, halftime, exodus.
    STANDING_DOWN = "standing-down"
    #: Armed, band in the stands, DCA closed.
    IDLE = "idle"
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
    #: Loss of consensus, or the operator reaching for the fade.
    RELEASE = "release"
    #: The fade reached the bottom.
    FADE_COMPLETE = "fade-complete"
    #: A send failed partway through a move, so the fader stopped short of
    #: where the state says it is going. Reported by the shell, never tapped.
    MOVE_FAILED = "move-failed"
    #: A ride-in reached the top. Reported by the shell, never tapped.
    RIDE_IN_COMPLETE = "ride-in-complete"


class FaderCommand(StrEnum):
    OPEN = "open"
    FADE = "fade"


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
    #: The operator opened the fader while the box was standing down, which
    #: armed it (#89). Says so on the why line for as long as that open lasts:
    #: the tap did two things, and only one of them was asked for in words.
    armed_by_open: bool = False
    #: A ride-in is on its way up. While set, a fast trigger snaps the rest of
    #: the way: the whistle or the drums say the band is coming in now, and the
    #: downbeat wins over the gesture (#45). A slow trigger does not restart it.
    riding_in: bool = False


@dataclass(frozen=True)
class Outcome:
    machine: Machine
    fader: FaderCommand | None = None
    #: Set when an event was declined, with the reason, for the why line.
    refusal: str | None = None
    #: False when the event was legal but changed nothing.
    changed: bool = True


def _unchanged(machine: Machine, refusal: str | None = None) -> Outcome:
    return Outcome(machine=machine, refusal=refusal, changed=False)


def step(machine: Machine, event: Event) -> Outcome:
    """Apply one event. Never raises: an illegal event is refused, not fatal."""
    if event.source is Source.DETECTOR and not machine.allow_detector:
        return _unchanged(
            machine,
            "detector input is ignored until phase 2 is declared; the operator is driving",
        )

    handler = _HANDLERS[machine.state]
    return handler(machine, event)


def _standing_down(machine: Machine, event: Event) -> Outcome:
    if event.source is Source.DETECTOR:
        # Before anything else, and whatever `allow_detector` says elsewhere.
        # Standing down is what says the band is not in the stands, so there is
        # nothing to detect - and the detector does not get to change the mode.
        return _unchanged(machine, "standing down; the detector cannot arm the box")
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
        return _opening(machine, event, armed_by_open=True)
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


def _opening(machine: Machine, event: Event, *, armed_by_open: bool = False) -> Outcome:
    """Every open the machine emits, fast or gradual, and nothing pending."""
    return Outcome(
        machine=replace(
            machine,
            state=State.OPEN,
            pending_stand_down=False,
            stalled=False,
            riding_in=event.gradual,
            armed_by_open=armed_by_open,
        ),
        fader=FaderCommand.OPEN,
    )


def _closing(machine: Machine, *, pending_stand_down: bool) -> Outcome:
    return Outcome(
        machine=replace(
            machine,
            state=State.RELEASING,
            pending_stand_down=pending_stand_down,
            stalled=False,
            riding_in=False,
            armed_by_open=False,
        ),
        fader=FaderCommand.FADE,
    )


def _idle(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        return _opening(machine, event)
    if event.command is Command.STAND_DOWN:
        return Outcome(machine=replace(machine, state=State.STANDING_DOWN))
    if event.command is Command.ARM:
        return _unchanged(machine)
    # RELEASE and FADE_COMPLETE are already true of a closed fader.
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
    State.OPEN: _open,
    State.RELEASING: _releasing,
}


_DESCRIPTIONS = {
    State.STANDING_DOWN: "Standing down. You can still drive the fader; an open arms the box.",
    State.IDLE: "Armed and closed, waiting for the band.",
    State.OPEN: "Open at unity.",
    State.RELEASING: "Fading out. Any trigger brings it straight back up.",
}


def describe(machine: Machine) -> str:
    """The plain-language why line for the UI (design.md 5.5)."""
    text = _DESCRIPTIONS[machine.state]
    if machine.armed_by_open:
        text += " Armed by that open: the box was standing down."
    if machine.stalled:
        text += " The last fader move did not finish; tap it again to retry."
    if machine.pending_stand_down:
        text += " Standing down once the fade lands."
    if machine.allow_detector:
        text += " The detector is driving; you can override at any time."
    else:
        text += " You are driving; the detector cannot move the fader."
    return text
