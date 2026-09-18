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

    `level_known` sits across all of the above, orthogonal to `state` (#107,
    which generalises #12): the box can be in any of them and also not know
    where the fader really is. That is true at every cold boot - the protocol is
    write-only, so nothing can be read back (#18) - and again after the operator
    hands the DCA to StageMix. Both are the same fact reached two ways, and the
    machine does not distinguish them: the cause is in the log.

    The invariant is absolute against relative. An absolute command (a snap
    TRIGGER, CLOSE_NOW) lands the fader in one known place whatever the box
    believed, so it is always safe, and it makes the level known. A relative
    one - RELEASE's fade, READY's ride, a gradual TRIGGER - ramps from
    `commanded_level`, which is fiction while the level is unknown, so it is
    refused (not queued: there is no intention worth holding onto, and a queued
    tap ran later on a belief nobody had looked at). ARM is refused too: it
    sends nothing and calls the fader closed, which only the level being known
    makes true. STAND_DOWN is never refused; it goes straight to STANDING DOWN
    and sends nothing. REPORT_READY is the one way to READY from an unknown
    level: READY has no fast form, so it cannot be driven to, only reported.
    HANDOFF makes the level unknown again. A snap TRIGGER from STANDING DOWN
    while unknown still arms and opens: STANDING DOWN never blocks the
    operator (#89), and an open is absolute.

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

The machine emits `OPEN`, `READY`, `FADE`, `CLOSE_NOW` and `REPORT_READY`, and
there is no other option. Faders only, never mutes: the band mics feed other
mixes pre-fader and post-mute, so there is deliberately no mute for anything
here to reach for. `READY` is still a fader write, to a hold level short of
target - not a mute and not silence. `CLOSE_NOW` is one write to -inf, the
complete close, so the mute is never needed. `REPORT_READY` is the one
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
    #: (#12). Makes the level unknown. A mode change, like ARM - no fader move
    #: of its own.
    HANDOFF = "handoff"
    #: One immediate packet to -inf. Absolute, so correct from any belief, which
    #: is what makes it the one move always available (#107).
    CLOSE_NOW = "close-now"
    #: The operator reporting that the fader is already at the hold level.
    #: Belief only, never a packet: READY has no fast form (see _readying), so
    #: it is the one state that cannot be reached by driving to it (#107).
    REPORT_READY = "report-ready"


class FaderCommand(StrEnum):
    OPEN = "open"
    #: Ride to the READY hold level, short of target. Still a fader write, not
    #: a mute (#6).
    READY = "ready"
    FADE = "fade"
    #: One immediate packet to -inf, confirming silence for real - never the
    #: 2 s fade, which would ramp from a belief that may be fiction (#107).
    CLOSE_NOW = "close-now"
    #: Belief only, never a packet: it names the level `Dm7Client.assume`
    #: should trust without sending anything (#107).
    REPORT_READY = "report-ready"


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
    #: Whether `commanded_level` is worth ramping from. Every ramp starts there,
    #: and it is only a belief: the DM7's OSC is write-only, so it can be wrong
    #: from the moment the box boots, and again the moment another interface
    #: (StageMix, #12) might be moving the fader. False at construction for
    #: exactly that reason - a fresh `Dm7Client` believes -inf only because that
    #: is a convenient number to start from (#107). Orthogonal to `state`, like
    #: `allow_detector`. Made true by an absolute command, made false by
    #: HANDOFF; a relative move changes neither way.
    level_known: bool = False


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


#: Commands that only a person can give. READY is entered on a prediction that
#: something is about to happen, and REPORT_READY says the fader is already at
#: the hold level: neither is something a detector can know (#6, #107).
_OPERATOR_ONLY = (Command.READY, Command.REPORT_READY)

#: Why a bare ARM is refused while the level is unknown. Quoted in
#: docs/troubleshooting.md, where a test holds it.
UNKNOWN_LEVEL_ARM = (
    "the box does not know where the fader is, so arming would claim a closed DCA "
    "it cannot vouch for; close it now, or open, to say where it is"
)
#: Why a detector-sourced CLOSE_NOW is refused. Not quoted in
#: docs/troubleshooting.md: like the other detector refusals it is not a
#: message the operator's page shows, since no detector exists before Phase 2.
DETECTOR_CLOSE_IS_THE_FADE = "the detector closes only with the 2 s fade; the instant close is the operator's"
#: Why a relative move is refused while the level is unknown. Quoted in
#: docs/troubleshooting.md, where a test holds it.
UNKNOWN_LEVEL_MOVE = (
    "the box does not know where the fader is, and this move ramps from that belief; close it now, or open, first"
)
#: Why REPORT_READY is refused once the level is known. Quoted in
#: docs/troubleshooting.md, where a test holds it.
LEVEL_ALREADY_KNOWN = "the fader level is already known; ready rides there instead of assuming it"


def step(machine: Machine, event: Event) -> Outcome:
    """Apply one event. Never raises: an illegal event is refused, not fatal."""
    if event.command in _OPERATOR_ONLY and event.source is Source.DETECTOR:
        # Unconditional, and checked before the phase gate below: READY is
        # entered on a prediction that something is about to happen, which
        # only a watching human can judge (design.md 2, CLAUDE.md "never gate
        # on level alone"). That is not the Phase 1/2 line TRIGGER sits on -
        # a detector confirming sound is present is exactly its Phase 2 job,
        # but guessing that sound is about to start never becomes one (#6).
        # REPORT_READY is the same kind of claim, made about the fader itself.
        return _unchanged(machine, "READY is operator-only; only a person can tell what is about to play")
    if machine.state is State.STANDING_DOWN and event.source is Source.DETECTOR:
        # Unconditional, whatever `allow_detector` says, and moved up here
        # (#12) so it covers HANDOFF and the level answers too, not only
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
        # second HANDOFF while the level is already unknown changes nothing.
        if not machine.level_known:
            return _unchanged(machine)
        return Outcome(machine=replace(_settled_before_unknown(machine), level_known=False))
    if event.command is Command.CLOSE_NOW and event.source is Source.DETECTOR:
        # CLAUDE.md principle 3: the machine cannot see the conductor's arms
        # come down, so every close it makes is reactive, and the 2 s fade is
        # what makes that acceptable. An instant close is the operator's, who
        # can; a detector that reached it would cut the band on a misread.
        # Refused with its own reason, not READY's operator-only text.
        return _unchanged(machine, DETECTOR_CLOSE_IS_THE_FADE)
    if event.command is Command.CLOSE_NOW:
        return _close_now(machine)
    if event.command is Command.REPORT_READY:
        return _report_ready(machine)
    if not machine.level_known:
        refused = _refused_while_unknown(machine, event)
        if refused is not None:
            return refused
    return _HANDLERS[machine.state](machine, event)


def _refused_while_unknown(machine: Machine, event: Event) -> Outcome | None:
    """What becomes of an event while the box does not know where the fader is
    (#107), or None when it goes through to the state's own handler. Only
    called with the level unknown.

    ARM is a mode change that sends nothing and calls the fader closed, which
    it cannot vouch for. The relative moves ramp from the belief. STAND_DOWN is
    never refused, and gets its own handling here because it must still change
    state: no fade is sent, so nothing would ever fire FADE_COMPLETE."""
    if event.command is Command.ARM:
        return _unchanged(machine, UNKNOWN_LEVEL_ARM)
    if event.command is Command.STAND_DOWN and machine.state is not State.STANDING_DOWN:
        # Not when already standing down: `_standing_down` deliberately reports
        # that as changing nothing, and this must not turn a no-op into a change.
        return Outcome(
            machine=replace(
                machine,
                state=State.STANDING_DOWN,
                pending_stand_down=False,
                stalled=False,
                riding_in=False,
                armed_by_operator=False,
            )
        )
    if _needs_a_known_level(event):
        return _unchanged(machine, UNKNOWN_LEVEL_MOVE)
    return None


def _settled_before_unknown(machine: Machine) -> Machine:
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


def _needs_a_known_level(event: Event) -> bool:
    """Whether this command is a ramp that depends on knowing the console's
    real level - the hazard #12 exists to prevent. A snap TRIGGER always
    passes straight through, correct from any start; STAND_DOWN and ARM get
    their own handling in `_refused_while_unknown`."""
    if event.command is Command.RELEASE:
        return True
    if event.command is Command.READY:
        return True
    return event.command is Command.TRIGGER and event.gradual


def _close_now(machine: Machine) -> Outcome:
    """One immediate packet to -inf, correct from any belief and legal in every
    state. Never the 2 s fade: the fade ramps from a number that may be fiction,
    the one thing this whole feature exists to prevent. Gated by nothing - not
    the state, not `level_known`, not a stall - so it is the one move always
    available.

    Lands in a closed state, so `state` matches what was commanded: IDLE, or
    STANDING DOWN if the box was standing down or a stand-down was pending."""
    landing = State.STANDING_DOWN if machine.state is State.STANDING_DOWN or machine.pending_stand_down else State.IDLE
    return Outcome(
        machine=replace(
            machine,
            state=landing,
            level_known=True,
            pending_stand_down=False,
            stalled=False,
            riding_in=False,
            armed_by_operator=False,
        ),
        fader=FaderCommand.CLOSE_NOW,
    )


def _report_ready(machine: Machine) -> Outcome:
    """The operator says the fader is already at the hold level. READY has no
    fast form, so this is the only way to READY from an unknown level: a belief
    is corrected and nothing is sent.

    Refused once the level is known: it would be a jump to READY with no fader
    move, claiming a hold level the box knows the fader is not at. To mean it,
    hand off first. From STANDING DOWN it arms the box exactly as READY does
    (#6, #89)."""
    if machine.level_known:
        return _unchanged(machine, LEVEL_ALREADY_KNOWN)
    return Outcome(
        machine=replace(
            machine,
            state=State.READY,
            level_known=True,
            pending_stand_down=False,
            stalled=False,
            riding_in=False,
            armed_by_operator=machine.state is State.STANDING_DOWN,
        ),
        fader=FaderCommand.REPORT_READY,
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
            # Every open ends at `open_level`, absolute, so the fader's place is
            # known from here whatever the box believed before (#107). Only
            # that: `_closing` and `_readying` are relative and never set this.
            # Known residual (#107): this is not a claim the packet was
            # delivered. `step` is pure and cannot know; a send that failed
            # still marks the level known, and only `stalled` (via MOVE_FAILED)
            # says so, loudly. Un-knowing it is a separate change.
            level_known=True,
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
    if event.command is Command.TRIGGER and (machine.stalled or not machine.level_known):
        # The open never got there. Send it again, at the speed now asked for.
        # Or the box does not know where the fader is (#107): "already open" is
        # then only a belief, and the snap is the one way to make it true. A
        # gradual trigger never gets here unknown - `step` refuses it first.
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
    if not machine.level_known:
        # Says what is not known, never why: the machine cannot tell a handoff
        # from a cold boot, and the log already records which it was.
        text += (
            " The box does not know where the fader really is - it can only say what it last"
            " asked for, never what the console did with it; close it now, or open, to say where it is."
        )
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
