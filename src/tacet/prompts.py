"""The arm / stand-down question (#19), as pure decisions.

Game 2 was armed before the band entered and never stood down at halftime. The
box does not decide that for the operator; it asks. Tapping `band-exits-stands`
(or starting `halftime-exodus`) raises "Stand down?", tapping
`band-enters-stands` raises "Arm?", and the operator answers with one tap or
leaves it. The answer is logged either way.

What a prompt may not do is the point of the module:

- Prompts never move a fader. A prompt is a question about duty state, not a
  duty state itself, so it is deliberately not part of `state.Machine`, and a
  `Decision` has no field a fader command could travel in.
- An annotation never changes the machine by itself. Raising a prompt writes
  log entries and nothing else; a mis-tapped `band-exits-stands` mid-drive must
  not disable anything (CLAUDE.md principle 4: announce, do not surprise).
- The accept tap is the only path from an annotation to a state change, and it
  is the ordinary `App.arm` / `App.stand_down`, with their own stale check and
  their own refusals, run by `tacet.app`. Nothing here runs a command.
- Automatic state changes are out (#19 "Out"). An RTD source for the same
  questions can come later; it would still only ask.

Everything here is pure and clock-free. The caller supplies the seq a new
prompt takes, so the module holds no state, and `tacet.app` writes the notes
these decisions return. It is standard library only, like the control path,
and imports nothing from `dm7` on purpose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from . import annotations as ann
from . import state


class Prompt(StrEnum):
    """What the box is asking. The value is what the log and the page carry."""

    ARM = "arm"
    STAND_DOWN = "stand-down"


#: Which tap asks which question. `halftime-exodus` is a span, and only its
#: start is asked about: the band never plays during it, and ending it says
#: nothing about whether to arm.
ASKS: Mapping[str, Prompt] = {
    ann.BAND_EXITS_STANDS: Prompt.STAND_DOWN,
    ann.HALFTIME_EXODUS: Prompt.STAND_DOWN,
    ann.BAND_ENTERS_STANDS: Prompt.ARM,
}

#: The log entries a prompt earns, written by `tacet.app`. Spelled once in
#: `tacet.annotations.VOCABULARY`, which says why there are five.
PROMPT_RAISED = "prompt-raised"
PROMPT_ACCEPTED = "prompt-accepted"
PROMPT_DISMISSED = "prompt-dismissed"
PROMPT_RESOLVED = "prompt-resolved"
PROMPT_WITHDRAWN = "prompt-withdrawn"


@dataclass(frozen=True)
class OpenPrompt:
    """The question currently on the page."""

    #: Its identity, increasing for as long as the box runs. An answer names
    #: it, so one that arrives after the question has moved on is recognised
    #: as late rather than acted on.
    seq: int
    prompt: Prompt
    #: The event key that raised it.
    source: str


@dataclass(frozen=True)
class Note:
    """One log entry the shell should write."""

    key: str
    prompt: Prompt
    #: None when the prompt was never raised, because the state already matched.
    seq: int | None
    source: str
    #: On a `prompt-withdrawn` note, the seq of the prompt that took its place;
    #: None when it was withdrawn because the operator said the opposite.
    replaced_by: int | None = None


@dataclass(frozen=True)
class Decision:
    """What the open prompt becomes, and what to log about it. That is all a
    decision can carry: no fader command, no machine command."""

    #: The prompt open afterwards, or None.
    prompt: OpenPrompt | None
    notes: tuple[Note, ...] = ()


def asked_by(event_key: str) -> Prompt | None:
    """The question this event raises, or None for the many that ask nothing."""
    return ASKS.get(event_key)


def already_true(prompt: Prompt, machine: state.Machine) -> bool:
    """Whether accepting `prompt` would change nothing.

    A prompt whose answer does nothing is a question the box has no business
    asking. A pending stand-down counts as standing down, and - the other way
    round - as armed: the fade will land on its own, and any up button cancels
    it, so there is nothing for an arm to do and no command that would.
    """
    if prompt is Prompt.STAND_DOWN:
        return machine.state is state.State.STANDING_DOWN or machine.pending_stand_down
    return machine.state is not state.State.STANDING_DOWN


def _note(key: str, prompt: Prompt, seq: int | None, source: str, *, replaced_by: int | None = None) -> Note:
    return Note(key=key, prompt=prompt, seq=seq, source=source, replaced_by=replaced_by)


def ask(current: OpenPrompt | None, *, source: str, machine: state.Machine, seq: int) -> Decision:
    """What one operator annotation does to the open prompt.

    `seq` is what a prompt raised now would be called; it is used only if one
    is raised, and the caller takes it only then.

    - No question behind the event: nothing changes.
    - The same kind already open: nothing changes at all, so a doubled tap
      neither restarts the operator's decision nor burns a seq.
    - The state already matches: logged as resolved without ever having been
      raised, and nothing is raised in its place - its accept would change
      nothing. If a prompt is open it is withdrawn, logged after the resolved
      note: the operator has just said the opposite, and a question they have
      contradicted must not stay on the page. That the open prompt is
      "necessarily the other kind" holds only because there are exactly two
      complementary kinds (the same kind returned above); a third kind
      forces this step to be revisited.
    - Otherwise it is raised, and replaces whatever was open, both logged.
    """
    kind = asked_by(source)
    if kind is None or (current is not None and current.prompt is kind):
        return Decision(prompt=current)
    if already_true(kind, machine):
        notes = [_note(PROMPT_RESOLVED, kind, None, source)]
        if current is not None:
            notes.append(_note(PROMPT_WITHDRAWN, current.prompt, current.seq, current.source))
            return Decision(prompt=None, notes=tuple(notes))
        return Decision(prompt=current, notes=tuple(notes))
    raised = OpenPrompt(seq=seq, prompt=kind, source=source)
    notes = []
    if current is not None:
        notes.append(_note(PROMPT_WITHDRAWN, current.prompt, current.seq, current.source, replaced_by=seq))
    notes.append(_note(PROMPT_RAISED, kind, seq, source))
    return Decision(prompt=raised, notes=tuple(notes))


def settle(current: OpenPrompt | None, machine: state.Machine) -> Decision:
    """The open prompt after the machine has moved: closed, and logged as
    resolved, once the state already matches what it asks for. The operator
    armed or stood down another way, and the question no longer applies."""
    if current is None or not already_true(current.prompt, machine):
        return Decision(prompt=current)
    return Decision(prompt=None, notes=(_note(PROMPT_RESOLVED, current.prompt, current.seq, current.source),))
