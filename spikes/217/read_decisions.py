"""Read the listener's decisions back from the spike copy of a game's Reaper project.

    .venv/bin/python spikes/217/read_decisions.py game-4

Writes out/<game>.clips.jsonl (decisions) and out/<game>.context.jsonl (context regions).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import games
import model
import rpp

BOUND_TOLERANCE_S = 0.05
MIN_CLIP_S = 5.0
MAX_CLIP_S = 30.0
NEW_PREFIX = "new"
EXIT_BAD = 2
KEPT, FIXED, ADDED, REJECTED = "kept", "fixed", "added", "rejected"
DECISIONS = (KEPT, FIXED, ADDED, REJECTED)


@dataclass(frozen=True)
class Parsed:
    cls: str
    ident: str
    note: str
    start: float
    end: float


def split_name(found: rpp.Found) -> Parsed:
    parts = found.name.split(model.NAME_SEP)
    cls = parts[1] if len(parts) > 1 else ""
    ident = parts[2] if len(parts) > 2 else ""
    return Parsed(cls, ident, model.NAME_SEP.join(parts[3:]), found.start, found.end)


def read_candidates(game: games.Game) -> dict[str, dict[str, object]]:
    path = games.OUT_DIR / f"{game.id}.candidates.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {str(r["id"]): r for r in rows}


def problems(parsed: list[Parsed]) -> list[str]:
    bad = [f"unknown class {p.cls!r} (id {p.ident!r})" for p in parsed if p.cls not in model.CLASSES | {model.REJECT}]
    counts = Counter(p.ident for p in parsed if not p.ident.startswith(NEW_PREFIX))
    return bad + [f"duplicate id {i!r} x{n}" for i, n in counts.items() if n > 1]


def decide(game: games.Game, candidates: dict[str, dict[str, object]], parsed: list[Parsed]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    added = 0
    for p in sorted(parsed, key=lambda q: q.start):
        if p.ident.startswith(NEW_PREFIX) or p.ident not in candidates:
            added += 1
            rows.append(_row(f"{game.prefix}-new-{added}", game, p.cls, p, None, ADDED))
            continue
        seen.add(p.ident)
        prop = candidates[p.ident]
        moved = (
            abs(p.start - float(str(prop["start"]))) > BOUND_TOLERANCE_S
            or abs(p.end - float(str(prop["end"]))) > BOUND_TOLERANCE_S
        )
        if p.cls == model.REJECT:
            decision = REJECTED
        elif p.cls != prop["class"] or moved:
            decision = FIXED
        else:
            decision = KEPT
        rows.append(_row(p.ident, game, p.cls, p, prop, decision))
    for ident, prop in candidates.items():
        if ident not in seen:
            rows.append(_row(ident, game, model.REJECT, None, prop, REJECTED, note="deleted"))
    return rows


def _row(
    ident: str,
    game: games.Game,
    cls: str,
    p: Parsed | None,
    prop: dict[str, object] | None,
    decision: str,
    note: str = "",
) -> dict[str, object]:
    return {
        "id": ident,
        "game": game.id,
        "class": cls,
        "start": None if p is None else round(p.start, 3),
        "end": None if p is None else round(p.end, 3),
        "decision": decision,
        "proposed_class": None if prop is None else prop["class"],
        "proposed_start": None if prop is None else prop["start"],
        "proposed_end": None if prop is None else prop["end"],
        "note": note if p is None else p.note,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game", choices=sorted(games.GAMES))
    args = ap.parse_args(argv)
    game = games.GAMES[args.game]
    text = game.spike_rpp.read_bytes().decode("utf-8", errors="surrogateescape")
    found = rpp.read_regions(text)
    context = [
        {"name": f.name, "start": round(f.start, 3), "end": round(f.end, 3)}
        for f in found
        if f.name.startswith(model.CONTEXT_PREFIX + model.NAME_SEP)
    ]
    clips = [split_name(f) for f in found if f.name.startswith(model.REGION_PREFIX + model.NAME_SEP)]
    bad = problems(clips)
    if bad:
        print("refusing:\n  " + "\n  ".join(bad), file=sys.stderr)
        return EXIT_BAD
    rows = decide(game, read_candidates(game), clips)
    for r in rows:
        if r["decision"] in (KEPT, FIXED, ADDED) and r["start"] is not None:
            length = float(str(r["end"])) - float(str(r["start"]))
            if not MIN_CLIP_S <= length <= MAX_CLIP_S:
                print(f"warning: {r['id']} is {length:.1f} s long")
    games.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (games.OUT_DIR / f"{game.id}.clips.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (games.OUT_DIR / f"{game.id}.context.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in context), encoding="utf-8"
    )
    print_table(rows, context)
    return 0


def print_table(rows: list[dict[str, object]], context: list[dict[str, object]]) -> None:
    table: dict[str, Counter[str]] = {}
    for r in rows:
        table.setdefault(str(r["class"]), Counter())[str(r["decision"])] += 1
    print(f"{'class':<18}" + "".join(f"{d:>10}" for d in DECISIONS))
    for cls, counts in sorted(table.items()):
        print(f"{cls:<18}" + "".join(f"{counts[d]:>10}" for d in DECISIONS))
    total = Counter(str(r["decision"]) for r in rows)
    print(f"{'total':<18}" + "".join(f"{total[d]:>10}" for d in DECISIONS))
    for c in context:
        print(f"context: {c['name']} {c['start']}..{c['end']}")


if __name__ == "__main__":
    raise SystemExit(main())
