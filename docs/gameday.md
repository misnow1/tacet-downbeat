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
| Channels recorded | `__________` | Armed tracks on [the patch list](reaper.md#tracks-to-record); `capture.channels`, and the box checks the recording path has room for them |
| Box address on the VLAN | `__________` | `ipconfig getifaddr en0` |

Put them in `tacet.toml` on the box too, so the startup command below is one
line. See [box.md](box.md#tacettoml).

Check the config before leaving for the stadium: `tacet-serve --log
~/games/<YYYY-MM-DD>.jsonl --check` prints the banner or the refusal and starts
nothing. See [box.md](box.md#checking-without-starting).

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
| Fader | `unknown`, tagged **unknown**. The box does not know where the fader is at any boot, and says so rather than showing a number it cannot vouch for |
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
3. **Tap Close now**, in the fader column beside the readout. It sends one write
   to `-∞ dB`, the fader reads `-∞ dB` tagged **commanded**, and Arm works from
   here. Do this once at every boot, and again if the fader ever reads
   `unknown` (after **StageMix has it**, below). It is correct from any starting
   point and never a mute, but it cuts rather than fades: with the band playing
   it is an abrupt cut of the band PA. Once the box knows the level, use
   **Faded out** to close.

---

## During the game: which button when

**There is no stop button, by design.** Stopping the recording is done
deliberately, in Reaper, after the game. The record button also refuses a
second press.

**Arm and stand down** follow the band, not the clock. The box asks rather than
deciding for you (CLAUDE.md principle 4: announce, don't surprise): tapping
**Band exits stands**, or starting **Halftime exodus**, raises a **Stand
down?** question in the prompt slot at the top of the left panel; tapping
**Band enters stands** raises **Arm?**. Ending Halftime exodus asks nothing -
the band never plays during it, and only its start says anything about duty.

The exact wording:

| When | Question |
|---|---|
| Stand down, fader position known | *Band left the stands. Stand down? Fades the band out if it is up.* |
| Stand down, fader position unknown | *Band left the stands. Stand down? Moves nothing while the fader position is unknown.* |
| Arm, either belief | *Band in the stands. Arm? Moves nothing.* |

One tap answers it either way - **Not yet** to leave things as they are, or
**Stand down** / **Arm** to accept - and the answer is logged whichever one you
tap. **Not yet** does not come back on its own; if you change your mind, tap
the same annotation again to raise it fresh. The status strip always shows the
current duty state and since when, whether or not a question is open:
`ARMED 10:42` or `STOOD DOWN 12:51`, in 24-hour time, on this device's clock, or
just the word with no time after a restart, which has no duty history yet.

The question does not appear if the box's answer would be a no-op - already
armed and asked to arm again, say. There is nothing to ask in that case, so
nothing is asked.

**The question never blocks the fader column.** It lives in its own fixed slot
above the tabs, never inside the fader column, and never pushes anything
below it. Taps on it are guarded for 700ms after it appears, so a tap already
on its way toward some other button cannot land on a question that has just
popped into the same place; a tap inside that window is silently ignored, not
refused - nothing to see, tap again once you mean it.

**While the fader position is unknown**, accepting **Arm** is refused (#107):
arming would claim a closed DCA the box cannot vouch for. It is the same
question, still on screen - not replaced by a new one - with the refusal on
the strip. Tap **Close now** (or an open, if the band is
already playing), then tap **Arm** again on the same question. **Stand down**
is never refused this way; at an unknown level it sends nothing and stands
down at once, which the unknown-level copy above says up front.

**If more than one browser is open** - the iPad and a laptop, say - they all
see the same question and whichever one answers it first is the answer: the
others' copy of it closes too, the same as any other snapshot.

**Arm** and **Stand down** in MORE still do the same thing directly, without
waiting to be asked: **Arm** goes straight to `IDLE`, **Stand down** fades an
open fader and then stands down. Use them if you get ahead of a question, or
if none was raised because nothing tapped `band-enters-stands` or
`band-exits-stands` to raise one.

**Standing down never blocks you.** The fader column works in every state: if
the band strikes up before you have armed, tap the open you wanted and the box
arms itself and opens, even while the fader reads `unknown`: an open says where
the fader is, so the box knows again afterwards. **Ready** does the same once
the box knows where the fader is; while it does not, **It's at ready
level** does, and arms the box too. The why line says it did. A fade
while standing down just fades. What standing down changes is the duty labels
in the log, and it is what will keep the detector out when there is one.

**The fader column**, pinned to the right edge on the thumb, moves the fader
*and* records why, in one tap. Nothing else does that: the move can be
reconstructed afterwards from the post-DCA reference channel, but nothing
reconstructs why.

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

**When something good happens for the home team** - a touchdown, a field goal, a
first down, a stop on third down - the band will almost always play, but not
instantly. Ride up to a hold level short of target and wait to see:

| Order | Tap |
|---|---|
| 1 | **Ready (band likely)**. Rides to a hold level below target and waits - the state goes to `READY` |
| 2 | The band starts: the ordinary up buttons, **Up on whistle** / **Up on drums**, fast, from wherever the ride got to. The play is called back, under review, or the band does not play: **Score reversed** - the same 2 s fade as Faded out, one tap, right next to it |
| 3 | The reason, when there is a moment: **Touchdown**, **Field goal**, **First down** or **Defensive stop**, in the MAIN tab, not the fader column |

The reason buttons record only. Never hunt for one before moving the fader: the
log joins them up by time, and a missed downbeat cannot be recovered.

There is no cannon button. It only fires on a touchdown or a field goal, so it
can be found afterwards near one of those, and it is loud enough on every mic
to be unmistakable.

**Touchdown means the score**, tapped when the referee signals it, home team
only. It is not the band's song starting - the fader tap already records that.
Game 2's logs use an older `touchdown-sequence` key for the song, which is why
this one is a different key.

**Everything else lives in MAIN and MORE**, beside the fader column, and
records only, moving nothing. MAIN holds the game clock, scoring and timeouts,
with no scrolling. MORE holds band movement, the other band, the drumline,
Note, and Arm / Stand down / Start recording / StageMix has it - a tap there
returns to MAIN once it is answered for, except Note, which stays open until
you finish typing. Buttons marked `(start)` are spans: tap once to open the
region, again to close it. They read `(end)` while open. A span left open by an
earlier run whose button has since gone appears under **OPEN FROM AN EARLIER
RUN**, at the bottom of MORE, with one button to end it.

**The fader line is what the box commanded, never what the console reports**,
with how long ago that was next to it (`0.00 dB - 3s ago`). The DM7 cannot
answer, and a move made in StageMix will not appear on it.

**If the fader reads `unknown`** - at boot, or after you took it to StageMix -
the box does not know where it really is. Nothing that ramps from a level can be
trusted, so the four column buttons that ramp - **Up slow**, **Ready (band
likely)**, **Faded out** and **Score reversed** - go grey where they are, and
the column says `Greyed: they ramp from an unknown level`. They are never hidden
and never move, so nothing shifts under your thumb when they come back. The two
snap opens, **Up on whistle** and **Up on drums**, stay live. **Arm** is refused
with the reason on the refusal line. Nothing is queued to run later. Two
answers sit beside the readout, always in the same place:

| You know | Tap |
|---|---|
| Nothing, and the band is not playing | **Close now**. One write to `-∞ dB`, then every button works |
| The band is playing | **Up on whistle** or **Up on drums**. A snap open says where the fader is, whatever it was before |
| It is sitting at the ready level | **It's at ready level**. Sends nothing, tells the box, and puts it in `READY` |

**Close now** is never refused, and is the one close that is never a fade: it
is a single write, because a 2 s fade would start from a level the box cannot
trust. It cuts the band PA at once, so once the box knows the level use
**Faded out**. **Stand down** is never refused either.

**If you take the fader to StageMix**, tap **StageMix has it** in MORE first
and confirm. The commanded reading goes to `unknown` - it would otherwise sit
there looking current while StageMix moves the real fader, which is exactly
what happened for 76 minutes in game 2. When you take it back, tap **Close now**
or an open, as above. The log's `handed-off` entry marks the hand-off; its
`took-back` marks the moment the box knew again, which also happens at every
boot. If the box restarts while StageMix still has it, tap **StageMix has it**
and confirm again - the reading already says `unknown` and nothing on the box
changes, but the log gets the `handed-off` entry the review needs.

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
