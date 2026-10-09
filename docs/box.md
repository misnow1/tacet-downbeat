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

Before each game, update it: [gameday.md](gameday.md#the-box-update-before-the-game).

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

### Room for the game

The box refuses to start without room for a whole game's recording on the volume
Reaper records to. It cannot ask Reaper where that is, so tell it, with how many
tracks this game records:

```
[capture]
audio_path = "/Users/you/Reaper Audio"
channels = 30
game_hours = 6.0
```

`game_hours` is the length of the whole recording, not the on-field game -
match it to whichever start time
[reaper.md](reaper.md#when-to-start-the-recording) recommends, not to kickoff,
or the check passes a disk that runs out before the recording does.

The size is `channels x 144 kB/s x game_hours`, plus 10%: 30 channels over 6
hours needs about 103 GB. The banner's `disk` row shows free space against that.
It also refuses if `audio_path` is not there (an unmounted share), or not set,
or if the disk holding the log and queue is nearly full. Each refusal names
`--no-disk-check`, a flag only, which starts the box anyway and says so in the
banner. Update `channels` when the patch list changes.

On Linux, an empty mountpoint directory passes as "there" and reports the local
disk underneath. Check the share is mounted before trusting the number.

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
  code        main @ 3a5613d
  config      /Users/you/tacet.toml
--------------------------------------------------------------------------
  console     10.0.0.5:49900   DCA 3
              commanded, never confirmed - the DM7's OSC is write-only
  ping        answered ping (0.4 ms)
              something is at this address; not proof it is the DM7
              and not proof the port is right
  fade        2.0s close, fast open, 1.5s ride-in
  target      0.0 dB   presets 0.0 / -3.0 / -6.0   cap 0.0 dB
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
Nothing has been sent to the console's OSC port at this point, and the console
could not answer if it had been. One ping has gone to its *address* (#73), and
the `ping` row says what came back. That is presence at the address, never the
DM7, the port or a delivered fader move:

| `ping` row | What it means |
|---|---|
| `answered ping (0.4 ms)` | Something at that address replied. Not proof it is the DM7 or that the port is right |
| `did not answer ping` | No reply, but the box's ARP entry for the address is resolved or could not be read. The console may ignore ping, or be gone. Fader moves are still sent |
| `nothing at this address (no ARP reply)` | No reply, and nothing answered ARP either: wrong address, wrong adapter or VLAN, cable out, console off. Also prints a `WARNING` row; the box starts anyway |
| `could not check: <why>` | ping is missing, did not finish, or the platform is unknown. Nothing is known |

The ping is `ping -c 1` from the system, with no privilege and no OSC, and a
read of the neighbour (ARP) table when it gets no reply. The box repeats it at
every arm and every 4 minutes for as long as it runs (every 30 seconds while the last result was `nothing at this address` or `could not check`), which also keeps the
laptop's ARP entry for the console warm; see design.md 5.7.

The `code` row is the one thing the box *did* check: it asked git, once, before
anything binds, read-only, with a 5 second limit (#157). It reads
`<branch> @ <commit>` and adds `(worktree)` when the box runs from a linked
worktree, `detached HEAD @ <commit>` on a detached head, and `, uncommitted
changes` when the tree is dirty, which also prints a `WARNING`. Untracked files
count as dirty only under `src/`; a scratch file elsewhere is logged and does
not warn. `not a git checkout` is an installed copy, and `unknown - <reason>`
means git could not be asked, which also prints a `WARNING`. The same facts are
the first entry of every run, `box-started`, and become a `SYS|box-started`
marker in Reaper, so a restart mid-game shows on the timeline.

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

### Checking without starting

Add `--check` to the exact command you will type on the day:

```
tacet-serve --log ~/games/<YYYY-MM-DD>.jsonl --check
```

It prints what a real start would print and exits instead of binding: the
banner and exit code 0 if the box would start, or the same refusal and the same
non-zero code it would stop with. `WARNING` rows are not refusals, so they still
exit 0. Nothing is bound, nothing is sent to the console's OSC port or to
Reaper, the log and queue are neither created nor opened, and a torn end is
reported but not repaired. It does send one ping to the console's address, so
away from the stadium its `ping` row reads `nothing at this address (no ARP
reply)` with a `WARNING`, which is expected there. Do it the night before, and
after any edit to `tacet.toml`.

`--check` is a flag only. A `check` key in the file is refused like any other
unknown key, because a file that set it would stop the box from ever starting.

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

It says nothing about whether the console took a fader move. Nothing can - OSC
is write-only, and a packet sent into a black hole succeeds (design.md 5.3).
`Console unreachable` appears only when the box's own send fails.

**The console chip, in the strip** says what the last ping at the console's
address found (#73), and shows only when something is wrong; no chip is the
healthy state. A red `Nothing answered at the console address at the last check` means nothing answered
ARP either: fader moves may not be reaching anything, so check the cable and
the console IP. The operator has the fader. A dim `Console did not answer ping`
means no reply but the address may still be the console, which may ignore ping;
fader moves are still sent, unconfirmed. `Could not check the console` carries
its reason. A tap reads the whole sentence, and the chip clears itself on the
next answer, so a fixed cable clears it within 30 seconds.

**An amber `Unreviewed code running` chip in the strip** means the box is running
uncommitted changes, so the log's commit does not describe what ran (#157). It
shows only while that is true, never changes during a run, and a tap reads the
whole sentence. `Running code not identified` is the quieter version: git could
not be asked, so the tree may be dirty. A clean checkout shows nothing.

**The duty chip (`ARMED 10:42` / `STOOD DOWN 12:51`), first in the strip, is
on the box's own monotonic clock** (#19), the same one `at` and every log
timestamp are on - the page converts it to local time with the same round-trip
estimate a tap's clock offset uses, so it can be a few seconds off before that
estimate has settled and is otherwise as accurate as the box's own clock is.
It carries no time at all, just the word, after a restart: the box's duty
clock starts at that restart and genuinely has no history before it, and
inventing a time - the boot time, say - would be exactly the confident-but-
wrong number CLAUDE.md's fail-visible principle forbids.

**The fader line is what the box expects, never what the console reports.** It
is tagged **commanded** for that reason: the DM7's OSC is write-only, so
nothing here is ever a readback, and an iPad move made in StageMix will not
appear. During a close the line reads

```
-3.00 dB → -∞ dB
```

— the left figure sweeping as the ramp goes out, the right one where it is
headed. The box describes the move once (`fader.move` in the snapshot: from, to,
duration, when it started, and the curve) and the page animates it, so a fade
costs the page three messages however many steps the ramp takes. While a move is
in flight the tag is **fading** (**riding** for `Up slow` and Ready), not
**commanded**, and the button that started it is painted magenta. That clears
when the box says the move has landed, never on the page's own timer: a page
that has not heard reads `→ -∞ dB - 3s late` and stays magenta. `Up slow` does
the same thing upward, `-∞ dB → 0.00 dB`. When the fader is settled there is no
arrow, because there is nowhere else to be. A page left open across a box
upgrade should be reloaded. To check the
expectation against reality, look at the DCA in StageMix; that comparison is the
only thing that can catch a wrong DCA number or a console that is not listening.

`Up slow` rides in over **1.5 seconds**, tapered the way a hand rides it: fast
through the bottom of the travel, which is inaudible under a crowd, reaching
-20 dB in the first 15% of the ride, then slowly through the top where the band
can be heard. Retune the length with `fader.slow_open_seconds` or
`--slow-open`; the other open buttons are unaffected. The shape itself is a
guess until it is fitted against the hand rides on game 3's post-DCA reference
channel, so it is not a config key.

The **target** is where every open goes, and the operator picks it from a short
list on the page (MORE > Target level). `fader.presets` is that list, in dB and
in the order the page shows it, and **the default is `fader.default_target_db`
when the site names one, and the first entry otherwise** (#139): the banner's
`target` row says the default first, and the page's readout goes amber
whenever the target is anything else. The default must be one of the presets
- **naming one that is not refuses to start**, from the file and from
`--default-target` alike, the same way a preset above the cap does.
`fader.max_target_db` is a cap on every preset, and **a preset above the cap
refuses to start**, from the file and from `--presets` / `--max-target` alike,
naming the value. The cap ships at 0 dB, so `+3` cannot exist by accident:
raise it only after an on-site ring-out with the stands empty, since +3 dB
costs 3 dB of feedback margin against a band PA that sits just behind the
mics. A site whose ring-out raised the cap to +3 dB writes
`presets = [3.0, 0.0, -3.0, -6.0]`, `max_target_db = 3.0` and
`default_target_db = 0.0`: the page reads loudest first, but the box still
boots to unity, and +3 is one tap away for a very loud crowd. Changing the
target on the page always stores the value, and while the fader is up rides it
there over `fader.retarget_ride_seconds` (default 1.0, `--retarget-ride`): the
open level to the new target, or READY's hold to the new hold. Closed or
fading, it stores and sends nothing; the next open uses it. The READY hold
level follows it (`fader.hold_below_db` below it). With
flags, a value that starts with a minus needs the equals form for `--presets`
(`--presets=-3,-6`), but not for `--default-target`: a bare negative number is
not mistaken for another flag, the way `--max-target -2.0` already is not.

---

## Stopping it

Stop `tacet-serve` with **Ctrl-C twice**. The first press prints what stopping
does and does not do and waits five seconds; the second one does it. Wait longer
than that and the next press warns again rather than stopping, so a stray Ctrl-C
early in a game cannot pair up with an unrelated one later.

It starts no fader move on the way out (#44), and the operator is left in
control. A fade already under way is let land, for no longer than the fade itself
plus half a second; a ride (up-slow, Ready, a target change) is stopped where
it is and logged `move-abandoned`. After that the console keeps whatever level it
was last commanded. The terminal says which, in its last `fader:` line, since
the page is gone by then.

- A Ctrl-C while a fade is landing leaves it where it is, also logged
  `move-abandoned`.
- SIGTERM (`kill`, a logout, a service manager) stops at once, the same way, with
  no confirmation.
- The next start always reads the level `unknown` (#107), so **Close now** or an
  open is the first fader tap after any restart.
- A crash or `kill -9` still parks a move part-way. The damage is contained to
  the band PA, and the next start reads the level unknown.

**Stopping the box does not stop the recording**; Reaper is still rolling and is
stopped in Reaper.

---

## After the game: the pilot check

`tacet-pilot-check` lays the box's log beside the `Pilot Reference` recording
(the DCA reference channel, a tone the console scales with the fader) and flags
any **ramp that started from a belief the pilot contradicts**: a fade that
jumped the fader up at its first moment (the Game 3 blast, seq 33), a rise that
dropped, or a belief the pilot simply disagrees with by more than 1.5 dB. It
checks ramps only - `fade`, `ready`, `up-slow` and retarget rides - because a
snap open and a close-now do not ramp from belief. It never touches the box or
the console.

```
tacet-pilot-check GAME.jsonl "Reaper Media/<game>/Media/<Pilot Reference>.wav"
```

A log with no recording anchor needs the wall time the recording started, with
a timezone. Game 3's log has none (#180), so:

```
tacet-pilot-check 20261002-pitt.jsonl "26-Pilot Reference-261002_1750.wav" \
    --take-start 2026-10-02T21:50:42.45Z
```

`--take-start` only selects the entries written during the take; each entry's
own `project_seconds` places it on the audio. It is refused if the log already
has an anchor, and a naive time (no `Z` or offset) is rejected. The WAV is
placed on the project timeline by its BWF time reference, so a file without a
`bext` chunk is refused.

**Reading it.** The header gives the calibration (`pilot = DCA -12.0 dB`,
measured on the take's own snap opens from silence, with the lowest and highest
so a step over the game shows) and how many ramps of each kind there were. Each
flagged or unchecked ramp gets a row: sequence number, local and UTC time,
project seconds, what the box believed, the pilot's median just before it (in
DCA dB, `-inf` for closed), the peak (falling ramps) or minimum (rising) just
after, and why. A take that ended early (a second recording in the log) is named in the header,
and the ramps after it are listed as unchecked. The last line is the verdict. "Unchecked" means the pilot does
not cover the ramp or the entry has no project position; that is a gap, not a
pass.

**Exit codes.** `0` every ramp checked, none flagged. `1` something flagged or
unchecked. `2` it could not run: no anchor (or two), unreadable log or WAV, no
snap open to calibrate from, numpy missing, bad arguments.

**Install.** It needs numpy, an optional extra kept off the box's control path:
`make install` includes it; with a plain `pip install -e .` run
`pip install -e '.[analysis]'`. A box installed before this change has no
`tacet-pilot-check` command until `make install` is run once more after pulling
([gameday.md](gameday.md#the-box-update-before-the-game));
`python -m tacet.pilot_check` is the same tool run as a module.

**Known gaps.** The Game 3 pilot is not a clean sine (a peak of -9.15 dBFS
against a -12 dBFS sine-equivalent level, with 1-2 dB spikes about every 1.3 s
in the 10 ms envelope). That is fine for a 1.5 dB tolerance and not for a 0.6 dB
measurement. A game split across several WAV files is not supported yet. With
`--take-start`, a second recording anchor in the log ends the take, which is
conservative.

---

## Not yet proven

Honest state of things, so nothing here reads as more settled than it is.

- **The console under a full cycle, wired.** The write path is proven
  (2026-09-12: addresses, IP, port and DCA all confirmed, arbitrary levels
  accepted, a 2 s fade watched gliding), and the box has since driven that
  fader through a whole game while an operator worked it (game 2) - but over
  wifi, both hops, because the wired adapter for the control VLAN went missing
  on the day (game-2.md). Driving it over the wired control path, under a full
  cycle, has not happened yet, which is a different claim from a bench probe or
  a game over wifi.
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
- **The DCA reference channel.** Not captured yet; it is what supplies the
  ground-truth fader labels. Planned for game 3 as a pilot tone, not the band
  PA - see [reaper.md](reaper.md#the-dca-reference-a-pilot-tone).

- **The iPad.** The page ran on an iPad through game 2 (game-2.md), so the
  device itself is proven. The layout has moved on since: the pinned fader
  column, the readout stack (the level, the console fault, the refusal or the
  "Greyed: they ramp from an unknown level" note, and the belief row of Close
  now / It's at ready level, all in the 96px readout gap) and the height of the
  fader column (760px, sized for a 768px-tall landscape iPad) have not been
  eyeballed on the device since #5's layout landed. Do that before game 3.

Reaper, the mirror script and the page itself have all been run against the real
thing.
