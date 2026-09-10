# Handoff: verifying against real hardware

Working state for a session on another machine. Everything else a new session
needs is already in `CLAUDE.md` and `docs/design.md`, which travel with the repo
and load automatically.

**Last updated:** 2026-09-08 (Session A run on the Mac; results below)

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

**Done - `AddressMap` was correct.** Verified 2026-09-08 against Reaper on the
Mac, both on the wire and end to end through `tacet.app`:

| Address | Observed |
|---|---|
| `/play` | `1.0` on roll, `0.0` on stop |
| `/record` | `1.0` on roll, `0.0` on stop |
| `/stop` | `1.0` / `0.0`; still never sent by us |
| `/time` | float seconds, about 11 Hz while rolling |

These match the installed `Default.ReaperOSC` verbatim (`t/play`, `t/record`,
`t/stop`, `f/time`). Driving the transport moved `/api/state` to
`{"known": true, "recording": true, "confirmed": true}`.

**Finding: Reaper is silent when the transport is stopped.** Not a slower feed -
nothing at all, confirmed over a 6 s listen. It sends only while the transport
moves, plus one burst per transport change. So `is_fresh` lapses 2 s after
Reaper stops and the UI reads "no feedback" whenever Reaper is merely idle,
which is exactly the pre-game window where the operator wants reassurance the
recorder is alive. Fixed in `reaper.Liveness`: silence is now read in
the light of what Reaper was last doing, so a parked Reaper reads "stopped
(idle)" and only silence where the `/time` stream should have been is a fault.
There is no heartbeat available to do it any other way - probing
`/device/track/count` answers only when the value changes, so it cannot be used
as a ping.

The original reasoning, kept because it is still why this step existed: UDP is
fire-and-forget, Reaper ignores paths it does not recognise, and the box would
report success while doing nothing.

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
is assumed.

**Done - all of it holds.** Run 2026-09-08 inside Reaper against a live
recording. The script tailed the queue and persisted `queue_offset=90` into
`reaper-extstate.ini` against a 90-byte queue, so the incremental tail and
`ExtState` round-trip both work. Four events produced:

| Mark | Bars.beats | Seconds |
|---|---|---|
| M1 `SYS\|recording-started` | 1.1.18 | 0.09 |
| R1 `GAME\|q1` start | 1.4.96 | 1.98 |
| M2 `BAND\|touchdown-sequence` | 3.1.27 | 4.14 |
| R1 `GAME\|q1` end | 6.3.86 | 11.43 |

The gaps reproduce the driving script's sleeps. Confirmed: argument order,
`isrgn` really distinguishing region from marker, start and end not transposed,
`GetPlayPosition` valid mid-record, and `|` names surviving into Reaper.

1. Copy `reaper/tacet_mirror.lua` into `REAPER/Scripts`.
2. Actions → Show action list → New action → Load ReaScript, then run it.
   ("Load ReaScript" is not a top-level action; on current Reaper you reach it
   through the action list.) It asks once for the queue path and remembers it in
   `ExtState`, so point `tacet-serve --queue` at whatever you enter. Anything
   under a path you can write to is fine.
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

Never opened in a browser. The Python behind it has an end-to-end smoke test
against a dead console, and the page's own script is tested under node against a
stubbed DOM (`make test-js`) - so the rendering logic is covered, including that
the four recording states stay tellable apart. What no test can reach is whether
a real browser agrees, and whether any of it is usable at arm's length on a
tablet. Treat the page as unproven in that sense.

```
tacet-serve --console-host 127.0.0.1 --dca 3 \
    --log /tmp/tacet/game.jsonl --queue /tmp/tacet/queue.tsv \
    --reaper-host 127.0.0.1 --listen 0.0.0.0 --http-port 8080
```

Pointing `--console-host` at loopback is deliberate for a first run: UDP goes
nowhere and nothing moves a real fader. Open `http://127.0.0.1:8080` on the
laptop, then on the iPad against the machine's real address.

`--listen` is the bind address, and it governs the HTTP site and the Reaper
feedback socket together. `--listen 127.0.0.1` reaches the UI from the machine
itself and **from nowhere else**, so the iPad cannot see it; that looks like a
firewall problem and is not one. Use `0.0.0.0` for anything off-box.

Worth checking:

- Buttons are big enough to hit without looking. They are sized for that and
  have never been near a real tablet.
- The fader reads **commanded** and recording reads **confirmed** or
  **no feedback**, visibly differently. If those two ever look alike, that is a
  bug, not a style question — see design.md §5.5.
- Killing the server shows the disconnected banner, and restarting it
  reconnects on its own. Harder and worth trying: pull the wifi rather than the
  server, so the socket stalls instead of closing. The banner should turn red
  within about 40 seconds on its own, which is the case the keepalive exists
  for and the only one a stub cannot rehearse.
- The banner is absent when the link is healthy and says `Connecting` only
  briefly. A banner that sits on `Connecting` means frames are not arriving.
- The wake advice at the foot of the page should be present on the iPad, since
  the API needs HTTPS. If Safari ever holds the lock instead, the line says so -
  and that is worth reporting, because it would mean the assumption in
  design.md 5.5 is wrong.
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

1. ~~Reaper's actual OSC addresses~~ - done, `AddressMap` was right.
2. ~~Whether the Lua mirror runs~~ - done, it got nothing wrong.
3. Whether the UI is usable on an iPad, and whether commanded and confirmed
   read as clearly different. **Still open** - the page has served and its API
   is exercised, but no browser has rendered it.
4. The granularity answer, if the press box happened. **Still open.**

Reaper is no longer a guess, and neither is the browser. What is left is the
console.

Operating the thing on a Saturday is `docs/gameday.md`, which is where the
startup order and the site-specific values live.

## Environment notes from the Mac

- `.python-version` pins 3.12.12, which was not installed; the venv was built on
  3.12.13. Repin or install 3.12.12.
- `make test-lua` uses whatever `lua` is on `PATH` - 5.5.1 here. Reaper embeds
  5.4, so the suite was also run against Homebrew's keg-only
  `/opt/homebrew/opt/lua@5.4/bin/lua5.4`. 27/27 under both.
