# Spike 217: pick clips by ear

Throwaway. Candidates are proposed by `find_candidates.py` and written as regions into a COPY of each game's
Reaper project, next to the original: `<name>.spike-217.RPP`. The originals are never touched.

1. Before the listening hour: open each copy in Reaper, save it untouched (Cmd-S, not Save As), then run
   `.venv/bin/python spikes/217/read_decisions.py game-4` (and `game-3`). Every candidate must read back as
   kept with zero diffs. If not, stop: Reaper rewrote the region lines in a way the reader does not follow.
2. Step through the regions named `SPIKE|<class>|<id>`. Listen, then edit the name:
   - Keep: leave it alone.
   - Fix the class: change the middle word, for example `SPIKE|cannon|G4-07` to `SPIKE|entrance-drums|G4-07`.
   - Fix the span: drag the region edges (moves of 0.05 s or less do not count).
   - Reject: rename the class to `reject`, or delete the region.
   - Add one the finder missed: a new region named `SPIKE|<class>|new`. Add a note after the id with `|`
     and no spaces, for example `SPIKE|cannon|G4-07|late-by-a-beat`.
   Classes: band-loud, pat-quiet, crowd, crowd-pa-music, cannon, whistle-official, entrance-drums,
   entrance-whistle. Names take no spaces, quotes or apostrophes.
3. Game 3, safety call: the maintainer does not know its time. The grey region
   `SPIKE-CONTEXT|safety-search` spans seq 712 (defensive-stop, 22:03:28 EDT) +/- 600 s. Solo
   `Announcer Mic Dry` (track 17) inside it, find the safety call, and add a region named
   `SPIKE-CONTEXT|safety` at it (a short region is fine). If it is outside the window, mark it wherever it is.
   The cannon candidate tagged "safety search" is only the loudest coincident onset in that window.
4. Save (Cmd-S, not Save As), then `.venv/bin/python spikes/217/read_decisions.py game-4` (and `game-3`). It writes
   `out/<game>.clips.jsonl` (decisions) and `out/<game>.context.jsonl` (context regions, passed through,
   never treated as clips). Exit 2 means an unknown class or a duplicate id; it lists them.

Regenerating: `find_candidates.py <game>` refuses to run if the `.spike-217.RPP` exists. Move it aside first.
`data/spike-217/` holds caches (envelopes, pilot, RTD replay); `--refresh-cache` rebuilds them.

Each run stamps a token into the candidates file and into the copy as `SPIKE-CONTEXT|run|<token>`. Do not delete
that region: `read_decisions.py` refuses if it is missing or does not match. It also refuses (exit 2) on a
misspelled `SPIKE` prefix, an unknown or empty id, a broken region pair, or a name it cannot parse, so a
decision is never lost silently. A pat-quiet clip whose note says PILOT CLOSED needs a check that the band
is still playing.
