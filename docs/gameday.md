# Game day

What to start, in what order, and what it should say once it is running.

This is Phase 0 / 1. **The detector drives nothing.** Everything the fader does
on the day, an operator asked it to do. The band director remains the
compliance layer; this system only tracks whether the band is playing.

Read `design.md` for why any of it is shaped this way. This file is the
checklist, not the reasoning.

---

## Site values to fill in

These are not in the repo because nobody has stood in the press box with them
yet. Write them down here the first time they are known.

| | Value | Where it comes from |
|---|---|---|
| Console IP | `__________` | DM7: Setup > Network > For Mixer Control |
| Band DCA number | `__________` | The console's DCA layout |
| Recording path | `__________` | NAS share; never the repo (`audio/` is gitignored) |
| Box address on the VLAN | `__________` | `ipconfig getifaddr en0` |

**The console has never been driven by this software.** `verify_dm7` has not
been run against a DM7, so whether the fade is smooth or steps in 0.5 dB
increments is unknown, and `--quantized` may turn out to be needed. Run the
granularity check before trusting the fade in front of an audience:

```
python -m tacet.verify_dm7 --host <console IP> --dca <n> --granularity
```

---

## Once per machine

```
git clone git@github.com:misnow1/tacet-downbeat.git
cd tacet-downbeat
make install
make check          # should be green before a game, not on the day
```

In Reaper, once:

1. DVS as the audio device, at 48 kHz, with the band channels mapped.
2. Preferences > Control/OSC/web > add an OSC device. **Listen port 8000**,
   **device port 9000**, feedback enabled. These are the defaults the box
   assumes; anything else needs `--reaper-port` / `--reaper-feedback-port`.
3. Copy `reaper/tacet_mirror.lua` into `REAPER/Scripts`.

---

## Starting up, in order

Order matters in two places, both called out below. Everything else is
preference.

### 1. Reaper

Open the game project, confirm DVS is the audio device, arm the band tracks,
and point the record path at the NAS. Do not start recording yet.

### 2. The mirror script

Actions > Show action list > New action > Load ReaScript > `tacet_mirror.lua`,
then run it. It asks once for the queue path and remembers it in `ExtState`.

The console should say:

```
[tacet] mirroring <path> from byte N
```

**Use one stable queue path and leave the file alone between games.** The queue
opens in append mode and the script remembers how far it has read, so the two
stay in step across games and restarts. Deleting the file is safe but not
useful - the script notices it has shrunk and starts over. The queue is a
transport, not a record; the annotation log is the source of truth and markers
can be regenerated from it.

### 3. The box

Whatever queue path the script is watching must be the one passed here. This is
the single most common way to have everything look healthy and mirror nothing.

```
tacet-serve \
    --console-host <console IP> \
    --dca <n> \
    --log ~/games/2026-09-13.jsonl \
    --queue "$HOME/Library/Application Support/REAPER/tacet/queue.tsv" \
    --reaper-host 127.0.0.1 \
    --listen 0.0.0.0 \
    --http-port 8080
```

- `--log` is **per game**. It is the irreplaceable artefact.
- `--queue` is **stable**, and must match step 2.
- `--listen 0.0.0.0` or the iPad cannot reach the page. `127.0.0.1` serves the
  machine and nothing else, which looks exactly like a firewall problem.
- Add `--quantized` only once the console is known to round.

### 4. The iPad

Open `http://<box address>:8080`. The page needs nothing installed and
reconnects on its own if the box restarts.

---

## Pre-flight

| Reading | Should say |
|---|---|
| State | `STANDING DOWN` |
| Fader | `-∞ dB`, tagged **commanded** |
| Recording | `unknown`, tagged **no feedback** |

`no feedback` before recording is correct, not a fault. Reaper says nothing at
all while its transport is parked, and announces the record state only when it
changes, so the box has genuinely not been told anything yet.

Then, in order:

1. **Tap Start recording.** The recording line should go to `ROLLING`, tagged
   **confirmed**, with a playhead counting up. That single tap also proves the
   OSC link in both directions.
2. **Check a marker lands.** Tap any annotation button and confirm a marker
   appears in Reaper at the playhead. This is the only check that covers the
   whole chain - box, queue, script, Reaper.
3. **Arm** when the band is in the stands. The state goes to `IDLE` and the
   fader buttons become live.

**Record before annotating.** Reaper has no negative timeline, so anything
logged before recording starts has nowhere to go on it. Those entries are kept
in the log and reported as `skipped_before_anchor` rather than dropped, but they
will not appear as markers.

---

## When to start the recording

**Before the band enters the stadium**, which in practice means as soon as
Reaper and the box are up - the pre-flight order above, unchanged.

The stadium clock runs a countdown to kickoff from around the time doors open,
150 minutes out. It ends at the team entrance, resets, and restarts at the top
of the first quarter. The band does not come in until roughly 60 minutes before
kickoff, so most of that countdown is a set of empty stands.

Record it anyway.

| Start at | Runs for | Multitrack |
|---|---|---|
| Doors, T-150 | ~6 h | ~44 GB |
| Band enters stadium, T-60 | ~4.5 h | ~33 GB |

At 14 channels of 24-bit/48 kHz that is about 11 GB between them, which is not
a reason to do anything. Two things are:

- **The empty stands are not empty of data.** A stadium filling up with no band
  playing is the cleanest negative sample available - the crowd competing on
  level with nothing underneath it, which is the exact case a gate fails
  (design.md 1). Every minute of it is free labelled material for a detector
  that does not exist yet, and it cannot be collected any other way.
- **Annotation has a floor and audio does not.** An entry logged before the
  record anchor is reported as `skipped_before_anchor` and never becomes a
  marker. `band-enters-stadium` is in the vocabulary and happens at about T-60,
  so recording that starts after it leaves that marker nowhere to land.

The second one is what sets the time. Audio started late loses audio;
annotation started late loses the annotation permanently, and design.md 5.6 is
entirely about the half that cannot be reconstructed afterwards.

There is no stop button, so starting early costs nothing that has to be undone.

---

## During the game

**There is no stop button, by design.** Each home game is a single
irreplaceable sample. Stopping is done deliberately, in Reaper. The record
button also refuses a second press: Reaper's `/record` is a toggle, and a second
press would stop the recording.

Three kinds of button:

- **OPEN / FADE OUT** - move the fader, no reason recorded. The fast path.
- **Fader buttons** (coloured, top of the grid) - move the fader *and* record
  why, in one tap. `Up on whistle`, `Up on drums`, `Up slow`, `Faded out`.
  Prefer these: the move can be reconstructed afterwards from the post-DCA
  reference channel, but nothing reconstructs why.
- **Everything else** - records only, moves nothing.

Buttons marked `(start)` are spans: tap once to open the region, again to close
it. They read `(end)` while open.

---

## When it goes wrong

The box fails visible. Anything it cannot confirm, it says.

| The page says | What it means | What to do |
|---|---|---|
| `LINK LOST` | Reaper stopped answering while its transport should have been streaming | Check Reaper is alive. The audio may still be recording |
| `not yet reported` | Link is fine, Reaper has not said whether it is recording | Normal after a box restart. Any transport change in Reaper resolves it |
| `Console unreachable` | The DM7 did not accept a packet | The operator has the fader. Ride it from the iPad and keep going |
| `Reaper is already recording` | Second press of the record button | Nothing. It refused on purpose |
| `not armed` | A fader button while standing down | Arm first. The tap was still logged |

**A restarted box mid-game cannot start recording**, and says so. It has heard
`/time` but no transport change, so it cannot tell whether Reaper is rolling,
and will not risk stopping a live recording to find out. Start or stop in Reaper
directly; the box will pick the state up from that change.

**If the mirror script dies**, the markers stop and the data does not. The
annotation log is the source of truth and every marker is derivable from it
afterwards. Do not stop the game over it, and do not try to fix it mid-game.

Note that `tacet.markers` is a library and there is no command that
regenerates a project from a log yet. Recovery is possible but is not a
button - it is not something to attempt on the day.

---

## After the game

1. **Stop the recording in Reaper**, deliberately.
2. Save the project.
3. Stop `tacet-serve` with Ctrl-C. It never fades on the way out - the operator
   is left in control.
4. Copy to the NAS: the multitrack, the project, and the annotation log. The
   log is small and the one thing that cannot be recreated.

Four streams have to end up on the NAS together (design.md 9): the DVS
multitrack, the post-DCA reference channel, the RTD capture, and the annotation
log.

---

## Not yet proven

Honest state of things, so nothing here reads as more settled than it is.

- **The console.** Never driven by this software. Addresses are verified
  against the spec, not against a DM7.
- **Fade granularity.** Unknown until `verify_dm7 --granularity` runs.
- **RTD.** Not implemented. Game timing is operator-tapped for now. One thing
  about it is known in advance and will bite on the first attempt: the
  scoreboard console **does not send its state when something connects**. It
  sends only changes, so a reader started mid-game shows blanks until each field
  first moves - no score until someone scores. Pressing `STOP` on the scoreboard
  console forces a full dump. So either the capture starts before the console
  comes up, or someone presses `STOP` once during pregame. See design.md 8.
- **The post-DCA reference channel.** Not captured yet; it is what supplies the
  ground-truth fader labels.

Reaper, the mirror script and the page have all been run against the real
thing.
