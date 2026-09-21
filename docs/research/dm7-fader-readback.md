# DM7 Fader Readback - Research (#18)

**Status:** Research note. No code, no design.md change, nothing sent to a
console.
**Last updated:** 2026-09-20 (every source below was read on this date)
**Question:** Can the DM7 report its DCA fader level back to a controller?

Conventions: **Documented** means a vendor-owned document or page that says it.
**Community-reported** is a lead only, with what it points at. **Unverified**
is anything recalled, inferred, or not found. Quotes are normalized to ASCII.
No port, path or command string appears below unless it was read in the source
named beside it.

---

## Bottom line

**No DM7-specific protocol document with `get`, subscribe or notify for the DCA
fader is published**, and no MIDI data format document is published either, so
nothing here is control-path eligible. But the negative result is softer than
design.md 5.3 states, in three ways. First, the DM7 OSC spec V1.1.0 does define
one `get`: `sscurrentt_ex` ("Get Current Scene_A Number"), which design.md's
"there is no `get`" overlooks; its reply format is not specified. Second,
Yamaha's own Crestron module for the DM7 (a download on the DM7 page) lists
DM7/DM7C, connects on "The TCP-Port ... Default is 49280", polls the console,
and exposes `DcaLevel_fb` "Feedback of Channel-Level", which is strong
first-party evidence that the DM7 answers reads on a second, TCP text protocol;
Yamaha publishes exactly that protocol (with `get` and `NOTIFY`) for the DME7
and MCP1 but not for the DM7, and a community module's DM7 parameter list marks
the DCA fader level `rw`. Third, Mixing Station has a documented desktop-only
REST/WebSocket/OSC API with get and subscribe, but its DCA addressing is served
only by the running app, its licence needs for the API are not stated in its
docs, and it is a paid GUI process, so it is at most a capture aid or a Phase 1
observer, never the control path. **Recommendation for #18:** record "no
documented readback path" for steps 1 and 2, ask Yamaha for the DM7 remote
control protocol document (questions in Recommended next steps), and do one
cheap read-only capture of a free client (Bitfocus Companion or DM Editor) to
learn whether the console notifies observers about fader moves made by *other*
clients, including OSC writes. **The maintainer must decide:** whether to email
Yamaha; whether a read-only `get` or a listen on the OSC socket may be sent to
the console for learning (outside the control path); whether a Mixing Station
licence is worth buying for research; and whether to hold the console on its
current firmware, since V2.00 shipped 2026-09-10 and cannot be rolled back.

---

## 1. Yamaha documentation

### Documented

**The OSC spec is set-only for parameters, with one exception.**
*DM7 Series OSC Specifications*, V1.1.0 (2025-07-01). URL:
https://usa.yamaha.com/files/download/other_assets/5/2234295/DM7_osc_specs_V110_en.pdf
(the repo copy in `docs/vendors/yamaha/`; the DM7 downloads page still lists
V110 today, so it is current). Read 2026-09-20.

- Preamble (p.2): "OSC (Open Sound Control) is a protocol for transmitting
  control information of electronic musical instruments / audio equipment, etc.
  via a network. This protocol can be used to remotely control the DM7
  series." Transport (p.3): "IP Port No.: UDP 49900". "Up to four OSC remote
  controllers can be connected to one DM7 Series console."
- Address grammar (p.3): `/yosc:req/<Action>/<Parameter ID>/<X>/<Y> <value>`,
  with "yosc:req" described as "a Yamaha identifier". The only Action
  documented for parameters is `set`.
- **The exception (p.16, Snapshot table):** "Get Current Scene_A Number",
  Action `sscurrentt_ex`, Parameter ID `scene_a`, type tag `s: string`; and the
  same for `scene_b` ("split mode only"). So the OSC front end does define a
  read. The spec never says what the console sends back or to where. I read
  this in the text extraction and re-derived it independently from the
  decrypted PDF. design.md 5.3 says "There is no `get`"; its count of 169
  `set` rows in the parameter list is right, but the Snapshot table is a
  `get`.
- Known issues (p.14, under "Known issues on DM7 V1.60"): "When the monitor
  source "Define 1" to "Define 8" is selected, "Define" is notified." This is
  the only use of the word "notified"; it implies the console reports state to
  a controller in at least this case, but the spec describes no notification
  channel.
- A note on p.6: "Refer to Table 2 in the full OSC Protocol document for full
  list of send values." Table 2 in this document is Pan/Balance, so this is
  most likely loose wording for this same document. It is not reliable
  evidence of a longer, separate document; it is worth asking about.
- The spec lags the firmware. DM7 firmware V2.00 notes say "The following
  parameters can now be controlled by OSC": `MIXER:Current/InCh/DigitalGain`
  and `MIXER:Current/InCh/Phase`. Neither is in V1.1.0.

**The DM7 downloads page lists no protocol document beyond the OSC spec.**
https://usa.yamaha.com/products/proaudio/mixers/dm7/downloads.html, read
2026-09-20 with plain `curl` (WebFetch got HTTP 403). The documents listed are:
Reference Manual, Owner's Manual, Block Diagram, Firmware Update Guide, DM
Editor Installation Guide, *DM7 Series OSC Specifications* (V110), *DM7 Series
Special Key Command List V1.6*, *MCP1 Remote Control Protocol Specifications
V1.0.0* (this is for the MCP1 control panel, not the DM7), data sheets, and a
zip "Crestron_module_DM7_DM3_CL_QL_TF_TFRACK_v220". No MIDI data format, "Data
List" or DM7 remote control protocol entry.

**Yamaha's own Crestron module lists the DM7 and has DCA level feedback.**
https://usa.yamaha.com/files/download/other_assets/1/1583711/Crestron_module_DM7_DM3_CL_QL_TF_TFRACK_v220.zip
(40 MB; `Doc/YamahaMixerLibrary.pdf`, `Doc/ReleaseNotes_V.2.2.0.txt`,
`Crestron/Prog/Yamaha Mixer Dca v2.2.0.usp`, whose header reads "Dealer Name:
Yamaha Corporation"). Read 2026-09-20.

- Help PDF: "Model: TF-X, QL-X, CL-X, DM7, DM3"; "Mixertype ... (TF1, TF3, TF5,
  TF-Rack, QL1, QL5, CL1, CL3, CL5, DM7, DM7C, DM3)"; "**Port**: The
  TCP-Port for controlling the mixer. Default is 49280."
- Help PDF, `poll`: "It triggers a polling of all parameters of all
  connected function blocks on the rising edge of the input. Usually this is
  not needed for this signal because the module triggers a poll automatically
  if it connects to the mixer or a preset reload is recognized."
- Help PDF, DCA module ("not for DM3"): output `DcaLevel_fb[x]`, "Feedback of
  Channel-Level", and `DcaOn_fb[x]`, "Feedback of Channel On/Off from the
  mixer"; input `DcaLevel[x]` sets the level. The `.usp` source declares the
  same `DcaLevel_fb` output.
- Release notes: "2024-10-02: V2.1.0 - added DM7 and DM7 compact"; "2024-03-29:
  V.2.0.1 Fixing a bug with polling of Input-Fader, Input-On and MuteGroup-On".
- What this proves and does not: a Yamaha-published module for the DM7 polls
  and reports fader level, over TCP 49280. It is a product, not a protocol
  specification; the help PDF contains no command strings, and the protocol
  lives in a compiled library (`YamahaMixer_V.2.2.0.clz`) that I did not open.

**Yamaha publishes exactly that TCP protocol, with get and notify, for other
products.** All read 2026-09-20.

- *DME7 Remote Control Protocol Specifications V1.0.0*:
  https://data.yamaha.com/files/download/other_assets/8/1623778/DME7_remote_control_protocol_spec_v100_en.pdf
  "IP Port No.: 49280"; "Up to eight remote controller devices can connect
  simultaneously to one DME7." Commands from device to controller include
  `NOTIFY set ...` ("Parameter change notification raw value") and
  `NOTIFY sscurrent_ex ...`; commands to the device include `get ...` (parameter
  query, reply `OK get ...`), `set ...`, `sscurrent_ex`, `ssrecall_ex`. A
  section 4.8 is titled "Sequence when parameters are changed by another
  controller". Syntax: "Each command must end with LF (0x0A)".
- *MCP1 Remote Control Protocol Specifications V1.0.0*:
  https://usa.yamaha.com/files/download/other_assets/5/2230685/MCP1-remote-V100_en.pdf
  "TCP Port: 49280"; `NOTIFY sscurrent`, `sscurrent`, `ssrecall`.
- These are other products' documents and their parameter addressing (for
  example `PROC:Remote/<index>` on the DME7) is not the DM7's. They show the
  vendor documents this protocol when it chooses to. The DM7's equivalent is
  not on the DM7 downloads page.
- The OSC address `sscurrentt_ex` in the DM7 spec is the same word as the
  `sscurrent_ex` commands in the DME7 document (with an extra `t`), and the DM7
  OSC parameter ID `MIXER:Current/DCA/Fader/Level` is the same form Yamaha's
  older TCP template uses (below). I infer that the OSC front end is a thin
  mapping over that TCP protocol. That is an inference, not a statement in any
  Yamaha document I read.

**Yamaha's official TCP template documents writes only, for other consoles.**
*Python Script Template V100* (zip):
https://usa.yamaha.com/files/download/other_assets/0/1266290/Python_Script_Template_V100.zip
`command.py` carries "# Port must be 49280"; `command_list.pdf` lists
`ssrecall_ex` and `set MIXER:Current/InCh/Fader/Level [x] 0 [y]` and similar
for CL/QL/TF, with no `get` and no DM7. Read 2026-09-20.

**Firmware release notes.** https://usa.yamaha.com/support/updates/dm7_firm.html
(V2.00, "Last Update 2026-09-10") and the per-version pages
`dm7_firm_v150` ... `dm7_firm_v175`, read 2026-09-20.

- No release note mentions OSC get, notify, subscribe, readback or a remote
  control protocol. OSC appears only as: scene recall broken in V1.52-V1.54
  ("The OSC command for Scene recall does not function correctly"), fixed
  V1.60; new OSC parameters in V2.00.
- V2.00: "Solved a problem where, if a scene was recalled during
  synchronization or resynchronization with DM StageMix, the console would
  sometimes stop responding to certain operations." StageMix and the console
  do synchronize state in both directions.
- V2.00 cautions: direct update from before V1.74 is not possible, and "Once a
  DM7 Series console has been upgraded to Firmware V1.76 or later, it will not
  be possible to rollback to any firmware older than V1.76." The StageMix app
  is now "DM StageMix" and the editor is now "DM Editor"
  (https://usa.yamaha.com/news_events/2026/0910_dm7_v20.html).

### Community-reported

- **Bitfocus Companion module `companion-module-yamaha-rcp`** (MIT, actively
  maintained; last commit 2026-09-09), https://github.com/bitfocus/companion-module-yamaha-rcp
  README lists "DM3/7" and revision entries "Add support for DM7 console"
  (3.2.0) and "Updated commands for DM7 firmware 1.60" (3.5.4). `index.js` sets
  `const RCP_PORT = 49280`, sends `get` requests, and on receipt treats
  `Status == 'NOTIFY'` lines; its `set`/`get` case stores any reply that is not
  an `OK` for a `set`, so a `NOTIFY set` from another controller updates its
  state. It also reacts to `NOTIFY sscurrent_ex` / `sscurrentt_ex`. It ships
  `DM7 Parameters-2.txt`, a list of console-reported parameter rows, including:
  `OK prminfo 44 "MIXER:Current/DCA/Fader/Level" 24 1 -32768 1000 0 "dB"
  integer any rw 100`. Reading: 24 DCAs, range -32768..1000, ending `rw`. The
  meaning of each column is not documented anywhere I read; `rw` presumably
  means read/write. Provenance of that file is not stated.
- **BrenekH/yamaha-rcp-docs**, https://github.com/BrenekH/yamaha-rcp-docs
  ("Unofficial documentation ... with an emphasis on the TF Series"; DM7 is not
  mentioned). It describes the same line protocol: `get`, `set`,
  `ssrecall_ex` from the client; `OK`, `OKm`, `NOTIFY`, `ERROR` from the
  mixer; "`NOTIFY` - Unsolicited message from the mixer indicating a change was
  made outside of the current connection". Its "Official Sources" section
  points at the same Yamaha documents cited above and says the Python template
  is "light on details".
- What these point at: the DM7 accepts `get` for `MIXER:Current/DCA/Fader/Level`
  on TCP 49280 and may send `NOTIFY` for changes made by other clients. Neither
  source establishes that a change made by an **OSC** client is notified to a
  TCP observer, which is the case the box needs.

### Unverified / not found

- A DM7 remote control protocol specification: **none found.** Searched: the
  DM7 downloads page, the firmware and Editor pages, Yamaha search for "DM7
  Remote Control Protocol", "RCP", the Crestron zip, the Python template, the
  DM7 Reference Manual (PDF text) for any protocol mention, and the
  manual.yamaha.com DM7 and StageMix guides. Not searched: a login-gated
  Yamaha developer portal, if one exists (none is linked from the public DM7
  pages).
- Whether the DM7 OSC front end sends any reply to a `get` such as
  `sscurrentt_ex`, and to which address and port. Not in the spec.
- Whether a TCP 49280 session consumes one of the DM7's client slots (OSC: 4;
  Editor + StageMix: 3, StageMix max 2; see section 3). A phantom client that
  displaced the iPad would be a real cost.
- Whether TCP 49280 is left enabled by default on the DM7 or is tied to a
  network setting. Not documented for the DM7.
- The DM7 command grammar from memory (`prminfo`, `OKm`, and so on) is not to be
  relied on beyond what the community sources above show.

---

## 2. MIDI SysEx

### Documented

The DM7 has **no MIDI data format document on Yamaha's DM7 downloads page**
(section 1). Searched: that page, the firmware pages, the Reference Manual and
its appendix. Where it would live: as one more entry beside the OSC spec on
https://usa.yamaha.com/products/proaudio/mixers/dm7/downloads.html, or a
"Data List" appendix. Neither exists as of 2026-09-20.

What the DM7 documentation does say about MIDI (Reference Manual D1, "Published
05/2025", https://usa.yamaha.com/files/download/other_assets/2/2148452/DM7_RM_En_D1.pdf,
and the HTML pages https://manual.yamaha.com/pa/mixers/dm7/rm/en-US/8463214475.html
and .../8463218315.html, read 2026-09-20):

- MIDI/GPI (MIDI Setup) screen: "This screen sets the MIDI input and output."
  Sources selectable are "USB port" and "PY slot" (the PY-MIDI-GPI card). There
  is a Program Change field (Tx, Rx, Echo), a Control Change field (Tx, Rx,
  Echo), "Other Command field ... echo output", and Program Change modes. There
  is **no Parameter Change or SysEx setting** on this screen.
- MIDI/GPI (Control Change) screen: "This screen assigns parameters such as
  fader operation and [ON] key on/off to Control Changes. MIDI Control Change
  messages can be used to control assigned parameters." "You can use control
  numbers 1-31, 33-95, and 102-119." The Control Change field switches "send"
  and "receive" separately, so the console can be configured to transmit CC when
  an assigned control is operated. The manual does not list which parameters
  are assignable, so **whether a DCA fader is assignable is not documented**.
- Reference Manual appendix, "MIDI Implementation Chart" (Version 1.0, PDF
  p.457): Control Change numbers "0,32", "6,38", "98,99", "1-31,33-95,102-119"
  with value range "0-127", and NRPN, Bank Select, Program Change. This is a
  7-bit range per message. The System Exclusive row of this chart appears to
  read X (not transmitted) and X (not recognized) in my text extraction, but
  the extraction cannot align the chart's columns reliably. **Read that cell in
  the PDF by eye before relying on it either way.**
- Firmware notes mention MIDI only for the PY-MIDI-GPI card (running status,
  GPI) and a configuration-file fix. Nothing about SysEx or parameter change.

Net for DCA readback over MIDI, on what is documented: at best coarse (0-127)
CC transmission over USB or a PY card, only if the DCA fader can be assigned,
with no network path. It does not look like a better route than TCP.

### Community-reported

Nothing found on DM7 SysEx. A general search returned only other Yamaha
products (DX7, CL) and generic Yamaha SysEx layout, which says nothing about
the DM7. Not cited.

### Unverified / not found

- The DM7 MIDI data format document: not found (above).
- Whether the DM7 transmits or accepts SysEx parameter change: unknown; the
  implementation chart cell is unreadable in my extraction.
- Which parameters the Control Change screen lets you assign.
- The CL/QL-era "Parameter Change" checkbox (from memory) is not on the DM7 MIDI
  Setup screen as documented above.

---

## 3. Mixing Station and StageMix

### Documented

**What the API is.** Mixing Station's own manual, "APIs" page:
https://mixingstation.app/ms-docs/use-cases/apis/ (and the same text in
https://mixingstation.app/ms-docs/print_page/, source markdown at
https://github.com/davidgiga1993/mixing-station-docs, "Official documentation
for the App"). Read 2026-09-20.

- "Mixing Station provides different APIs for integration with external
  software and hardware. The goal of these APIs is to cover the majority of
  console parameters with a unified API, allowing your application to work with
  every mixer supported by Mixing Station."
- "Note that the APIs are only available in the desktop version of Mixing
  Station." Desktop is Windows, macOS 10.15 or later, and Linux (arm64, x64;
  needs OpenGL ES 3.0; the Linux install notes launch a Java jar) per the
  Desktop page.
- Two APIs: REST ("http and websockets": "App interaction, Mixer parameters,
  subscriptions, metering") and OSC ("OSC via UDP": "Mixer parameters,
  subscriptions"). "To enable API access, open the global app settings and
  enable REST and/or OSC. Here you can also change the port number used by the
  APIs." Command-line flags `-web` and `-osc` give each port ("disabled by
  default"). No default port number is stated.
- Where the API is documented: "The full API documentation can be viewed via
  the REST api ... Open the following URL in your browser:
  http://localhost:<your-configured-port> The webpage describes all available
  API endpoints and allows you to explore all data exposed by Mixing Station."
  **The endpoint reference is served only by a running, enabled app; it is not
  published on the public docs site.**
- Subscribe and get: "To receive value updates you'll need to subscribe first.
  A subscription describes a parameter path for which the client will receive
  updates in case of values changes. Requesting a new subscription will also
  send you the initial value(s)." WebSocket example, subscribe to every
  channel's fader: `{"path": "/console/data/subscribe", "method": "POST",
  "body": {"path": "ch.*.mix.lvl", "format": "val"}}`. OSC: "Send the following
  packet at least once every 5 seconds to get updates for all parameters"
  (`/hi/v` for plain values, `/hi/n` normalized); "A OSC packet without any
  parameters is used to request the current value": `/con/[vn]/{dataPath}`;
  set adds a typed argument, e.g. `/con/v/ch.0.mix.lvl f -5`. "Note that OSC
  bundles are not supported." Values are "Plain values" (for example dB) or
  "Normalized" (0-1).
- Connection state is reported too. APIs page: "The app state parameter
  indicates in which state Mixing Station currently is. This is important to
  know in case for example the connection to the mixer has been lost." The
  states (top level IDLE, CONNECTING, CONNECTED, RECONNECTING) are on
  https://mixingstation.app/ms-docs/use-cases/app-states/.

**What it says about the DM7.**
- Supported: on the compatible-mixers list, "DM7: V1.70-V2.0" and "DM7C:
  V1.70-V2.0". Yamaha DM page: "DM5 / DM7 ... Access Mode / Maximum number of
  connections: FoH 3; Personal / custom 48."
  https://mixingstation.app/ms-docs/mixers/yamaha/dm/
- **DCA fader level over the API: not stated anywhere public.** The docs'
  examples use `ch.0.mix.lvl` (channel 1 fader). The docs describe "Unlimited
  DCA" as Mixing Station's own "IDCA" groups ("The IDCA stores the ratios of the
  levels of the assigned channels"), which look like app-side groups rather
  than the console's DCAs; I could not confirm how the console's own DCAs are
  exposed. The console DCA path can only be read from the data explorer
  in a running licensed app. I did not find it, so I do not state it.
- Only DM network fact stated: "In personal monitoring mode metering it
  received from the mixer using multicast (239.192.0.164). Make sure your
  network equipment (wifi AP, Router, ...) allows multicast traffic!"

**Licence.**
- https://mixingstation.app/ms-docs/license/overview/: two models. "A single
  license unlocks a single mixer series on a single platform (iOS, Android,
  Desktop)"; a subscription "will unlock all supported mixers" on up to 4
  devices. "One license can be activated on up to 4 devices of the same
  platform."
- Subscription renewal "you'll need to connect to the internet once the next
  billing period started - with a grace period of 1 week." The control VLAN has
  no internet route, so a subscription would need a periodic other-network
  visit; a one-time licence does not expire.
- Desktop page: "Please make sure the app is working in offline mode before
  purchasing any licenses!" iOS page: "You can use all features of the app in
  the offline mode for free. As soon as you want to connect to a mixer you must
  buy the app for that mixer model." (iOS-specific wording.)
- **The docs do not state an API-specific licence tier.** The Free/Pro feature
  table does not list the API. The maintainer's report of a paid requirement is
  consistent with "connect to a mixer requires a licence" but I could not
  confirm a separate API rule from Mixing Station's own pages.

**How Mixing Station talks to the DM7.** **Not documented.** The manual gives
no protocol or port for Yamaha consoles. Facts that bear on it: the metering
multicast above; the FoH-3 connection limit; and the Wireshark support page
(below), which tells users to "Install the editor software of the mixer" for a
capture. That page implies nothing about the protocol.

**Mixing Station has capture tooling of its own.**
https://mixingstation.app/ms-docs/support/wireshark/ (read 2026-09-20): "This
page describes how to capture packets. This is only required if you have been
instructed by the support to do so." In-app: needs "Mixing Station 2.5.0 or
newer"; Gear icon -> `Global` -> `Development` -> `Packet capture`; connect;
"Gear icon -> `Create Backup`". The backup is meant for support; its format is
not documented. Wireshark route: "Start wireshark as administrator ... Start
the editor software of the mixer ... Once the software has been connected to
the mixer and synchronized all the data, stop the capture."

**Support model.** https://mixingstation.app/ms-docs/print_page/ "Support":
"Mixing Station is developed by a single person, thus I can't provide in-depth
support for every customer." Relevant for a Saturday dependency.

**StageMix and the same console channel.**
- OSC spec p.3: the OSC IP setting "Uses the same IP address as Editor and
  StageMix." i.e. the "For Mixer Control" network. StageMix Guide (PDF, read
  2026-09-20, https://usa.yamaha.com/files/download/other_assets/4/2139204/DM7_StageMix_UG_En_b0.pdf,
  p.7): "On the FOR MIXER CONTROL tab, set the CONSOLE IP SETTING to ENABLE".
- Client limits: StageMix Guide p.11: "the maximum number per control surface/
  console (two instances) of StageMix". Reference Manual (p.407): "DM7 Editor
  and DM7 StageMix can be used on up to three terminals at the same time.
  However, DM7 Editor can only be used on one of them." The console's status
  area shows
  "ONLINE:[n]: The number of online Editor, StageMix, and DM7 Control units"
  (Reference Manual p.283).
- Meters: StageMix Guide p.26: "The DM7 Series console can be connected to even
  if set up on a different subnet, but the level meter information cannot be
  displayed." Reference Manual: "Turn UniCast Level Meter on the Setup menu on
  to display level meter information, even if the DM7 Series is on a different
  subnet." Consistent with a multicast or broadcast meter path separate from
  the control connection.
- **Which protocol or port StageMix or Editor use is not documented** in any
  Yamaha document I read (the Editor Installation Guide has no port list; it
  only says that a firewall "may" stop communication). Firmware V2.00's fix
  about "synchronization or resynchronization with DM StageMix" shows the
  console synchronizes state with StageMix.

### Community-reported

- Bitfocus Companion and the RCP docs (section 1) show a third-party client
  reading and subscribing to DM7 state over TCP 49280. That is community
  evidence about the console, not about Mixing Station. There is no source I
  read that says Mixing Station or StageMix use TCP 49280; that link is an
  inference from the shared "For Mixer Control" network and the shared
  three-slot pool (the Mixing Station FoH limit of 3 matches the console's
  Editor + StageMix limit of 3, which suggests but does not prove that it
  consumes the same slots).
- A community search result suggested, for a different mixer, that the API
  numbers all channels 0..N in one index space including DCAs. Not fetched, not
  about the DM7; not relied on.

### Unverified / not found

- The Mixing Station API path for a console DCA fader (`dca...`, or channel
  index): **not found**; the explorer needs a running licensed app.
- Whether the API needs a paid licence, and which tier: not stated in the docs.
- How Mixing Station or StageMix reach the DM7 on the wire: not stated.
- Apple's `rvictl` method for capturing an iPad's traffic over USB to a Mac
  (recalled, not read; the Apple page did not render): **unverified**.

### Assessment against the hard constraints

(a) **Mixing Station as a runtime readback source.** Not for the control path:
it is a closed, paid, GUI-first desktop application (Java, OpenGL ES) that
would have to stay connected to the console in the press box; its DCA
addressing is not public; the manual ties support to a single developer; a
subscription needs an internet visit the control VLAN cannot make; it consumes
a console connection slot (FoH 3). It breaks "nothing undocumented" and "no
extra moving parts". Two roles are conceivable and both sit outside the control
path: a Phase 1 *observer* that logs subscribed DCA fader values as extra
ground-truth labels for operator moves (design.md 9 currently derives those
from the post-DCA reference channel), and a research aid. Both are optional
dependencies, may take dependencies under CLAUDE.md ("Everything else may take
dependencies"), and would be the maintainer's call.

(b) **As a packet-capture aid.** Yes, but it is not the cheapest one. Mixing
Station runs on macOS, so Wireshark on the same laptop sees its console
traffic with no mirror port. But connecting to a console requires buying a
licence, and it teaches the protocol only if the traffic is plain text. A free
client (Bitfocus Companion) or Yamaha's own DM Editor gives the same
observations at no cost; see What a capture would need.

---

## What a positive result would simplify

If the DM7 is confirmed to answer a read and notify on fader moves, and Yamaha
documents it, a design.md amendment would reverse "OSC is write-only" and the
following would change. Writes stay on OSC `MIXER:Current/DCA/Fader/Level`;
readback is an **additional** confirmation channel, never a dependency for
moving the fader.

- **5.3 "OSC is write-only":** the section, the corrected note that
  `sscurrentt_ex` is a read, and the last paragraph of "What the box believes
  about the fader" ("If the console turns out to report the fader (#18), 'who
  has control' and 'is the position known' become two independent facts").
- **#107 `level_known`:** the boot-unknown case is closed by a read at
  connect (the Yamaha Crestron module polls on connect); a StageMix hand-off
  stops being a belief loss, because an iPad move would be observed. The
  relative-vs-absolute gate remains as the fallback whenever the readback link
  is down (fail safe).
- **#116, the lost-packet half:** "a packet that leaves the box and is simply
  lost is indistinguishable from one that landed" would be resolved by a
  confirmed value.
- **#12/#118/#103:** `handed-off` stays as the record of who held the fader
  (Phase 1 needs it), but the flag stops being the only evidence.
- **5.5 UI "commanded vs confirmed":** the DCA readout could show a confirmed
  value beside the commanded one, rendered differently as the rule already
  demands, and would show iPad moves. Commanded-only would remain the display
  when the readback link is down, said plainly.
- **Phase 1 labels (design.md 9):** notify events would be a second source of
  operator fader moves, complementing the post-DCA reference channel.

What would **not** change: writes on OSC, faders only, the hold/fade behavior,
and the operator's permanent override authority.

---

## What a capture would need

Purpose: learning, and deciding whether to ask Yamaha. Not for shipping.
Everything below sends a client's traffic to a console, so do it at a bench or
in a quiet non-game window, on a DCA that feeds nothing live, and never on a
game day. Store captures on the NAS, not in git.

**Common setup (macOS laptop, Wireshark).**
1. Record the console firmware version and its "For Mixer Control" IP address.
   Put the laptop on that subnet (StageMix and Editor need the same subnet for
   meters; the OSC address is the For Mixer Control IP).
2. Wireshark, capture on the interface facing the console, with a capture
   filter on the console's IP. Start the capture before the client connects,
   so the connect and initial sync are included. Saving pcapng.
3. Keep a timestamped log of every action (which fader, from where, when).
   Wireshark's capture is on the laptop's clock; note the wall-clock time
   at each action so events can be matched afterward.
4. Look for: the client's first lines (a dump or a read of every parameter), any
   `get`/`OK`/`NOTIFY` text, and any UDP reply to the OSC port. Yamaha's
   MCP1/DME7 documents say the protocol is ASCII with LF line ends, so plain
   text is expected if it is that protocol; a binary or TLS session would say
   otherwise.

**Capture A - Bitfocus Companion with the yamaha-rcp module (free).** A working
DM7 client that speaks TCP 49280 per its source. No licence, no Editor
synchronization risk. Configure it with the console IP and no actions bound so
that it only reads. Note: even so it sends `get` requests, which is a decision
for the maintainer (see next steps).

**Capture B - DM Editor for macOS (Yamaha's own client, free).** DM Editor
V2.0.0 for Mac runs on macOS 26/15/14 (downloads page, 2026-09-10); older
firmware needs the matching Editor version, so use the Editor from Yamaha's
"Compatibility List" for the console's firmware ("Using an unsupported
Firmware/Editor ... may result in unexpected behavior"). **Danger: the Editor's
first step is synchronization with a direction.** Reference Manual p.421:
"DM7->PC Parameter settings on the DM7 Series unit will be copied to DM7
Editor. DM7<-PC Parameter settings on DM7 Editor unit will be copied to the
DM7 Series unit." Choose only DM7->PC, keep a console backup, and "Do not
operate the DM7 Series unit during synchronization." The Editor counts against
the Editor + StageMix limit of 3 (Editor once).

**Capture C - Mixing Station desktop, if the maintainer buys a licence.** The
manual's Wireshark page says to capture while the client connects and syncs,
then stop. Steps: enable the REST API in Global settings, run the built-in
data explorer at `http://localhost:<port>` to find the DCA path, subscribe,
and note the FoH connection limit of 3. The in-app Packet capture (2.5.0+) is a
support tool with an undocumented backup format, so prefer Wireshark. It adds
what the free clients cannot: how Mixing Station's own API presents DCA fader
changes and what path it uses.

**Experiments to run in each capture (in order):**
1. Connect only. Observe the initial state transfer.
2. Move the DCA fader on the console's own surface. Does the observer receive a
   notification?
3. Move it from the iPad (StageMix), if a second client is available.
4. **Write it from the box's OSC path** (for example `verify_dm7` on a bench
   DCA). Does the observer receive a notification? This is the case the box
   needs; the community sources do not settle it.
5. Optional: with only OSC, send the documented `/yosc:req/sscurrentt_ex
   scene_a` and listen on the sending UDP socket for any reply; this shows
   whether the OSC front end replies at all.

**StageMix on an iPad.** The iPad connects over Wi-Fi to the same For Mixer
Control network, so the laptop will not see its traffic unless one of: a
mirror (SPAN) port on the switch the console and AP share, with the laptop on
that port; a capture on the Wi-Fi access point or router if it supports it; or
the iPad tethered to the Mac by USB with Apple's remote virtual interface
(unverified; the Apple documentation page did not render for me). Captures A
and B already observe the console's side of the same channel, so a StageMix
capture is lower priority. It answers only whether StageMix uses the same
protocol as the clients above.

---

## Recommended next steps

1. **Record the negative result and the correction (small, docs only).** Steps
   1 and 2 of #18 found no documented get/subscribe/notify for the DCA fader
   and no MIDI data format. A PR should amend design.md 5.3 (not this note) to
   say: `sscurrentt_ex` is a documented OSC `get` (scene number); the console
   also has a TCP protocol on port 49280 that Yamaha documents for other
   products and that a Yamaha DM7 Crestron module uses; and that DM7 readback
   is unverified and out of the control path this season. Also update the open
   item in design.md 7 ("MIDI ... needs the DM7 MIDI data format document") to
   "no such document is published; Reference Manual documents CC and Program
   Change only".
2. **Ask Yamaha.** Through the contact form linked from the DM7 downloads page
   (https://usa.yamaha.com/support/contacts/form.html?page=pa) or the maintainer's
   dealer channel. Ask, concretely:
   (a) Is there a "DM7 Series Remote Control Protocol Specification" (the TCP
   port 49280 text protocol, as published for the DME7 and MCP1)? If not
   public, can it be shared for a non-commercial integration?
   (b) Does the DM7 support `get` and unsolicited notification for
   `MIXER:Current/DCA/Fader/Level`? Does it notify a controller when the
   change was made by an OSC client?
   (c) What does the OSC front end return for `sscurrentt_ex`, and where?
   (d) Does a TCP 49280 session consume one of the DM7's client slots
   (OSC 4; Editor + StageMix 3)?
   (e) Is there a "full OSC Protocol document" beyond V1.1.0 (the spec
   references one), and a MIDI data format document, and does the DM7 send SysEx
   parameter change?
   (f) Which firmware do these hold for; V2.00 shipped 2026-09-10?
3. **One cheap read-only capture (Capture A, or B with DM7->PC only), bench
   only, with experiments 1, 2 and 4.** This is what would tell us whether
   notification exists for OSC-originated changes. It needs the maintainer's
   explicit go-ahead, since it sends read requests to a console (the tool, not
   the control path). No purchase.
4. **Defer a Mixing Station purchase** until step 3 shows whether the free
   clients already answer the question. If it does not, or if a Phase 1
   observer role is wanted, revisit.
5. **Hold the console firmware.** Do not update to V2.00 for this work: it
   cannot be rolled back below V1.76, requires V1.74 first, and renames the
   StageMix and Editor apps. Record the current version with every capture.
6. **Optional bounded look:** read the visible O/X cell for "System Exclusive"
   in the Reference Manual's MIDI Implementation Chart (PDF p.457), which my
   text extraction could not align.

---

## Could not retrieve or verify

- Yamaha DM7 downloads pages on usa.yamaha.com returned HTTP 403 to WebFetch
  (also europe.yamaha.com and in.yamaha.com); the usa page was read with plain
  `curl`, the others were not needed.
- Yamaha DM3 and RIVAGE PM OSC specifications (URLs taken from a community
  page): returned HTML, not PDF. Not read.
- Yamaha's DM7 MIDI data format / Data List: does not appear to exist on the
  public site.
- Mixing Station's live REST/OpenAPI reference: served only by a running app.
  mixingstation.app main pages are a script-rendered shell with no text.
- Mixing Station's statement on whether the API needs a licence: silent in the
  docs read.
- Apple's Recording a Packet Trace page (rvictl): did not render.
- The Yamaha `YamahaMixer_V.2.2.0.clz` library (the actual DM7 command strings):
  not opened. Doing so would be reverse engineering a vendor binary; not done.
- The MIDI Implementation Chart's SysEx cell: text extraction ambiguous.
- Community search-result leads not fetched: the yamaha-rcp Rust crate
  (docs.rs), the Chataigne module, a Mixing Station DCA indexing remark.

Note on method: the Yamaha PDFs are AES encrypted (the Editor, StageMix and
Reference Manual use AES-256, the others AES-128, empty user password). I
decrypted them with a throwaway script outside the repo and read the text;
extracted text can misalign table columns, so any table cell relied upon should
be checked against the PDF.

---

## Sources

All read 2026-09-20 unless stated.

Yamaha (vendor-owned)
- DM7 Series OSC Specifications V1.1.0:
  https://usa.yamaha.com/files/download/other_assets/5/2234295/DM7_osc_specs_V110_en.pdf
- DM7 downloads page: https://usa.yamaha.com/products/proaudio/mixers/dm7/downloads.html
- DM7 Firmware V2.00: https://usa.yamaha.com/support/updates/dm7_firm.html
  and V1.75, V1.70, V1.60, V1.54, V1.53, V1.52 pages of the same form
  (`dm7_firm_v175.html` and so on)
- Firmware V2.0 announcement:
  https://usa.yamaha.com/news_events/2026/0910_dm7_v20.html
- DM7 Reference Manual D1 (PDF):
  https://usa.yamaha.com/files/download/other_assets/2/2148452/DM7_RM_En_D1.pdf
- Reference Manual HTML, MIDI Setup and Control Change screens:
  https://manual.yamaha.com/pa/mixers/dm7/rm/en-US/8463214475.html
  https://manual.yamaha.com/pa/mixers/dm7/rm/en-US/8463218315.html
- DM7 StageMix User Guide (PDF):
  https://usa.yamaha.com/files/download/other_assets/4/2139204/DM7_StageMix_UG_En_b0.pdf
- DM Editor Installation Guide A0:
  https://usa.yamaha.com/files/download/other_assets/7/3236457/DM_Editor_Installation_Guide_En_A0.pdf
- Crestron module for DM7/DM3/CL/QL/TF, v2.2.0:
  https://usa.yamaha.com/files/download/other_assets/1/1583711/Crestron_module_DM7_DM3_CL_QL_TF_TFRACK_v220.zip
- DME7 Remote Control Protocol Specifications V1.0.0:
  https://data.yamaha.com/files/download/other_assets/8/1623778/DME7_remote_control_protocol_spec_v100_en.pdf
- MCP1 Remote Control Protocol Specifications V1.0.0:
  https://usa.yamaha.com/files/download/other_assets/5/2230685/MCP1-remote-V100_en.pdf
- Python Script Template V100 (CL/QL/TF):
  https://usa.yamaha.com/files/download/other_assets/0/1266290/Python_Script_Template_V100.zip

Mixing Station (vendor-owned)
- APIs: https://mixingstation.app/ms-docs/use-cases/apis/
- Print version (Yamaha DM, licence, desktop, support sections):
  https://mixingstation.app/ms-docs/print_page/
- Source markdown: https://github.com/davidgiga1993/mixing-station-docs
  (docs/use-cases/apis.md, use-cases/app-states.md, mixers/yamaha/dm.md,
  license/overview.md, platforms/desktop.md, support/wireshark.md,
  feature-list.md, settings/global.md)

Community (leads only)
- https://github.com/bitfocus/companion-module-yamaha-rcp (README.md,
  index.js, "DM7 Parameters-2.txt", MIT)
- https://github.com/BrenekH/yamaha-rcp-docs (README.md)
