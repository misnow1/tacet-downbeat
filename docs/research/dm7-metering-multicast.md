# DM7 Metering Multicast - Research

**Status:** Research note. No code, no design.md change, nothing sent to a
console. Every capture was passive (listen only).
**Last updated:** 2026-10-02 (game day, Game 3 soundcheck)
**Question:** The DM7C sends a lot of multicast onto the control VLAN. What is
it, and can it read back the band DCA?

Conventions follow `dm7-fader-readback.md`. **Observed** means seen directly
in the captures below. **Inferred** is a reading of the data that fits but has
not been tested on its own. **Unknown** is open. Yamaha publishes nothing on
this protocol, so nothing here is documented.

---

## Bottom line

The multicast is the console's **level metering**, sent about 33 times a
second. It decodes completely. With the pilot tone running on ch 53 (the DCA
reference, `reaper.md`), **the ch 53 direct-out meter reads the band DCA back to
0.2 dB**, for any source of the move (box, iPad or console). It shows within
55-82 ms of an open, and it is passive: no Dante routing, no recording, and
nothing sent to the console. That is a readback path for #18, #107 and #137
that the OSC spec does not offer.

Limits:
- **Undocumented.** Firmware can change it, so it is not a control-path input.
- **It depends on the tone.** It only works while the tone is on and ch 53 is
  untouched.
- **Closes show late.** The meter's release is slower than the 2 s fade, so a
  close shows about 0.8 s after the fade has landed. It cannot show fade shape.
- **Channel meters are pre-DCA.** On the band mics, no channel meter block
  follows the DCA. Only the buses and direct outs do.

---

## Captures

All three are on the control VLAN, captured on 2026-10-02 (local time, EDT).
They are in `data/`, which is gitignored and never committed.

| File | Start | Length | SHA-256 | What happened |
|---|---|---|---|---|
| `console.pcap` | 14:27:02 | 14 s | `70823e94...adcda8` | Idle console before soundcheck |
| `ref-test.pcap` | 14:38:59 | 114 s | `7b9e75af...0e0f4b` | Ref mic test (Ref Primary, Ref BU, main PA). DCA 0/+3/-3 moves from the box, **pilot tone muted** |
| `tone-test.pcap` | 14:47:08 | 57 s | `86d5a465...cbeb9` | Pilot tone on. Box snap-opens to 0, +3, -3, 0, each followed by the 2 s fade |

The DCA move times come from the box's own log,
`~/games/2026-10-02-soundcheck.jsonl` (`commanded` / `move-landed` events,
`wall` field). **Assumed:** the captures and the box log share a clock. The
latency was consistent across every event in both captures, which supports
that, but it was not checked separately.

Reaper readings during `tone-test`, given by the operator: the tone generator
output meter reads **-12 dBFS**. Track 26 (ch 53 direct out, Post Fader) reads
**-11.4 dBFS** with the DCA at 0 dB.

---

## Wire format (Observed)

- **Source:** the console, `192.168.20.121:50240`, from a Yamaha OUI MAC.
- **Destination:** `239.192.0.168:50240`, UDP, IP TTL 1.
- **Rate:** about 3.9 Mbit/s and 468 packets/s, steady, whether or not
  anything is moving.
- Mixing Station's docs give `239.192.0.164` for DM metering. That differs from
  this one, so the group is not one fixed address. **Unknown:** whether it is
  per console, per firmware or per mode.
- The only other traffic seen was a few mDNS packets.
- Every packet in all three captures splits into header plus blocks with no
  bytes left over.

### Header (20 bytes)

| Offset | Bytes | Meaning |
|---|---|---|
| 0 | `59 4d 44 50` | ASCII `YMDP` |
| 4 | 4 | Console IPv4 address (`c0 a8 14 79`) |
| 8 | `00 00 00 00` | Always zero |
| 12 | `44 4d 37 00` | ASCII `DM7\0` |
| 16 | 4, big-endian | **Millisecond counter.** It advanced 14,007 over 14,006 ms of capture time. **Inferred:** console uptime, about 47.5 h at the first capture |

### Blocks (repeated to the end of the packet)

| Offset | Bytes | Meaning |
|---|---|---|
| 0 | 1 | Type. Bit 7 set means another block follows in this packet. The low 7 bits are the kind: `0x08`, `0x09` or `0x05` |
| 1 | 1 | Block number, `0x00`-`0x86` |
| 2 | 4 | Variant. First byte `00` = live meter, `01` = **peak hold** (never below the live value in the same frame). Other bytes always zero |
| 6 | 2, big-endian | Data length |
| 8 | n | Data. Kind `0x08` is one unsigned byte per meter |

### Cadence

- A full set of blocks goes out about every 30 ms, so roughly 33 frames a
  second, split across about 14 packets.
- Blocks `0x00`-`0x76` arrive about 393 times in 14 s.
- The 768-byte blocks `0x77`-`0x7e` arrive about 468 times in 14 s. Some
  bursts carry only those blocks.

---

## Meter scale (Observed, calibrated against the tone)

```
dBFS = (value - 216) / 5        one unit = 0.2 dB
```

| Value | Meaning | Evidence |
|---|---|---|
| 3 | Floor, no signal | Every silent channel in every capture |
| 144 / 159 / 174 | -14.4 / -11.4 / -8.4 dBFS | The ch 53 direct out at DCA -3 / 0 / +3. Steps are exactly 15 units per 3 dB, and 159 matches Reaper's -11.4 dBFS |
| 155 | -12.2 dBFS | Ch 53 input meter. Reaper's source reads -12.0, one unit off |
| 214-216 | Near or at 0 dBFS | Loud channels bunch here and never go higher |
| 255 | **Inferred:** over/clip flag | Values jump straight from about 216 to 255 with nothing in between |

The slope is **verified only from -14.4 to -8.4 dBFS**. During fades the bottom
of the range compresses: curves from different start levels converge. That
could be a non-linear scale at the low end or the meter's own release.
**Unknown** which.

Kind `0x09` blocks use 175 as their resting value (see the block map).

---

## Block map

Channel numbers are 1-based console input channels. That means index `i` in a
120-wide block is ch `i+1`.

| Block(s) | Width | Reading | Confidence |
|---|---|---|---|
| `0x00`-`0x04`, `0x11`-`0x14` | 120 | Input channel meters, at metering points before the fader. They differ among themselves by fixed amounts per channel (gain, HPF/EQ) | Observed; which point is which is Inferred |
| `0x05`, `0x0b` (kind 9) | 120 | **Inferred:** dynamics meters (gain reduction?). 175 on most channels. `0x05` reads low (0-23) on ch 37-48 only, and `0x0b` reads low on ch 1 only | Inferred |
| `0x06`-`0x0a`, `0x0c`, `0x0d` | 120 | Channel meters after the channel fader but **before the DCA**. Ch 37-46 sit a steady ~28 units (~5.6 dB) below the pre-fader blocks, whether the DCA is open or closed | Observed |
| `0x0e`-`0x10` | 120 | **Direct-out meters.** They light only on channels with direct outs: ch 2, 7 and 11 in `console.pcap`, and ch 53 (Post Fader) in `tone-test.pcap`. These **follow the DCA** | Observed for ch 53; the "direct out" label is Inferred |
| `0x15`-`0x22` | 48 | Mix bus meters. **Index 17 follows the band DCA**: floor when closed, signal when open | Observed |
| `0x23`-`0x30` | 12 | Matrix meters. **Index 8 follows the band DCA** | Observed |
| `0x31`-`0x3e` | 4 | **Inferred:** stereo/mono masters. The tone never reached them | Inferred |
| `0x3f` index 17, `0x6b` index 78 | 24 / 144 | Carry the pilot tone at the ch 53 level. **Inferred:** output port meters for Dante out 53 | Inferred |
| `0x77`-`0x7e` | 768 | **Inferred:** I/O device port meters. In `0x78`, indices 65-80 match Rio inputs 1-16 (console ch 37-52). These update slowly (about 2 Hz), and so do ch 37-50 in the channel blocks | Inferred |
| `0x50`, `0x75`, `0x76`, `0x7f`-`0x85` (kind 5) | various | Not meters. `0x7f` looks like a curve (EQ or RTA display?) that changed mid-capture | Unknown |

The DM7C's channel count is not the 120 slots in these blocks. Unused slots
stay at the floor.

### Patch cross-check (`reaper.md`, tracks to record)

- **Band mics, ch 37-50:** active in the pre-fader blocks.
- **Ch 38, 47 and 48:** stay at the floor. They are MV Spare and Horns 3/4,
  which were not in use.
- **Ref test speech:** shows on ch 3 and 4, and on ch 17/18 as a linked pair.
  **Unknown:** which of these are Ref Primary and Ref BU.
- **Ch 3 hit 255 in 142 frames** (about 4 s in total) in the first pre-fader
  block during the ref test, and ch 4 in 16 frames. If 255 is the over flag,
  Ref Primary's input gain was hot. This was flagged for soundcheck.

---

## DCA readback

### Pilot tone, `tone-test.pcap`

Ch 53 direct-out meter (block `0x10`, index 52):

| Box command | Steady value | dBFS |
|---|---|---|
| closed | 3 | floor |
| open 0 dB | 159 | -11.4 |
| open +3 dB | 174 | -8.4 |
| open -3 dB | 144 | -14.4 |
| open 0 dB | 159 | -11.4 |

Every hold was dead flat, with no jitter at all.

**Opens** (snap, `up-whistle`):
- The last frame at the floor came 12-50 ms after the command.
- The first frame showing the tone came **55-82 ms** after it. That first frame
  is partial, so the meter integrates over each frame.
- Full value by 88-119 ms.
- About 30 ms of that is just waiting for the next frame.

**Fades** (2 s, linear in dB to -60 dB, then -inf, per `dm7.ramp_steps`):
- The meter starts falling 117-151 ms after the command.
- It then falls more slowly than the fader, at about 20-25 dB/s at first
  against the fader's 30 dB/s.
- It reaches the floor **2.8 s** after the command. The box landed -inf at
  2.0 s.
- The fall looks the same from +3, 0 and -3. That is meter release, not the
  fader.

**Ch 53 is not at unity.**
- Input meter 155 against output 159 at DCA 0 is +0.8 dB (+/- 0.2).
- Reaper's -12.0 against -11.4 is +0.6 dB.
- `reaper.md` says ch 53's fader is fixed at 0 dB. Something in ch 53's path
  adds about +0.6 dB: check the fader and the digital gain.
- Until that is fixed, "0 dB" on the reference means about +0.6.

The tone is at -12 dBFS, against the -20 that `reaper.md` suggests. That is
fine at the +3 cap (about -8 dBFS), but at the console's +10 limit it would
peak around -1 dBFS.

### Band signal only (tone muted), `ref-test.pcap`

- Mix 17 and matrix 8 sat at the floor whenever the box had the DCA closed and
  came up whenever it was open. That held for all four open/fade cycles.
- The snap open showed in 22 ms. The 1.5 s `up-slow` ramps showed in 95-162 ms
  (they start at -inf).
- The fades reached the floor 1.7-2.06 s after the command.
- Every box command landed when it was logged, including the ones logged
  `"delivered": null`.
- The +3/0/-3 levels **could not** be told apart. The only signal was
  empty-stadium band noise near the bottom of the scale, and it wanders more
  than 3 dB on its own. That is why the tone is needed.

### Correction

In the first look at `console.pcap`, ch 37-48 dropping out of blocks `0x06`+
was read as "DCA 17 is down". **That was wrong.** The channel blocks are
pre-DCA. The dropout was quiet input minus the fixed ~28-unit channel-fader
offset.

---

## Implications

These are not decided. Each is a question for an issue, not a plan.

- **#107 (fader position unknown):** a passive listener could tell the box
  where the DCA actually is, including after an iPad or StageMix move. Today
  only an absolute command does that.
- **Commanded vs confirmed in the UI:** this is a confirmed value 33 times a
  second for the open level. For closes it lags about 0.8 s.
- **#137 (box tone with measured readback):** this gets the measured readback
  without capturing audio from Dante. The tone still comes from Reaper today.
- **#18:** a third readback route alongside the undocumented TCP protocol on
  49280 and Mixing Station. Like those, it is undocumented, so it is at most a
  monitor, never something the control path depends on.
- **Phase 1 ground truth:** the meter stream could log DCA history next to the
  multitrack as a check on the Dante reference recording. It is not a
  replacement: 0.2 dB and 33 Hz against 48 kHz audio.
- **Network:** 3.9 Mbit/s of constant multicast. If the iPad's Wi-Fi is bridged
  onto the control VLAN, the access point may be sending this at a low basic
  rate. Check that if the page feels sluggish.
- **Firmware:** the console stays on its current firmware through Game 3
  (`priorities.md`). Any firmware change invalidates this note until it is
  re-captured.

## Open questions

- Which pre-fader block is which metering point. A capture while changing one
  channel's gain, HPF and EQ in turn would settle it.
- What the kind 9 blocks mean, and what 175 is.
- What blocks `0x3f` and `0x6b` are.
- Whether the scale stays at 5 units/dB below -15 dBFS. A slow DCA ramp with
  the tone would map it.
- Whether the band channels really update at about 2 Hz. If they do, that rules
  out the channel meters for anything time-sensitive.
- Whether IGMP snooping on the control switch would stop this reaching a box
  that has not joined the group. **Game 3 suggests it does:** a passive capture
  during the game (`game-capture.pcap`, 6 KB) saw no metering at all, on a
  different switch from the soundcheck captures. A passive capture never sends
  a join. Test with a listener that joins `239.192.0.168` on the game-day port,
  the week before a game.
- Whether `239.192.0.168` is stable across power cycles.

---

## Appendix: decoder used for this note

Throwaway analysis code. It is standard library only and not linted or tested;
do not import it. Point `PCAP` at a capture.

```python
import os, struct

PCAP = os.environ["PCAP"]
METER_PORT = 50240


def packets():
    """Yield (timestamp, udp_payload) for port-50240 IPv4 UDP in a classic pcap."""
    with open(PCAP, "rb") as f:
        f.read(24)  # global header (little-endian, microsecond pcap assumed)
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            ts, us, incl, _orig = struct.unpack("<IIII", h)
            d = f.read(incl)
            if d[12:14] != b"\x08\x00" or d[23] != 17:  # IPv4, UDP
                continue
            ihl = (d[14] & 0x0F) * 4
            udp = 14 + ihl
            if struct.unpack(">H", d[udp + 2 : udp + 4])[0] != METER_PORT:
                continue
            yield ts + us / 1e6, d[udp + 8 :]


def records(p):
    """Split one payload into (header, [(type, block, variant, data)])."""
    assert p[:4] == b"YMDP" and p[12:16] == b"DM7\x00"
    ms = struct.unpack(">I", p[16:20])[0]
    off, out = 20, []
    while off < len(p):
        kind, block = p[off], p[off + 1]
        variant = p[off + 2]
        length = struct.unpack(">H", p[off + 6 : off + 8])[0]
        out.append((kind, block, variant, p[off + 8 : off + 8 + length]))
        off += 8 + length
    return ms, out


def dbfs(value):
    """Calibrated 2026-10-02 against the pilot tone; valid about -15..-8 dBFS."""
    return None if value <= 3 else (value - 216) / 5


# Example: ch 53 direct out (block 0x10, live variant), as dBFS over time.
# for t, p in packets():
#     ms, recs = records(p)
#     for kind, block, variant, data in recs:
#         if block == 0x10 and variant == 0:
#             print(t, dbfs(data[52]))
```
