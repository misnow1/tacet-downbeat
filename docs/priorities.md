# Priorities

What to build next, in order, and why. The issues hold the detail and the
decisions; this page only holds the order, which otherwise lives in nobody's
head but the last conversation's.

**Last updated 2026-09-16.** #6 is built (the state machine, the `up-ready` and
`score-reversed` keys, and the config for the hold level and its ride); #89 and
#14 were already done. #6 now slots ahead of #5, as flagged on 2026-09-15: the
fader column is laid out once knowing every button that lives in it.
Decisions on the remaining issues are recorded as comments on #5, #9, #12 and
#19, and on #6 and #89 themselves.

## Game 3 (2026-10-02)

In build order. Each step says why it comes where it does.

| # | Issue | Why here |
|---|---|---|
| ~~1~~ | ~~[#89](https://github.com/misnow1/tacet-downbeat/issues/89) Let the operator drive the fader while STANDING DOWN~~ | Done. A forgotten Arm is no longer a missed downbeat, which is the hazard #19 was designed around |
| ~~2~~ | ~~[#14](https://github.com/misnow1/tacet-downbeat/issues/14) Vocabulary: field goal, cannon, and what the band plays for~~ | Done, except the fader keys: `up-ready` and `score-reversed` belonged with #6, which gives them a state to move to |
| ~~3~~ | ~~[#6](https://github.com/misnow1/tacet-downbeat/issues/6) READY, then go~~ | Done. A new state between IDLE and OPEN: the operator rides to a hold level short of target and waits to see whether the band starts, before committing with the ordinary up buttons or abandoning with Score reversed |
| 4 | [#5](https://github.com/misnow1/tacet-downbeat/issues/5) Button layout for thumbs | Game 2's biggest page failure was scrolling and a grid that moved under the thumb. The fixed status strip and prompt slot are where #12 and #19 put things, and #6 is what the fader column's buttons are now all known |
| 5 | [#12](https://github.com/misnow1/tacet-downbeat/issues/12) StageMix handoff and take-back | Removes the only known path to a full-level blast: a ramp from a level the box only believes in |
| 6 | [#19](https://github.com/misnow1/tacet-downbeat/issues/19) Prompt arm / stand down from band annotations | Small once #5's prompt slot exists, and it keeps Phase 1's duty labels right from game 3 on |

Alongside, not in the build order above:

- [#13](https://github.com/misnow1/tacet-downbeat/issues/13) gameday.md: game 3 patch list, network plan, stale sections. Needs site information; must land before game day regardless. Includes setting `capture.audio_path` and `capture.channels` in the box's `tacet.toml` (#53).
- [#15](https://github.com/misnow1/tacet-downbeat/issues/15) design.md: pre-open and cannon hard cases, now also READY (#6) as a fader-up-no-band span Phase 1 has to be able to exclude. Documentation; any time.
- [#18](https://github.com/misnow1/tacet-downbeat/issues/18) Research: DM7 fader readback. Research; if the console turns out to report the fader, #12 gets simpler, so worth an early look.

## Proposed for Game 4

- [#9](https://github.com/misnow1/tacet-downbeat/issues/9) Preset target levels. The safest of the operator page issues to defer: 0 dB is standard practice (design.md 4), and `+3` means nothing until the on-site ring-out. Still in the Game 3 milestone until the maintainer moves it.

The Game 4 milestone holds the rest: #17, #21, #42, #43, #44, #66.

## Keeping this current

Update this page in the same PR that changes the order: when an issue here
closes, when a new one jumps the queue, or when a milestone moves. Keep the
date above current. The issues stay the source of truth for *what* each one
does; if this page and an issue disagree about scope, the issue wins.
