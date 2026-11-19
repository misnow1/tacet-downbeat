"""Latched faults: a fault the page keeps showing until the operator clears it (#233).

Pure and standard library only. A fault is raised by something the box
observed, and stays up after the evidence has gone, because the operator may
have looked away at the moment it happened. It is cleared only from the page,
by id, so a page that has not seen a re-raise cannot clear the new one (the
same idea as a prompt's `seq`, #19). Ids are never reused.

Ids are never reused within a run, and a run seeds them from the log's last
sequence number (`FaultLatch(next_id=log.last_seq + 1)`), so they are not reused
across a restart onto the same log either: every raise writes an entry, so a
run's ids stay below the next run's seed. One gap remains: a page left open
across a restart onto a *new* log could still hold an id that the new run has
reissued.

Nothing here sends anything: a fault observes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class FaultKind(StrEnum):
    #: Reaper says it is recording but its playhead is frozen and its audio
    #: device is silent (#233).
    TAKE_STOPPED = "take-stopped"
    #: One kind per network-adapter role (#234). The latch keys on kind, so each
    #: role latches on its own.
    INTERFACE_DANTE = "interface-dante"
    INTERFACE_CONTROL = "interface-control"
    INTERFACE_SCORING = "interface-scoring"
    #: The game clock reader hit an exception it was not built for (#235). The
    #: reader was reset and carries on; the fader is unaffected.
    RTD_READER = "rtd-reader"


class Change(StrEnum):
    RAISED = "raised"
    CLEARED = "cleared"


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    id: int
    #: On the box's monotonic clock.
    raised_at: float
    message: str

    def as_data(self) -> dict[str, Any]:
        """What names this fault in a log entry."""
        return {"fault": self.kind.value, "id": self.id}

    def as_snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind.value, "message": self.message, "raised_at": self.raised_at}


@dataclass(frozen=True)
class FaultLatch:
    faults: tuple[Fault, ...] = ()
    next_id: int = 1

    def latched(self, kind: FaultKind) -> Fault | None:
        for fault in self.faults:
            if fault.kind is kind:
                return fault
        return None

    def latch(self, kind: FaultKind, message: str, *, now: float) -> tuple[FaultLatch, Fault | None]:
        """Raise `kind`. The fault is None when that kind is already latched."""
        if self.latched(kind) is not None:
            return self, None
        fault = Fault(kind=kind, id=self.next_id, raised_at=now, message=message)
        return replace(self, faults=(*self.faults, fault), next_id=self.next_id + 1), fault

    def clear(self, fault_id: int) -> tuple[FaultLatch, Fault | None]:
        """Clear the fault with this id. None when no latched fault has it."""
        for fault in self.faults:
            if fault.id == fault_id:
                kept = tuple(other for other in self.faults if other.id != fault_id)
                return replace(self, faults=kept), fault
        return self, None

    def as_snapshot(self) -> list[dict[str, Any]]:
        return [fault.as_snapshot() for fault in self.faults]
