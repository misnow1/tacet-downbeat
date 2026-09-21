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

**Pre-open on a score.** After a score the crowd cheers, and the operator rides
the fader up during the cheering so it is already up when the band hits: the
READY state and the `up-ready` button (#6, §6.3). The hard part is that the
fader is up with no band. A detector trained on where the fader was would learn
that crowd cheering means open, the amplitude failure §1 describes; keeping the
two apart is what §9 is for. As an operator act it is harmless, since the ball
is dead. If the DJ or the other band plays instead (mostly Q4), it is pulled
back quietly under the cheering with Score reversed, the ordinary 2 s fade. The
box never does this by itself, in any phase (§6.4).

**The cannon.** It fires on a touchdown or a field goal: a broadband
simultaneous onset on all 14 mics, the same shape as the ensemble entrance §6.2
opens on. The detector must **not** open on it. That is a design goal,
discriminated and measured offline against captured scores (§7), not something
to be accidentally right about. If it slips the cost is small: the ball is
dead, and the 2 s fade closes it. That harmlessness does not hold in the state
the cannon most often lands in, which is the reason to discriminate it anyway.
It fires on exactly the scores that put the operator into READY, and from READY
any trigger commits straight to OPEN from wherever the ride had got to
(`tacet.state`, §6.3), a snap when the trigger is the detector's, not a ride.
So a mistaken one there is a fast open from a fader already part-way up, with no
band playing. There is no cannon button, deliberately - it fires in the busiest
ten seconds of the night - so its instant is found offline, near a logged
`touchdown` or `field-goal` (#14, §9).

**Scoring songs repeat.** The band has a small repertoire, and the same song
follows a field goal and a touchdown. The same music, many takes, across games,
is the cheapest template available for testing hold-open offline, in particular
whether a hold measure survives the PAT diminuendo above. That is the limit of
it: offline test material only, never a runtime song classifier. The system
decides whether sound is present and never why it stopped (§10, principle 4);
recognising which song is playing is a different system with a different
failure mode.

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
and in §9, where Phase 1's fader-state labels come from a post-DCA reference
channel rather than from OSC. There is no contention over authority only in the
weak sense that the console accepts the last write from any controller; nothing
arbitrates, and neither side can see the other.

The DM7 also speaks MIDI, and Yamaha consoles have historically emitted
parameter changes as SysEx. Whether the DM7 does, and whether it would give
readback, is unverified — it needs the MIDI data format document, which is not
the OSC spec. See §7.

#### What the box believes about the fader

The box cannot read the fader back, so the level it has is the last one it
wrote, never a measurement. That belief can be wrong two ways, and they are the
same fact reached twice: at boot, where nothing has been written yet and the
-inf the client starts from is only a convenient number; and after the operator
hands the DCA to StageMix, where another interface can move it. `tacet.state`
tracks one flag, `level_known`, and deliberately does not record which of the
two it was - the annotation log's `handed-off` entry does that. Handing off is
the operator's word: it is refused from a detector whatever `allow_detector`
says (#117), since it is a claim about who is holding the fader, not about the
audio, and the detector hears the band, it cannot see an iPad in somebody's
hands.

What the flag gates is absolute against relative:

- An **absolute** command puts the fader in one known place whatever the box
  believed: the snap open to the current target, which defaults to unity, and
  the operator's instant close to -inf. Neither waits on the belief, and both
  make the level known. The instant close is available in every state; a snap
  open is too, except when the box already knows the fader is open and nothing
  is pending, where it would only repeat what is true. An absolute command whose send failed puts the belief back: the
  shell reports the failure, the level reads unknown again, and the stall says
  to tap it again (#116). That is only the half the box can see. The protocol
  has no acknowledgement (#18), so a packet that leaves the box and is simply
  lost is indistinguishable from one that landed, and the level then reads
  known when it is not. Nothing in this box can close that half; only console
  feedback could. A failed absolute command still logs `took-back` for the
  belief it briefly held, immediately followed by `move-failed`: read that
  pair together, since the `took-back` alone did not survive its own packet.
- A **relative** command ramps from the believed level: the 2 s fade, READY's
  ride to the hold level, and the slow open. Each is refused while the level is
  unknown, with the reason on the page. Refused, not queued - a tap held back
  and run later runs on a belief nobody has looked at since. The page also
  greys the four column buttons that make these moves, in place, so the refusal
  is rarely reached.
- `ARM` is refused too. It sends nothing and calls the fader closed, which only
  a known level makes true.
- `STAND_DOWN` is never refused for want of a known level. While the level is
  unknown it changes the duty state at once and sends nothing, since there is
  no fade to wait for.

READY is the one state that needs a control of its own. It has no fast form -
there is no snap to the hold level, only a ride - so from an unknown level it
can only be reported, never driven to. That is what "It's at ready level" is on
the page: a belief correction that writes no packet.

After a restart while StageMix really has the DCA, the level is already
unknown, so the hand-off changes nothing on the box - but the tap still writes
its `handed-off` entry, which is the record Phase 1's control-authority labels
need (#118). The box's state was already right; only the log entry would
otherwise have been lost.

If the console turns out to report the fader (#18), "who has control" and "is
the position known" become two independent facts, and this collapses to the
second of them. #103 covers the operator-facing mechanism in detail.

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

Implemented in `tacet.web`, with the logic in `tacet.app` so the operator flow
is testable without a server. Run it with `tacet-serve`.

The site values it needs — console IP, DCA number, Reaper host, queue path —
are flags, and optionally an `tacet.toml` read by `tacet.config` and shared with
`verify_dm7` and `verify_reaper`. The file is a convenience, never a
requirement: a flag always beats it, so nothing about the operating path depends
on a file being present or correct. Deliberately, no flag that *initiates*
anything is a config key; the file supplies addresses, not actions. Keys are
listed in `tacet.toml.example` and the runbook is docs/gameday.md.

Minimum during-game requirements:

- Current DCA level as **last commanded by the box**, labelled as commanded
  rather than confirmed. There is no readback, and an iPad move will not
  appear here (§5.3)
- **Open** and **fade out** buttons
- Plain-language "why" line — what state the machine is in and what put it there
- Recording state, which unlike the fader **is** confirmed (§5.9). Commanded and
  confirmed values must be visually distinct; never render them alike
- **The state of the link to the box itself.** Everything above is a claim the
  page can only make about the last thing it was told, so a link that has
  stopped delivering turns the whole screen into a lie that looks correct. The
  box therefore sends a keepalive the page can see, and the page distinguishes
  three states: connecting, delivering, and open-but-silent-too-long. An open
  socket is not evidence of a working link (§5.7)
- **Something that says the link is fine, not only that it is broken.** A
  banner alone cannot: its absence is also what a page that has stopped
  executing looks like, which on this link is precisely the ambiguity worth
  removing. A counter of seconds since the last frame does it, and does it as
  two separable facts - digits that change every second are the renderer
  proving it runs, and digits dropping back to zero are the link proving it
  delivers. Neither says anything about the console, and nothing can: OSC is
  write-only and a datagram into a black hole succeeds (§5.3)
- **The screen has to stay on.** A locked iPad is no interface at all. The
  browser API for holding it awake requires a secure context and the page is
  served over plain HTTP, so the page asks for the lock where it can and
  otherwise says which device setting to change. gameday.md carries the step
- **Duty state, always visible.** Whether the box is armed or standing down,
  and since when, whatever tab is showing and whether or not a question is
  open (#19). It is a status chip in the fixed strip alongside the link
  counter. On the wire it is the box's own monotonic clock, like every other
  timestamp it sends, but the box's monotonic reading has no time-of-day
  meaning on its own - it is seconds since some arbitrary start, not seconds
  since midnight - so the page converts it to a local time before it is shown,
  the same round-trip estimate a tap's clock offset uses. That conversion is
  also why fixtures built against it stay deterministic: the box never has to
  know what time of day it is, only how long ago something happened, and the
  box's own idea of time-of-day - which does not exist - never has a chance to
  reach the screen. It carries no time at all after a restart rather than
  inventing one - the same fail-visible discipline as the fader belief in
  §5.3.
- **The arm / stand-down question.** Principle 4 - announce, don't surprise -
  applied to duty state as well as to the hand-off in §5.3: the box does not
  decide to arm or stand down for the operator, it asks, from the same
  annotations that already record band movement (§5.6). See §6.4 for the
  mechanism.

Full telemetry is interesting but not required for game operations.

### 5.6 Annotation

The same UI carries one-tap **event annotation**, timestamped against the same
clock as audio, RTD, and OSC state. §5.9 describes how that shared clock is
obtained.

Each entry also carries **Reaper's own playhead** as it is written, whenever
Reaper is streaming one. That is the shared clock made explicit: the position
comes from the recorder rather than from arithmetic on ours, so it cannot drift
across a three-hour game and does not depend on which recording in the log the
timeline is being measured from. It is recorded as-is and never extrapolated -
`/time` arrives about eleven times a second while the transport moves, which is
an order of magnitude finer than the operator's reaction time already in the
tap. Reaper is silent while parked, so an entry made then carries no position
and falls back to the arithmetic from the anchor.

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

**Some annotations also ask a question (#19).** `band-exits-stands`, the start
of `halftime-exodus`, and `band-enters-stands` are ordinary annotations first -
they record what the operator saw - and additionally raise the arm /
stand-down question described in §6.4. Raising the question never itself
changes duty state: a mis-tapped `band-exits-stands` mid-drive must not
disable anything on its own (principle 4 again), and the only path from the
question to a state change is the operator's own tap on its accept button,
which runs the ordinary Arm or Stand down command with its own stale check and
its own refusals. Ending `halftime-exodus` asks nothing; the band never plays
during it, and only its start says anything about duty.

### 5.7 Network resilience

Stadium Wi-Fi degrades as the stands fill; 2.4 GHz is absorbed by human bodies,
and the venue holds ~60,000 of them. The current control path depends entirely
on that link.

A wired box on the control VLAN speaking OSC is **strictly more reliable than
the iPad, before it detects anything at all**. See §9 — this is a worthwhile
standalone deliverable.

That moves the unreliable link rather than removing it: the operator's page
still reaches the box over the same degrading wifi. The failure to design for is
not the socket that closes but the one that half-opens — 2.4 GHz absorbed by a
filling stand, TCP stalled, no close event, and a page rendering a snapshot from
six minutes ago with complete confidence. Nothing else on the screen looks wrong
while that is happening, which makes it exactly the silent degradation this
document forbids elsewhere. Hence the keepalive in §5.5: liveness has to be
something the page observes arriving, not something it infers from a socket that
has not told it otherwise.

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

READY sits beside IDLE, entered only by the operator:

```
IDLE ──(operator: band likely)──> READY ──(any trigger)──> OPEN
                                    |
            score reversed, stand down, or a plain fade
                                    v
                              RELEASING (2 s fade)
```

- **STANDING DOWN** — boot state. Band not in the stands. Pregame with the other
  band on the field, halftime, exodus.
- **IDLE** — armed, band in stands, DCA closed.
- **READY** — the fader ridden to a hold level short of target with no band
  playing yet: something good has just happened for the home team, the band is
  likely and has not started (#6). Entered only by the operator - from IDLE, or
  from STANDING DOWN, which it arms on the way (#89) - or reported with "It's at
  ready level" when the box does not know where the fader is (§5.3). Never
  entered by the detector, in any phase (§6.4). Any trigger commits it the rest
  of the way to OPEN from wherever the ride had got to. Score reversed, a plain
  fade and a stand-down take the ordinary 2 s fade rather than a snap (§5.3
  covers the level-unknown case), because the fader is up and the box cannot be
  sure the band has not quietly started.
- **OPEN** — DCA at 0 dB. Held by tonality and envelope consensus, reinforced by
  on-grid whistle. **Must hold through the PAT diminuendo.**
- **RELEASING** — 2 s fade. Any qualifying trigger snaps straight back to OPEN.
  This is what makes the reactive close survivable: the machine cannot see arms
  come down and will always trail the operator, and the fade absorbs that.

READY is a fader-up-with-no-band span by construction, and the first one the
log names. Game 3 is the first capture whose log carries them, which is why §9
separates what the fader was doing from whether the band was playing.

### 6.4 Mode handling

**The arm / stand-down question (#19), as shipped.** The box **prompts**
rather than acting silently: tapping `band-exits-stands`, or starting
`halftime-exodus`, raises a persistent *"Band left the stands. Stand down?"*
in the operator page's prompt slot; tapping `band-enters-stands` raises *"Band
in the stands. Arm?"* Two reasons for asking rather than acting: it varies by
game, and a system that announces what it is about to do earns trust faster
than one that silently changes behavior.

The question is a box-owned state machine (`tacet.prompts`), independent of
`tacet.state`: a `Decision` carries log entries and a next question, never a
fader command, so raising one can never itself move the DCA or change duty -
only the operator's own accept tap does that, running the ordinary Arm or
Stand down command with its own refusals (§5.3's unknown-level Arm refusal
applies here exactly as it does to the bare button; Stand down is never
refused *for want of a known level* - but its accept is stale-checked like
any other fader tap (§5.6, #16), and a late one is refused the same way a
late tap on the fader column would be). A question whose answer would be a
no-op - already armed and asked to arm again - is never raised at all. The
page adds a 700ms tap guard, keyed
on the question's own sequence number, so a tap already on its way toward
some other button on the fixed layout (§5.5) cannot land on a question that
has just appeared in its place.

Ending `halftime-exodus` asks nothing; the band never plays during it, and
only its start says anything about duty. Same idea on return during Q3 -
`band-enters-stands` asks again rather than assuming.

**RTD is a second source of the same questions, not yet built.** §5.4's
halftime-exodus arming (Q2 under 5:00) and the return during Q3 are meant to
raise the identical questions above once RTD exists, through the same
`tacet.prompts` mechanism and the same operator accept - RTD would only add a
second *source* for a question already fully specified here, never a new kind
of question or a path that skips the accept tap.

**Nothing but a person ever pre-opens (#6, #95).** READY is a prediction that
something is about to play. Only someone watching can tell whether it will be
the band, the DJ or the other band, and opening with no band sound is not "is
sound present" at all. This is not the Phase 1/2 line a trigger sits on: a
detector confirming that sound *is* present is exactly its Phase 2 job, but
guessing that sound is about to start never becomes one. So `tacet.state`
refuses a detector-sourced READY, and "It's at ready level" with it,
unconditionally and before the phase gate, whatever `allow_detector` says; it is
not a Phase 2 to-do. RTD changes the source of the question and nothing else
(#95): a home score, a first down or a defensive stop can each raise a prompt
in the same slot, answered by a thumb, with the decline logged too. Nothing
moves the fader on RTD alone, in any phase, and §5.4's context-not-interlock
rule holds here as it does for the play clock. Why write it here: "prompt, never
act" reads like a Phase 1 caution that a better detector would retire, and it is
not one. The fader would be going up on something nobody has heard yet.

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
- **Measure the cannon** from a captured score and decide which of §6.2's
  measures separates it from an ensemble entrance. §4 makes "the detector does
  not open on the cannon" a design goal, and a goal like that has to be
  discriminated deliberately and scored offline, not left to the 2 s fade.
- ~~**Confirm DCA fader granularity.**~~ **Answered 2026-09-12: arbitrary
  hundredths.** The spec gives `min -32768 / max 1000 / scaling 100`, implying
  arbitrary hundredths of a dB, while the parameter notes point at Table 1 — a
  43-entry list whose steps are 1 dB near unity, 2 dB from −10 to −30, 5 dB
  below that. Had only Table 1 values been accepted, a 2 s close from 0 dB to −∞
  would have been 32 steps, ~62 ms apart. `verify_dm7 --granularity` sent
  `-1550`; the console displayed −15.50 rather than snapping to −16.00. So the
  spec governs and Table 1 is a display convention. `quantize()` and
  `Dm7Client(quantized=True)` stay in the code as a contingency for a different
  console or a firmware change, and are unused here.
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
supplementary fixed-digit displays. Classic RTD over RS-232 is 19200 8N1.

### What the protocol actually is

Verified 2026-09-08 against open-source decoders. Not verified against this
stadium, and nothing below has been seen on a wire here.

RTD is not a semantic protocol. It is a **display-buffer write protocol**: the
console holds one flat buffer of ASCII, and each packet says *write these
characters at this offset*. Only changed regions are transmitted, so a reader
has to accumulate state rather than parse events.

```
0x16  "00000000"  0x01  "004210" NNNN   0x02  <ascii>  0x04  <2 hex>   0x17
SYN   header      SOH   prefix, offset  STX   payload  EOT   checksum  ETB
```

- The `004210` prefix is what identifies an RTD data packet. Other packet types
  share the framing and are not this — `004010` is the Venus display
  position-text command, which is what most of the community write-ups on the
  0x16/0x17 framing are actually describing.
- `NNNN` is a decimal offset and is **zero-based**. The published field offsets
  are **one-based**. Play clock at documented offset 201 is buffer index 200.
- The checksum is a wrapping byte sum over the payload, including the 0x04
  separator, rendered as two uppercase hex digits.

The framing is public. **The offset-to-meaning map is not.** It is per sport
"field set", it lives in Daktronics' own manuals, and the open-source decoders
carry it as tables transcribed by hand from a vendor PDF.

### The football field set

From those transcribed tables. Every context use in §5.4 is covered by six
fields:

| Use in §5.4 | Field | Offset | Width |
|---|---|---|---|
| Play-clock-low caution | Play Clock Time | 201 | 8 |
| Permissive: clock stopped | Main Clock Stopped (`' '` or `'s'`) | 28 | 1 |
| Quarter breaks | Quarter / Quarter Description | 142 / 148 | 2 / 12 |
| Timeouts | Home/Guest Time Out Indicator and Text | 132–141 | 1 / 4 |
| Halftime exodus arming | Quarter and Main Clock Time | 142, 1 | 2 / 5 |

**The play clock is present in the football field set.** That settles the
original question in the abstract; what remains is whether it is populated on
this install. Clock-stopped arriving as a literal one-byte flag, rather than
something inferred from clock deltas, is better than was assumed.

Two hedges. The transcribed tables carry their own warning that they were
generated semi-automatically and may contain errors; and they are **All Sport
5000**-era, while this install is an All Sport **Pro**. The framing should
survive that gap — backward compatibility with existing dumb displays is the
entire reason RTD exists — but field sets are likelier to have drifted. Treat
the offsets as a strong prior to validate against a capture, not as settled. The
8-byte `mm:ss` play clock is the first thing to check; that is a strange shape
for a 40-second clock.

### The console does not dump state on connect

A reader that attaches mid-game sees a **buffer full of holes**, filling in only
as each field first changes: no score until someone scores, possibly no team
names all night. Pressing `STOP` on the scoreboard console forces a full dump.

Consequence for capture: the log has to distinguish *field is blank* from *field
has never been written*. Those are different facts, collapsing them quietly
poisons the Phase 3 analysis, and it cannot be recovered afterwards. The
operational half of this is in box.md, "Not yet proven".

### The pregame countdown occupies a clock

Observed from the press box, not yet seen on a wire. The stadium clock runs a
countdown to kickoff from around the time doors open, roughly 150 minutes out.
It ends at the team entrance, resets, and restarts at the top of the first
quarter.

Capture will normally already be running through all of that (reaper.md), so a
reader attaches during the countdown rather than at kickoff. Two consequences:

- **A 150-minute countdown does not fit the shape of a game clock.** Main Clock
  Time is 5 wide at offset 1, which holds `mm:ss` for a quarter and cannot hold
  `150:00`. Whether pregame drives that field in some other format, drives a
  separate timer, or leaves it alone entirely is unknown. It is a question for
  the Data Monitor capture requested below, and it is free to answer there -
  the same capture settles it. Do not assume the field is idle before kickoff.
- **Halftime-exodus arming must not fire on it.** The rule in 5.4 is Q2 under
  5:00. A pregame countdown passing 5:00 is a different fact wearing the same
  digits, so the quarter field has to be a positive term in that test rather
  than an assumed one. This is the same discipline as 5.4 generally: RTD is
  context, and a permissive has to be asserted, not inferred from absence.

The countdown reaching zero is also the cheapest kickoff anchor available. It
is machine-visible and does not depend on anyone tapping a button.

### Requested

1. **A read-only RTD feed reachable from the press box.** UDP on the scoreboard
   network is preferred over serial. This is one-way and consumes no scoreboard
   resources.
2. **The UDP port and data source configuration** for football.
3. **A capture of the live stream during a game**, using Daktronics' **Data
   Monitor**. This is the highest-value item on the list and the cheapest to
   supply — it settles the port, the field set, and whether the play clock is
   populated, in one artifact, without anyone having to go find a manual. The
   published field tables already say the play clock exists; only a capture says
   it is populated here.
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
- Football field offsets, transcribed from the vendor PDF —
  https://github.com/zabackary/daktronics-allsport-5000-rs
  (`sports_data/`, `src/sports/football.rs`)
- RTD framing, community reference —
  https://timingguys.com/topic/daktronics-rtd-protocol-reference

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
annotation is the one part of the labelling that cannot be reconstructed later.

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
  mics it recovers the fader moves as applied, and **this is the source of the
  fader-state labels** (below). It replaces the OSC subscription originally
  planned here, which the protocol does not support (§5.3). It is also the
  better measurement: it captures gain as actually applied to the PA, and it is
  the only available confirmation that a command reached the console at all.
- RTD stream, timestamped
- Every detector decision the box would have made
- **Live operator annotations** (§5.6) — the portion of the labelling that
  cannot be reconstructed afterward

**Two label sets, and they are not the same set.** Phase 1 labels two different
things, and the detector is trained on only one of them.

- **`band-present`** - was the band sounding. This is the detector's training
  target, and the only question this system answers (§1, §2). Spans over the
  capture timeline, with two values: present and absent. The PAT diminuendo is
  *inside* a present span: the band gets quiet, it does not stop (§4). No value
  says why a span ended (§10, principle 4), so there is no "breath mark" or
  "end of song" value, ever. What else was sounding - the other band, the DJ,
  the PA - is context, from the `other-band-on-field` annotation and from what
  #17 will display and log, and is not a value of this label. It is binary by
  decision; if a quiet-present value is ever wanted, that is a decision for
  whatever format ends up holding the offline labels (below), not for this
  document.
- **`fader-state`** - where the DCA actually was, as three separable facts. The
  *applied gain over time*, recovered by comparing the post-DCA reference
  channel against the pre-fader mics: the truth of it, whoever was driving. The
  box's own *intent and cause*, from the `commanded` entries (`command`,
  `detail`, `state`, `level`, `target`), which exist only where the box was
  driving. And *who was holding the fader*, from `handed-off`, `took-back` and
  `still-mine` (§5.3, #118).

How the hard cases of §4 map onto the two:

- **Pre-open, the READY span:** band absent. Fader up at the hold level, reached
  by a ride (`state: "ready"`).
- **Commit out of READY on the downbeat:** band flips to present at the true
  first onset, which is earlier than the tap. Fader open.
- **The cannon:** band absent, at a broadband onset instant. Fader usually
  already up (READY), sometimes idle.
- **PAT diminuendo:** band present. Fader open, while the level on the mics
  looks like absence.
- **Ragged stop:** band present, trailing to absent. Fader open, then
  releasing.

The rows where the two disagree are the valuable part: a fader that is up with
no band, and a band that started before the fader did. `fader-state` is also the
easy one to get - one channel, no human - and using it as the training target
would teach the detector that crowd cheering means open, the amplitude failure
§1 describes. The convenient label is the wrong one.

**What produces each.** `fader-state` is derivable by machine from the capture
and the log, with no human pass. `band-present` is not derivable from it at all:
it comes from the live annotations (§5.6) plus one offline review pass over the
14 mics. That squares with §5.6, which says most band state cannot be
reconstructed afterwards. Whether the band was *sounding* is the exception, the
one thing the multitrack really does carry, so it needs no live button, only
someone to sit down with the audio once. What cannot be reconstructed is where
the band was, whose timeout it was, and why a close happened. `false-open` and
`missed-entrance` (§5.6) are the operator's judgement of what a detector would
have got wrong: evidence for the review pass, not a label set of their own.

**What the log already carries.** Nothing new is needed in the schema before
Game 3:

- *READY spans:* `commanded` entries carry `command` (`ready` or
  `report-ready`), `detail` (`up-ready` or `report-ready`), `state` (`"ready"`),
  `level` and `target`, and `move-landed` and `move-failed` carry where the ride
  ended. A span ends at the next entry whose `state` is no longer `ready`: a
  `commanded` entry, or the `stood-down` that ends it when the level was unknown
  and no fade was sent (§5.3).
- *Why the operator got ready:* the game instants `touchdown`, `field-goal`,
  `first-down` and `defensive-stop`.
- *How it ended:* `up-whistle`, `up-drums` or `up-slow` for a commit,
  `score-reversed` or `out` for an abandon.
- *Duty and where the band was:* `armed`, `stood-down`, `band-enters-stands`,
  `band-exits-stands` and `halftime-exodus`.
- *Control authority:* `handed-off`, `took-back` and `still-mine`.
- *Taps that did not execute:* `stale-tap`, or a fader button's own entry marked
  `executed: false`.
- *The common clock:* Reaper's playhead on every entry (§5.6, §5.9).

The one capture prerequisite is not a schema change: the post-DCA reference
channel has to actually be patched and recorded (`docs/reaper.md`'s track list,
#13's patch list). Game 2 had none, so its only fader labels are the `commanded`
entries, and only where the box was driving; `fader-state` has not yet been
recovered from a real capture. Game 2 also predates `up-ready`, so its pre-opens
look like ordinary opens; telling them apart is for #21's corrections sidecar
(Game 4).

**The gap: nowhere for offline labels to live.** The box's log is live and
append-only, and #21's corrections sidecar corrects the log rather than
labelling the audio. `band-present` spans, and instants found offline like the
cannon (which has no key, on purpose), are produced weeks later, from the audio.
This document deliberately does not decide their format. It is wanted before the
Game 3 capture is analysed, not before it is made, which is why it does not
block the capture.

After two or three games this yields real audio, real crowd, real touchdown
sequences, and audio labelled for whether the band was playing, once it has been
reviewed. Candidate detectors can then be replayed against all of it offline at
many times real speed, with thresholds and time constants tuned against reality
instead of guessed.

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
   Some things only ever prompt: nothing but a person puts the fader up before
   the band plays (§6.4).
6. **Structure, not level.** Level is the one dimension where the crowd wins.
7. **Faders, never mutes.** The mics feed other mixes pre-fader, post-mute.
   Muting would take the band out of those mixes too.
