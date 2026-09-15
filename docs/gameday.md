# Game day

What to start, in what order, and which button to tap when.

This is Phase 0 / 1. **The detector drives nothing.** Everything the fader does
on the day, an operator asked it to do. The band director remains the
compliance layer; this system only tracks whether the band is playing.

The detail lives in three other docs. None of it is needed to follow this page
top to bottom:

- [reaper.md](reaper.md) - Reaper setup, **the tracks to record**, the mirror
  script and its queue, and why the recording starts early.
- [box.md](box.md) - installing the box, `tacet.toml`, the startup banner, the
  flags, and reading the page in detail.
- [troubleshooting.md](troubleshooting.md) - what every message on the page, the
  terminal and the Reaper console means, and what to do about it.

Read `design.md` for why any of it is shaped this way.

---

## Site values to fill in

These live here and nowhere else in the repo. As of 2026-09-12 the console has
been reached, so the IP and the DCA number are both known good - write them in.
An empty row below now means nobody wrote it down, not that nobody knows.

| | Value | Where it comes from |
|---|---|---|
| Console IP | `__________` | DM7: Setup > Network > For Mixer Control |
| Band DCA number | `__________` | The console's DCA layout |
| Recording path | `__________` | NAS share; never the repo (`audio/` is gitignored) |
| Box address on the VLAN | `__________` | `ipconfig getifaddr en0` |

Put them in `tacet.toml` on the box too, so the startup command below is one
line. See [box.md](box.md#tacettoml).

---

## Starting up, in order

Order matters in two places: the mirror script before the box, and the queue
path matching between them.

### 1. Reaper

Open the game project and confirm DVS is the audio device. Check every track on
[the patch list](reaper.md#tracks-to-record) is there, named, and **armed** -
including the post-DCA band reference, proven by pulling the DCA down and
watching its track go silent. Point the record path at the NAS. **Do not start
recording yet.**

### 2. Load and run the ReaScript

Actions > Show action list > New action > Load ReaScript > `tacet_mirror.lua`,
then **run it**. The Reaper console should say:

```
[tacet] mirroring <path> from byte N
```

Use the same queue path every game and leave the queue file alone. Anything
else in the console: [troubleshooting.md](troubleshooting.md#the-mirror-console).

### 3. The box

```
tacet-serve --log ~/games/<YYYY-MM-DD>.jsonl
```

A fresh `--log` every game, named for the date. Read the banner's `queue` line
against the path the ReaScript printed in step 2: **they must be the same
path**, or everything looks healthy and nothing mirrors. A `WARNING` row or a
refusal: [troubleshooting.md](troubleshooting.md#the-terminal).

### 4. The iPad

Open `http://<box address>:8080`. The page needs nothing installed and
reconnects on its own if the box restarts.

**Turn Auto-Lock off**: Settings > Display and Brightness > Auto-Lock > Never.
An iPad that locks its screen stops being an operator interface, and nobody
finds out until they look down at a black slab in the middle of a drive. The
page says so at the bottom of the screen, because it cannot do it itself. Put
the iPad on a charger too; Never plus a bright screen is a three-hour draw.

---

## Pre-flight

| Reading | Should say |
|---|---|
| State | `STANDING DOWN` |
| Fader | `-∞ dB`, tagged **commanded** |
| Recording | `unknown`, tagged **no feedback** (correct until something is recorded) |
| Top of the screen | Nothing. No banner is the healthy state |
| Counter beside the state | A green dot and a figure in seconds, resetting to `0s` |
| Bottom of the screen | The Auto-Lock advice, until the setting is changed |

Then, in order, **before the band enters the stadium** - as soon as the above
reads right. Starting early costs nothing; see
[reaper.md](reaper.md#when-to-start-the-recording).

0. **If Start recording is greyed out**, roll a short recording in Reaper and
   stop it. Expected on this rig every session, not a fault
   ([why](reaper.md#the-record-button-is-greyed-out-before-you-touch-anything)).
1. **Tap Start recording on the page, not in Reaper.** The recording line goes
   to `ROLLING`, tagged **confirmed**, with a playhead counting up. Only this
   button writes the anchor the whole log is measured from.
2. **Check a marker lands.** Tap any annotation button and confirm a marker
   appears in Reaper at the playhead. This is the only check that covers the
   whole chain - box, queue, script, Reaper.

---

## During the game: which button when

**There is no stop button, by design.** Stopping the recording is done
deliberately, in Reaper, after the game. The record button also refuses a
second press.

**Arm and stand down** follow the band, not the clock:

| When | Tap |
|---|---|
| The band is in the stands | **Arm**. The state goes to `IDLE` and the fader buttons go live |
| The band leaves the stands - halftime exodus, end of the game | **Stand down**. If the fader is up it fades first, then stands down |
| The band is back in the stands after halftime | **Arm** again |

**The fader buttons** (coloured, top of the grid) move the fader *and* record
why, in one tap. Prefer them to OPEN / FADE OUT: the move can be reconstructed
afterwards, but nothing reconstructs why.

| When | Tap |
|---|---|
| The conductor whistles the band in | **Up on whistle** |
| The drums start before any whistle | **Up on drums** |
| The band has already started and you missed it | **Up slow**. Rides in over 1.5 s rather than snapping open under a phrase in progress |
| The music has stopped, for any reason | **Faded out**. The same 2 s fade whatever the reason |

- **Up on whistle** or **Up on drums** while **Up slow** is still riding in
  **snaps the rest of the way**: the whistle means the band is about to play. If
  the play is then called back or flagged and nothing plays, fade as usual.
- Tapping **Up slow** again mid-ride does not restart it.
- A quiet section is not a stop. Under the PAT after a touchdown the band drops
  quiet and comes back; leave it open.

**Everything else** records only and moves nothing. Tap it as it happens. Buttons
marked `(start)` are spans: tap once to open the region, again to close it. They
read `(end)` while open.

**The fader line is what the box commanded, never what the console reports.**
The DM7 cannot answer, and a move made in StageMix will not appear. Glance at
the DCA in StageMix to check the two agree.

---

## When it goes wrong

The operator keeps the fader. Ride the band from StageMix and keep going, then
look the message up in [troubleshooting.md](troubleshooting.md). Neither the
recording nor the log depends on the page or the markers working.

---

## After the game

1. **Stop the recording in Reaper**, deliberately.
2. Save the project.
3. Stop `tacet-serve` with **Ctrl-C twice**. It never fades on the way out, and
   stopping the box does not stop the recording.
4. Copy to the NAS: the multitrack, the project, and the annotation log. The
   log is small and the one thing that cannot be recreated.

Four streams have to end up on the NAS together (design.md 9): the DVS
multitrack, the post-DCA reference channel, the RTD capture, and the annotation
log.
