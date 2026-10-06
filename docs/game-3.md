# Game 3

The third home game and the fifth overall game of the season.

Friday, October 2, 2026
Virginia Tech vs Pitt
Lane Stadium, Blacksburg, VA

Reaper Project Path: "/Users/misnow1/Reaper Media/20261002 Pitt"
Log File: "/Users/misnow1/games/20261002-pitt.jsonl"

## Notes

* The control surface was *much* more stable with the computer on two wired networks
* We made an emergency change to reduce the number of updates sent over the wire
* The control network can be rather noisy - if the DM7C is sending multicast metering data, that constitutes a flood of frames at ~33Hz with a data rate of a lot. That said, I attempted to sniff for the metering data during the game and I didn't see any. I may have been on a switch port that was filtering multicast data. The question of reliably getting metering and/or fader position data is still open.
* At the top of the game, I ended up using the DM7 app to get the band balance dialed in and fix the dynamics on some of the band channels (for example, the compression on the front channels was a little aggressive which lead to ringing after the band played).
* Game timing data is, for the most part, wrong. I'm very forgetful.
* Band triggers should be correct for the most part. Since I could see the conductor, I could see when they were pointing at the drum major vs whistling for the entire band. That said, I still missed some cues and there are cases where I anticipated one thing but something different happened. Those should be visible as rapid pushes for different things like an "up on whistle" push followed quicky by an "up on drums" push.

During the game, I noted the following:

* It would be nice to have an option to assign and unassign items to/from the Hype PA ducker bus. Before the game, I had the Ref, announcer, playback laptop, and HokieVision assigned to the Ducker's sidechain. However during the actual game, the playback operator was super obnoxious and liked to stomp on the band. It would be useful to have an interface page where the sources for the sidechain could be toggled. This should be doable with OSC commands to turn that channel's send to the sidechain group on and off.
* Buttons that trigger fades, should change to a color while fading and then back when the fade completes.
* It would be nice to be able to swipe between tabs instead of having to tap the tab title.
* Icons or colours for important buttons so they're easier to differentiate. The sea of grey buttons is hard to navigate quickly in-game.
* On the main tab, scoring buttons should be above the game buttons.
* The fader updates don't need to be nearly as fast as they are during fades.

---

# Post-game review

A structured interview held on 2026-10-04, run against the log, the Reaper
project and the audio, in the same shape as [game 2's](game-2.md). It has two
halves: **what the capture actually contains**, so later analysis does not
trust the wrong parts of it, and **what changes before game 4**. Each change is
an issue in the `Game 4` milestone unless it says otherwise.

All times are EDT. The log itself is stamped in UTC (EDT + 4 h).

## What was captured

Checked directly, not recalled:

- **Audio is intact.** One continuous take of 30 tracks, 17:50:42 to 22:45:59
  (4 h 55 m), 48 kHz / 24-bit mono WAV, the patch list from `reaper.md`.
  `Pilot Reference` (track 26) is there: the first game with a post-DCA
  reference.
- **Log:** 929 entries, 16:34 to 22:45. The mirror ran (908 markers in the
  project).
- **No `recording-started` anchor.** The page's Start recording button was
  greyed out before the game, so the take was started by hand in Reaper.
  This is the known lockout from game 2 (`reaper.md`, "The record button is
  greyed out before you touch anything"), and Pre-flight step 0 has the
  workaround: roll and stop a take in Reaper, then start the real one on the
  page. The step was missed. A fix that does not depend on remembering it,
  and a reason shown on the page, is #163. The box now writes the anchor
  itself when Reaper confirms a take, whoever started it (#158).
- **The timeline is intact** despite the missing anchor (checked 2026-10-05,
  #180):
  - Every item sits at `POSITION 0`, `SOFFS 0`, and the WAVs carry a BWF
    `bext` chunk: originator REAPER, 2026-10-02 **17:50:42**, TimeReference 0.
    Sample 0 of every track is project time 0.000.
  - **All 903 entries written during the take carry a playhead stamp**
    (`project_seconds`). Wall time minus the BWF start minus the stamp is a
    constant **0.45 s** (0.34-0.53) across all of them, so the take began at
    about **17:50:42.45**; the BWF time is truncated to whole seconds.
  - The only entries that cannot be placed are the **26 written before the
    take started** (16:34-17:50), including the first `band-enters-stands`.
    They predate the audio, so no anchor could place them.
  - Reaper's live markers sit a median **0.18 s after** the log's stamps (max
    0.36 s), because the mirror places them where Reaper's cursor is when it
    polls (#66). **For offline work the log's stamps are the reference, not
    the markers.**
  - What is missing is tooling only: `markers.py` needs a `recording-started`
    entry before it places anything. The corrections sidecar supplies a
    synthetic one at project time 0.0 (#159).
- **`2026-10-02-game.jsonl` is empty** (0 bytes, 16:30): a box start with a
  different `--log`, before the real one at 16:34. Nothing is missing from it.
- **What the box ran:** `b7cec64`, from a worktree on `147-snapshot-churn`,
  the emergency fix for page-update churn (#147). All three commits were made
  by 16:30; the only later commit on the branch is a docs-only merge from
  `main`. Functionally what merged as #148. The box recording this itself is
  #157.
- **Tap path was healthy.** Tap-to-box delay median 23 ms, p95 300 ms, worst
  2.1 s. One `out` refused as stale (seq 676, 21:57:05). The two wired
  adapters, control and Dante Primary, were stable all game.
- **Metering capture was empty.** `data/game-capture.pcap` is 6 KB. Most likely
  the game-day switch port filters multicast nothing has joined; a passive
  capture never joins. See the research note.

## Control authority

| Who | When | Logged |
|---|---|---|
| The box, tapped by the operator | Almost every move, all game | Yes: all 319 `commanded` entries are `source: operator` |
| The DM7 iPad app | A handful of times, when a hand cue landed while the operator was already in the app for a mix change. Never because the box was unresponsive | **No** |

So the `commanded` entries are good fader labels except around the app moves,
and the box's belief was wrong after each app move until its next absolute
command. A fade in that window ramps from the wrong level: the full-level
blast case. Whether it happened is answered from `Pilot Reference` (#159).

**The pilot fader was at +0.6 dB** until about the top of the game, when it was
reset to 0.0 in the app alongside the band mix. That is why the soundcheck's
-12.0 dBFS tone read back as -11.4. The step's exact time is to be recovered
from the pilot track (#159); a quick peak pass on 2026-10-04 was inconclusive.

**Console changes** made from the app at the top of the game: band balance, and
the front channels' compression backed off (it was ringing after the band
stopped). DVS records the inputs ahead of all of that, so the recordings are
unaffected; the band PA, `Hype PA Output` and `Band Group` are not.

## Annotations

- **Fader taps are good fader-state labels**, apart from the app moves above.
- **Trigger taps:** 20 whistle/drums swaps within 5 s (16 whistle then drums).
  In each pair the **second tap is the correction**: the conductor moved, a
  whistle entry was expected, and the drums came in first. The conductor
  **points at the drums for a drum entry and whistles the tempo for the whole
  band** (#152). The swaps did not tail off: 7 by Q1, 13 from 21:20 to 22:37.
  The trigger label comes from the audio, with the tap as a window.
- **Open then `out` within 5 s** (6 pairs): the operator anticipated and the
  band did not play. Kept as a label, not cleaned out.
- **Game clock taps are mostly wrong** (`q1` tapped at 19:48 and 19:53, `q2` at
  19:56 and again at 20:31, and so on). Reconstructed offline from `Announcer
  Mic Dry` and `HokieVision` instead (#159).
- **Timeouts:** the announcer only says "official timeout on the field", which
  may be media or injury; telling them apart needs a look behind for the red
  jacket. Tap what was heard, classify offline.
- **Target level** was changed 14 times. Several were taps while the fader was
  open, expecting a move (seq 53-58, 120); nothing moves until #128. Evidence
  is on #128.
- **Band enters stands** was tapped at 17:43:53 and 17:53:38. The first is
  before the take started.
- **Arm / stand-down prompt worked**: halftime exodus at 20:27:39, stood down
  12 s later.

## Decisions

- **App DCA moves: no rule.** Switching apps to the page is slower than
  dragging the fader on the custom layer, and "tap hand-off after every app
  move" would be forgotten. The post-game pilot check (#159) is the guard for
  now.
- **Metering readback, when proven, may only remove trust**: a disagreement
  un-knows the level, as a hand-off does. Never adds confidence, never moves
  the fader. Game 5 or later; needs a design.md amendment (#160).
- **Ducker sidechain stays in the DM7 app.** Toggling it from the box would be a
  second kind of console write, and the box's guarantee is that it writes the
  band DCA and nothing else (design.md 5.3). The starting sources go on the
  gameday checklist (#151). Revisit only if forgetting to toggle it costs the
  band.
- **Fades are described once and animated on the page**, with a fading colour
  that ends only when `move-landed` arrives (#154).
- **A target tap while open rides** (#128); until then the page says it was
  stored and the fader did not move (#153). An open button while open stays a
  no-op.
- **MAIN:** scoring first, quarters and halftime off, timeout wording, colour
  and icons by category with state colours reserved (#155).
- **Swipe between tabs** in the annotation panel only, never from the fader
  column (#156).
- **Game-day fixes:** a branch and PR, run from a worktree, merged after the
  game (#151, #157).
- **Save the console file after every game**, next to the recordings (#151).

## RTD

Not yet sniffed. The Daktronics guide says RTD is broadcast by default, and the
scoring network is probably flat. The switch is an SG300 in the broadcast
booth, and the port to the AllSport hub (scoreboard time controller and iPad)
is known. Plan: a passive capture the Friday before game 4 if there is time,
port mirroring if it turns out to be multicast. Otherwise game 5 (#95).

## Filing

The NAS was not mounted at the review. Same procedure as game 2, which has
not been filed yet either:

1. Copy each project folder, its log and the pcaps to the NAS as they are.
   Verify sizes and a checksum of each log.
2. Rename the **NAS copies** to `YYYY-MM-DD VT-<Opp>[ <what>]`. Media paths in
   the `.RPP` are relative, so folders can be renamed; the `.RPP` keeps its
   name. **Do not touch the Reaper queue file.**

   | Laptop | NAS |
   |---|---|
   | `Reaper Media/260912 ODU - Test/`, `games/2026-09-13.jsonl` | `2026-09-12 VT-ODU/`, `2026-09-12 VT-ODU.jsonl` |
   | `Reaper Media/20261001 Pitt Test/`, `games/2026-10-01-test.jsonl` | `2026-09-30 Runthrough/`, `2026-09-30 Runthrough.jsonl` |
   | `Reaper Media/20261002 Pitt Soundcheck/`, `games/2026-10-02-soundcheck.jsonl`, `data/{console,ref-test,tone-test}.pcap` | `2026-10-02 VT-Pitt Soundcheck/`, `2026-10-02 VT-Pitt Soundcheck.jsonl` |
   | `Reaper Media/20261002 Pitt/`, `games/20261002-pitt.jsonl`, `data/game-capture.pcap` | `2026-10-02 VT-Pitt/`, `2026-10-02 VT-Pitt.jsonl` |

3. Delete `games/2026-10-02-game.jsonl` (empty).
4. Save the console file from this game, if it still matches, next to it.
5. Point this document and `game-2.md` at the NAS copies.
6. Keep the laptop copies until each NAS copy has been opened in Reaper once.

The site values' recording path says `~/Reaper Audio`; the projects are in
`~/Reaper Media` (#151).
