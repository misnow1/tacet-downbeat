# Reaper

Everything about the recorder: setting Reaper up, which tracks to record, the
mirror script and its queue, and the rules for the recording itself. The short
version, in the order it happens on the day, is in [gameday.md](gameday.md).

---

## Once per machine

1. DVS as the audio device, at 48 kHz, with every track in the patch list below
   mapped.
2. Preferences > Control/OSC/web > add an OSC device. **Listen port 8000**,
   **device port 9000**, feedback enabled. These are the defaults the box
   assumes; anything else needs `--reaper-port` / `--reaper-feedback-port`, or
   `reaper.send_port` / `reaper.receive_port` in `tacet.toml` so it survives
   into next game.
3. Copy `reaper/tacet_mirror.lua` into `REAPER/Scripts`.

---

## Tracks to record

Agreed for game 3 (see #13; game 2's list is in [game-2.md](game-2.md),
"Capture: patch list") and corrected at the 2026-09-30 media runthrough
(#145). The source of truth is the **"MV Recording Routing"** sheet in Drive,
whose names match the Reaper template exactly. The stadium's DM7 I/O master
sheet is out of date: where the two disagree, the routing sheet wins. Every
feed gets a **fixed track name**, so offline tools find it by name rather than
position, and **every track is present from the moment recording starts** - a
feed patched in mid-game leaves everything before it with nothing to compare
against.

| Reaper / DVS rx | Track | Source | Notes |
|---|---|---|---|
| 1 | MV Conductor | Rio ch 1, Dante split (pre-delay) | Console ch 37 |
| 2 | MV Spare | Rio ch 2, Dante split (pre-delay) | Console ch 38 |
| 3 | MV Snare | Rio ch 3, Dante split (pre-delay) | Console ch 39 |
| 4 | MV BD | Rio ch 4, Dante split (pre-delay) | Console ch 40 |
| 5 | MV Sax 1 | Rio ch 5, Dante split (pre-delay) | Console ch 41 |
| 6 | MV Sax 2 | Rio ch 6, Dante split (pre-delay) | Console ch 42 |
| 7 | MV Clarinets 1 | Rio ch 7, Dante split (pre-delay) | Console ch 43 |
| 8 | MV Clarinets 2 | Rio ch 8, Dante split (pre-delay) | Console ch 44 |
| 9 | MV Horns 1 | Rio ch 9, Dante split (pre-delay) | Console ch 45 |
| 10 | MV Horns 2 | Rio ch 10, Dante split (pre-delay) | Console ch 46 |
| 11 | MV Horns 3 | Rio ch 11, Dante split (pre-delay) | Console ch 47 |
| 12 | MV Horns 4 | Rio ch 12, Dante split (pre-delay) | Console ch 48 |
| 13 | MV Near | Rio ch 13, Dante split (pre-delay) | Console ch 49 |
| 14 | MV Far | Rio ch 14, Dante split (pre-delay) | Console ch 50 |
| 15 | TMMPO L Split | Rio ch 15, Dante split (pre-delay) | Console ch 51 |
| 16 | TMMPO R Split | Rio ch 16, Dante split (pre-delay) | Console ch 52 |
| 17 | Announcer Mic Dry | Console ch 1 direct out | Dante out 65. Dry speech transcribes far better than the mix |
| 18 | Laptop | Console ch 2 direct out | Dante out 74 |
| 19 | Ref Primary | Dante split (Axient) | |
| 20 | Ref BU | Dante split (Axient) | |
| 21 | HH Red | Dante split (Axient) | |
| 22 | HH Blue | Dante split (Axient) | |
| 23 | DJ TMMPO Direct Out | Console ch 7 direct out | Dante out 75. From the channel's own direct out, not the split, because the DJ's input can move |
| 24 | HokieVision | Console ch 11 direct out | Dante out 76. New at the 2026-09-30 runthrough |
| 25 | TruckFX | Console ch 15 direct out | Dante out 77. Same reason as track 23: it can move |
| 26 | Pilot Reference | Console ch 53 direct out, **Post Fader** | Dante out 78. Source is the Pilot track's tone (track 31), injected on DVS send 1. See "The DCA reference: a pilot tone" below |
| 27 | Hype PA Output | Console Matrix 8 out | Dante out 56. Band Group plus the other Hype PA group, post-ducker: what the Hype PA actually plays |
| 28 | Main PA Output | Console matrix out | Dante out 50 |
| 29 | Band Group | Console Band Group out, pre-ducker | Dante out 17. Context only, not a DCA reference; see below |
| 30 | PJ Playback | Dante split | New at the 2026-09-30 runthrough |
| 31 | Pilot | Reaper's tone generator, out on DVS send 1 | **Not armed; records nothing.** A Reaper track with no DVS receive: the source for track 26. See below |

That is **30 recorded tracks**, plus the unarmed Pilot. Uncompressed 48 kHz /
24-bit mono is 144 kB/s a track: **about 15.6 GB an hour at 30 tracks.**
Recording from doors, as below (~6 h), plus the box's 10% disk-check margin
([box.md](box.md#room-for-the-game)), is about 103 GB - which is why
`game_hours = 6.0` there, not the on-field game length.

Ref Primary, Ref BU and HH Red were digital silence for the whole 2026-09-30
runthrough, while HH Blue carried signal - probably transmitters off on a
media day, but in a recording a dead feed and a quiet one look the same.
Talk-check all four before kickoff.

### The DCA reference: a pilot tone

Track 26 is the ground truth for every fader move (design.md 9); OSC cannot
supply it, because it is write-only. Game 2 had no reference at all.

It is **not** the band PA (track 29, now context only): every band channel but
the DJ carries an expander or a 5045 in its first dynamics slot, so the PA
level follows the dynamics as well as the DCA, and the DCA's own contribution
cannot be recovered reliably from it. Instead the reference is a steady pilot
tone that only the DCA (and nothing downstream of it) can touch:

1. **Reaper**: Cockos's stock JS **Tone Generator** (under Synthesis) on its
   own track, `Pilot` (track 31), **not armed**. Master send **off** - it must
   never reach the PA. The 2026-09-30 template had it on, harmless only
   because the master has no hardware outputs; turn it off anyway. Hardware
   output to DVS send channel 1.
2. **Dante Controller**: route DVS send 1 to console input channel 53.
3. **Console channel 53**: assigned to the band DCA and nothing else.
   Dynamics and EQ bypassed. No stereo, mix or matrix sends - the DM7 defaults
   new channels **TO STEREO on**; turn it off. Fader fixed at 0 dB. Name it
   something like `DCA REF`.
4. **Direct out**: set to **Post Fader**, console Dante out 78, patched to DVS
   receive 26 (track 26 above). Per the DM7 block diagram the Post Fader tap
   sits after LEVEL/DCA and before ON, so it follows the fader and the DCA but
   not mute - consistent with faders-only (design.md 5.3).

The DM7's internal oscillator was considered and ruled out: it is one global
oscillator, so the console operator could never use or switch it off without
killing the reference.

Suggested tone: around 1 kHz at about -20 dBFS (the DCA's +10 dB max peaks
near -10 dBFS).

**Verified at the 2026-09-30 runthrough**, end to end: the generator ran at
1 kHz, -12 dB, and track 26 carried it at about -14.4 dBFS with the DCA open,
digital silence (-inf) with it closed, and the DCA's moves in between.

**Checks, before the gates open:**

- The tone generator keeps running with Reaper's transport stopped. If it does
  not, arm the track with input monitoring on, which keeps it processing while
  stopped - and then records it too, so add one to `capture.channels`.
- **Proof test**: pull the DCA down and watch track 26 go silent in Reaper.
  Then confirm the tone shows on no other console output meter.

A box-generated tone with measured readback, so the box can confirm its own
commands instead of inferring them from a recording, is future work (#137).

### The files Reaper writes

Reaper splits every track's recording at about 1 GiB, which at 144 kB/s is
about 2 h 04 min - so a ~6 h game is three files a track. Within a track the
pieces are contiguous. Across tracks they are not split at the same instant:
at the 2026-09-30 runthrough the split point differed by one 512-sample
buffer (about 10.7 ms) between tracks, which is the scale of the onset timing
the detector compares across channels. So offline tools must place every file
by its item **position** in the `.RPP`, never by concatenating in file order
or assuming the Nth file of every track starts together.

File names carry the record date and time (`YYMMDD_HHMM`), not the project's
name, so a project named for one day can hold files stamped with another.

---

## The mirror script

Actions > Show action list > New action > Load ReaScript > `tacet_mirror.lua`,
then run it. It asks once for the queue path and remembers it in `ExtState`.

The console should say:

```
[tacet] mirroring <path> from byte N
```

`N` is where the script left off last game, remembered in Reaper's `ExtState`.
On a queue that has never been used it is `0`. Anything else the console says
instead is in [troubleshooting.md](troubleshooting.md#the-mirror-console), with
what each line needs from you.

**Do:**

- Use **one stable queue path**, every game, indefinitely.
- **Leave the file alone between games.** It is append-only and the script
  remembers its position, so a new Reaper project each game resumes exactly
  where the last one stopped and places nothing from an earlier game.
- Match the box's `--queue` to the path entered here. Better, set it once as
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

---

## One recording, one log, started early

The rule the rest of the runbook assumes: **one continuous recording per game,
in one Reaper project, with its own `--log`, started before the crowd arrives
and not stopped until the band is out.**

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

The stamp is missing only when Reaper has not sent its position in the last
two seconds -- it stops whenever the transport is parked, even on this rig,
where meter feedback keeps flowing regardless -- and those entries fall back to
the arithmetic, which is the case the warning is really about.

The same goes for halftime. The stand mics then carry your band on the field,
the visiting band on the field, and cadences on the way out and back -- music
present while the fader must stay **closed**, which is the single hardest case
the detector has to learn and the reason `other-band-on-field` is in the
vocabulary at all. A halftime is 20-30 minutes, about 5-8 GB at the track count
above. If space ever genuinely bites, trim the `halftime` span afterwards; it is
already delimited in the log.

If you want a genuine throwaway test first, make it a throwaway in both places:

```
tacet-serve --log /tmp/preflight.jsonl      # scratch project, scratch log
```

then stop in Reaper, close that project, Ctrl-C the box twice, and start again
against the game project and the game log. Run `tacet_mirror.lua` in the new
project **before** the box, or the queue lines written before it comes up are
never mirrored. The script keeps its byte offset in `ExtState`, so a new project
will not re-stamp the test's markers.

**Start it from the page, not in Reaper.** `recording-started` is written only
by the page's Start recording button, and it is the anchor the whole log is
measured from. Start the recording in Reaper instead and the log has no anchor,
so markers cannot be derived from it afterwards at all.

**Record before annotating.** Reaper has no negative timeline, so anything
logged before recording starts has nowhere to go on it. Those entries are kept
in the log and reported as `skipped_before_anchor` rather than dropped, but they
will not appear as markers.

---

## When to start the recording

**Before the band enters the stadium**, which in practice means as soon as
Reaper and the box are up.

The stadium clock runs a countdown to kickoff from around the time doors open,
150 minutes out. It ends at the team entrance, resets, and restarts at the top
of the first quarter. The band does not come in until roughly 60 minutes before
kickoff, so most of that countdown is a set of empty stands.

Record it anyway.

| Start at | Runs for | 30 tracks |
|---|---|---|
| Doors, T-150 | ~6 h | ~93 GB |
| Band enters stadium, T-60 | ~4.5 h | ~70 GB |

The difference between them is about 23 GB, which is not a reason to do
anything. Two things are:

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

## The record button is greyed out before you touch anything

Observed in the press box, 2026-09-12, with Reaper open and stopped and the box
freshly started.

*Do this:* start a recording in Reaper and stop it again. The button enables and
stays enabled for the session. Then tap **Start recording** in the UI for the
real one. Roughly a five second detour, and worth doing during setup rather than
discovering it at kickoff. The throwaway take at the top of the project can be
deleted later.

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
quiet afternoon rather than a game day. Do not skip arming to avoid the detour:
an unarmed track records nothing, which is the one failure with no recovery at
all.
