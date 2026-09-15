# The box

Installing and configuring `tacet-serve`, what its startup banner says, the
flags, and how to read the page in detail. The short version, in the order it
happens on the day, is in [gameday.md](gameday.md). What each error means is in
[troubleshooting.md](troubleshooting.md).

---

## Once per machine

```
git clone git@github.com:misnow1/tacet-downbeat.git
cd tacet-downbeat
make install
make check          # should be green before a game, not on the day

cp tacet.toml.example tacet.toml
$EDITOR tacet.toml  # console IP, DCA, queue path -- the site values in gameday.md
```

---

## tacet.toml

Once the site values are known, put them in `tacet.toml` on the box rather than
retyping them into every command. Copy `tacet.toml.example`, fill it in, and the
startup command shrinks to the part that actually changes between games. The
file is gitignored; the table in gameday.md stays the written record.

Every value in it still has a flag, and the flag wins:

```
built-in default  <  tacet.toml  <  the flag you type
```

so nothing stops working if the file is absent, wrong, or deliberately
overridden mid-game. Each tool says which config it read on startup -- `tacet-serve`
in the `config` row of its banner, the two `verify_` tools on a line of their
own. If that reads `none (flags only)` and you expected a file, you are in the
wrong directory.

---

## The console

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

## Starting it

Whatever queue path the mirror script is watching must be the one the box
writes. This is the single most common way to have everything look healthy and
mirror nothing.

With `tacet.toml` filled in, the only thing left to type is the game, and it
has to be typed: `--log` is never read from the file, and the box will not start
without it. Use the date of the game:

```
tacet-serve --log ~/games/<YYYY-MM-DD>.jsonl
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
  log         /Users/you/games/<YYYY-MM-DD>.jsonl
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

**Read the `queue` line against what the mirror script said.** They have to be
the same path, and the banner repeats it under step 2 of its own checklist so
the two can be compared without scrolling.

Everything in the banner is what the box was *told*, not what it has confirmed.
Nothing has been sent to the console at this point, and the console could not
answer if it had been.

If the `config` row says `none (flags only)`, it did not find a file and
everything above came from flags and defaults. A `WARNING` row is explained in
[troubleshooting.md](troubleshooting.md#the-terminal).

Without a config file, or to override it, the whole thing is still flags:

```
tacet-serve \
    --console-host <console IP> \
    --dca <n> \
    --log ~/games/<YYYY-MM-DD>.jsonl \
    --queue "$HOME/Library/Application Support/REAPER/tacet/queue.tsv" \
    --reaper-host 127.0.0.1 \
    --listen 0.0.0.0 \
    --http-port 8080
```

- `--log` is **per game**, and per *recording*. It is the irreplaceable artefact.
  It is a flag and only a flag: there is no `capture.log` key, because a value
  that changes every game does not belong in a file that does not. Game 2's log
  is named for the day after the game for that reason -- the example date,
  copied into the config. If the log already holds a recording, or spans left
  open by an earlier run, the banner says so in a `WARNING` row.
- `--queue` is **stable**, and must match the mirror script. Good candidate for
  the file.
- `--listen 0.0.0.0` or the iPad cannot reach the page. `127.0.0.1` serves the
  machine and nothing else, which looks exactly like a firewall problem.
- Leave `--quantized` off: this console does not round (2026-09-12). `--no-quantized`
  turns it back off when the file sets it and you want it gone for one run.

---

## Reading the page

The page cannot keep the iPad awake itself, which is why gameday.md has you turn
Auto-Lock off: the browser API for holding the screen on needs HTTPS, and the
page is plain HTTP.

`no feedback` on the recording line before recording is correct, not a fault.
Reaper says nothing at all while its transport is parked, and announces the
record state only when it changes, so the box has genuinely not been told
anything yet.

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

**The fader line is what the box expects, never what the console reports.** It
is tagged **commanded** for that reason and the tag never changes: the DM7's OSC
is write-only, so nothing here is ever a readback, and an iPad move made in
StageMix will not appear. During a close the line reads

```
-3.00 dB → -∞ dB
```

— the left figure sweeping as the ramp goes out, the right one where it is
headed. `Up slow` does the same thing upward, `-∞ dB → 0.00 dB`. When the fader
is settled there is no arrow, because there is nowhere else to be. To check the
expectation against reality, look at the DCA in StageMix; that comparison is the
only thing that can catch a wrong DCA number or a console that is not listening.

`Up slow` rides in over **1.5 seconds**, tapered the way a hand rides it: fast
through the bottom of the travel, which is inaudible under a crowd, reaching
-20 dB in the first 15% of the ride, then slowly through the top where the band
can be heard. Retune the length with `fader.slow_open_seconds` or
`--slow-open`; the other open buttons are unaffected. The shape itself is a
guess until it is fitted against the hand rides on game 3's post-DCA reference
channel, so it is not a config key.

---

## Stopping it

Stop `tacet-serve` with **Ctrl-C twice**. The first press prints what stopping
does and does not do and waits five seconds; the second one does it. Wait longer
than that and the next press warns again rather than stopping, so a stray Ctrl-C
early in a game cannot pair up with an unrelated one later.

It never fades on the way out - the operator is left in control, and the console
keeps whatever level it was last commanded. **Stopping the box does not stop the
recording**; Reaper is still rolling and is stopped in Reaper.

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
  ground-truth fader labels. It is on the patch list in
  [reaper.md](reaper.md#tracks-to-record).

- **The iPad.** The page has been rendered in a desktop browser and looked
  right, but no tablet has run it, so button sizing at arm's length and the
  Auto-Lock advice on iPadOS Safari are both unverified. See handoff.md, which
  also explains why the laptop shows a different wake-lock reading than the iPad
  will.

Reaper, the mirror script and the page itself have all been run against the real
thing.
