# Priorities

What to build next, in order, and why. The issues hold the detail and the
decisions; this page only holds the order, which otherwise lives in nobody's
head but the last conversation's.

**Last updated 2026-09-21.** #5 is built (the pinned fader column, the fixed
status strip and prompt slot, MAIN/MORE), right after #6 landed and gave it
every button the column needed to know about. #89, #14 and #6 were already
done. #12 closed too, but a post-merge review (2026-09-17) found it shipped
short of what it decided: a real state-machine bug plus missing UI (return
prompt, accept-delay, pulse cue, always-available take-back), split into #101
and #102 so #12 itself stays closed. #101 (the backend half) is now done too.
A same-day design conversation then reworked the model #102 was scoped
against: the box's distrust of its own fader belief generalizes to cold boot,
not just a StageMix handoff, and the underlying invariant is absolute-vs-relative
fader commands rather than "handed off or not." #102 was superseded by #107,
which is now done in two PRs - the state machine and shell (#114), then the
operator page (#115) - leaving #108 and #109 as two small, independent UI
fixes from the same conversation. The #114 review itself filed three more
follow-ups, deliberately left out of that PR: #116, #117 and #118, closed
together here. Decisions on the remaining issues are recorded as comments on
#9, #12 and #19, and on #5, #6 and #89 themselves; #107/#108/#109 carry their
own decisions in their bodies, and #102's closing comment maps what became of
each of its original items. A milestone-assignment pass (2026-09-19) gave the
remaining open issues a milestone: #46, #47, #48, #49 and #51 (the P3 findings
from the same baseline review that already put #42/#43/#44 in Game 4) and #73
(console reachability check / ARP keepalive) joined Game 4. #68 and #56 stay
unmilestoned - neither is tied to a specific game. #19 is now done too, in two
PRs - the box owns the arm / stand-down question (#121), then the operator
page and the docs that describe it (#125). #109 is done too: a tap in
MORE now leaves the operator on MORE, and the tab moves only when they tap a
tab button. #108 is done as well: the StageMix hand-off confirmation now
renders under its button in MORE, and switching tabs cancels an unanswered
one, so it can never suppress #19's question. #9 is built at a narrower scope
than its body (a maintainer decision, 2026-09-20): Game 3 ships the standing
target only - the presets, the cap, the control on MORE and the readout - and
the mid-song retarget ride is split out as #128 for Game 4. #15 is done:
design.md section 4 now carries the pre-open, the cannon and the repeating
scoring songs, 6.3 defines READY, 6.4 records that nothing but a person ever
pre-opens in any phase (#6, #95, and state.py's unconditional refusal), and 9
separates band-present from fader-state, the point of the issue. One follow-up
falls out, now #133 (Game 4): where offline labels live, since the box's log
is append-only and #21's sidecar corrects the log rather than labelling the
audio - wanted before the Game 3 capture is analysed, not before it is made.
Three more small ones came out of the same review and the #18 note: #132
(CLAUDE.md still calls the post-DCA channel "the ground truth labels" and omits
READY), #134 (design.md 5.3 says the OSC spec has no `get`, and it has one) and
#136 (a principle-numbering slip), all Game 3; and #135 (rename `fader.target`,
which #9 made ambiguous), Game 4.

## Game 3 (2026-10-02)

In build order. Each step says why it comes where it does.

| # | Issue | Why here |
|---|---|---|
| ~~1~~ | ~~[#89](https://github.com/misnow1/tacet-downbeat/issues/89) Let the operator drive the fader while STANDING DOWN~~ | Done. A forgotten Arm is no longer a missed downbeat, which is the hazard #19 was designed around |
| ~~2~~ | ~~[#14](https://github.com/misnow1/tacet-downbeat/issues/14) Vocabulary: field goal, cannon, and what the band plays for~~ | Done, except the fader keys: `up-ready` and `score-reversed` belonged with #6, which gives them a state to move to |
| ~~3~~ | ~~[#6](https://github.com/misnow1/tacet-downbeat/issues/6) READY, then go~~ | Done. A new state between IDLE and OPEN: the operator rides to a hold level short of target and waits to see whether the band starts, before committing with the ordinary up buttons or abandoning with Score reversed |
| ~~4~~ | ~~[#5](https://github.com/misnow1/tacet-downbeat/issues/5) Button layout for thumbs~~ | Done. Game 2's biggest page failure was scrolling and a grid that moved under the thumb; the fixed status strip and prompt slot are where #19 now puts its question, and where #12's confirmation sat until #108 moved it under its button |
| ~~5~~ | ~~[#12](https://github.com/misnow1/tacet-downbeat/issues/12) StageMix handoff and take-back~~ | Closed, but incompletely - see #101 and #102 below. Was meant to remove the only known path to a full-level blast: a ramp from a level the box only believes in |
| ~~6~~ | ~~[#101](https://github.com/misnow1/tacet-downbeat/issues/101) Take-back state/queue bugs~~ | Done. `TAKE_BACK_UP` from a handed-off READY now commits to OPEN, and a queued move survives a failed confirming packet through to a retry (`TAKE_BACK_CONFIRMED`, mirroring `RIDE_IN_COMPLETE`) |
| ~~7~~ | ~~[#107](https://github.com/misnow1/tacet-downbeat/issues/107) Generalize fader-position trust (level_known)~~ | Done, in two PRs. Superseded #102. The same hazard #12 was for - a ramp from a level the box only believes in - generalized to cold boot as well as StageMix handoff, with the invariant restated as absolute against relative |
| ~~8~~ | ~~[#19](https://github.com/misnow1/tacet-downbeat/issues/19) Prompt arm / stand down from band annotations~~ | Done, in two PRs - the box owns the question (#121), then the page and the docs. Independent of #12 - built on #5's prompt slot, not the fader column |

Alongside, not in the build order above:

- [#13](https://github.com/misnow1/tacet-downbeat/issues/13) gameday.md: game 3 patch list, network plan, stale sections. Needs site information; must land before game day regardless. Includes setting `capture.audio_path` and `capture.channels` in the box's `tacet.toml` (#53), holding the console's firmware through game day (#18), and patching the post-DCA reference channel, which the fader-state labels depend on.
- ~~[#15](https://github.com/misnow1/tacet-downbeat/issues/15)~~ Done. design.md now carries the pre-open, cannon and repeating-scoring-song hard cases, READY in 6.3, the rule that only a person ever pre-opens in 6.4, and section 9's split into band-present and fader-state labels. The format for offline labels is deliberately left undecided (the log is append-only; #21's sidecar corrects it, it does not label audio) and is wanted before the Game 3 capture is analysed (#133).
- [#18](https://github.com/misnow1/tacet-downbeat/issues/18) Research: DM7 fader readback. The first pass is written up in `docs/research/dm7-fader-readback.md`: no documented readback path exists, but Yamaha's own Crestron module for the DM7 points at a second, TCP protocol on 49280 that answers reads, and the OSC spec does define one `get` (`sscurrentt_ex`, scenes), so design.md 5.3's "no `get`" needs correcting (#134). Decided 2026-09-21: not asking Yamaha yet; a passive bench capture with a free client (Bitfocus Companion or DM Editor, sync DM7->PC only) is approved, to see whether the console notifies an observer of an OSC-originated fader move; no Mixing Station licence yet; and the console stays on its current firmware through Game 3 (V2.00 cannot be rolled back, see #13). If the capture says it does notify, #107 gets simpler; nothing here reaches the control path this season. The issue stays open for the capture.
- [#103](https://github.com/misnow1/tacet-downbeat/issues/103) design.md: document the fader-belief mechanism in operator terms. Unblocked: #107 has landed, so it describes what shipped. What it documents is `level_known` and absolute-vs-relative, not handoff/take-back - there is no take-back pair any more, and design.md 5.3 now carries the short version for it to build on.
- [#132](https://github.com/misnow1/tacet-downbeat/issues/132) CLAUDE.md: narrow "the ground truth labels" to the fader-state labels, add READY to the state machine, and carry the never-pre-open rule. Project instructions, so its own PR; land it after #131.
- [#134](https://github.com/misnow1/tacet-downbeat/issues/134) design.md 5.3 says the OSC spec has no `get`; it defines `sscurrentt_ex` for the current scene number. Found by the #18 note; does not change the fader conclusion.
- [#136](https://github.com/misnow1/tacet-downbeat/issues/136) design.md 5.5 and 5.6 call "announce, don't surprise" principle 4; in section 10 it is principle 5. Tiny; can ride with #132.
- ~~[#108](https://github.com/misnow1/tacet-downbeat/issues/108)~~ Done. The StageMix hand-off Yes / No confirmation now renders directly under the button in MORE instead of the top slot. Beyond the issue's wording, and decided in its comments: switching tabs cancels an unanswered confirmation and sends nothing, so an invisible one can never suppress #19's arm / stand-down question.
- ~~[#109](https://github.com/misnow1/tacet-downbeat/issues/109)~~ Done. A tap in MORE, including Note, Arm / Stand down / Start recording and the StageMix answers, now leaves the operator on MORE; the tab moves only when they tap a tab button. Revisit if it turns out to be the wrong call once used in a game.
- ~~[#9](https://github.com/misnow1/tacet-downbeat/issues/9)~~ Done, at its new scope: the standing target only. `fader.presets` (the first entry is the default, shipping `[0.0, -3.0, -6.0]`) and `fader.max_target_db` (shipping 0 dB until the on-site ring-out; a preset above it refuses at load), a runtime target on the app that every open and READY's hold level follow, MORE > Target level, and a strip readout that goes amber when the target is not the default. Changing it stores a value and sends nothing, in every state; the next open uses it. The mid-song retarget ride is #128, in Game 4.
- ~~[#116](https://github.com/misnow1/tacet-downbeat/issues/116)/[#117](https://github.com/misnow1/tacet-downbeat/issues/117)/[#118](https://github.com/misnow1/tacet-downbeat/issues/118)~~ Done, together. #116: an absolute command whose send failed now un-knows the level again, instead of marking it known on a packet that never landed. #117: HANDOFF is now operator-only, so a detector can never un-know the level once Phase 2 is declared. #118: a hand-off always earns its `handed-off` entry, even when the level already reads unknown - the record a restart under StageMix could not otherwise leave.

## Proposed for Game 4

- [#128](https://github.com/misnow1/tacet-downbeat/issues/128) Retarget ride: changing the target while the fader is open rides to the new level. Split from #9, which ships the standing target alone for Game 3. It is a real fader tap (stale-checked, refused while the level is unknown), which is why it is not folded in: #9 stores a value and moves nothing.

The Game 4 milestone holds the rest: #17, #21, #42, #43, #44, #46, #47, #48,
#49, #51, #66, #73, #128, #133, #135.

## Keeping this current

Update this page in the same PR that changes the order: when an issue here
closes, when a new one jumps the queue, or when a milestone moves. Keep the
date above current. The issues stay the source of truth for *what* each one
does; if this page and an issue disagree about scope, the issue wins.
