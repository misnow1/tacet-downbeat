# Game 5

The fifth home game and the tenth overall game of the season.

Saturday, November 14, 2026
Virginia Tech vs Stanford
Lane Stadium, Blacksburg, VA

Reaper Project Path: "/Users/misnow1/Reaper Media/20261114 Stanford"
Log File: "/Users/misnow1/games/20261114-stanford.jsonl"
Daktronics RTD Capture: "/Users/misnow1/workspace/tacet-downbeat/data/rtd-26111401-stanford.pcap"

## Notes

* This is the second game where we also had Daktronics RTD data captured.
* Audio was checked during the Friday morning rehearsal and recorded during the soundcheck before the game.
* No audio was recorded during the game. Prior to the game, some idiot (we can never know who) spilled half a thermos of water on my laptop. This caused my Dante network adapter to fail which I didn't noticed until after the game (I misinterpreted Reaper's symptoms). As a result, the app correclty reported that Repear shows its status as recording but its playhead was not moving. This was consistent with Reaper's state since it was in record mode but couldn't access the audio interface (because it didn't exist).
* The game clock was stable during the entire game.
* It was near freezing during the game.
  * I left my laptop in the control room (which is conditioned). As such, I couldn't see that Reaper had failed because the Dante interface didn't exist.
  * I was wearing touch-enabled gloves but they're not perfect. I missed several taps because the iPad thought I was moving my finger, thus either selecting text or scrolling the display.
  * I would like to discuss ways we can move the whole operation inside for such scenarios.
* The network connection appeared to be unstable - the refresh indicator was green but counted up - but was never unresponsive. This, along with the Reaper error, lead to confusion on my part even though there weren't any actual network issues.

## Requested Changes

* Clock time and status (period and running/stopped) display on the main page
  * This needs to stay out of the main loop obviously. My instict is a separate process (managed by tacet-serve) that listens for clock data, parses it, and uses IPC or presents an RPC or API interface for the main display page to query and display in the status area.
* Network configuration checking and polling
  * Display of configured network ports and their IP addresses in tacet-serve.
  * An error message in the console when a network interface disappears or Reaper enters an unexpected state
  * An error message in the status area of the main page when a defined network interface is not present as expected or disappears during the event. This error message should not clear unless manually cleared by an operator (to prevent flapping from being undetected). Such events should be logged as well.
* A quick way to recover from feedback (added 2026-11-15, left out of the notes above)
  * I've been able to run the band at +3dB for the past two games, but on some songs in this game that caused not great to very bad ringing. I ended up dropping the band bus by 1 or 2dB and leaving the DCA at 0dB or +3dB (or some combination thereof) during the game. There's no quick way to recover from feedback in the app: the band is either all on or all off (or ready). Being able to nudge the fader down without totally killing it, or having to switch layers to get to the DCA target, would be super useful.

---

# Post-game review

A structured interview held 2026-11-15, the day after the game, run against
the log, the Reaper project, the audio and the RTD capture, in the same shape
as [game 4's](game-4.md). It has two halves: **what the capture actually
contains**, so later analysis does not trust the wrong parts of it, and **what
changes before game 6**. Tracked in #238; each change is its own issue.

All times are EST. The log itself is stamped in UTC (EST + 5 h).

## What was captured

Checked directly, not recalled:

- **There is no game audio.** The project holds two takes, both pregame:

  | Take | Wall clock | Project time | Length |
  |---|---|---|---|
  | 1 | 18:09:45 - 19:01:25 | 0 - 3100.5 s | 51 m 41 s |
  | 2 | 19:03:07 - 19:36:14 | 3100.5 - 5087.7 s | 33 m 07 s |

  The break at 19:01 is the spill. Take 2 picks up where take 1 ended in project
  time. The last 1.2 s of take 2 are digital zero on every track, so the
  interface dropped out mid-take. Kickoff was 19:35:00 (RTD) and the first open
  was 19:36:25, so the capture ends 74 s into the first quarter and 11 s before
  the band first played.
- **The project does not match the 34-track patch list in `reaper.md`** (#232).
  It has 32 tracks, with `Pilot Reference` on track 26 (not 35), `Stereo A` on
  31 (stereo) and `WetRef Bus` on 32. A `39-NEZ Reference Mic` item, 26 s long,
  sits in take 1 only. 48 kHz / 24-bit WAV.
- **Log:** 1096 entries, 17:00:51 to 23:41:30. **The anchor is there.** The
  page's Start recording wrote `recording-requested`, and Reaper confirmed
  `recording-started` at 18:09:45. Reaper reported take 2 starting at 19:03:07
  with no tap behind it. **After 19:36:14 the playhead stamps are wrong.** 368
  entries carry `project_seconds` 5087.67, the end of the take (#233).
- **What the box ran:** `c8e6c35`, a clean `main` (`box-started`, seq 1), with
  no untracked files.
- **Tap path was healthy, and better than game 4.** 794 timed taps. Tap-to-box
  delay median 12 ms, p95 43 ms, worst 90 ms (game 4: 44 ms, 213 ms, 2.4 s).
  No tap was refused as stale. One tap was untimed: seq 578, Injury timeout at
  21:06:11, sent before the page had timed its link.
- **RTD capture:** `data/rtd-26111401-stanford.pcap`, 17:11:33 to 23:41:18. One
  source (`192.168.0.25`) all night, no gaps, no faults. That agrees with
  "the game clock was stable". The pregame sniffs are
  `rtd-26111401-stanford-pregame.pcap` and `-pregame-2.pcap`.
- **Soundcheck:** a separate project, `Reaper Media/20261114 Stanford
  Pregame/`, with its own log, `games/20261114-stanford-soundcheck.jsonl`. One
  take, 15:31:55 to 16:28:41 (3405.9 s), anchored the same way
  (`recording-requested`, then `recording-started` confirmed by Reaper). The same
  32 tracks plus `39-NEZ Reference Mic`, this time full length. Every `FILE`
  path is relative (`Media/...`) and resolves. It was moved into place by hand
  on 2026-11-15. The `.RPP` is named `202611114 Stanford Pregame.RPP`, with an
  extra `1` in the date.

## The Reaper failure

| When | What | Evidence |
|---|---|---|
| ~19:01 | Water on the laptop. The Dante adapter drops | Take 1 ends 19:01:25 (operator: the spill) |
| 19:03:07 | The adapter is back, and Reaper starts take 2 | `recording-started`, confirmed by Reaper, no tap |
| 19:35:00 | Kickoff | RTD |
| 19:36:14 | The adapter fails for good. Take 2 ends | Last 1.2 s digital zero |
| 19:36:25 | First open (Up whistle) | seq 11 |
| 19:36:27 - 23:32:27 | Reaper still says it is recording, playhead frozen at 5087.67 | 291 `recording-found`, about every 20 s |

**The box saw it, and named it wrong.** Every ~20 s, Reaper answered the box's
refresh with `/record 1` and the same `/time`, then went quiet again. So the page
went back and forth between ROLLING and **LINK LOST** all game. LINK LOST says
*"Reaper has stopped answering"*. Reaper was answering. What had stopped was its
audio device. With the laptop in the control room and the operator outside,
nothing else was going to show it. The fault gets its own name and a latched
error on the page in #233. The interface check that would have caught the
adapter directly is #234.

## The network was fine

The operator read two things as an unstable network. Neither was:

- **The green counter climbing** is the link pulse working as designed. It
  counts up and drops back to `0s` on each keepalive, which comes at most every
  15 s ([box.md](box.md)). A green counter that climbs and resets is a healthy
  link. One past the threshold turns stale, and one that stops moving is a dead
  page.
- **LINK LOST** was the Reaper fault above, not the network.

The tap path (above), the single RTD source with no gaps, and the console reach
check all agree. The reach check answered at startup and at arm, and did not
change until the laptop was torn down after the game (23:24).

## Control authority

| Who | When | Logged |
|---|---|---|
| The DM7 iPad app | Before 19:18:23 | **No** |
| The box, tapped by the operator | From the take-back at 19:18:23 (close-now) to the end | Yes: all 293 `commanded` entries are `source: operator`, none undelivered |

The box armed at 19:28:17. **No pilot check is possible for the game.**
`tacet-pilot-check` needs the `Pilot Reference` track, and it stops at 19:36:14.
The `commanded` entries are the only fader record, and nothing checks them. With
no audio to label, **this game is of no use to the detector** (#217).

## Annotations

- **No game or band annotations at all.** No Touchdown, Field goal, First
  down, Defensive stop, Halftime exodus or band taps. Game 4 had dozens. The
  operator wore touchscreen gloves in the cold and kept to the fader and timeout
  buttons; hunting for the rest was too hard (#236). The scores come from RTD:

  | VT score | When | |
  |---|---|---|
  | 6, then 7 | 21:00:39, 21:02:52 | Touchdown, PAT |
  | 13, then 14 | 21:55:53, 21:56:33 | Touchdown, PAT |
  | 20, then 22 | 22:45:18, 22:46:03 | Touchdown, two-point conversion |

  Final VT 22, Stanford 40.
- **Timeouts:** 17 single taps, the first game since #216. `tacet-timeouts`
  matches 15 to a play-clock hold. Seq 474 (Official timeout, 20:50:42) has no
  stoppage. Seq 1026 repeats seq 1025 a second later. It flags seq 310
  (Away, 20:24:18) and seq 640 (Home, 21:15:06) as showing no team timeout. **The
  taps are right**: the scoreboard's timeouts-left dropped 3-4 s before each
  hold began, so the tool called the drop stray (#239). Four team timeouts were
  not tapped: guest 21:11:25, home 22:50:57 and 22:52:13, guest 22:56:56.
- **Quarters from RTD** (`tacet-quarters`, #212): Q1 19:35:00-20:17:40, Q2
  20:17:40-21:18:02, halftime 21:18:02-21:38:14, Q3 21:38:14-22:14:03, Q4
  22:14:03-23:07:51. No Last two minutes taps.
- Neither tool has been run with `--write`. The sidecar does not exist yet.
- **Target level** was set five times, each time while closed: +3.0 at 19:28:19,
  0.0 at 19:54:50, +3.0 at 20:14:25, 0.0 at 20:14:47, +3.0 at 21:05:38.
- **Ready** was tapped 7 times.

## Decisions

- **The box names a dead audio device as its own fault**, latched on the page
  until the operator clears it, and stops stamping a frozen playhead (#233,
  Game 6).
- **The box's network interfaces are named by role and MAC in `tacet.toml`**,
  listed at startup, polled, with a latched page error when one is missing or
  disappears (#234, Game 6). A MAC, because macOS renumbers USB adapters.
- **Game clock, period and running/stopped on the main page**, read from RTD in
  a separate process that tacet-serve manages. Display only (#235, Game 6).
- **Taps must survive gloves**: no text selection, and no scroll or swipe from a
  small drag (#236).
- **Running from indoors in the cold is still a question.** The operator was
  interviewed on 2026-11-15. The second half was run from under the NEZ stands
  on the conductor mic, with no sightline. The options and what each depends on
  are in #237; no decision yet.
- **`tacet-timeouts` attaches a team timeout that drops just before its hold**
  (#239).
- **Nudge the DCA 1 dB down or up from MAIN** to recover from ringing. It moves
  the live level only, never the target, and is refused while the level is
  unknown (#242, Game 6).
- **A standalone, racked machine for tacet and Reaper** replaces the personal
  laptop eventually: the spill, the lock screen, the walk back to the booth
  (#241).

## Filing

Same procedure as game 3 ([Filing](game-3.md#filing)):

| Laptop | NAS |
|---|---|
| `Reaper Media/20261114 Stanford/`, `games/20261114-stanford.jsonl`, `data/rtd-26111401-stanford.pcap` | `2026-11-14 VT-Stanford/`, `2026-11-14 VT-Stanford.jsonl` |
| `Reaper Media/20261114 Stanford Pregame/`, `games/20261114-stanford-soundcheck.jsonl`, `data/rtd-26111401-stanford-pregame{,-2}.pcap` | `2026-11-14 VT-Stanford/` beside the game, as the soundcheck and pregame sniff |

Both Reaper projects are pregame recordings only. File them anyway: they are the
only audio of this game's 32-track setup, and the soundcheck is the only take
with `NEZ Reference Mic` at full length.
