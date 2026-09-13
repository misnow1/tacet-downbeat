# Game day

What to start, in what order, and what it should say once it is running.

This is Phase 0 / 1. **The detector drives nothing.** Everything the fader does
on the day, an operator asked it to do. The band director remains the
compliance layer; this system only tracks whether the band is playing.

Read `design.md` for why any of it is shaped this way. This file is the
checklist, not the reasoning.

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

Once they are known, put them in `tacet.toml` on the box rather than retyping
them into every command. Copy `tacet.toml.example`, fill it in, and the startup
commands below shrink to the part that actually changes between games. The file
is gitignored; this table stays the written record.

Every value in it still has a flag, and the flag wins:

```
built-in default  <  tacet.toml  <  the flag you type
```

so nothing below stops working if the file is absent, wrong, or deliberately
overridden mid-game. Each tool says which config it read on startup -- `tacet-serve`
in the `config` row of its banner, the two `verify_` tools on a line of their
own. If that reads `none (flags only)` and you expected a file, you are in the
wrong directory.

**The console has been driven by this software** (2026-09-12). `verify_dm7
--granularity` sent `-1550` and the DM7 displayed **−15.50**, not −16.00: it
takes arbitrary hundredths of a dB and does not snap to Table 1. So the 2 s
close is a smooth 50 Hz ramp rather than a 32-step staircase, and **`--quantized`
is not needed at this site.** Leave it off; `quantized = false` in `tacet.toml`.

That one probe also retired the largest assumption in the repo. The fader moving
at all proves the OSC packet format is accepted, the address
`MIXER:Current/DCA/Fader/Level` is right, the IP and port 49900 are right, and
the DCA number is right. None of that had ever been confirmed against hardware.

A `--fade 2` close was watched on StageMix the same day and glided rather than
stepping. Read that as ruling out gross stepping, not as proof of sample-accurate
tracking: StageMix's own refresh over wifi is the slower of the two things being
observed, so it could hide a fine stagger. Nothing suggests one.

Re-run the check after any console firmware update, and at a new venue - this is
a fact about *this* DM7, not about DM7s:

```
python -m tacet.verify_dm7 --host <console IP> --dca <n> --granularity
```

or, once `tacet.toml` has the console in it, just:

```
python -m tacet.verify_dm7 --granularity
```

`--granularity`, `--fade` and `--level` are never taken from the config file.
They choose what this tool *does* to a console, and a file that could select one
of them would move a fader nobody asked to move.

---

## Once per machine

```
git clone git@github.com:misnow1/tacet-downbeat.git
cd tacet-downbeat
make install
make check          # should be green before a game, not on the day

cp tacet.toml.example tacet.toml
$EDITOR tacet.toml  # console IP, DCA, queue path -- the table above
```

In Reaper, once:

1. DVS as the audio device, at 48 kHz, with the band channels mapped.
2. Preferences > Control/OSC/web > add an OSC device. **Listen port 8000**,
   **device port 9000**, feedback enabled. These are the defaults the box
   assumes; anything else needs `--reaper-port` / `--reaper-feedback-port`, or
   `reaper.send_port` / `reaper.receive_port` in `tacet.toml` so it survives
   into next game.
3. Copy `reaper/tacet_mirror.lua` into `REAPER/Scripts`.

---

## One recording, one log, started early

The rule the rest of this file assumes: **one continuous recording per game, in
one Reaper project, with its own `--log`, started before the crowd arrives and
not stopped until the band is out.**

It is tempting to record a test, stop, and start properly when the band arrives.
Prefer not to, and take a fresh `--log` if you do. `tacet.markers` anchors the
whole timeline to the **first** `recording-started` in a log, so a second
recording in the same log is measured from the wrong place. The box warns when
the log you name already holds a recording.

That is now **untidy rather than wrong**: every entry is stamped with Reaper's
own playhead as it is written, and a stamped position is used in preference to
any arithmetic, so an annotation lands on the audio it describes whichever
recording it belongs to. The warning still fires, and `tacet.markers` still
reports the extra recording, because a log covering two recordings is a thing
you want to know about. But it no longer silently misplaces anything.

The stamp is missing only when Reaper is not streaming its position -- it is
silent whenever the transport is parked -- and those entries fall back to the
arithmetic, which is the case the warning is really about.

Two things make starting early the easy choice rather than a sacrifice:

- **The empty-but-filling stands are data**, not waste. A stadium filling up with
  no band playing is the cleanest negative sample there is, and it cannot be
  collected any other way.
- **`band-enters-stadium` and `band-enters-stands` have nowhere to land** if the
  recording starts after them. Audio started late loses audio; annotation
  started late loses the annotation permanently.

The same goes for halftime. The stand mics then carry your band on the field,
the visiting band on the field, and cadences on the way out and back -- music
present while the fader must stay **closed**, which is the single hardest case
the detector has to learn and the reason `other-band-on-field` is in the
vocabulary at all. A halftime is 20-30 minutes, about 2.4-3.6 GB against a
33-44 GB game. If space ever genuinely bites, trim the `halftime` span
afterwards; it is already delimited in the log.

If you want a genuine throwaway test first, make it a throwaway in both places:

```
tacet-serve --log /tmp/preflight.jsonl      # scratch project, scratch log
```

then stop in Reaper, close that project, Ctrl-C the box twice, and start again
against the game project and the game log. Run `tacet_mirror.lua` in the new
project **before** the box, or the queue lines written before it comes up are
never mirrored. The script keeps its byte offset in `ExtState`, so a new project
will not re-stamp the test's markers.

---

## Starting up, in order

Order matters in two places, both called out below. Everything else is
preference.

### 1. Reaper

Open the game project, confirm DVS is the audio device, arm the band tracks,
and point the record path at the NAS. Do not start recording yet.

Arming is also the current suspect for the greyed-out record button below, so
expect that detour every session until it is chased down. Do not skip the arming
to avoid it: an unarmed track records nothing, which is the one failure with no
recovery at all.

### 2. The mirror script

Actions > Show action list > New action > Load ReaScript > `tacet_mirror.lua`,
then run it. It asks once for the queue path and remembers it in `ExtState`.

The console should say:

```
[tacet] mirroring <path> from byte N
```

`N` is where the script left off last game, remembered in Reaper's `ExtState`.
On a queue that has never been used it is `0`. Anything else the console says
instead is in **The mirror console** under *When it goes wrong*, with what each
line needs from you.

**Do:**

- Use **one stable queue path**, every game, indefinitely.
- **Leave the file alone between games.** It is append-only and the script
  remembers its position, so a new Reaper project each game resumes exactly
  where the last one stopped and places nothing from an earlier game.
- Match `--queue` in step 3 to the path entered here. Better, set it once as
  `capture.queue` in `tacet.toml`: it is stable for the life of the setup, and
  a value in the file cannot be mistyped in a hurry the way a flag can.

**Do not:**

- **Do not rotate, rename or clear the queue per game.** It looks tidy, and it
  is the one thing that can leave the script resuming in the middle of a line
  against a file it has no position for. A whole season of queue is well under a
  megabyte; there is nothing to reclaim by tidying it.
- **Do not touch the file while the script is running.** If it ever has to be
  cleared, stop the box and the script first, and clear it between games rather
  than during one.

The queue is a transport, not a record. The annotation log is the source of
truth and every marker is derivable from it, so nothing in the queue is worth
protecting - only its byte count matters, and only to the script.

### 3. The box

Whatever queue path the script is watching must be the one passed here. This is
the single most common way to have everything look healthy and mirror nothing.

With `tacet.toml` filled in, the only thing left to type is the game:

```
tacet-serve --log ~/games/2026-09-13.jsonl
```

It prints a banner before it binds anything -- what console, what DCA, what
Reaper, and where the log and queue are going:

```
==========================================================================
  tacet       band DCA - Phase 0/1, the detector drives nothing
  config      /Users/you/tacet.toml
--------------------------------------------------------------------------
  console     10.0.0.5:49900   DCA 3
              commanded, never confirmed - the DM7's OSC is write-only
  fade        2.0s close, fast open, 1.5s ride-in
  reaper      127.0.0.1   send 8000   feedback 9000
  log         /Users/you/games/2026-09-13.jsonl
  queue       /Users/you/Library/Application Support/REAPER/tacet/queue.tsv
  page        http://<this box>:8080
              bound to 0.0.0.0; use the box's address on the VLAN
--------------------------------------------------------------------------
  before kickoff
    1  Reaper up, OSC device on: listen 8000, device 9000, feedback on
    2  tacet_mirror.lua running in Reaper, watching exactly this queue:
       /Users/you/Library/Application Support/REAPER/tacet/queue.tsv
    3  open the page, tap Start recording, confirm a marker lands
    4  arm when the band is in the stands
==========================================================================
```

**Read the `queue` line against what the script said in step 2.** They have to
be the same path, and the banner repeats it under step 2 of its own checklist so
the two can be compared without scrolling.

Everything in the banner is what the box was *told*, not what it has confirmed.
Nothing has been sent to the console at this point, and the console could not
answer if it had been.

If the `config` row says `none (flags only)`, it did not find a file and
everything above came from flags and defaults.

Without a config file, or to override it, the whole thing is still flags:

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

- `--log` is **per game**, and per *recording*. It is the irreplaceable artefact.
  Keep it a flag even when everything else lives in the file -- it is the value
  that changes, and a stale `capture.log` overwrites nothing but does bury today
  under yesterday's filename. If the log already holds a recording the banner
  says so in a `WARNING` row; see *When it goes wrong*.
- `--queue` is **stable**, and must match step 2. Good candidate for the file.
- `--listen 0.0.0.0` or the iPad cannot reach the page. `127.0.0.1` serves the
  machine and nothing else, which looks exactly like a firewall problem.
- Leave `--quantized` off: this console does not round (2026-09-12). `--no-quantized`
  turns it back off when the file sets it and you want it gone for one run.

### 4. The iPad

Open `http://<box address>:8080`. The page needs nothing installed and
reconnects on its own if the box restarts.

**Turn Auto-Lock off**: Settings > Display and Brightness > Auto-Lock > Never.
An iPad that locks its screen stops being an operator interface, and nobody
finds out until they look down at a black slab in the middle of a drive. The
page says so at the bottom of the screen, because it cannot do it itself - the
browser API for holding the screen on needs HTTPS and this is plain HTTP. Put
the iPad on a charger too; Never plus a bright screen is a three-hour draw.

---

## Pre-flight

| Reading | Should say |
|---|---|
| State | `STANDING DOWN` |
| Fader | `-∞ dB`, tagged **commanded** |
| Fader during a close | `-3.00 dB → -∞ dB` — sweeping, with its destination |
| Recording | `unknown`, tagged **no feedback** |
| Top of the screen | Nothing. No banner is the healthy state |
| Counter beside the state | A green dot and a figure in seconds, resetting to `0s` |
| Bottom of the screen | The Auto-Lock advice, until the setting is changed |

`no feedback` before recording is correct, not a fault. Reaper says nothing at
all while its transport is parked, and announces the record state only when it
changes, so the box has genuinely not been told anything yet.

Then, in order:

0. **If Start recording is greyed out, roll a short recording in Reaper and
   stop it.** On this rig that is expected every session, not a fault - see
   *The record button is greyed out before you touch anything* below. The
   throwaway take at the top of the project can be deleted later.
1. **Tap Start recording.** The recording line should go to `ROLLING`, tagged
   **confirmed**, with a playhead counting up. That single tap also proves the
   OSC link in both directions.
   **Start it here, not in Reaper.** `recording-started` is written only by this
   button, and it is the anchor the whole log is measured from. Start the
   recording in Reaper instead and the log has no anchor, so markers cannot be
   derived from it afterwards at all.
2. **Check a marker lands.** Tap any annotation button and confirm a marker
   appears in Reaper at the playhead. This is the only check that covers the
   whole chain - box, queue, script, Reaper.
3. **Arm** when the band is in the stands. The state goes to `IDLE` and the
   fader buttons become live.

**The counter beside the state is the one thing that says the page is
alive.** It reads seconds since the box last said anything, so it climbs to
about 15 and drops back to `0s` - the box only speaks every 15 seconds when it
has nothing to report, and the reset is the proof the link still delivers. Two
readings tell you two different things:

- **A figure that has stopped changing** means the page has stopped, not that
  the box has gone quiet. Reload it.
- **A figure climbing past about 40**, with a red dot and a red banner, means
  the link is down and what is on screen is stale.

It says nothing about the console. Nothing can - OSC is write-only, and a packet
sent into a black hole succeeds (design.md 5.3). `Console unreachable` appears
only when the box's own send fails.

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

  `Up slow` is the one that moves differently. `Up on whistle` and `Up on
  drums` snap open, because a missed downbeat is unrecoverable. `Up slow` rides
  in over **1.5 seconds** instead: it is for the case where the first phrase was
  already missed, where snapping open under a phrase in progress is its own kind
  of wrong. Retune it with `fader.slow_open_seconds` or `--slow-open`; the other
  two are unaffected.
- **Everything else** - records only, moves nothing.

Buttons marked `(start)` are spans: tap once to open the region, again to close
it. They read `(end)` while open.

**The fader line is what the box expects, never what the console reports.** It
is tagged **commanded** for that reason and the tag never changes: the DM7's OSC
is write-only, so nothing here is ever a readback, and an iPad move made in
StageMix will not appear. During a close the line reads

```
-3.00 dB → -∞ dB
```

— the left figure sweeping as the ramp goes out, the right one where it is
headed. `Up slow` does the same thing upward, `-∞ dB → 0.00 dB`. When the fader
is settled there is no arrow, because there is nowhere else to be. To check the expectation against reality, look at the DCA in
StageMix; that comparison is the only thing that can catch a wrong DCA number or
a console that is not listening.

---

## When it goes wrong

The box fails visible. Anything it cannot confirm, it says.

| The page says | What it means | What to do |
|---|---|---|
| The counter has stopped changing | The page itself has stopped, not the box. This is what no banner cannot tell you on its own | Reload the page. The box, the log and the recording are unaffected |
| `Not connected to the box` | The websocket is down. The page is retrying once a second | Everything on screen is the last thing the box said. The console is unaffected; ride the fader from the DM7 app if it does not come back |
| `Connecting to the box` | The socket opened but the box has not delivered anything through it yet | Normal for a moment. If it stays, the box is up and wedged rather than down |
| `No word from the box for Ns` | The socket still looks open and nothing is arriving through it, which is what stadium wifi does as the stands fill | Treat the whole page as stale. The link usually drops properly a moment later and the retry takes over |
| `LINK LOST` | Reaper stopped answering while its transport should have been streaming | Check Reaper is alive. The audio may still be recording |
| `not yet reported` | Link is fine, Reaper has not said whether it is recording | Normal after a box restart. Any transport change in Reaper resolves it |
| `Console unreachable` | The DM7 did not accept a packet | The operator has the fader. Ride it from the iPad and keep going |
| `Reaper is already recording` | Second press of the record button | Nothing. It refused on purpose |
| `not armed` | A fader button while standing down | Arm first. The tap was still logged |
| **Start recording** greyed out, Reaper stopped | Expected on this rig. Reaper is never silent, so the box cannot infer the transport is parked and will not send a toggle blind | Roll a recording in Reaper and stop it. See *The record button is greyed out before you touch anything* |

The box can also refuse to start at all, before any of the above. Those are
config problems and it says so on the terminal rather than the page:

| The terminal says | What it means | What to do |
|---|---|---|
| `config      none (flags only)` in the banner | It found no `tacet.toml`. Not an error, but if you expected one you are in the wrong directory | `cd` to the repo, or pass `--config <path>` |
| `WARNING     this log already contains a recording` | The `--log` you gave already holds a `recording-started` -- yesterday's game, or the pre-flight test. Markers derived from it anchor to **that** recording. Entries stamped with Reaper's playhead still land correctly; any made while Reaper was parked fall back to arithmetic from the wrong anchor | Stop and pass a fresh `--log`, unless you genuinely meant to append. Nothing is lost either way; the log is append-only and can be split afterwards |
| `WARNING     log ends in a torn write` (or `queue ends in a torn write`) | The last run stopped in the middle of writing a line: a crash, a kill, a laptop that lost power, or a full disk. Appending straight after that fragment used to corrupt the log so the *next* restart refused to start | Nothing. It is repaired as the box opens the file: a whole entry that only lost its newline is kept, and anything else is moved to the `.torn` file named on the next line, never deleted. Everything before it is intact. If the disk is full, that is the thing to fix |
| `unknown key '...' in [...]` | A misspelled key. It refuses rather than silently using a default and driving the wrong fader | Fix the spelling; the message lists the keys that exist |
| `... must be a whole number` / `must be true or false` | A value of the wrong type, e.g. `dca = "3"` with quotes | Drop the quotes. Numbers and booleans are bare in TOML |
| `--config names ..., which does not exist` | A config was asked for by name and is not there | Check the path. A file merely looked for and absent is fine; one you named is not |
| `--console-host is required or console.host in the config file` | Neither the flag nor the file supplied it | Give it either way |

The top banner is about the iPad's link to the box. `LINK LOST` on the
recording line is about the box's link to Reaper. They are different failures
and can happen separately: the box can be talking to Reaper perfectly while the
iPad cannot see the box.

**The mirror script talks in Reaper's console, not on the page.** Everything it
can say, and whether it needs you:

| The console says | What it means | What to do |
|---|---|---|
| `mirroring <path> from byte N` | Normal. `N` is where it left off last game | Nothing |
| `queue does not exist yet; waiting for the box to create it` | Normal before step 3 | Start the box |
| `no queue path given; not starting` | The path prompt was cancelled or left empty. **The script is not running** and nothing will mirror | Run it again from the action list and enter the path. It is not remembered until it is entered once |
| `mirror stopped` | The script has exited - the action was run a second time, or Reaper closed | Markers stop, the log does not. Re-run it if the game is still going |
| `no remembered position in a queue with history; starting at its end` | Reaper forgot the position - a reinstall, a cleared `reaper-extstate.ini`, a different machine. It refused to read the queue from the beginning, which on a new project would have stamped every marker of every past game onto today's timeline | Nothing. This session mirrors normally from here. If the box was already running, the few events written before the script came up were skipped - they are in the log |
| `queue is shorter than the stored offset; starting from the beginning` | The file was cleared or replaced, so it is reading all of it | Expect markers for whatever is in that file. If it holds an earlier game, those markers are wrong: delete them in Reaper. The log is unaffected |

The last row is the reason for *do not rotate the queue* in step 2. None of
these costs annotation data: the log is written by the box and does not depend
on the script at all.

**A restarted box mid-game cannot start recording**, and says so. It has heard
`/time` but no transport change, so it cannot tell whether Reaper is rolling,
and will not risk stopping a live recording to find out. Start or stop in Reaper
directly; the box will pick the state up from that change.

**The record button is greyed out before you touch anything.** Observed in the
press box, 2026-09-12, with Reaper open and stopped and the box freshly started.

*Do this:* start a recording in Reaper and stop it again. The button enables and
stays enabled for the session. Then tap **Start recording** in the UI for the
real one. Roughly a five second detour, and worth doing during setup rather than
discovering it at kickoff.

*Why:* the box will not send `/record` unless it knows the transport is stopped,
because `/record` is a toggle and sending it blind could stop a recording rather
than start one. It infers "stopped" from silence, since Reaper is normally
silent when parked and streams `/time` only while the transport moves.

On this rig Reaper is **never silent.** Something in its OSC device transmits
continuously while parked, so the link always reads live, and the box has been
told nothing about the record state - so it refuses. `/api/state` shows the
signature: `liveness: "live"` with `position: null` and `known: false`. Waiting
does not help, and neither does Play/Stop: the missing message is `/record`, and
Reaper sends transport state only when it *changes*. Rolling a recording is what
makes it say the word.

*Suspected cause, untested:* an **armed** track. An armed track meters its input
whether or not the transport is moving, and Reaper's OSC feedback carries meter
data. That would produce exactly this - a steady stream that never mentions the
transport. Disarming to test would also disable the recording, so it needs a
quiet afternoon rather than a game day.

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
3. Stop `tacet-serve` with **Ctrl-C twice**. The first press prints what
   stopping does and does not do and waits five seconds; the second one does it.
   Wait longer than that and the next press warns again rather than stopping, so
   a stray Ctrl-C early in a game cannot pair up with an unrelated one later.

   It never fades on the way out - the operator is left in control, and the
   console keeps whatever level it was last commanded. **Stopping the box does
   not stop the recording**; Reaper is still rolling and is stopped in Reaper.
4. Copy to the NAS: the multitrack, the project, and the annotation log. The
   log is small and the one thing that cannot be recreated.

Four streams have to end up on the NAS together (design.md 9): the DVS
multitrack, the post-DCA reference channel, the RTD capture, and the annotation
log.

---

## Not yet proven

Honest state of things, so nothing here reads as more settled than it is.

- **The console under a full cycle.** The write path is proven (2026-09-12:
  addresses, IP, port and DCA all confirmed, arbitrary levels accepted, a 2 s
  fade watched gliding). What has still never happened is the box driving that
  fader through a whole game while an operator works, which is a different
  claim from a bench probe on a quiet afternoon.
- **The console's packet-rate headroom.** The ramp sends 50 levels a second and
  nothing has measured whether any are dropped. The observed fade was smooth on
  StageMix, whose refresh is slower than the ramp, so a fine stagger would not
  have shown. No reason to think there is one.
- **RTD.** Not implemented. Game timing is operator-tapped for now. One thing
  about it is known in advance and will bite on the first attempt: the
  scoreboard console **does not send its state when something connects**. It
  sends only changes, so a reader started mid-game shows blanks until each field
  first moves - no score until someone scores. Pressing `STOP` on the scoreboard
  console forces a full dump. So either the capture starts before the console
  comes up, or someone presses `STOP` once during pregame. See design.md 8.
- **The post-DCA reference channel.** Not captured yet; it is what supplies the
  ground-truth fader labels.

- **The iPad.** The page has been rendered in a desktop browser and looked
  right, but no tablet has run it, so button sizing at arm's length and the
  Auto-Lock advice on iPadOS Safari are both unverified. See handoff.md, which
  also explains why the laptop shows a different wake-lock reading than the iPad
  will.

Reaper, the mirror script and the page itself have all been run against the real
thing.
