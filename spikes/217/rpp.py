"""Region lines in a Reaper project, as text. Pure: no files, no Reaper.

A region is two top-level `  MARKER` lines sharing an index:

      MARKER 1 5799.67 GAME|timeout 1 0 1 B {GUID} 0 1
      MARKER 1 5961.94 "" 1

Everything else in the file is left byte for byte as it was, line endings
included (Reaper on macOS wrote these with CRLF).

Step 3 would add `<TRACK` blocks here; `insert_before_tracks` is the seam.
"""

from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass

MARKER_LINE = re.compile(r"^  MARKER (\d+) ")
TRACK_OR_BAY = re.compile(r"^  <(PROJBAY|TRACK)\b")
REGION_FLAG = 1
COLOR_FLAG = 0x1000000
EOL_CR = "\r"


@dataclass(frozen=True)
class Region:
    name: str
    start: float
    end: float
    color: int = 0


def native_color(r: int, g: int, b: int) -> int:
    return COLOR_FLAG | r | (g << 8) | (b << 16)


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _lines_for(index: int, region: Region, eol: str) -> list[str]:
    name = region.name
    if not name or any(c.isspace() or c == '"' for c in name):
        raise ValueError(f"region name must be non-empty with no whitespace or quotes: {name!r}")
    first = f"  MARKER {index} {region.start:.6f} {name} {REGION_FLAG} {region.color} 1 B {_guid()} 0 1"
    second = f'  MARKER {index} {region.end:.6f} "" {REGION_FLAG}'
    return [first + eol, second + eol]


def _marker_parts(lines: list[str]) -> list[list[str]]:
    return [shlex.split(line.strip()) for line in lines if MARKER_LINE.match(line)]


def insert_regions(text: str, regions: list[Region]) -> str:
    """The project text with `regions` added; existing markers are kept."""
    lines = text.split("\n")
    eol = EOL_CR if lines and lines[0].endswith(EOL_CR) else ""
    # Markers and regions are numbered separately in Reaper (M1 and R1 coexist).
    indexes = [int(p[1]) for p in _marker_parts(lines) if int(p[4]) & REGION_FLAG]
    marker_rows = [i for i, line in enumerate(lines) if MARKER_LINE.match(line)]
    first_track = next((i for i, line in enumerate(lines) if TRACK_OR_BAY.match(line)), len(lines))
    at = marker_rows[-1] + 1 if marker_rows else first_track
    nxt = max(indexes, default=0) + 1
    new: list[str] = []
    for k, region in enumerate(regions):
        new += [line.removesuffix(EOL_CR) + eol for line in _lines_for(nxt + k, region, eol)]
    # each element of `lines` except the last is followed by "\n" at join time
    return "\n".join(lines[:at] + new + lines[at:])


def insert_before_tracks(text: str, block: str) -> str:
    """Seam for step 3 (feature tracks as `<TRACK` blocks). Not built."""
    raise NotImplementedError


@dataclass(frozen=True)
class Found:
    name: str
    start: float
    end: float


def read_regions(text: str) -> list[Found]:
    """Every region in the project, in file order of the named line. Order-independent pairing."""
    by_index: dict[int, list[tuple[str, float]]] = {}
    for parts in _marker_parts(text.split("\n")):
        index, pos, name, flags = int(parts[1]), float(parts[2]), parts[3], int(parts[4])
        if flags & REGION_FLAG:
            by_index.setdefault(index, []).append((name, pos))
    found: list[Found] = []
    for rows in by_index.values():
        named = [r for r in rows if r[0]]
        if len(rows) != 2 or len(named) != 1:
            continue
        positions = sorted(p for _, p in rows)
        found.append(Found(named[0][0], positions[0], positions[1]))
    return found
