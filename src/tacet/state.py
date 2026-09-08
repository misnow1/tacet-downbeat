"""The operating state machine.

    STANDING DOWN --(arm)--> IDLE --(trigger)--> OPEN
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
set. Phase 0's buttons are operator-initiated and are not the same thing; see
CLAUDE.md. The gate is here rather than in a comment because a comment does not
fail a test.

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


class FaderCommand(StrEnum):
    OPEN = "open"
    FADE = "fade"


@dataclass(frozen=True)
class Event:
    command: Command
    source: Source = Source.OPERATOR
    #: Free text for the why line - "whistle", "drums", "operator button".
    detail: str = ""


@dataclass(frozen=True)
class Machine:
    state: State = State.STANDING_DOWN
    #: Set when the operator stands down mid-fade. Standing down while open
    #: fades rather than slamming, so the intent has to outlive the fade.
    pending_stand_down: bool = False
    #: The Phase 2 gate. False means the detector cannot move anything.
    allow_detector: bool = False


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
    if event.command is Command.ARM:
        # Deliberately no fader move. A mode change is not a fader move:
        # announce, do not surprise.
        return Outcome(machine=replace(machine, state=State.IDLE))
    if event.command is Command.STAND_DOWN:
        return _unchanged(machine)
    return _unchanged(machine, "not armed; the band is not in the stands")


def _idle(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        return Outcome(
            machine=replace(machine, state=State.OPEN, pending_stand_down=False),
            fader=FaderCommand.OPEN,
        )
    if event.command is Command.STAND_DOWN:
        return Outcome(machine=replace(machine, state=State.STANDING_DOWN))
    if event.command is Command.ARM:
        return _unchanged(machine)
    # RELEASE and FADE_COMPLETE are already true of a closed fader.
    return _unchanged(machine)


def _open(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.RELEASE:
        return Outcome(machine=replace(machine, state=State.RELEASING), fader=FaderCommand.FADE)
    if event.command is Command.STAND_DOWN:
        # Fade out and stand down when it lands, rather than slamming shut.
        return Outcome(
            machine=replace(machine, state=State.RELEASING, pending_stand_down=True),
            fader=FaderCommand.FADE,
        )
    # A trigger while open only confirms what is already true.
    return _unchanged(machine)


def _releasing(machine: Machine, event: Event) -> Outcome:
    if event.command is Command.TRIGGER:
        # The snap back. A pending stand-down is cancelled: the band started
        # again, so standing down would now be wrong.
        return Outcome(
            machine=replace(machine, state=State.OPEN, pending_stand_down=False),
            fader=FaderCommand.OPEN,
        )
    if event.command is Command.FADE_COMPLETE:
        landing = State.STANDING_DOWN if machine.pending_stand_down else State.IDLE
        return Outcome(machine=replace(machine, state=landing, pending_stand_down=False))
    if event.command is Command.STAND_DOWN:
        return Outcome(machine=replace(machine, pending_stand_down=True))
    # Already fading.
    return _unchanged(machine)


_HANDLERS = {
    State.STANDING_DOWN: _standing_down,
    State.IDLE: _idle,
    State.OPEN: _open,
    State.RELEASING: _releasing,
}


_DESCRIPTIONS = {
    State.STANDING_DOWN: "Standing down. The fader will not move until you arm it.",
    State.IDLE: "Armed and closed, waiting for the band.",
    State.OPEN: "Open at unity.",
    State.RELEASING: "Fading out. Any trigger brings it straight back up.",
}


def describe(machine: Machine) -> str:
    """The plain-language why line for the UI (design.md 5.5)."""
    text = _DESCRIPTIONS[machine.state]
    if machine.pending_stand_down:
        text += " Standing down once the fade lands."
    if machine.allow_detector:
        text += " The detector is driving; you can override at any time."
    else:
        text += " You are driving; the detector cannot move the fader."
    return text
