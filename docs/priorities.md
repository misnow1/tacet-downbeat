# Priorities

What to build next, in order, and why. The issues hold the detail and the
decisions; this page only holds the order, which otherwise lives in nobody's
head but the last conversation's.

**Last updated 2026-09-15**, after the UX review of the operator page issues.
Its decisions are recorded as comments on #5, #9, #12, #14 and #19, and in #89.

## Game 3 (2026-10-02)

In build order. Each step says why it comes where it does.

| # | Issue | Why here |
|---|---|---|
| 1 | [#89](https://github.com/misnow1/tacet-downbeat/issues/89) Let the operator drive the fader while STANDING DOWN | Small, and it changes the hazard #19 is designed around: a forgotten Arm stops being a missed downbeat |
| 2 | [#14](https://github.com/misnow1/tacet-downbeat/issues/14) Vocabulary: field goal, cannon, up-for-score | Small, and it fixes the set of buttons #5 lays out. Takes #6's `up-for-score` key with it |
| 3 | [#5](https://github.com/misnow1/tacet-downbeat/issues/5) Button layout for thumbs | Game 2's biggest page failure was scrolling and a grid that moved under the thumb. The fixed status strip and prompt slot are where #12 and #19 put things |
| 4 | [#12](https://github.com/misnow1/tacet-downbeat/issues/12) StageMix handoff and take-back | Removes the only known path to a full-level blast: a ramp from a level the box only believes in |
| 5 | [#19](https://github.com/misnow1/tacet-downbeat/issues/19) Prompt arm / stand down from band annotations | Small once #5's prompt slot exists, and it keeps Phase 1's duty labels right from game 3 on |

Alongside, not in the build order above:

- [#6](https://github.com/misnow1/tacet-downbeat/issues/6) Pre-open on a score: the `up-for-score` ride-in. Its key arrives with #14; check how much the ride-in work (#8) already covers before starting.
- [#13](https://github.com/misnow1/tacet-downbeat/issues/13) gameday.md: game 3 patch list, network plan, stale sections. Needs site information; must land before game day regardless. Includes setting `capture.audio_path` and `capture.channels` in the box's `tacet.toml` (#53).
- [#15](https://github.com/misnow1/tacet-downbeat/issues/15) design.md: pre-open and cannon hard cases. Documentation; any time.
- [#18](https://github.com/misnow1/tacet-downbeat/issues/18) Research: DM7 fader readback. Research; if the console turns out to report the fader, #12 gets simpler, so worth an early look.

## Proposed for Game 4

- [#9](https://github.com/misnow1/tacet-downbeat/issues/9) Preset target levels. The safest of the operator page issues to defer: 0 dB is standard practice (design.md 4), and `+3` means nothing until the on-site ring-out. Still in the Game 3 milestone until the maintainer moves it.

The Game 4 milestone holds the rest: #17, #21, #42, #43, #44, #66.

## Keeping this current

Update this page in the same PR that changes the order: when an issue here
closes, when a new one jumps the queue, or when a milestone moves. Keep the
date above current. The issues stay the source of truth for *what* each one
does; if this page and an issue disagree about scope, the issue wins.
