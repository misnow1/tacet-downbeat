# tacet-downbeat

Automates the marching band microphone DCA on a Yamaha DM7C during home football
games. The band mics feed a PA separate from the main stadium PA and are
currently ridden by hand from an iPad.

**Read `docs/design.md` before proposing anything.** It contains the reasoning
behind every constraint below. The constraints look arbitrary without it, and
several of them contradict what would otherwise be sensible defaults.

## Current phase

**Phase 0 / 1. The detector drives nothing.**

- Phase 0 — wired OSC control path + web UI + Reaper recording/annotation
  transport. Standalone value; ships regardless.
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
  silence the band for nearly the entire play cycle. RTD is context, not gate. See
  design.md §2.
- **Must hold open through a diminuendo.** After a touchdown the band plays
  loud, drops quiet under the PAT, then returns loud. It does not stop. Any
  measure that reads the quiet section as a stop is wrong, and that section is
  where amplification matters most.
- **Faders only, never mutes.** The band mics feed other mixes pre-fader and
  post-mute. Muting the channels or the DCA would pull the band out of those
  mixes too. Move `MIXER:Current/DCA/Fader/Level` and nothing else — `-32768`
  (−∞) is a complete close, so the mute is never needed. See design.md §5.3.
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

- **Console** — Yamaha DM7C, press box. Control via **OSC**, UDP 49900. The
  protocol is **write-only** — no get, no subscribe, no notify — so the box
  cannot read DCA state or see iPad moves. The UI shows commanded, not
  confirmed. Spec and extracted text in `docs/vendors/yamaha/`.
- **Audio** — 14 band mics via Dante. **Tap pre-delay**; console alignment
  delays serve the mix, but detection wants earliest arrival. Re-apply known
  offsets in software where time-coherence is needed.
- **Game data** — Daktronics RTD over UDP, read-only. Positive permissives,
  play-clock-low release bias, halftime-exodus arming, and logging.
- **Recorder** — Reaper, DVS as its audio device, driven over OSC on localhost
  for now. Its OSC *is* bidirectional, so recording state is genuinely
  confirmed. Annotations live in the box's own log; Reaper markers are a derived,
  regenerable view. Markers align to audio by construction — that is the common
  clock. Regions for spans, markers for instants. **No stop button in the UI.**
  See design.md §5.9.
- **UI** — web, served over the control VLAN. Works on iPad, phone, laptop.
  Open/fade buttons, last commanded DCA level, plain-language "why" line, and
  annotation buttons. Commanded and confirmed values must render differently.
- **Config** — an *optional* `tacet.toml`, read by `tacet.config` and shared by
  `tacet-serve`, `verify_dm7` and `verify_reaper`, so the site values are not
  retyped as flags every game. Standard library only (`tomllib`), so it adds no
  dependency and stays importable from the control path. Precedence is
  `built-in default < config file < explicit flag`, asserted per tool. Unknown
  sections and keys are errors, never silent no-ops. **No flag that initiates
  something is ever a config key** — the file supplies addresses, not actions,
  which is why `verify_dm7 --fade/--granularity/--level` and `verify_reaper
  --listen` are excluded. Keys live in `tacet.toml.example`; the real
  `tacet.toml` is gitignored and its values belong in `docs/gameday.md`.
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

- **Python 3.11+.** The control path is standard library only; the UI and
  tooling may take dependencies. Phase 2 shares a process with a numpy/scipy
  detector, which is why the runtime is Python.
- Development and Phase 1 capture: **macOS + DVS** (license owned), recorded
  in Reaper.
- Detector development happens **offline against WAV files** — no interface, no
  Dante, no real-time path. Prefer this for all algorithm work.
- Phase 2 target is Linux; macOS is acceptable for season one.
- No multichannel hardware purchase until Phase 1 establishes the channel
  requirement.

## Data

**Recordings never go in git.** A 14-channel 24-bit/48 kHz multitrack is roughly
7 GB/hour. Store on the home lab NAS; the repo references paths, not contents.
`audio/` is gitignored.

Phase 1 captures four aligned streams, all on a common clock:

1. DVS multitrack, 14 channels, recorded in Reaper
2. Post-DCA reference channel — one Dante channel of the band PA feed. Compared
   against the pre-fader mics it recovers actual operator fader moves (**the
   ground truth labels**). OSC cannot supply these; it is write-only.
3. RTD stream
4. Live operator annotations — the portion that **cannot be reconstructed
   afterward**

Assume a scrubbable review timeline will eventually exist. Build capture so it
stays possible.

## Python standards

Non-negotiable, same status as the hard constraints above.

- **The control path takes no dependencies.** `tacet.osc`, `tacet.net`,
  `tacet.dm7` and `tacet.state` are what move the fader during a game, and they
  import nothing but the standard library. That is why OSC is hand-rolled rather
  than taken from PyPI. `tests/test_dependency_policy.py` enforces this by
  reading the imports, so it fails rather than drifts. Ask before adding
  anything here.
- **Everything else may take dependencies.** The web UI, Reaper integration,
  capture and offline analysis sit outside the control path, and a mature,
  pinned library there beats hand-rolling. Dev tooling never ships at all.
  When the detector arrives it will need numpy and scipy *and* it will move the
  fader — that tension gets resolved deliberately, not by accident.
- **Everything is type-annotated** and `mypy` runs strict. Annotations without a
  checker are decoration.
- **ruff must be clean** — lint and format both.
- **Tests are required**, and preferably written first. Constraints that matter
  get asserted directly: `test_every_packet_of_a_full_cycle_is_a_fader_level_write`
  is what actually enforces faders-only, not a comment.
- **No magic numbers or strings.** Named constants or configuration. Derive
  related values from one another (`LEVEL_MAX = 10 * UNITS_PER_DB`) and pin the
  spec's printed numbers in a test.
- **Small functions, and a pure core with a thin I/O shell.** `ramp_steps` is a
  pure generator of `(offset, level)` pairs and the async driver just executes
  them, so ramp shape is testable with no socket and no clock. Prefer that shape.
- **Inject collaborators** behind a Protocol rather than reaching for a socket
  inside a class. See `dm7.Sender`.
- ASCII only in source. A docstring containing a typographic minus is a trap for
  anyone who copy-pastes a level out of it.

### Layout and commands

`src/` layout; the package is `src/tacet/`. Python 3.11+, developed on 3.12
(`.python-version`), venv at `.venv`.

```
make install    # venv + dev tooling
make hooks      # install pre-commit
make check      # lint + typecheck + test, exactly what CI runs
make fmt        # fix what ruff can fix
```

Pre-commit runs lint and formatting only — a hook that takes a minute stops
getting used. CI (`.github/workflows/ci.yml`) runs the full suite on Linux and
macOS, on every push to `main` and on every pull request.

**Branch protection is not enabled**, so CI reports and does not block — a push
to `main` lands whether the checks passed or not. A required status check is
deferred until after Phase 0 ships. Until then a red `make check` is blocking by
convention only, so run it before committing rather than relying on the badge.

## Working notes

- **Commits go straight to `main`.** Solo repo; no branch or PR ceremony. Commit
  only when asked, and do not push unless asked.
- `docs/gameday.md` is the operating runbook: startup order, what the page
  should read before kickoff, and what each failure message means. Anything
  learned by running the system on a Saturday belongs there, not in a commit
  message. It carries the site values (console IP, DCA number, NAS path) that
  are deliberately not anywhere else in the repo.
- Vendor PDFs are encrypted and need decrypting before they can be read at all.
  See `docs/vendors/README.md` before wrestling with one.
- `reaper/tacet_mirror.lua` runs inside Reaper. Its logic **is** tested here,
  against a stubbed Reaper API (`reaper/test_tacet_mirror.lua`, `make
  test-lua`); what is not tested is whether Reaper really behaves as the stub
  pretends. Install a Lua 5.4 to run it: `brew install lua`, `apt install
  lua5.4`.
- `src/tacet/static/app.js` is the operator page's script. It is inlined into
  the page at import - the control VLAN has no route to the internet, so the
  page fetches nothing - but it lives in its own file so it can be tested:
  `tests/test_app_js.mjs`, `make test-js`, against a stubbed DOM. Same bargain
  as the Lua: the logic is tested, the browser is not. Needs node. Mind the
  escaping - the file is inlined into a Python string, and a doubled backslash
  there reaches the screen as a literal.
- `python -m tacet.verify_reaper` and `verify_dm7` are the harnesses for the two
  things that genuinely need hardware in front of them.
- Prefer offline replay over live testing. There are a limited number of home
  games per season and each one is a single irreplaceable sample.
- When tuning, tune against captured games, not intuition. Thresholds guessed in
  a quiet room do not survive 60,000 people.
- Ask before adding dependencies. This has to run reliably in a press box on a
  Saturday with no one available to fix it.
