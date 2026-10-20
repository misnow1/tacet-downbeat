"""What a candidate is, and the class names everything agrees on."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BAND_LOUD = "band-loud"
PAT_QUIET = "pat-quiet"
CROWD = "crowd"
CROWD_PA = "crowd-pa-music"
CANNON = "cannon"
WHISTLE = "whistle-official"
ENTRANCE_DRUMS = "entrance-drums"
ENTRANCE_WHISTLE = "entrance-whistle"
REJECT = "reject"

#: Overlap priority: earlier wins.
PRIORITY = (PAT_QUIET, CANNON, ENTRANCE_DRUMS, ENTRANCE_WHISTLE, WHISTLE, BAND_LOUD, CROWD_PA, CROWD)
CLASSES = frozenset(PRIORITY)
UNLIMITED = 99
DEFAULT_QUOTAS = {
    BAND_LOUD: 2,
    PAT_QUIET: 4,
    CROWD: 2,
    CROWD_PA: 2,
    CANNON: UNLIMITED,
    WHISTLE: 2,
    ENTRANCE_DRUMS: 2,
    ENTRANCE_WHISTLE: 2,
}

REGION_PREFIX = "SPIKE"
CONTEXT_PREFIX = "SPIKE-CONTEXT"
NAME_SEP = "|"
CONTEXT_SAFETY_SEARCH = "safety-search"
CONTEXT_SAFETY = "safety"
CONTEXT_RUN = "run"


@dataclass(frozen=True)
class Candidate:
    game: str
    cls: str
    start: float
    end: float
    source: str
    why: str
    evidence: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def as_row(self, run: str = "") -> dict[str, Any]:
        return {
            "run": run,
            "id": self.id,
            "game": self.game,
            "class": self.cls,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "source": self.source,
            "why": self.why,
            "evidence": self.evidence,
        }
