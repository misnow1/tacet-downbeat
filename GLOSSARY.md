# tacet-downbeat

Rides the marching band's microphone DCA on the stadium console during home
football games: open fast when the band starts, fade when it stops, with an
operator supervising. The reasoning behind these terms is in `docs/design.md`.

## The room and the rig

**Box**:
The computer that commands the band DCA, serves the operator page and keeps the
log.
_Avoid_: the automation, the controller

**Operator**:
The person in the press box who supervises the box and keeps override authority
over it at all times.
_Avoid_: engineer, user, fallback

**Hype PA**:
The PA beside the band, separate from the main PA. It was built for hype music
and now also carries the band.
_Avoid_: band PA

**Band feed**:
The band mics' path into the hype PA, undelayed, because its timing reference
is the acoustic band a few feet away.

**Hype music path**:
Recorded music's path into the hype PA, delayed to line up with the main PA.

**Main PA**:
The stadium's primary PA, in the scoreboard behind the stands behind the band.
_Avoid_: stadium PA

**Band mics**:
The 14 microphones placed among the band in the stands.

**Band DCA**:
The one DCA on the console that holds every band mic, and the only thing the
box ever moves.
_Avoid_: band group, VCA

**Other mixes**:
Every destination the band mics feed before the fader, which a mute would empty
and a fader never touches.

## People and sources

**Band director**:
The person responsible for the band, bound by the same noise rule, and so the
one who decides when it may legally play.
_Avoid_: MD

**Drum major**:
The band member on the stand who conducts, whistles count-ins and cues
entrances.
_Avoid_: conductor

**Drumline**:
The snares, bass drums and cymbals, which often start a song on their own.

**Other band**:
The second marching band at a home game, trading pregame and halftime duties
with this one.

**DJ**:
The stadium's recorded-music source, which competes with the band for the gaps
in play.

## Fader levels and moves

**Target**:
The level an open takes the band DCA to; unity unless the operator changes it.
_Avoid_: 0 dB (as a name), full

**Hold level**:
The level short of target that READY rides the fader to.

**Closed**:
The band DCA at minus infinity, which needs no mute.
_Avoid_: muted, off

**Snap open**:
An instant move to target.
_Avoid_: slam open

**Slow open**:
A ride up to target, used to disguise a late entrance.

**Fade**:
The 2 s move to closed, the same for every reason the band stops.
_Avoid_: release (as a verb), fade out, close slow

**Instant close**:
An immediate move to closed, made only by the operator.
_Avoid_: slam shut, mute

**Ride**:
A gradual move to a new level that is not a fade: READY's ride to hold level, a
slow open, a retarget.

**Nudge**:
A 1 dB step up or down of an open fader, which leaves the target unchanged.
_Avoid_: trim

**Retarget**:
Changing the target; the fader rides to it if it is up.

**Absolute move**:
A move that lands in one known place whatever the box believed: a snap open or
an instant close.

**Relative move**:
A move ramped from the level the box believes: a fade, a ride or a nudge.

## States and duty

**STANDING DOWN**:
The boot state: band not in the stands, detector refused, operator never
blocked.

**IDLE**:
Armed, band in the stands, fader closed.

**READY**:
The fader ridden to hold level with no band playing yet, because something good
just happened for the home team; entered only by the operator.
_Avoid_: pre-open

**OPEN**:
Fader at target (or nudged from it), held while consensus says the band is
playing.

**RELEASING**:
The state while a fade runs; any trigger snaps back to OPEN.

**Duty state**:
Whether the box is armed or standing down.

**Arm**:
The operator act that takes the box from STANDING DOWN to IDLE.
_Avoid_: arm (for Reaper; say record-arm)

**Stand down**:
The operator act that returns the box to STANDING DOWN.

**Prompt**:
A question the box puts to the operator that changes nothing until the operator
accepts it.
_Avoid_: auto-arm, auto-stand-down

## Belief about the fader

**Commanded**:
A value the box last sent, which nothing has verified.
_Avoid_: current, actual

**Confirmed**:
A value the device itself reported back, as Reaper does for recording.

**Level known**:
The box's belief about the fader, valid only after an absolute move since boot
or since the last hand-off.
_Avoid_: synced

**Hand-off**:
The operator's report that another controller, such as the StageMix iPad, now
holds the band DCA. It makes the level unknown until the next absolute move.
_Avoid_: take back (as an operator act; the box takes back by moving)

**Report ready**:
The operator's statement that the fader already sits at hold level, which
corrects the belief and moves nothing.

## Sound and detection

**Band present**:
Whether the band is sounding; the only question the system answers, and the
detector's training target.
_Avoid_: fader up, music on

**Fader state**:
Where the band DCA actually was over time and who held it; never a training
target.

**Entrance**:
The band starting to play after silence; its first note is the downbeat.
_Avoid_: cue (the cue is the drum major's)

**Stop**:
The band ceasing to play, never classified by cause.
_Avoid_: end of song, breath mark (as kinds of stop)

**Trigger**:
Evidence that an entrance has happened: a unison onset, a whistle or a broad
simultaneous entrance.

**Unison onset**:
The drumline hitting together across several mics, which one person cannot
produce.

**Whistle**:
The drum major's count-in whistle, on the beat and at a stable pitch.
_Avoid_: whistle (for an official's; say officials' whistle)

**Officials' whistle**:
A whistle from a game official, rejected by its pitch and its off-beat timing.

**Consensus**:
The slow, structural evidence that holds OPEN: band mic envelopes moving
together and coherent pitch across channels.

**Detector**:
What decides band present from the band mics' structure, never from level
alone.

**Shadow mode**:
The detector computing and logging everything while driving nothing.

## Game moments

**Touchdown sequence**:
Loud after a touchdown, quiet under the PAT, loud again if it is good; the band
never stops through it.

**PAT diminuendo**:
The quiet middle of the touchdown sequence, which is band present and where
amplification matters most.

**Scoring song**:
One of the few songs the band plays after a score, repeated across games.

**Cannon**:
The cannon fired on a touchdown or field goal, a broadband onset on every mic
that is never band present.

**Drums-first entrance**:
The drumline starting a song alone, with the rest of the band whistled in
measures later.

**Ragged stop**:
A stop that frays over a couple of seconds as the band notices a play starting.

**Halftime exodus**:
The band leaving the stands late in the second quarter, partial and dwindling.

**RTD**:
The scoreboard's live game-data stream: clocks, period, down and distance.
Context, never a gate.
_Avoid_: scoreboard feed

**Play clock**:
The 40 s clock from ready-for-play, most of which is legal band time, so it is
never an interlock.

## Capture and annotation

**Annotation**:
The operator's one-tap record of something seen, the one label that cannot be
reconstructed later.
_Avoid_: marker

**Instant**:
An annotation with no duration.

**Span**:
An annotation with a start and an end.

**Marker**:
Reaper's derived view of an instant, regenerable from the box's log.

**Region**:
Reaper's derived view of a span.

**Take**:
One continuous Reaper recording.

**Record-arm**:
Enabling a Reaper track to record.
_Avoid_: arm

**Multitrack**:
The capture of every band mic on one clock.

**Pilot tone**:
A known tone on a console channel assigned only to the band DCA, recorded after
the fader so its level gives the fader state.
_Avoid_: post-DCA reference (as a name for the tone)
