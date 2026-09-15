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

Agreed after game 2 (see [game-2.md](game-2.md), "Capture: patch list", and
#13). Every feed gets a **fixed track name**, so offline tools find it by name
rather than position, and **every track is present from the moment recording
starts** - a feed patched in during Q2, as the scoreboard PA was at game 2,
leaves pregame and Q1 with nothing to compare against.

| Priority | Track | Source | Notes |
|---|---|---|---|
| Required | The 14 band mics (`MV ...`) | As at game 2 | Unchanged |
| Required | Post-DCA band reference | New console bus | Post-DCA, **pre-ducker**. Fix the existing routing loop first, and do not feed the bus from the band PA loop-back input. Unity, no extra gain. **Prove it before the gates open:** pull the DCA down and watch the track go silent in Reaper. This is the ground truth for every fader move; OSC cannot supply it |
| Required | Scoreboard PA mix | Existing (`MV Spare 2`) | From the start, not from Q2. Event timeline and transcription source |
| Required | DJ direct | Existing (`DJ TMMPO`) | Unchanged |
| If patchable | Announcer mic | Channel direct out | Dry speech transcribes far better than the mix |
| If patchable | Video playback | Channel direct out | Needed if program-aware context (#6) goes anywhere |
| If patchable | Ref and MC mics | Axient, already on Dante | A patch away |

That is **17 tracks required**, and about **22** with everything patchable
patched. Uncompressed 48 kHz / 24-bit mono is 144 kB/s a track: **8.8 GB an
hour at 17 tracks, 11.4 GB at 22.** Game 2's 16 tracks came to 39.7 GB, which
matches the arithmetic.

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
vocabulary at all. A halftime is 20-30 minutes, about 3-6 GB at the track counts
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

| Start at | Runs for | 17 tracks | 22 tracks |
|---|---|---|---|
| Doors, T-150 | ~6 h | ~53 GB | ~68 GB |
| Band enters stadium, T-60 | ~4.5 h | ~40 GB | ~51 GB |

The difference between them is 13-17 GB, which is not a reason to do anything.
Two things are:

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
