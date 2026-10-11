# Priorities

This page gives the build order: what to build next, and why. The issues hold
the detail and the decisions. This page holds only the order. Without it, the
order lives only in the last conversation.

Last updated 2026-10-11. Game 3 was played and debriefed (`game-3.md`). The
Game 4 order below comes from that review.

## Before Game 3

The history up to Game 3.

- #13 is done. See its entry under Game 3.
- #89, #14 and #6 were done first. #5 is built (the pinned fader column, the
  fixed status strip and prompt slot, and MAIN/MORE). It came right after #6
  landed and gave it every button that the column needed to know about.
- #12 closed, but a post-merge review (2026-09-17) found that it shipped short
  of what it decided. It had a real state-machine bug and missing UI (the
  return prompt, the accept delay, the pulse cue and an always-available
  take-back). These went into #101 and #102, so #12 itself stays closed.
  #101, the backend half, is done.
- A design conversation on the same day changed the model that #102 was scoped
  against. The box's distrust of its own fader belief applies to a cold boot
  too, not only to a StageMix handoff. The underlying invariant is absolute
  against relative fader commands, not "handed off or not."
- #107 superseded #102 and is done, in two PRs: the state machine and shell
  (#114), then the operator page (#115). #108 and #109 were left as two small,
  independent UI fixes from the same conversation.
- The #114 review filed three more follow-ups, #116, #117 and #118. They were
  left out of that PR on purpose and closed together later.
- Decisions on the other issues are comments on #9, #12 and #19, and on #5, #6
  and #89 themselves. #107, #108 and #109 have their decisions in their own
  bodies. The closing comment on #102 says what became of each of its original
  items.
- A milestone pass (2026-09-19) gave most open issues a milestone. #46, #47,
  #48, #49 and #51 joined Game 4. They are the P3 findings from the baseline
  review that had already put #42, #43 and #44 in Game 4. #73 (the console
  reachability check and ARP keepalive) joined Game 4 too. #68 and #56 have no
  milestone, because neither is tied to a specific game. Since then, #154
  absorbed #51, and #42, #43 and #46 to #49 moved to Game 5 on 2026-10-05.
- #19 is done, in two PRs. First the box took ownership of the arm / stand-down
  question (#121). Then the operator page and its docs changed (#125).
- #109 is done. A tap in MORE leaves the operator on MORE. The tab changes only
  when the operator taps a tab button.
- #108 is done. The StageMix hand-off confirmation shows under its button in
  MORE. A tab change cancels an unanswered confirmation, so it can never
  suppress the question from #19.
- #9 is built at a narrower scope than its body, by a maintainer decision
  (2026-09-20). Game 3 ships only the standing target: the presets, the cap,
  the control on MORE and the readout. The mid-song retarget ride went into
  #128, which is done.
- #15 is done. design.md section 4 has the pre-open, the cannon and the
  repeating scoring songs. Section 6.3 defines READY. Section 6.4 records that
  only a person ever pre-opens, in any phase (#6, #95, and the unconditional
  refusal in state.py). Section 9 separates band-present from fader-state,
  which was the point of the issue.
- One follow-up came from #15: #133, where offline labels live. The box's log
  is append-only, and #21's sidecar corrects the log but does not label the
  audio. #133 is wanted before the Game 3 capture is analysed, not before it is
  made. It was Game 4, and moved to Game 5 on 2026-10-05.
- The same review and the #18 note gave three more small issues, all Game 3 at
  the time:
  - #132. CLAUDE.md still calls the post-DCA channel "the ground truth labels"
    and omits READY.
  - #134. design.md 5.3 says that the OSC spec has no `get`, but it has one.
  - #136. A slip in the principle numbers.

  They moved to Game 5 on 2026-10-11. #135 (rename `fader.target`, which #9
  made ambiguous) was Game 4, and is done.

## Game 3 (2026-10-02)

The table is in build order. Each row says why the step comes at that point.

| # | Issue | Why here |
|---|---|---|
| ~~1~~ | ~~[#89](https://github.com/misnow1/tacet-downbeat/issues/89) Let the operator drive the fader while STANDING DOWN~~ | Done. A forgotten Arm is no longer a missed downbeat. #19 was designed around that hazard |
| ~~2~~ | ~~[#14](https://github.com/misnow1/tacet-downbeat/issues/14) Vocabulary: field goal, cannon, and what the band plays for~~ | Done, except for the fader keys `up-ready` and `score-reversed`. They belonged with #6, which gives them a state to move to |
| ~~3~~ | ~~[#6](https://github.com/misnow1/tacet-downbeat/issues/6) READY, then go~~ | Done. READY is a new state between IDLE and OPEN. The operator rides to a hold level below the target and waits to see if the band starts. Then the operator commits with the ordinary up buttons, or abandons with Score reversed |
| ~~4~~ | ~~[#5](https://github.com/misnow1/tacet-downbeat/issues/5) Button layout for thumbs~~ | Done. Game 2's biggest page failure was scrolling and a grid that moved under the thumb. #19 now puts its question in the fixed status strip and prompt slot. #12's confirmation was there too, until #108 moved it under its button |
| ~~5~~ | ~~[#12](https://github.com/misnow1/tacet-downbeat/issues/12) StageMix handoff and take-back~~ | Closed, but incomplete. See #101 and #102 below. It was to remove the only known path to a full-level blast, which is a ramp from a level that the box only believes in |
| ~~6~~ | ~~[#101](https://github.com/misnow1/tacet-downbeat/issues/101) Take-back state/queue bugs~~ | Done. `TAKE_BACK_UP` from a handed-off READY now commits to OPEN. A queued move now survives a failed confirming packet until a retry (`TAKE_BACK_CONFIRMED`, which mirrors `RIDE_IN_COMPLETE`) |
| ~~7~~ | ~~[#107](https://github.com/misnow1/tacet-downbeat/issues/107) Generalize fader-position trust (level_known)~~ | Done, in two PRs. It superseded #102. It is for the same hazard as #12, a ramp from a level that the box only believes in. The hazard now includes a cold boot as well as a StageMix handoff. The invariant is now absolute against relative |
| ~~8~~ | ~~[#19](https://github.com/misnow1/tacet-downbeat/issues/19) Prompt arm / stand down from band annotations~~ | Done, in two PRs. First the box took ownership of the question (#121), then the page and the docs changed. It is independent of #12. It uses #5's prompt slot, not the fader column |

Alongside, not in the build order above:

- ~~[#13](https://github.com/misnow1/tacet-downbeat/issues/13)~~ Done.
  reaper.md has the 29-track patch list. gameday.md has these:
  - The site values.
  - The network plan (two wired adapters, control and Dante Primary).
  - The console fixes before the game.
  - The firmware hold and ring-out.
  - A list to fill in on the day.

  The post-DCA reference is now a pilot tone that Reaper generates. It goes
  through the Post Fader direct out of a console channel that only the DCA
  controls. The band PA is not the reference, because its expanders confound
  it (design.md 9 is updated). #137 is the box-generated version with measured
  readback, and has no milestone.

  Still to set in the box's own `tacet.toml` are `capture.channels = 30` (29
  until the 2026-09-30 runthrough, #145) and `capture.game_hours = 6.0`.
- ~~[#15](https://github.com/misnow1/tacet-downbeat/issues/15)~~ Done.
  design.md now has the hard cases for the pre-open, the cannon and the
  repeating scoring song. It also has READY in 6.3, and the rule in 6.4 that
  only a person ever pre-opens. Section 9 splits the labels into band-present
  and fader-state. The format for offline labels is left undecided on
  purpose. The log is append-only, and #21's sidecar corrects the log but does
  not label audio. The format is wanted before the Game 3 capture is analysed
  (#133).
- [#18](https://github.com/misnow1/tacet-downbeat/issues/18) Research: DM7
  fader readback. `docs/research/dm7-fader-readback.md` has the first pass. It
  found no documented readback path. But Yamaha's own Crestron module for the
  DM7 points to a second protocol, on TCP port 49280, that answers reads. Also,
  the OSC spec does define one `get` (`sscurrentt_ex`, for scenes), so the "no
  `get`" in design.md 5.3 needs a correction (#134). The decisions of
  2026-09-21 are these:
  - Do not ask Yamaha yet.
  - A passive bench capture with a free client is approved: Bitfocus Companion
    or DM Editor, with sync from the DM7 to the PC only. It is to show whether
    the console notifies an observer of a fader move that came from OSC.
  - No Mixing Station licence yet.
  - The console stays on its current firmware through Game 3. V2.00 cannot be
    rolled back (see #13).

  If the capture shows that the console notifies, #107 gets simpler. Nothing
  here reaches the control path this season. The issue stays open for the
  capture. Moved to Game 5 on 2026-10-11.
- ~~[#103](https://github.com/misnow1/tacet-downbeat/issues/103)~~ Closed
  2026-09-17. design.md: document the fader-belief mechanism in operator terms.
  It was unblocked when #107 landed, so it describes what shipped. It documents
  `level_known` and absolute against relative, not handoff and take-back. There
  is no take-back pair any more. design.md 5.3 has the short version for it to
  build on.
- [#132](https://github.com/misnow1/tacet-downbeat/issues/132) CLAUDE.md:
  narrow "the ground truth labels" to the fader-state labels, add READY to the
  state machine, and carry the never-pre-open rule. It changes the project
  instructions, so it gets its own PR. Land it after #131. Moved to Game 5 on
  2026-10-11.
- [#134](https://github.com/misnow1/tacet-downbeat/issues/134) design.md 5.3
  says that the OSC spec has no `get`, but the spec defines `sscurrentt_ex` for
  the current scene number. The #18 note found this. It does not change the
  fader conclusion. Moved to Game 5 on 2026-10-11.
- [#136](https://github.com/misnow1/tacet-downbeat/issues/136) design.md 5.5
  and 5.6 call "announce, don't surprise" principle 4, but section 10 numbers
  it principle 5. It is a very small fix, and it can go in the same PR as #132.
  Moved to Game 5 on 2026-10-11.
- ~~[#108](https://github.com/misnow1/tacet-downbeat/issues/108)~~ Done. The
  Yes / No confirmation for the StageMix hand-off now shows directly under its
  button in MORE, not in the top slot. One more change goes beyond the issue's
  wording, and its comments record the decision. A tab change cancels an
  unanswered confirmation and sends nothing. Thus a hidden confirmation can
  never suppress the arm / stand-down question from #19.
- ~~[#109](https://github.com/misnow1/tacet-downbeat/issues/109)~~ Done. A tap
  in MORE now leaves the operator on MORE. This includes Note, Arm / Stand down
  / Start recording and the StageMix answers. The tab changes only when the
  operator taps a tab button. Revisit this if use in a game shows that it is
  the wrong decision.
- ~~[#9](https://github.com/misnow1/tacet-downbeat/issues/9)~~ Done, at its new
  scope, which is the standing target only. It added these:
  - `fader.presets`, which ships as `[0.0, -3.0, -6.0]`. The default is
    `fader.default_target_db`, or the first entry when that is unset (#139).
  - `fader.max_target_db`, which ships as 0 dB until the on-site ring-out. The
    box refuses a preset above it at load.
  - A runtime target on the app. Every open and READY's hold level follow it.
  - MORE > Target level.
  - A strip readout that goes amber when the target is not the default.

  A change to the target stores the value in every state, and the next open
  uses it. While the fader is up, the fader also rides to it (#128).
- ~~[#116](https://github.com/misnow1/tacet-downbeat/issues/116)/[#117](https://github.com/misnow1/tacet-downbeat/issues/117)/[#118](https://github.com/misnow1/tacet-downbeat/issues/118)~~
  Done, together.
  - #116. When the send of an absolute command fails, the level is unknown
    again. Before, the box marked the level known on a packet that never
    arrived.
  - #117. Only the operator can do HANDOFF now. Thus a detector can never make
    the level unknown after Phase 2 is declared.
  - #118. A hand-off always gets its `handed-off` entry, even when the level is
    already unknown. Without that entry, a restart under StageMix leaves no
    record.

## Game 4 (2026-10-17)

This order comes from the Game 3 debrief (2026-10-04). Things that the operator
touches on the night come first. The layout change lands early, so the pre-game
runthrough (about 10-14 or 10-15) is not the first time a thumb meets it.

| # | Issue | Why here |
|---|---|---|
| ~~1~~ | ~~[#149](https://github.com/misnow1/tacet-downbeat/issues/149) Commit the metering research note~~ | Done |
| ~~2~~ | ~~[#155](https://github.com/misnow1/tacet-downbeat/issues/155) MAIN: scoring first, quarters off, timeout wording, colour and icons~~ | Done, and Safety is added. Muscle memory still needs the runthrough |
| ~~3~~ | ~~[#154](https://github.com/misnow1/tacet-downbeat/issues/154) Describe a fade once, animate it, fading colour~~ | Done, and it absorbs #51. `fader.move` describes a fade or ride once, and the page animates it. Thus a fade costs three page messages. The button that started the move shows the fading colour until the box says the move landed. The box sends the button list once per socket |
| ~~4~~ | ~~[#153](https://github.com/misnow1/tacet-downbeat/issues/153) Target set while open: stored, not moved~~ | Done. While the fader is up, a target tap leaves a note under the segments. The note says that the target was stored and the fader did not move, and the chip adds `(next open)`. A tap while the fader is closed shows no note, and `stored_because` in the log records why |
| ~~5~~ | ~~[#128](https://github.com/misnow1/tacet-downbeat/issues/128) Retarget ride~~ | Done. A target tap while the fader is up rides the fader to the target over `fader.retarget_ride_seconds` (default 1.0). The box refuses the ride for a stale tap, or when the level is unknown. When the fader is closed or fading, the tap only stores the target |
| ~~6~~ | ~~[#163](https://github.com/misnow1/tacet-downbeat/issues/163) Say why the record button is grey~~ (done). ~~[#172](https://github.com/misnow1/tacet-downbeat/issues/172) Ask Reaper for its transport state~~ (done). ~~[#158](https://github.com/misnow1/tacet-downbeat/issues/158) Write the anchor when Reaper starts recording~~ (done) | Done. The record button works when Reaper is parked, and it says why when it is grey. The box asks Reaper for its state. The anchor is Reaper's confirmation, whoever started the take. Game 3's timeline is intact from its playhead stamps (#180). Only `markers.py` needs the synthetic anchor from #159 |
| ~~7~~ | ~~[#157](https://github.com/misnow1/tacet-downbeat/issues/157) Log the running commit and dirty flag~~ | Done. Every run starts with a `box-started` entry. The banner has a `code` row, and the page shows an amber chip while the tree is dirty |
| ~~8~~ | ~~[#151](https://github.com/misnow1/tacet-downbeat/issues/151), [#150](https://github.com/misnow1/tacet-downbeat/issues/150), [#152](https://github.com/misnow1/tacet-downbeat/issues/152) Docs: gameday checks, this review, the conductor's cue~~ | Done, with #166 (Reaper OSC behaviour, the mirror toggle) |
| ~~9~~ | ~~[#44](https://github.com/misnow1/tacet-downbeat/issues/44) Stopping the box mid-fade leaves the fader at an arbitrary level~~ | Done. A confirmed stop starts no move. It lets a fade land and leaves a ride where it is. The box handles SIGTERM |
| ~~10~~ | ~~[#73](https://github.com/misnow1/tacet-downbeat/issues/73) Check the console answers at startup and at arm~~ | Done. The box pings the console at startup, at every arm and every 4 minutes. The banner has a `ping` row, and the console chip shows only when something is wrong. The runthrough must still measure if the DM7 answers ping |
| ~~11~~ | ~~[#197](https://github.com/misnow1/tacet-downbeat/issues/197) make install keeps an old mypy, so make check fails on numpy 2.5 stubs~~ | Done. Found 2026-10-11 on the dev machine. On a venv from before #193, the update in gameday.md before the game failed at `make check`. The box's venv can be that old. The `dev` extra now requires `mypy>=2.4`, so `make install` upgrades an older mypy |

Nice to have for Game 4, both done:

- ~~[#156](https://github.com/misnow1/tacet-downbeat/issues/156)~~ Swipe, never
  from the fader column.
- ~~[#135](https://github.com/misnow1/tacet-downbeat/issues/135)~~ The
  `fader.target` rename, unblocked by #128. It was removed, not renamed.
  `fader.move.to_db` carries the value now.

Not gating any game: [#159](https://github.com/misnow1/tacet-downbeat/issues/159),
the Game 3 sidecar and pilot analysis, after #133.

Game 5 or later:

- [#160](https://github.com/misnow1/tacet-downbeat/issues/160). Metering
  readback may only remove trust. It needs a design.md amendment.
- RTD (#95), unless the booth sniff happens the Friday before Game 4.

These issues moved to Game 5 on 2026-10-05 (#182), because they will not land
for the 2026-10-17 game:

- #17, #21, #42, #46, #47, #48, #49 and #133.
- #43, which #158 partly addresses.
- #66. Live markers lag the stamps by 0.18 s (see #180).

On 2026-10-11 the Game 3 milestone was closed. These four issues were still open
in it, and moved to Game 5:

- #18, the fader readback research, which waits on the bench capture.
- #132, #134 and #136, the CLAUDE.md and design.md wording fixes.

[#167](https://github.com/misnow1/tacet-downbeat/issues/167) (safety: confirm
every band track is armed before kickoff, beyond the 8-track OSC bank) got the
Game 5 milestone on the same day.

## Keeping this current

Update this page in the same PR that changes the order. These changes need an
update:

- An issue on this page closes.
- A new issue moves ahead in the order.
- A milestone moves.

Keep the date at the top current. The issues stay the source of truth for
*what* each one does. If this page and an issue disagree about scope, the issue
wins.
