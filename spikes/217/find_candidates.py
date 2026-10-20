"""Propose clip candidates for one game and write them as regions into a copy of its Reaper project.

.venv/bin/python spikes/217/find_candidates.py game-4 [--dry-run] [--only CLASS]
    [--quota cls=N] [--refresh-cache]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import classes
import games
import model
import rpp
import rtd_game
import timeline
from model import Candidate

from tacet import markers, pilot
from tacet.annotations import Entry

COLORS = {
    model.BAND_LOUD: (230, 120, 30),
    model.PAT_QUIET: (60, 130, 220),
    model.CROWD: (140, 140, 140),
    model.CROWD_PA: (160, 100, 200),
    model.CANNON: (220, 50, 50),
    model.WHISTLE: (230, 200, 40),
    model.ENTRANCE_DRUMS: (50, 170, 90),
    model.ENTRANCE_WHISTLE: (40, 190, 190),
}
CONTEXT_COLOR = (90, 90, 90)
EXIT_REFUSED = 2
MIN_TRIMMED_CANNON_S = 5.0
RUN_REGION_S = 1.0
RUN_TOKEN_CHARS = 12


def overlaps(a: Candidate, b: Candidate) -> bool:
    return a.start < b.end and b.start < a.end


def trim_cannon(cand: Candidate, others: list[Candidate]) -> Candidate | None:
    """The cannon clip with the overlap cut away on the side without the hit, or None if too little is left."""
    start, end = cand.start, cand.end
    hit = cand.evidence.get("hit_t")
    for other in others:
        if not (start < other.end and other.start < end):
            continue
        before, after = other.start - start, end - other.end
        keep_before = (hit < other.start) if isinstance(hit, float) else before >= after
        if keep_before:
            end = other.start
        else:
            start = other.end
    if end - start < MIN_TRIMMED_CANNON_S or (isinstance(hit, float) and not start <= hit <= end):
        return None
    return replace(cand, start=start, end=end, why=cand.why + f" (trimmed to {end - start:.0f} s to clear a PAT clip)")


def select(proposed: dict[str, list[Candidate]], quotas: dict[str, int]) -> list[Candidate]:
    chosen: list[Candidate] = []
    for cls in model.PRIORITY:
        taken = 0
        for cand in proposed.get(cls, []):
            if taken >= quotas.get(cls, 0):
                break
            hits = [c for c in chosen if overlaps(cand, c)]
            if hits and cls == model.CANNON:
                if "hit_t" in cand.evidence:
                    for pat in (c for c in hits if c.cls == model.PAT_QUIET):
                        evidence = {**pat.evidence, "cannon_hit_t": cand.evidence["hit_t"]}
                        chosen[chosen.index(pat)] = replace(pat, evidence=evidence)
                trimmed = trim_cannon(cand, hits)
                if trimmed is None or any(overlaps(trimmed, c) for c in chosen):
                    continue
                cand = trimmed
            elif hits:
                continue
            chosen.append(cand)
            taken += 1
    return chosen


def assign_ids(chosen: list[Candidate], prefix: str) -> list[Candidate]:
    ordered = sorted(chosen, key=lambda c: c.start)
    return [replace(c, id=f"{prefix}-{i:02d}") for i, c in enumerate(ordered, start=1)]


def region_name(c: Candidate) -> str:
    return model.NAME_SEP.join((model.REGION_PREFIX, c.cls, c.id))


def regions_for(cands: list[Candidate], context: list[rpp.Region]) -> list[rpp.Region]:
    out = [rpp.Region(region_name(c), c.start, c.end, rpp.native_color(*COLORS[c.cls])) for c in cands]
    return out + context


def context_regions(game: games.Game, ctx: classes.Ctx) -> list[rpp.Region]:
    safety = classes.safety_window(ctx) if game.id in classes.SAFETY_SEQ else None
    if safety is None:
        return []
    name = model.NAME_SEP.join((model.CONTEXT_PREFIX, model.CONTEXT_SAFETY_SEARCH))
    return [rpp.Region(name, max(0.0, safety[0]), safety[1], rpp.native_color(*CONTEXT_COLOR))]


def parse_quotas(items: list[str]) -> dict[str, int]:
    quotas = dict(model.DEFAULT_QUOTAS)
    for item in items:
        cls, _, n = item.partition("=")
        if cls not in model.CLASSES or not n.isdigit():
            raise SystemExit(f"bad --quota {item!r}; classes are {sorted(model.CLASSES)}")
        quotas[cls] = int(n)
    return quotas


def which_proposers(only: str | None) -> list[str]:
    if only is None:
        return list(classes.PROPOSERS)
    if only not in model.CLASSES:
        raise SystemExit(f"unknown class {only!r}; classes are {sorted(model.CLASSES)}")
    return ["entrances"] if only in (model.ENTRANCE_DRUMS, model.ENTRANCE_WHISTLE) else [only]


def calibration_line(state: timeline.PilotState, entries: list[Entry]) -> str:
    """Report only: pilot dBFS minus DCA dB on snap opens (Game 4 should read about -12.03)."""
    try:
        take = pilot.take_of(entries, pilot.resolve_anchor(entries, None))
        found = pilot.calibrate(pilot.Envelope(state.dbfs, state.step, 0.0), take)
    except (pilot.PilotCheckError, markers.NoAnchorError) as exc:
        return f"pilot calibration: unavailable ({type(exc).__name__})"
    return f"pilot calibration: {found.offset_db:.2f} dB from {found.count} snap opens"


def build_ctx(game: games.Game, refresh: bool) -> classes.Ctx:
    layout = games.check_layouts(game)
    print(
        f"{game.id}: 24-bit mono, {layout.sample_rate} Hz, time reference {layout.time_reference}, "
        f"{layout.duration_seconds / 3600:.2f} h"
    )
    entries, timing = timeline.trusted_entries(timeline.read_log(game))
    print(
        f"take start (wall median) {timing.take_start_wall:.3f}, spread {timing.spread_s:.3f} s, "
        f"{len(entries)} trusted entries, dropped seqs {list(timing.dropped)}"
    )
    rep = rtd_game.replay(game, refresh=refresh) if game.pcap else None
    state = timeline.pilot_state(game, refresh=refresh)
    print(calibration_line(state, timeline.read_log(game)))
    print(f"pilot: {state.duration / 3600:.2f} h, closed {state.closed.mean():.1%} of the take")
    return classes.Ctx(game, entries, timing.take_start_wall, state, rep, refresh)


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game", choices=sorted(games.GAMES))
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--only", metavar="CLASS")
    ap.add_argument("--quota", action="append", default=[], metavar="CLS=N")
    ap.add_argument("--refresh-cache", action="store_true")
    args = ap.parse_args(argv)
    game = games.GAMES[args.game]
    quotas = parse_quotas(args.quota)
    if args.only and not args.dry_run:
        print("--only implies --dry-run: a partial set is never written")
        args.dry_run = True
    stale = games.OUT_DIR / f"{game.id}.clips.jsonl"
    if not args.dry_run and stale.exists():
        print(f"refusing: {stale} holds decisions from an earlier run; move it aside first", file=sys.stderr)
        return EXIT_REFUSED
    if not args.dry_run and game.spike_rpp.exists():
        print(f"refusing: {game.spike_rpp} exists; move or delete it first", file=sys.stderr)
        return EXIT_REFUSED
    started = time.time()
    ctx = build_ctx(game, args.refresh_cache)
    proposed: dict[str, list[Candidate]] = {}
    for key in which_proposers(args.only):
        t0 = time.time()
        for cand in classes.PROPOSERS[key](ctx):
            proposed.setdefault(cand.cls, []).append(cand)
        print(f"-- {key} done in {time.time() - t0:.0f} s")
    if args.only in (model.ENTRANCE_DRUMS, model.ENTRANCE_WHISTLE) or args.only:
        quotas = {k: (v if k == args.only else 0) for k, v in quotas.items()}
    chosen = assign_ids(select(proposed, quotas), game.prefix)
    print_table(proposed, chosen)
    if args.dry_run:
        print(f"dry run: nothing written ({time.time() - started:.0f} s)")
        return 0
    write_outputs(game, chosen, context_regions(game, ctx), uuid.uuid4().hex[:RUN_TOKEN_CHARS])
    print(f"done in {time.time() - started:.0f} s")
    return 0


def print_table(proposed: dict[str, list[Candidate]], chosen: list[Candidate]) -> None:
    taken = Counter(c.cls for c in chosen)
    print(f"{'class':<18}{'proposed':>9}{'chosen':>8}")
    for cls in model.PRIORITY:
        print(f"{cls:<18}{len(proposed.get(cls, [])):>9}{taken.get(cls, 0):>8}")
    print(f"{'total':<18}{sum(map(len, proposed.values())):>9}{len(chosen):>8}")
    for c in chosen:
        print(f"  {c.id} {c.cls:<17}{c.start:9.1f} {c.end:9.1f}  {c.why}")


def write_outputs(game: games.Game, chosen: list[Candidate], context: list[rpp.Region], run: str) -> None:
    games.OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_name = model.NAME_SEP.join((model.CONTEXT_PREFIX, model.CONTEXT_RUN, run))
    context = [*context, rpp.Region(run_name, 0.0, RUN_REGION_S, rpp.native_color(*CONTEXT_COLOR))]
    text = game.rpp.read_bytes().decode("utf-8", errors="surrogateescape")
    new_text = rpp.insert_regions(text, regions_for(chosen, context))
    assert game.spike_rpp != game.rpp
    game.spike_rpp.write_bytes(new_text.encode("utf-8", errors="surrogateescape"))
    write_jsonl(games.OUT_DIR / f"{game.id}.candidates.jsonl", [c.as_row(run) for c in chosen])
    print(f"wrote {game.spike_rpp} ({len(chosen)} regions, {len(context)} context incl. run {run})")


if __name__ == "__main__":
    raise SystemExit(main())
