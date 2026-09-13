# Game 2

Second home game of 2026, and the first with the operator page. Home game 1 was
2026-09-05. The plan below is tracked as issues in the `Game 3` milestone.

Saturday, September 12
Virginia Tech vs. ODU
Lane Stadium, Blacksburg, VA

Reaper Project Path: "/Users/misnow1/Reaper Media/260912 ODU - Test"
Log File: "/Users/misnow1/games/2026-09-13.jsonl"

## Notes

* I have misplaced my second Ethernet adapter so I was connected to console control via the same hostile wireless network that my iPad was using. As a result, I had to bail on using the app because I would lose connectivity to my laptop or when tacet would fire console commands, it would crush what little network was available. I reverted to using the iPad StageMix app for direct console control. Annotations may not be super useful for this game.
* During the second quarter, I changed the `MV Spare 2` channel to be a copy of what was sent to the scoreboard PA. That includes the house DJ (TMMPO), announcers, video playback, and ref mics (among other things) at the level they were being played through the scoreboard PA.
* I filed several issues in GitHub - some are bugs and some are features/enhancements. I did not take the time to properly label them during the game.
* There may be other bugs or potential enhancements for which I have no filed GitHub issues.

---

# Post-game review

A structured interview held after the game, run against the log, the Reaper
project and the audio. It has two halves: **what the capture actually contains**
(so later analysis does not trust the wrong parts of it), and **what changes
before game 3**. Nothing below has been built yet; this is the agreed plan.

All times are EDT. The log itself is stamped in UTC (EDT + 4 h).

## What was captured

Checked directly, not recalled:

- **Audio is intact.** One continuous take, 10:43:07 to 15:40, 16 tracks: the
  14 `MV` band mics, `DJ TMMPO` (the DJ's direct feed, patched from the snake
  that morning) and `MV Spare 2`. 48 kHz / 24-bit mono WAV, rolled into ~1 GB
  files with contiguous item positions. The two tiny takes at 09:13 and 10:42
  are the pre-flight test and the greyed-out record button workaround.
- **No dropouts.** Near, Conductor, BD, DJ and Spare 2 were scanned end to end
  for digital silence of 100 ms or longer. The band mics have none, so moving
  the laptop under the stands in Q4 cost no audio.
- **`MV Spare 2` was repatched at about 13:06:26.** It is digitally silent for
  17 s from 13:06:09, which is the patch change. Before that it is the spare
  mic; after it, the scoreboard PA copy.
- **No post-DCA reference channel.** Nothing recovers actual fader moves for
  this game, so the log's `commanded` entries are the only fader labels, and
  only where tacet actually drove.
- **Log:** 160 entries. The mirror ran (214 markers in the project).
- **Nothing is on the NAS yet.** 38 GB of project on the laptop, and the log in
  `~/games`.
- The log is named `2026-09-13.jsonl` but holds the 09-12 game. Not a timezone
  effect: it is the example filename from `gameday.md`, copied into `tacet.toml`
  as `capture.log`.
- The Reaper project's sample rate reads 44.1 kHz against 48 kHz media. Harmless
  for tools that read the WAVs directly.

## Control authority

Who was actually moving the band DCA. Only rows marked *tacet* make `commanded`
entries usable as fader labels, and even those are late by an unknown amount.

| Window | Who drove | Notes |
|---|---|---|
| 11:04 - 11:48 pregame | tacet | Clean open/fade pairs |
| 12:10 - 12:37:41 Q1 | tacet, **late** | iPad -> AP -> box -> AP -> console, two wifi hops under a full stadium. Commands landed eventually; the page lost the box after most of them |
| 12:37:41 - 14:15 | StageMix | Nothing in the log |
| 14:15:13 | tacet | Deliberate `up-slow` for the second-half kickoff |
| 14:15 - 15:31 | StageMix | The box believed the DCA was at 0 dB throughout. It was not told otherwise |
| 15:31 - 15:40 late Q4 | tacet | From the laptop under the stands; wifi usable again as the crowd thinned |

Log entries are stamped when the **box received** the tap, not when it was
tapped. Every Q1 timestamp is a delivery time with an unknown lag.

**Panic taps.** Without position feedback, buttons were tapped repeatedly.
Repeats of the same direction are harmless: the state machine ignored them
(e.g. six `out` taps at 12:28:36-43, seq 86-91, one command). Rapid
*alternations* were real commands and may not match intent, e.g. `out` /
`up-slow` / `out` at 12:27:12-21 (seq 74-79).

**Possible full-level blast at 15:31:55.** The `out` there ramped from the box's
belief of 0 dB. If StageMix had the DCA down at that moment, the band PA jumped
to full and faded over 2 s. Nothing captured can confirm or rule it out.

## Corrections

To be written by hand into a sidecar next to the log,
`2026-09-12.corrections.jsonl`. **The raw log is never edited.** Each entry
either supersedes a `seq` with a corrected time or adds a missing event, and
carries a `source` (`post-game review`, later `asr`) and a reason. Times come
from the audio: crowd reaction is the most obvious sign of an event, and the
announcer on `Spare 2` confirms it after the fact.

- **Q2 end (seq 117, 14:10:22)** is late: the operator was away. Real end to be
  found on `Spare 2`.
- **Halftime and halftime exodus** spans are missing entirely, and neither band
  on the field at halftime is marked. Both are the hardest negative case the
  detector has.
- **`band-exits-stands`** tapped twice (seq 115-116); **`band-enters-stands` and
  `band-returns-to-stands`** both tapped for the same return (seq 118-119).
- **`Fed` (seq 108)** means field goal; **`TD` (seq 109)** means touchdown. Both
  were tapped late, after the scoring song had started. They are 93 s apart,
  which is unusual enough that the audio should decide. The same song follows
  both.
- **The fade at 12:37:41 (seq 110)** was a real close, not the handoff. The
  scoring sequence, including any diminuendo under the PAT, sits before it and
  probably overlaps the `up-slow` at 12:34:10. This is the first captured
  example of the case the hold-open constraint exists for.
- **Quarter starts/ends** (other than Q2 end) and `band-exits-stands` are
  accurate.
- The box was armed at 10:42:49, before the band entered, and never stood down
  at halftime.

**Transcription** of `Spare 2` from 13:06 onward, run once locally (not a cloud
API) to find unannotated events. Proposed entries need the operator's approval.
It cannot see pregame or Q1, which predate the repatch. It becomes a repo tool
only if game 3 shows it is worth keeping.

## Filing

1. Copy the project folder and the log to the NAS as they are; verify sizes and
   a checksum of the log.
2. Then rename the NAS copies: `2026-09-12 VT-ODU/`, log `2026-09-12.jsonl`.
   Media paths in the `.RPP` are relative, so the folder can be renamed. **Do not
   touch the Reaper queue file.**
3. Point this document at the NAS copy.
4. Keep the laptop copy until the NAS copy has been opened in Reaper once.

## Before game 3 (Friday 2026-10-02)

### Capture: patch list

Every program feed gets a fixed track name, so offline tools find it by name
rather than position. All present from the moment recording starts.

| Priority | Feed | Source | Notes |
|---|---|---|---|
| Required | Post-DCA band reference | New console bus | Post-DCA, **pre-ducker**. Fix the existing routing loop first, and do not feed the bus from the band PA loop-back input. Proven by pulling the DCA down and watching the track go silent in Reaper. Unity, no extra gain |
| Required | Scoreboard PA mix | Existing (`MV Spare 2`) | From the start, not from Q2. Event timeline and transcription source |
| Required | DJ direct | Existing (`DJ TMMPO`) | Unchanged |
| If patchable | Announcer mic | Channel direct out | Dry speech transcribes far better than the mix |
| If patchable | Video playback | Channel direct out | Needed if program-aware context (#6) goes anywhere |
| If patchable | Ref and MC mics | Axient, already on Dante | A patch away |

### Console

- **Ducker** on the hype PA output's dynamics, keyed from the announcer and ref
  mics. It is post-DCA and downstream of the reference bus, so it neither
  touches the other mixes (faders-only reasoning) nor contaminates the fader
  labels. It also ducks the hype music on that output.
- **Ring-out on site**, stands empty, to set the highest safe preset level
  (see presets below). The quiet section under a PAT is where margin matters.

### Network

- Box wired to the console. More adapters are on order; carry a spare.
- Keep tuning the dedicated control AP. Stray RF in the bowl is severe even a
  few feet from it, so the page's link must be assumed unreliable regardless.
- `reaper.host = "127.0.0.1"`. The wired Dante address worked, but it vanishes
  if that cable comes out, and the laptop was moved mid-game.
- If the iPad link degrades, fall back to the page on the laptop rather than to
  StageMix, so annotation continues.

## Software changes

In rough order of priority. Issue numbers are existing GitHub issues.

### Taps that arrive late

- The page shows an immediate *sending* state on every button, so a tap is
  visibly received and nobody taps again.
- The page stamps the tap time; the box logs both tap time and receipt time.
  The clock offset is estimated from the existing keepalives, so it is
  approximate by design.
- **Stale fader taps are refused**: logged with their true time and a
  `stale, not executed` flag, shown loudly on the page. The threshold is
  generous (seconds) and set from game 3's measured delays. Annotation-only taps
  are never refused.

### Handing off to StageMix

- An explicit **"StageMix has it"** mode. The fader level renders as unknown,
  with the age of the last command, and the handoff is logged, which writes the
  control authority table above as it happens. Annotation buttons keep working.
- The first fader tap afterwards asks **"Take back control. Where is the DCA
  now?" It's up / It's down**, answered in the fader column:
  - *Up*: commanded level becomes the current preset, then the tap runs.
  - *Down*: commanded level becomes -inf; a fade sends a single -inf, a ride-in
    runs in full.
  - Snap opens skip the question: they are correct from any start, so the
    prompt can never cause a missed downbeat.

### Ride-in shape (#8; #7 is a duplicate)

Today `up-slow` steps to -60 dB and rises linearly in dB, spending half its time
below -30 dB where nothing is audible under a crowd. Instead, a tapered curve:
fast through the bottom, slow through the top (for example, reach -20 dB in the
first 15% of the time, then linear in dB), as named constants in the pure
`ramp_steps`. Those numbers are a guess, to be fitted against hand rides
captured on game 3's reference channel. Closes are unchanged.

### Pre-opening on a score

After a touchdown the operator starts riding the fader up as the crowd cheers,
so it is fully up when the band hits. The ball is dead, so an open with no band
is harmless, and it is quietly pulled back if the DJ or the other band plays
instead.

- New **`up-for-score`** fader action: the tapered ride-in over about 4 s,
  configurable next to `fader.slow_open_seconds`.
- **Operator-only now.** Once RTD exists, a home score change **prompts** a
  pre-open. The system never pre-opens by itself: opening without band sound is
  not "is sound present", and only a watching human can tell whether the band,
  the DJ or the other band is about to play.

### Preset levels (#9)

- A set of target levels (e.g. +3 / 0 / -3 / -6 dB) from `fader.presets`,
  capped by a named maximum set from the ring-out; anything above it fails
  config load.
- A standing setting, not a slider. Open buttons go to the current preset.
- Changing preset while open rides to the new level over about 1 s. Logged and
  shown next to the commanded level.
- The preset control is kept away from the fader buttons.

### Layout for one thumb (#5)

The iPad is held in landscape, left hand through the case strap, operated with
the right thumb.

- A **pinned fader column** on the thumb edge, never scrolling: `Up on whistle`,
  `Up on drums`, `Up slow`, `Up for score`, a clear gap, then `Faded out` at the
  bottom where the thumb rests. The commanded level sits at its top. The link
  banner must not push or cover it.
- **Handedness** is a per-device setting remembered by the page, default right.
- **Main panel, no scrolling:** Q1-Q4, halftime, halftime exodus, the six
  timeout types, touchdown, field goal, cannon.
- **Second tab:** band enters/exits stadium and stands, other band on/off field,
  drumline cadence, last two minutes, detector-wrong pair, free-text note,
  presets, arm / stand down / start recording, handedness.

### Vocabulary

- Add `field-goal`, `cannon`, `up-for-score`.
- Retire as buttons (keys stay valid for old logs): `band-returns-to-stands`,
  `band-in-stands`.
- Relabel `touchdown-sequence` to "Touchdown".

### Arm and stand-down prompts

Tapping `band-exits-stands` or `halftime-exodus` raises a persistent
**Stand down?** prompt; `band-enters-stands` raises **Arm?**. One tap to accept
or dismiss, logged either way, and never covering the fader column. RTD becomes
a second source for the same prompts when it lands. Automatic state changes are
out: a mis-tap would disable the fader buttons mid-drive.

### Log path

- Drop `capture.log` from the config file; `tacet-serve` refuses to start
  without `--log` on the command line. It is the one value that must change
  every game.
- `gameday.md` examples become `~/games/<YYYY-MM-DD>.jsonl`.

### DJ and video program (#6)

The band sometimes plays over program deliberately, so a hard interlock would
block real events and is out. Now: display and log only. Goal: context, not
gate, like RTD, with operator taps always going through. First, measure from
game 2's audio how DJ and band actually overlap. `DJ TMMPO` is a clean direct
feed, so its level is trustworthy in a way the band mics' is not.

## Design amendments

To be made in `design.md`:

- **Section 4 hard cases:** the pre-open on a score, and the **cannon**, a
  broadband simultaneous onset across all 14 mics that looks like an ensemble
  entrance.
- **Phase 1 labels:** the training target is *band sound present*, kept
  separate from *fader state*. Pre-opens put the fader up with no band, and
  naive labels would teach the detector that crowd cheering means open.
- Scoring songs are a small repeating repertoire: the same song follows a field
  goal and a touchdown. A candidate template for testing hold-open.

## Research

- **Console readback.** Check Yamaha's documentation for a DM7 protocol with
  get or subscribe, and for MIDI SysEx (design.md section 7). Failing that,
  sniff Mixing Station first (it runs on macOS, so Wireshark on the laptop needs
  no mirror port), then StageMix. Research only; nothing undocumented enters the
  control path this season. Whatever wins is a design.md amendment, since it
  reverses "OSC is write-only".

## Housekeeping

- Label the issues filed during the game (#5-#9), and file issues for the
  software changes above that have none.
- `gameday.md` "Not yet proven" is stale: the page has now run on an iPad, and
  the console has been driven through a real game, over wifi.
