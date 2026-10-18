# Game 4

The fourth home game and the seventh overall game of the season.

Saturday, October 17, 2026
Virginia Tech vs Georgia Tech
Lane Stadium, Blacksburg, VA

Reaper Project Path: "/Users/misnow1/Reaper Media/20261017 GT"
Log File: "/Users/misnow1/games/2026-10-17-gt.jsonl"
Daktronics RTD Capture: "/Users/misnow1/workspace/tacet-downbeat/data/rtd-game-4-gt.pcap"
Console File: "/Users/misnow1/workspace/tacet-downbeat/data/Lane 20261014 mk2.dm7f"

## Notes

* This is the first game where we also had Daktronics RTD data captured
    * The clock stream broke during the fourth quarter, presumably due to a disconnected cable in the booth. That affected all of the clocks in the stadium.
    * At the 2-minute timeout in the 4th quarter, the clock was accidentally reset to 20:00 instead of 2:00.
* The scoreboard fields may also include the following extra data:
    * Red zone efficiency: visits, field goals, touchdowns
    * Yards: rushing, passing, total
    * Penalties, penalty yards, time of posession
    * 1st downs, 3rd down conversions, 4th down conversions
    * Sacks, interceptions made, fumbles recovered

## Requested Changes

* The ordering of the "Good Stuff" buttons is not great based on what I was hunting for at this game. My proposed order is:
    1. First Down
    2. Safety
    3. Field Goal
    4. Touchdown
    5. Defensive Stop
* The hand-entered timing data for timeouts is very fuzzy, especially the ends. As before, I lost track of the end of the timeout periods except for a couple of the media and official timeouts.

---

# Post-game review

A structured interview held the evening of the game, 2026-10-17, run against
the log, the Reaper project, the audio, `tacet-pilot-check` and the RTD capture,
in the same shape as [game 3's](game-3.md). It has two halves: **what the
capture actually contains**, so later analysis does not trust the wrong parts
of it, and **what changes before game 5**. Tracked in #208; each change is its
own issue.

All times are EDT. The log itself is stamped in UTC (EDT + 4 h).

## What was captured

Checked directly, not recalled:

- **Audio is intact.** One continuous take of 30 tracks, 14:02 to 18:42
  (16800.7 s, 4 h 40 m), 48 kHz / 24-bit mono WAV, the patch list from
  `reaper.md`. Every file is full length (2,419,306,866 bytes each, 72.6 GB in
  all). `Pilot Reference` is there.
- **It was recorded into the wrong folder.** The project was not saved before
  the take, so Reaper recorded into its default `~/Documents/REAPER Media/`.
  It was saved into `20261017 GT/` only at 18:42, after the take, with
  absolute paths back to `~/Documents`. Fixed by hand after the game: the WAVs
  moved to `20261017 GT/Media/` and the 30 `FILE` lines rewritten to
  `Media/<name>.wav`, the same layout as game 3, every path checked to resolve.
  The original is beside it as `20261017 GT.RPP.before-media-fix`; delete it
  once the project has opened in Reaper. The page warning about this, so it
  does not depend on remembering, is #204.
- **Log:** 796 entries, 13:59:54 to 18:45:36. **The anchor is there**: the
  page's Start recording button wrote `recording-requested` and Reaper
  confirmed `recording-started` at 14:02:07, the first game where that worked
  (game 3's take was started from Reaper with no anchor). 788 entries carry a
  playhead stamp. The mirror ran.
- **What the box ran:** `247c045`, a clean `main` (`box-started`, seq 1), with
  one untracked file and no uncommitted changes.
- **Tap path was healthy.** 783 taps, tap-to-box delay median 44 ms, p95
  213 ms, worst 2.4 s (game 3: 23 ms, 300 ms, 2.1 s). Three taps refused as
  stale, each about 2 s late: 18:23:39 and 18:23:40 (`out`, `up-whistle`) and
  18:31:02 (`up-whistle`). The operator was under the stands on unreliable
  Wi-Fi, checking the clock fault; these were panic taps, and the refusal did
  its job.
- **RTD capture:** `data/rtd-game-4-gt.pcap`, 14:24:53 to 18:42:32, on the
  same laptop clock as the log's `wall`. The first game with RTD. Gaps and
  source changes are under [Q4](#q4-two-network-events-one-of-them-ours).
- **Console file:** `data/Lane 20261014 mk2.dm7f`, saved 2026-10-14. The band
  and hype channels are untouched since, and **nothing in the mix changed
  during the game**.

## Q4: two network events, one of them ours

The scoring VLAN (VLAN 4) and the control network share the laptop's cable.
The pcap tells the two events apart: while the laptop's link was up, the
gateway's periodic broadcasts kept arriving even when RTD did not.

| When | What | Evidence |
|---|---|---|
| 18:05:41 - 18:06:34 | Booth: RTD silent 53 s, back from `.26` instead of `.30`. The stadium clocks broke; the operator was told a cable came loose | RTD source change |
| 18:19:28 - 18:24:40 | Booth again: RTD silent 5 min, back from `.31` | Gateway broadcasts continue throughout, so the laptop's link was up |
| 18:24:51 - 18:26:10 | **The operator unplugged the laptop** to rule it out. Control and scoring both dropped | All traffic stops for 79 s; the console check at 18:25:34 gets no answer ("not on a directly connected network"), the next at 18:29:34 answers |

**No fader command was lost to the unplug**: no tap arrived between 18:23:40
and 18:27:07. The stale taps on either side are the Wi-Fi ones above.

The RTD source address moved twice in one game, and the morning feed was a
fourth address (`.28`, frozen clock). Whatever reads RTD must accept port 21000
from any source and log each change (#206, #95).

## Control authority

| Who | When | Logged |
|---|---|---|
| The DM7 iPad app | Before kickoff, driving the DCA by hand while the band mix was dialed in | **No** |
| The box, tapped by the operator | The vast majority of moves, all game | Yes: all 263 `commanded` entries are `source: operator` |

**The pilot check agrees.** `tacet-pilot-check` calibrated pilot = DCA
-12.03 dB from 100 snap opens from silence (spread -12.05 to -11.98). Of 155
ramps, 154 match the box's belief to within 0.05 dB (median 0.01 dB). One is
flagged:

```
seq  local        command     believed  pilot before  after  findings
33   15:15:13.86  fade (out)  +3.0      -0.0          +1.8   jumped up at the start of a falling ramp; pilot disagrees with the box's belief
```

The box opened to +3.0 at 15:13:27 (seq 31); by the fade at 15:15:13 the DCA
read 0.0, set in the app during the pregame mix. So the fade's first step was
a +1.8 dB bump: the known app-move case, pregame, small. **The `commanded`
entries are good fader labels for the whole game** except 15:13:27 to 15:15:13.

## Annotations

- **Correction taps: the second tap is the label.** When the operator missed
  the start, they tapped Up slow, then what actually started the band (8 Up
  slow then drums within 5 s). When they tapped whistle and the drums came in,
  they tapped drums (8 whistle then drums, 2 the other way). The same rule as
  game 3; the trigger comes from the audio, with the taps as a window.
- **17:42:35:** `field-goal` (seq 508) then `touchdown` (seq 509) in the same
  second. The first was the wrong button; the touchdown is the label.
- **Score reversed** (16:38:49, 18:13:02) was tapped as an annotation: a good
  thing stopped being good, for example a first-down catch ruled incomplete.
  What each one was comes from the audio. But it is a fader release: at
  16:38:49 it faded the band 23 s after a whistle open (seq 358-361), with
  Faded out a second later, so the band may have been stopping anyway. At
  18:13:02 the fader was already closed. An annotation must not move the
  fader; #202.
- **Ready** was tapped once, pregame (14:59:51). It is not part of the
  operator's routine.
- **Timeouts:** 18 spans (10 official, 3 injury, 3 away, 1 home, 1 media),
  all closed, but **the ends are fuzzy** apart from a couple of media and
  official timeouts. Recover them from RTD rather than ask for better taps
  (#205).
  `tacet-timeouts` on the capture: 15 ends corrected, 2 nested, 1 not checked
  (seq 687, whose hold spans the booth outage) (#205).
- **Halftime exodus** was tapped at 16:41:10 and again at 16:41:12, so its
  span is 2 s long; the first tap is the label. The stand-down prompt it
  raised was dismissed at 17:20:07: the operator left for food and forgot to
  stand down, so the box stayed armed through halftime. No consequence in
  Phase 1 (#207).
- **Last two minutes** spans 18:36:15 to 18:40:26. The game clock was reset to
  20:00 instead of 2:00 at that timeout (operator's note); RTD will show it.
- **Target level** changed three times: +3.0 at 15:11:45 (rode, open), 0.0 at
  17:15:09 (stored, closed), +3.0 at 17:31:05 (rode, open).
- **Other band** on the field 15:16:16 to 15:35:24, pregame.
- **Defensive stop is a per-play label**, not a change of possession: the
  operator taps it for a good defensive play while GT has the ball, mostly a
  stop on first or third down. RTD agrees: 15 of the 16 taps fall in GT
  possession, and the 16th (17:44:36) is 5 s before the scoreboard flipped to
  GT. Each is followed 2-12 s later by the scoreboard's down advancing without
  a first down (2nd & 10, 4th & 7, ...), or by a VT turnover (16:32:45, the
  interception at 16:32:53).
- **Scores against RTD:** all four touchdown taps match the scoreboard to
  within a second; the field goal at **18:00:11 was not tapped**. The
  scoreboard itself read VT 35 for 7 s at 18:09:28 before going back to 34.
  Final VT 34, GT 24.

## Decisions

- **Overturned replaces Score reversed** as a game annotation that never moves
  the fader (#202; operator: "probably", confirm in the PR).
- **Scoring order:** First down, Safety, Field goal, Touchdown, Defensive stop
  (#203).
- **Timeout ends come from RTD**, once the capture proves it; keep tapping
  starts, they carry the kind (#205).
- **An unsaved Reaper project is the box's to notice**, warned on the page
  before kickoff, never blocking a take (#204).
- **A forgotten stand-down gets no new tap.** It matters only once the detector
  drives the fader; Phase 2 answers it on the box side (#207).
- **design.md 8 is corrected from the first capture**: checksum, refresh,
  moving source address, field offsets (#206).

## RTD

First seen on a wire the morning of game 4: UDP broadcast on port 21000, a
display-buffer write protocol as design.md 8 describes, with a different
checksum, a full refresh every ~10 s and a source address that does not stay
put. Details in #206. The operator also saw extra stat fields on the board that
may be in the buffer:

- Red zone efficiency: visits, field goals, touchdowns
- Yards: rushing, passing, total
- Penalties, penalty yards, time of possession
- 1st downs, 3rd down conversions, 4th down conversions
- Sacks, interceptions made, fumbles recovered

## Filing

Same procedure as game 3 ([Filing](game-3.md#filing)):

| Laptop | NAS |
|---|---|
| `Reaper Media/20261017 GT/`, `games/2026-10-17-gt.jsonl`, `data/rtd-game-4-gt.pcap`, `data/Lane 20261014 mk2.dm7f` | `2026-10-17 VT-GT/`, `2026-10-17 VT-GT.jsonl` |
| `data/{dak-hv,dak-hv-2,rtd-test4,rtd-pregame-1}.pcap`, `games/2026-10-17-test.jsonl` | `2026-10-17 VT-GT/` beside the game, as the morning sniff |

Do not file `data/dak-hv-3.pcap`: the unfiltered capture swept up the laptop's
own HTTPS. Delete `20261017 GT.RPP.before-media-fix` once the NAS copy has
opened in Reaper.
