# Handoff: verifying against real hardware

Working state for a session on another machine. Everything else a new session
needs is already in `CLAUDE.md` and `docs/design.md`, which travel with the repo
and load automatically.

**Last updated:** 2026-09-08

## Getting set up on a fresh machine

```
git clone git@github.com:misnow1/tacet-downbeat.git
cd tacet-downbeat
make install     # creates .venv, installs the dev tooling
make hooks       # pre-commit
make check       # should be green before you change anything
```

Python 3.11+. The control path takes no dependencies; the web UI takes
`aiohttp`. `make install` handles both, plus the dev tooling.

## What is built

| Piece | State |
|---|---|
| `tacet.osc` | OSC 1.0 codec. Verified against the spec's worked example. |
| `tacet.dm7` | DCA fader control and ramps. Addresses verified against the spec PDF; **never exercised against a console.** |
| `tacet.annotations` | Append-only JSONL log. The source of truth. |
| `tacet.markers` | Offline derivation of markers and regions. Pure. |
| `tacet.mirror` | The TSV queue the Lua script tails. |
| `tacet.reaper` | Transport control and feedback. **OSC addresses are a guess.** |
| `reaper/tacet_mirror.lua` | Logic tested against a stubbed Reaper API (27 checks, mutation-verified). **Never run inside Reaper.** |
| `tacet.state` | The operating state machine. Pure, mutation-tested. |
| `tacet.app` | Everything wired together, transport-free. |
| `tacet.web` | aiohttp UI. Smoke-tested end to end against a dead console. |
| `tacet.serve` | `tacet-serve`, the entry point. |

## Session A — Mac with Reaper

Nothing here needs the stadium.

### 1. Confirm the OSC addresses

`tacet.reaper.AddressMap` holds the stock `Default.ReaperOSC` names *from
memory*. This is the one assumption that fails silently: UDP is fire-and-forget,
Reaper ignores paths it does not recognise, and the box would report success
while doing nothing.

In Reaper: Preferences → Control/OSC/web → add or open the OSC device. Note its
**listen port** (we send there, default 8000) and **device port** (it sends
there, default 9000). Then:

```
python -m tacet.verify_reaper --listen 30
```

Drive the transport while it listens — play, stop, record-arm. It prints every
address Reaper sends and finishes with a verdict on whether `AddressMap` matches.
Paste that output back.

To check the sending direction too:

```
python -m tacet.verify_reaper --send /play --listen 10
```

### 2. Try the mirror script

Its logic is tested against a stubbed Reaper API — `make test-lua`, or
`lua reaper/test_tacet_mirror.lua`. Queue parsing, span pairing, partial lines,
offset persistence and the orphan-end path are all covered, and the suite was
mutation-checked to confirm it actually fails when the script is broken.

What that does **not** cover is whether Reaper behaves the way the stub pretends:
that `AddProjectMarker2` takes those arguments in that order, that
`GetPlayPosition` is valid while recording, that `defer` and `ExtState` do what
is assumed. That is what this step is for.

1. Copy `reaper/tacet_mirror.lua` into `REAPER/Scripts`.
2. Actions → Load ReaScript → pick it → run. It asks once for the queue path and
   remembers it. Anything under a path you can write to is fine.
3. Generate some events:

```python
from pathlib import Path
from tacet import annotations as ann, mirror

queue = Path("/tmp/tacet/queue.tsv")
with mirror.MirrorQueue(queue) as q, ann.AnnotationLog("/tmp/tacet/game.jsonl", mirror=q) as log:
    log.record("recording-started")
    span = log.start_span("q1")
    log.record("touchdown-sequence")
    log.end_span(span)
```

Roll a recording first so the playhead is moving. Expect markers to appear at the
playhead as each line lands, and a `GAME|q1` region spanning the two span events.
Reaper's console shows what the script is doing.

Worth reporting: whether it runs at all, whether markers land near the right
place, and whether the region gets created.

### 3. First run of the UI

Never opened in a browser. The Python behind it has 202 tests and an end-to-end
smoke test against a dead console, but nothing has exercised the DOM, so treat
the page itself as unproven.

```
tacet-serve --console-host 127.0.0.1 --dca 3 \
    --log /tmp/tacet/game.jsonl --queue /tmp/tacet/queue.tsv \
    --reaper-host 127.0.0.1 --listen 127.0.0.1 --http-port 8080
```

Pointing `--console-host` at loopback is deliberate for a first run: UDP goes
nowhere and nothing moves a real fader. Open `http://127.0.0.1:8080` on the
laptop, then on the iPad against the machine's real address.

Worth checking:

- Buttons are big enough to hit without looking. They are sized for that and
  have never been near a real tablet.
- The fader reads **commanded** and recording reads **confirmed** or
  **no feedback**, visibly differently. If those two ever look alike, that is a
  bug, not a style question — see design.md §5.5.
- Killing the server shows the disconnected banner, and restarting it
  reconnects on its own.
- With Reaper running and its OSC device on, recording state should go from
  unknown to confirmed. That also double-checks the address map from step 1.
- Span buttons (quarters, halftime exodus) should highlight while open and
  clear when tapped again.

## Session B — press box with the DM7

```
python -m tacet.verify_dm7 --host <console IP> --dca <n> --granularity
```

Sends `-1550`, which is deliberately not a Table 1 value. Read the console:

- **−15.50** — arbitrary hundredths accepted; the fade can be a smooth ramp.
- **−16.00** — snapped to Table 1; set `Dm7Client(quantized=True)`.
- **no movement** — wrong address, IP or port. Nothing in software can tell you
  which; the protocol is write-only.

Then `--fade 2` runs a real two-second close from unity. Watch for stepping.

The console IP is the one under Setup → Network → For Mixer Control. Port 49900.

## What to bring back

1. Reaper's actual OSC addresses for record, play and position.
2. Whether the Lua mirror runs, and what it got wrong.
3. Whether the UI is usable on an iPad, and whether commanded and confirmed
   read as clearly different.
4. The granularity answer, if the press box happened.

With those, every remaining guess in the codebase is gone. Phase 0 is
code-complete; what is left is proving it against the three real things it has
never met — the console, Reaper, and a browser.
