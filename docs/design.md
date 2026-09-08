# Band Mic Automation — Design Document

**Status:** Draft / pre-implementation
**Venue:** Home football stadium, ACC
**Author:** Michael
**Last updated:** 2026-09-07

---

## 1. Problem

During home football games, ~14 microphones are placed among the marching band
in the stands. These feed the **band PA** (also "hype PA"), a system separate
from the main stadium PA.

The mics must be silenced while the ball is in play, and generally whenever the
band is not playing. Today this is done manually: an operator rides a single DCA
containing all band mics from an iPad connected to the console.

**Goal:** automate the open/close decision, with the operator supervising rather
than continuously riding.

**Non-goal:** replacing the operator. The operator remains in the room and
retains override authority.

### Why a gate or expander does not work

The stadium is loud and the crowd is loud. Any amplitude-threshold device sees
crowd noise and band as the same thing, because level is the one dimension in
which the crowd competes directly with the band. Discrimination has to come from
structure — onset simultaneity, harmonic content, inter-channel behavior — not
from level.

---

## 2. Regulatory context

The governing rule is **NCAA Football Rule 1-1-6**. Persons subject to the rules
— explicitly including bands *and audio/video/lighting system operators* — shall
not create noise or distraction that prevents a team from hearing its signals or
obstructs play. Enforcement is a dead-ball foul, 15 yards from the succeeding
spot, assessed against the team on the field.

No ACC-specific amplification language was located. If conference-level guidance
exists, it should be obtained and this section revised.

### Practical reading

- The rule is a **distraction standard, not a stopwatch**. There is no rule text
  drawing a line at a clock value.
- Widely published compliance guidance is that the band stops from the time the
  offense breaks the huddle until the snap, and plays during pregame, halftime,
  postgame, quarter breaks, and timeouts (excluding injury timeouts).
- The referee has authority to regulate noise that interferes with the conduct
  of the game. Enforcement is one official's judgment, and a warning
  effectively always precedes a flag.

### Consequence for the design

**The band director is the primary compliance layer, not the audio operator.**
The director is bound by the same rule. If the band is playing, the legal
judgment has already been made.

Therefore the system is *not* deciding whether the band may play. It is tracking
whether the band **is** playing, and amplifying accordingly — with a fast,
reliable close so that a band mistake is not amplified into an operator
violation.

### Observed: play clock is not a valid interlock

Field observation (2026-09-05) confirmed the band playing while the play clock
ran, with no violation called, because the teams were not yet at the line of
scrimmage.

The play clock starts at ready-for-play and runs 40 seconds. Most of that
window — huddle, substitution, walking up — is legal band time. The prohibited
window is roughly the last 10–15 seconds. **A hard interlock on "play clock
running" would silence the band for nearly the entire play cycle** and would be
substantially worse than current manual operation.

---

## 3. Existing signal path

| Element | Detail |
|---|---|
| Console | Yamaha DM7C, 7th-floor press box control room |
| Stage box | Rio3224-D3 under the band bleachers, fiber to console |
| Control | DM7 iPad app over stadium WAPs |
| Network | Control VLAN and Dante VLAN; wired ports near the Rio, configurable either way or as a trunk |
| Console GPIO | Exists, but physically remote and awkward to reach |

### Input list (14 band mics + 2)

Order as patched:

1. Cymbals — band center front, closest to the conductor stand
2. Spare — **currently unused; see §7**
3. Snares
4. Bass drum(s)
5. Sax 1
6. Sax 2
7. Clarinets/flutes 1
8. Clarinets/flutes 2
9. Horns 1
10. Horns 2
11. Horns 3
12. Horns 4
13. Front line near — mostly trumpets
14. Front line far — mostly trumpets

Plus 2 additional channels feeding the same PA, used **pregame only**, outside
the band DCA.

### Physical and time alignment

- Band is ~330 people, ~20 ft deep, very wide.
- Snare mic ~6 ft from conductor mic; bass drum mic ~8 ft.
- Channel delays are already applied so arrivals match a virtual listener dead
  center in front of the band.
- Band PA sits just behind the front plane of the band. **Band feed: 0 ms**
  (never adjusted, not deliberately aligned). Hype music path: 120 ms, aligned
  to the main PA.
- Main PA is in the scoreboard, behind a further set of stands **behind** the
  band.

### Acoustic notes

- A 20 ft deep ensemble self-smears by roughly 18 ms front to back, and the mics
  are distributed through that same depth.
- Sound travels roughly 1 ft/ms.

---

## 4. Current operating practice (the behavior to reproduce)

- Two states only: DCA at **0 dB** and **off**. Levels were formerly reduced
  when the crowd thinned; current head coach prefers loud, so 0 dB is standard.
- **Open fast** on the conductor's whistle or the drum major starting.
- **Close slow** — roughly a 2 second fade.
- If the first phrase is missed, the operator brings the mix up more slowly to
  disguise the late entry.
- Slamming it shut is rare, and only happens on noticing a late close.
- The same slow fade is used for *every* stop reason — end of song, breath mark,
  missed cue, injured player. **The system never needs to classify why the music
  stopped.**
- The true "stop" cue is visual: the conductor's arms come down. This is
  unavailable to any audio-based system.
- The MD is on comms with the stage manager, but that feed is not accessible. No
  usable out-of-band signaling exists.

### Known hard cases

**Drums-first entrances.** The drumline frequently starts a song and the
conductor whistles the rest of the band in a few measures later. This is common,
not exceptional.

**The touchdown sequence.** Band plays full out after a touchdown, drops to
quiet under the PAT, then comes back up loudly if it's good. The band does not
stop — it gets quiet. Any level-based measure reads the quiet section as a stop
and begins closing, precisely when the mics are most needed and when
band-to-crowd ratio is worst.

**Ragged stops.** When the band stops because a play is starting, it typically
ravels out over a couple of seconds as the band works out what's happening.

**Halftime exodus.** The band begins leaving the stands with about 5:00 left in
the second quarter. During that window the ensemble is partial and dwindling —
the worst case for any consensus-based measure.

**Pregame/halftime duties vary by game.** Two marching bands trade off; typically
the other band does pregame on the field and this band does halftime on the
field.

---

## 5. Architecture

### 5.1 Compute

A laptop or small PC. Two viable locations:

- **Press box** — climate controlled, serviceable mid-game, closer to the
  scoreboard network.
- **Under the band bleachers** — space is ample; protected from direct weather
  but subject to heat and cold; stored in a closet between games, so full
  weatherproofing is not required.

Press box is the current lean, primarily for mid-game serviceability and
scoreboard network access.

### 5.2 Audio input

AES67 is **not** enabled on the Dante domain and is out of scope.

Options, in order of preference:

1. **Dante AVIO USB-C adapter** — class compliant, so it works on Linux without
   a DVS license or a Windows host to babysit.
2. **DVS** — license already owned; Windows/Mac only. Already the tool for
   multitrack capture regardless.
3. Analog split into a USB interface — avoided, as it introduces another gain
   stage to manage.

**Channel count matters.** Summing to one or two Dante channels on the console
is attractive for simplicity, but the detector needs per-channel access for
onset simultaneity and inter-channel consensus. Plan for individual channels, or
purpose-built sub-mixes, not a single sum.

Phase 1 additionally needs one **post-DCA reference channel** carrying the
band PA feed (§9). Budget 14 + 1.

**Tap pre-delay.** The console's alignment delays exist to serve the mix. For
detection, earliest arrival wins, since there is no lookahead available. Tapping
pre-delay recovers roughly 7 ms on the bass drum. The detector can re-apply the
known offsets in software for measures that need time-coherent channels.

### 5.3 Console control

The DM7 supports MIDI and OSC, and Yamaha publishes a DM7-specific spec — *DM7
Series OSC Specifications*, V1.1.0, July 2025 — covering connection setup and
the full parameter list. A copy, with an extracted text version for grep, is in
`docs/vendors/yamaha/`. Console GPIO is therefore not required.

Transport: **UDP port 49900**, addressed to the console's *For Mixer Control*
IP. Up to **four** OSC remote controllers may be connected to one console, so
the box and the iPad coexist without displacing each other.

Address format, and the only control this system needs:

```
/yosc:req/<Action>/<Parameter ID>/<X>/<Y> <value>
/yosc:req/set/MIXER:Current/DCA/Fader/Level/<dca> <value>
```

`<value>` is an integer in hundredths of a dB — `0` = 0 dB, `-2000` = −20 dB,
`-32768` = −∞. Range −32768…1000. There are 24 DCA groups.

#### Faders only, never mutes

**The band mics feed other mixes pre-fader and post-mute.** Muting — either the
channel ON key or the DCA ON key — sits upstream of the pre-fader send tap, and
would pull the band out of those other mixes as well. A fader does not: sends
tap ahead of the fader, and a DCA only scales the channel fader.

So this system moves **`DCA/Fader/Level` and nothing else.** Never
`DCA/Fader/On`, never `InCh/Fader/On`, never `InCh/Fader/Level`. The constraint
costs nothing, because `-32768` (−∞) is a complete close on its own — the mute
was never needed.

It also contains the failure mode. If the box dies mid-fade and parks the DCA at
an intermediate level, only the band PA is affected. Every pre-fader send to the
other mixes is untouched, and the operator still has the DCA on the iPad.

#### OSC is write-only

**This corrects an earlier assumption in this document.** V1.1.0 defines only
`set`, plus `event` and `ssrecallt_ex` for scene recall. There is no `get`, no
subscribe, no notify, and nothing describing the console transmitting anything.
All 169 parameter rows in §2.1 of the spec are `set`.

The box therefore cannot:

- read back the DCA's actual level
- see iPad fader moves
- confirm that the console acted on a command

Consequences are handled in §5.5 — the UI shows *commanded*, not *confirmed* —
and in §9, where Phase 1 ground truth comes from a post-DCA reference channel
rather than from OSC. There is no contention over authority only in the weak
sense that the console accepts the last write from any controller; nothing
arbitrates, and neither side can see the other.

The DM7 also speaks MIDI, and Yamaha consoles have historically emitted
parameter changes as SysEx. Whether the DM7 does, and whether it would give
readback, is unverified — it needs the MIDI data format document, which is not
the OSC spec. See §7.

### 5.4 Game data (RTD)

Scoreboard and PA were replaced by Daktronics this year. See §8 for the specific
ask.

RTD is used as **context, not interlock**:

- **Positive permissives** — clock stopped, quarter breaks, timeouts, halftime.
  Windows where the band is unambiguously clear; the detector can relax.
- **Play-clock-low caution** — inside roughly 15 seconds, bias toward a faster
  release, so a trailing-off band closes promptly rather than hanging open into
  the snap. A bias, not a gate.
- **Halftime exodus arming** — Q2 under 5:00 triggers the stand-down prompt.
- **Logging** — every fader move timestamped against down, distance, game clock
  and play clock. This is the only way to answer "were we open during a play"
  after the fact.

### 5.5 Operator interface

A **web UI** served from the box over the control VLAN. Works on the iPad,
phone, or laptop; nothing to install, nothing extra to maintain.

Minimum during-game requirements:

- Current DCA level as **last commanded by the box**, labelled as commanded
  rather than confirmed. There is no readback, and an iPad move will not
  appear here (§5.3)
- **Open** and **fade out** buttons
- Plain-language "why" line — what state the machine is in and what put it there
- Recording state, which unlike the fader **is** confirmed (§5.9). Commanded and
  confirmed values must be visually distinct; never render them alike

Full telemetry is interesting but not required for game operations.

### 5.6 Annotation

The same UI carries one-tap **event annotation**, timestamped against the same
clock as audio, RTD, and OSC state. §5.9 describes how that shared clock is
obtained.

This is not a convenience feature. Most band state is **not recoverable after
the fact** — it is not in the multitrack, not in RTD, and not in the fader
moves. A game that is not annotated live is permanently unlabeled. Everything
else captured in Phase 1 could in principle be reconstructed later; this cannot.
The marginal cost is low, since the UI already exists for the override buttons.

Initial event vocabulary (extend freely — unknown-unknowns are the point):

- Band enters stadium (pregame)
- Band enters stands — *note: they enter from the far end of the stadium*
- Band exits stands (halftime exodus)
- Band returns to stands
- Band exits stadium
- Other band on field / off field
- Drumline-only cadence started
- Touchdown sequence started
- Detector was wrong — false open
- Detector was wrong — missed entrance
- Free-text note

Plus, standing in for RTD until it exists (§5.4), the game-timing events an
operator can see from the box: quarter start and end, halftime, and the
last-two-minutes window.

Two-tap maximum, no typing required for the common cases. Anything requiring
attention mid-game will not get logged.

### 5.7 Network resilience

Stadium Wi-Fi degrades as the stands fill; 2.4 GHz is absorbed by human bodies,
and the venue holds ~60,000 of them. The current control path depends entirely
on that link.

A wired box on the control VLAN speaking OSC is **strictly more reliable than
the iPad, before it detects anything at all**. See §9 — this is a worthwhile
standalone deliverable.

802.11ah / HaLow is a reasonable redundant path — sub-GHz penetrates bodies far
better, and the payload is a few hundred bytes per second. Redundant path, not
primary.

### 5.8 Platform and hardware

**Decision: develop on macOS with the existing DVS license. No hardware purchase
before Phase 1 is complete.**

The capture job and the live job have opposite requirements, and only one needs
hardware.

| | Phase 1 (capture + development) | Phase 2 (live detector) |
|---|---|---|
| Real-time? | No — writing to disk | Yes |
| Latency matters? | No | Marginally (see below) |
| Platform | macOS + DVS (owned) | Linux target; macOS acceptable for season one |
| Hardware needed | **None** | TBD — depends on Phase 1 findings |

Detector development happens **offline against WAV files**. No audio interface,
no Dante, no real-time path. A Linux VM is well suited to this precisely because
it never touches audio. macOS also matches existing development practice for
other projects.

#### Latency is not the reason to buy an interface

Dedicated hardware beats DVS by a few milliseconds, but that is not the binding
constraint. Onset detection needs an analysis window regardless, the state
machine needs to decide, and the fader ramp takes tens of milliseconds after
that. DVS overhead disappears into that budget.

The real arguments for an interface are **Linux support and headless
reliability**, not speed.

#### Linux + Dante is thinner than expected

- **RME Digiface Dante** — 64 ch, the obvious candidate. RME advertises Windows
  and macOS drivers only; at least one retailer lists class-compliant mode among
  its features. **Verify before purchase:** (a) that CC mode exists on the Dante
  variant, and (b) what channel count survives in CC mode.
- **Precedent** — RME Digiface *USB* CC mode is reported working on Linux, with
  ALSA enumerating 32/34 channels, and RME's manual now includes a Linux
  screenshot. Different product, but encouraging.
- **Caution** — CC mode is a firmware flash, and reverting has reportedly given
  users trouble.
- **Dante AVIO USB-C** — 2 ch, class compliant, cheap, works on Linux today.

#### Staged approach

1. **Phase 0** needs no audio at all. Build the OSC control path, web UI, and
   logging with nothing connected.
2. **Prove the live loop on Linux with a Dante AVIO USB-C.** Two channels
   against a single console submix is enough to validate OSC control, state
   machine, RTD ingest, and logging end to end.
3. **Buy the multichannel interface only after Phase 1** determines whether the
   detector needs 14 raw channels or a smaller set of purpose-built submixes.
   Buying earlier means guessing at the requirement.

#### Season-one risk split

Running the live box on macOS with DVS for the first season is acceptable and
possibly preferable — it is a proven path, and it avoids debugging a new
detector and a new audio stack simultaneously. Migrate to Linux once the
detector has stopped changing weekly.

### 5.9 Recording and annotation transport (Reaper)

Recording is done in **Reaper**, with DVS as its audio device. Reaper also
accepts OSC control, and for now runs on the same machine as the box, so the
control connection is localhost. Treat the endpoint as configuration, not a
constant — the Phase 2 Linux move separates them.

**Why this matters more than convenience.** §9 requires four streams on a common
clock, which normally means timestamp discipline and a drift-correction pass. A
Reaper marker sits at a sample position in the same project as the audio, so
annotation and audio are aligned *by construction* and there is nothing to
correct. It also delivers much of the deferred review timeline in §9, because
Reaper already is a scrubbable timeline with the audio and the marks on it.

**The box's log is the source of truth; markers are a derived view.** Every
annotation is appended to the box's own log as it happens. Markers are mirrored
into Reaper from that log and can be regenerated from it at any time, given the
record-start anchor. If Reaper crashes or the mirroring script is not loaded,
the convenient view is lost and the data is not.

This also settles marker naming. Reaper's built-in insert-marker action yields
either an unnamed marker or a modal dialog, and a modal dialog mid-game is
unusable. Mirroring from the log instead keeps the event vocabulary in the box,
where §5.6 requires it to stay extensible.

Conventions:

- **Regions for spans, markers for instants.** Quarters, halftime exodus,
  last-two-minutes and band-in-stands are intervals; a fader move is a point.
- **Parseable names** — a delimited prefix (`FDR|up-whistle`, `GAME|q3-start`)
  so the later read is a split rather than a regex.

**Transport control is deliberately incomplete.** The UI arms and starts
recording; it offers no stop. Each home game is a single irreplaceable sample,
and a stop button does not belong on a screen being tapped by someone watching a
field. Stopping is done deliberately, in Reaper.

Unlike the console, Reaper's OSC **is** bidirectional, so record and transport
state are genuinely confirmed. The UI must show that distinction rather than
blur it: recording is *confirmed*, fader position is only *commanded* (§5.3).

---

## 6. Detection design

### 6.1 Latency budget

The band PA cannot take meaningful lookahead. Its alignment reference is the
**acoustic band playing a few feet behind the speakers**, not another
loudspeaker. Added delay becomes slapback against the live source and would drag
the band's tempo. 150–250 ms of lookahead is off the table.

However: the ensemble already self-smears by ~18 ms, so **10–20 ms is
available** and unlikely to be perceptible. That is not enough to *decide*, but
it is enough to shape the opening ramp so it starts before the transient reaches
the output — the difference between a first note that arrives and one clipped at
the front.

To be verified live. Note the hype path would go to 135 ms; the two paths may
need independent delay.

**The detector must be causal and fast.**

### 6.2 Measures

**Unison onset (open trigger).** A cadence begins with the whole line hitting
together — often a stick click count-off, then a unison downbeat across snares
and bass drums. Simultaneous onsets across mics 6–8 ft apart, played by
independent humans, indicate ensemble intent. One person noodling cannot produce
it. Available on the **first hit**, which periodicity is not: establishing that
onsets are periodic takes several onsets — a second or more at 120 bpm.
Periodicity therefore *confirms and holds*, it does not open.

**Whistle detection (open trigger and confirmation).** Two independent
discriminators against officials' whistles:

- *Pitch* — the band whistle has a distinct, stable fundamental. Measure it once
  from multitrack, then match a narrow band around a known center. Officials'
  whistles are typically pealess and warbling, which looks quite different.
- *Grid position* — the conductor whistles **in tempo**, on a beat, as a
  count-in. Once the drumline establishes a pulse, a whistle-shaped event on the
  grid is the conductor; one between beats is an official. Essentially free if
  the drumline is already being tracked.

Either is sufficient; both are cheap.

**Ordering note.** The whistle is *not* the primary trigger. When the whole band
starts together, the whistle leads. When the drums start first, the drums open
the fader and the whistle arrives later as confirmation while already open. The
whistle is a high-confidence event that either opens or reinforces, depending on
current state.

**Envelope correlation (hold).** Band-limit each channel, take a short-time
envelope, and measure how the 14 envelopes move together. Band playing = 14
envelopes rising on the same beat. Crowd = 14 envelopes drifting independently.

Do **not** correlate raw waveforms. Mics metres apart are not phase-coherent
above a few hundred Hz even on the same note. Envelopes only.

**Tonality / harmonic coherence (hold — critical).** Coherent musical pitch
content across channels. This is the measure that **survives the PAT
diminuendo**, because the band never stops during the touchdown sequence — it
gets quiet. Pitch structure persists when level collapses. Envelope measures
cannot do this.

**Arrival-order direction cue (rejection).** The main PA is behind the stands
behind the band, so main PA content reaches the rear mics *before* the front
mics. Band sources do the reverse. This discriminates against main PA leakage
into 14 open mics, which to a naive detector looks exactly like the band
starting. Crowd, being diffuse, shows no consistent propagation pattern in
either direction.

### 6.3 State machine

```
STANDING DOWN ──(operator arms)──> IDLE
     ^                               |
     |                     unison onset / whistle /
     |                     broad simultaneous entrance
     |                               v
     |                            OPEN  <──any qualifying trigger──┐
     |                               |                             |
     |                    loss of consensus                        |
     |                               v                             |
     └───────────────────────────RELEASING ────────────────────────┘
                                (2 s fade)
```

- **STANDING DOWN** — boot state. Band not in the stands. Pregame with the other
  band on the field, halftime, exodus.
- **IDLE** — armed, band in stands, DCA closed.
- **OPEN** — DCA at 0 dB. Held by tonality and envelope consensus, reinforced by
  on-grid whistle. **Must hold through the PAT diminuendo.**
- **RELEASING** — 2 s fade. Any qualifying trigger snaps straight back to OPEN.
  This is what makes the reactive close survivable: the machine cannot see arms
  come down and will always trail the operator, and the fade absorbs that.

### 6.4 Mode handling

**Halftime exodus.** Q2 under 5:00 is available from RTD. The box **prompts**
rather than acting silently: *"Q2 under 5:00 — band leaving, stand down?"* with
a confirm button. Two reasons: it varies by game, and a system that announces
what it is about to do earns trust faster than one that silently changes
behavior. Same on return during Q3 — ask, don't assume.

**Pregame.** STANDING DOWN as the boot state covers it. The two pregame channels
are outside the DCA and unaffected; the automation only arms when the operator
confirms the band is in the stands.

---

## 7. Open items

- **The spare input (ch. 2) is a free asset.** Candidates: contact mic or
  trigger on a bass drum head; mic at the conductor stand aimed at the drum
  major for whistle capture; or a crowd-reference mic aimed deliberately *away*
  from the band for differential measurement. Decide after reviewing multitrack.
- **Separating the drumline onto its own DCA.** Not needed for mix control —
  worth considering because it would allow validating a drums-only detector
  against a season of real fader moves without changing the band's experience.
- **Confirm the 10–20 ms delay is inaudible** in the live room, and whether hype
  and band paths need independent delay.
- **Confirm the band whistle's fundamental** from multitrack.
- **Confirm DCA fader granularity.** The spec gives `min -32768 / max 1000 /
  scaling 100`, implying arbitrary hundredths of a dB, but the parameter notes
  point at Table 1 — a 43-entry list whose steps are 1 dB near unity, 2 dB from
  −10 to −30, 5 dB below that. If only Table 1 values are accepted, a 2 s close
  from 0 dB to −∞ is 32 steps, ~62 ms apart. Send `-1550` and see whether the
  console lands on −15.50 or snaps to −16.
- **Whether MIDI offers state readback**, which OSC does not (§5.3). Needs the
  DM7 MIDI data format document.
- Obtain ACC-specific amplification guidance if it exists.

---

## 8. Daktronics / scoreboard ask

*(This section is intended to be extractable for email.)*

The scoreboard and PA were replaced by Daktronics this year. Scoring is now
operated from an iPad; a serial controller was previously in place.

Daktronics **Real-Time Data (RTD)** remains available in current systems. The
All Sport Pro interface box broadcasts RTD over UDP with configurable ports per
data source, and can also drive an additional serial or USB MDP output for
supplementary fixed-digit displays. Classic RTD over RS-232 is 19200 8N1, with
packets framed by 0x16 and terminated by a checksum followed by 0x17 — a
well-documented format with open-source decoders available in several languages.

### Requested

1. **A read-only RTD feed reachable from the press box.** UDP on the scoreboard
   network is preferred over serial. This is one-way and consumes no scoreboard
   resources.
2. **The UDP port and data source configuration** for football.
3. **Confirmation that the play clock is present in the football field set**, not
   only the game clock. Ideally: a capture of the live stream using Daktronics'
   **Data Monitor** during a game, so the actual available fields can be
   inspected directly rather than assumed.
4. **A network path** from the scoreboard VLAN to the audio control VLAN, or
   provision for a second NIC in the receiving machine.

### Timing

Worth requesting **during the current punchline window**. This is a
configuration change now and a change order later.

### Reference

- Daktronics, *RTD explained in 5 minutes* —
  https://www.daktronics.com/blog/rtd-explained-in-5-minutes
- All Sport Pro interface box configuration —
  https://www.daktronics.com/web-documents/manuals/dd5092697.pdf

---

## 9. Phased rollout

### Phase 0 — Wired control path

Standalone value, independent of any detection. A wired box on the control VLAN
speaking OSC to the DM7, with a web UI exposing open / fade-out buttons and the
last commanded DCA level.

This is more reliable than the iPad on day one and provides a working fallback
for Wi-Fi failure, which is a real and recurring problem. **Ship this first
regardless of what happens to the rest of the project.**

Phase 0 also carries the annotation and recording transport of §5.9 — record
arming, the event vocabulary of §5.6, and marker mirroring into Reaper. This is
a deliberate widening of the original scope. It pays immediately: it means the
*first* captured game is annotated even though no detector exists yet, and
annotation is the one part of ground truth that cannot be reconstructed later.

**Runtime: Python 3.11+.** The choice anticipates Phase 2, where the detector —
inevitably numpy/scipy — has to share a process with fader control.

Dependencies are tiered. The **control path** — the OSC codec, the console
client and the state machine — imports only the standard library, because that
is what moves the fader with nobody available to fix it; OSC is UDP carrying a
simple binary encoding and is short enough to implement directly. Everything
outside that path, the web UI included, may take a mature pinned library rather
than hand-rolling. A test reads the imports and enforces the split.

### Phase 1 — Shadow mode

The box computes everything and **drives nothing**. The game is mixed exactly as
it is today.

Captured per game:

- DVS multitrack of all 14 channels (license already owned)
- A **post-DCA reference channel** — one additional Dante channel carrying the
  band PA feed, recorded alongside the 14 mics. Compared against the pre-fader
  mics it recovers the operator's fader moves, and **this is the ground truth
  label set**. It replaces the OSC subscription originally planned here, which
  the protocol does not support (§5.3). It is also the better measurement: it
  captures gain as actually applied to the PA, and it is the only available
  confirmation that a command reached the console at all.
- RTD stream, timestamped
- Every detector decision the box would have made
- **Live operator annotations** (§5.6) — the portion of ground truth that cannot
  be reconstructed afterward

After two or three games this yields real audio, real crowd, real touchdown
sequences, and a labeled answer key. Candidate detectors can then be replayed
against all of it offline at many times real speed, with thresholds and time
constants tuned against reality instead of guessed.

### Phase 2 — Assisted

Detector drives the DCA; operator supervises with a hand on the override. Risk
posture supports an **aggressive** tuning: no false opens have occurred to date,
the operator is in the room, and the operator would notice first if it
misbehaved. Tune to open eagerly and accept occasional early opens.

If this were unattended it would have to be timid, and timid means a slower,
more cautious open on every cue — considerably less useful.

### Phase 3 — Refinement

Season-over-season tuning from accumulated logs. Revisit the spare channel,
drumline separation, and whether any of the RTD context measures earned their
keep.

**Review timeline.** A scrubbable timeline aligning audio, detector state, band
state annotations, operator fader moves, and game clock — the tool for asking
"what happened at 8:32 in Q3 and why did it open." Deferred, but every
Phase 1 capture decision should assume it will exist, since retrofitting the
data is impossible and building the viewer later is easy.

---

## 10. Design principles

1. **The band decides legality; the machine tracks whether they're playing.**
2. **Fast triggers open, slow consensus holds, the 2 s fade closes.**
3. **The machine is worse-informed than the operator** — it cannot see arms come
   down. Every close is reactive. The fade is what makes that acceptable.
4. **Never classify why the music stopped.** Fade the same way for all of them.
5. **Announce, don't surprise.** Mode changes prompt; they don't happen silently.
6. **Structure, not level.** Level is the one dimension where the crowd wins.
7. **Faders, never mutes.** The mics feed other mixes pre-fader, post-mute.
   Muting would take the band out of those mixes too.
