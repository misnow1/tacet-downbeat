# tacet-downbeat

Automates the marching band microphone DCA on a Yamaha DM7C during home football
games. The band mics feed a PA separate from the main stadium PA and are
currently ridden by hand from an iPad.

**Read `docs/design.md` before proposing anything.** It contains the reasoning
behind every constraint below. The constraints look arbitrary without it, and
several of them contradict what would otherwise be sensible defaults.

## Current phase

**Phase 0 / 1. The detector drives nothing.**

- Phase 0 — wired OSC control path + web UI. Standalone value; ships regardless.
- Phase 1 — shadow mode. Compute everything, log everything, drive nothing.
- Phase 2 — assisted. Detector drives the DCA, operator supervises.
- Phase 3 — refinement from accumulated logs.

Do not write code that moves the fader autonomously until Phase 2 is explicitly
declared. Phase 0's buttons are operator-initiated and are not that.

## Hard constraints

These are settled. If a design seems to require violating one, say so and stop
rather than working around it.

- **The detector must be causal.** No lookahead beyond ~15 ms. The band PA is
  aligned to the acoustic band a few feet behind it, so added delay becomes
  slapback and drags the band's tempo.
- **Never gate on level alone.** The stadium is loud and the crowd competes
  directly on amplitude. Discrimination comes from structure — onset
  simultaneity, harmonic coherence, inter-channel behavior. A gate or expander
  has already been tried and does not work.
- **Play clock running is NOT a mute interlock.** The play clock runs 40 seconds
  from ready-for-play; most of that is legal band time. Interlocking on it would
  mute the band for nearly the entire play cycle. RTD is context, not gate. See
  design.md §2.
- **Must hold open through a diminuendo.** After a touchdown the band plays
  loud, drops quiet under the PAT, then returns loud. It does not stop. Any
  measure that reads the quiet section as a stop is wrong, and that section is
  where amplification matters most.
- **Fast open, ~2 s fade close.** Asymmetric by design. A missed downbeat is
  unrecoverable; a slightly late close is absorbed by the fade.
- **Never classify why the music stopped.** End of song, breath mark, missed
  cue, injured player — same fade for all. The system only decides whether sound
  is present.
- **Correlate envelopes, never raw waveforms.** Mics are metres apart and not
  phase-coherent above a few hundred Hz.
- **Fail safe and fail visible.** On any fault, leave the operator in control
  and say so in the UI. Silent degradation is worse than an obvious stop.

## Design principles

1. The band director decides legality; this system only tracks whether they are
   playing. It is not a compliance interlock.
2. Fast triggers open, slow consensus holds, the fade closes.
3. The machine is worse-informed than the operator — it cannot see the
   conductor's arms come down. Every close is reactive. The fade is what makes
   that acceptable.
4. Announce, don't surprise. Mode changes prompt; they never happen silently.
5. The operator is a supervisor, not a fallback. They keep override authority
   permanently.

## Architecture summary

Full detail in design.md §5.

- **Console** — Yamaha DM7C, press box. Control via **OSC** (Yamaha publishes a
  DM7-specific spec). Bidirectional: subscribe to real DCA state rather than
  assuming it, so iPad and web UI never fight.
- **Audio** — 14 band mics via Dante. **Tap pre-delay**; console alignment
  delays serve the mix, but detection wants earliest arrival. Re-apply known
  offsets in software where time-coherence is needed.
- **Game data** — Daktronics RTD over UDP, read-only. Positive permissives,
  play-clock-low release bias, halftime-exodus arming, and logging.
- **UI** — web, served over the control VLAN. Works on iPad, phone, laptop.
  Open/fade buttons, DCA level readback, plain-language "why" line, and
  annotation buttons.
- **AES67 is not available** on this Dante domain and is out of scope.

## State machine

```
STANDING DOWN ──(operator arms)──> IDLE ──(trigger)──> OPEN
                                     ^                   |
                                     |            loss of consensus
                                     |                   v
                                     └──────────── RELEASING (2s fade)
                                                          |
                                            any trigger snaps back to OPEN
```

- **STANDING DOWN** is the boot state. Pregame, halftime, band not in stands.
- Triggers that open: drumline **unison onset**, conductor **whistle**, or a
  broad simultaneous ensemble entrance.
- Whistle is not the primary trigger. When the whole band starts together the
  whistle leads; when the drums start first they open it and the whistle
  arrives later as confirmation.
- Officials' whistles are rejected two ways: distinct fundamental, and
  off-grid timing (the conductor whistles in tempo).

## Platform

- Development and Phase 1 capture: **macOS + DVS** (license owned).
- Detector development happens **offline against WAV files** — no interface, no
  Dante, no real-time path. Prefer this for all algorithm work.
- Phase 2 target is Linux; macOS is acceptable for season one.
- No multichannel hardware purchase until Phase 1 establishes the channel
  requirement.

## Data

**Recordings never go in git.** A 14-channel 24-bit/48 kHz multitrack is roughly
7 GB/hour. Store on the home lab NAS; the repo references paths, not contents.
`data/` is gitignored.

Phase 1 captures four aligned streams, all on a common clock:

1. DVS multitrack, 14 channels
2. OSC subscription — actual operator fader moves (**the ground truth labels**)
3. RTD stream
4. Live operator annotations — the portion that **cannot be reconstructed
   afterward**

Assume a scrubbable review timeline will eventually exist. Build capture so it
stays possible.

## Working notes

- Prefer offline replay over live testing. There are a limited number of home
  games per season and each one is a single irreplaceable sample.
- When tuning, tune against captured games, not intuition. Thresholds guessed in
  a quiet room do not survive 60,000 people.
- Ask before adding dependencies. This has to run reliably in a press box on a
  Saturday with no one available to fix it.
