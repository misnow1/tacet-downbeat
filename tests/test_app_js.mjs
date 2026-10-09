// The operator page's logic, tested against a stubbed browser.
//
// The same bargain as reaper/test_tacet_mirror.lua: the logic here is ours and
// gets tested, while whether a real browser behaves the way this stub pretends
// is what the first-run checklist in docs/handoff.md is for.
//
//     node tests/test_app_js.mjs      (or: make test-js)

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(here, "..", "src", "tacet", "static", "app.js"), "utf8");

// Each id on the page and its nearest ancestor that has an id (#156). The stub
// uses it to give nodes a parent, so a touch can bubble as it does in the real
// markup; tests/test_web.py holds the file to the page's own HTML.
const PAGE_PARENTS = JSON.parse(readFileSync(join(here, "fixtures", "page-parents.json"), "utf8"));

let checks = 0;
let failures = 0;

function check(label, got, want) {
  checks += 1;
  const [g, w] = [JSON.stringify(got), JSON.stringify(want)];
  if (g !== w) {
    failures += 1;
    console.log(`FAIL  ${label}\n        got  ${g}\n        want ${w}`);
  }
}

// -- the stub ---------------------------------------------------------------

function element(id) {
  const classes = new Set();
  const children = [];
  let text = "";
  let html = "";
  return {
    id,
    // How many times the page assigned this node's text or HTML (#51): a
    // repaint of something unchanged must not touch it.
    writes: 0,
    className: "",
    style: {},
    dataset: {},
    onclick: null,
    disabled: false,
    children,
    parentNode: null,
    // Every listener added, by type, so a test can see what the page listens
    // for and where (#156).
    listeners: {},
    addEventListener(type, fn, options = {}) {
      (this.listeners[type] ||= []).push({ fn, options });
    },
    // As the DOM: a node's text is its descendants' text, and setting it (or
    // innerHTML) replaces the children. The page's buttons hold an icon and a
    // label span since #155, and the tests read a button's text as before.
    get textContent() {
      return children.length ? children.map((c) => c.textContent).join("") : text;
    },
    set textContent(value) {
      this.writes += 1;
      children.length = 0;
      text = String(value);
    },
    get innerHTML() {
      return html;
    },
    set innerHTML(value) {
      this.writes += 1;
      children.length = 0;
      text = "";
      html = String(value);
    },
    classList: {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      contains: (name) => classes.has(name),
    },
    appendChild(child) {
      child.parentNode = this;
      children.push(child);
      return child;
    },
  };
}

function browser(options = {}) {
  const nodes = new Map();
  const created = [];
  const sockets = [];
  const posted = [];
  const intervals = [];
  const frames = [];
  const documentListeners = {};
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) {
        const node = element(id);
        if (PAGE_PARENTS[id]) node.parentNode = document.getElementById(PAGE_PARENTS[id]);
        nodes.set(id, node);
      }
      return nodes.get(id);
    },
    createElement(tag) {
      const node = element(tag);
      node.tag = tag;
      created.push(node);
      return node;
    },
    // The page only ever asks for buttons across the fader column and the
    // two tabs (#5). The stub ignores the selector itself and answers with
    // every button it has been asked to make.
    querySelectorAll: () => created.filter((node) => node.tag === "button"),
    // Recorded, never fired: the page re-takes its wake lock when the tab
    // comes back, and what is tested is the branch it calls into. Recording
    // lets a test see that nothing listens for touches up here (#156).
    addEventListener(type, fn, options = {}) {
      (documentListeners[type] ||= []).push({ fn, options });
    },
    hidden: false,
  };
  const context = createContext({
    console,
    // The page reads `Date.now()` for its own clock. A test that needs the
    // page's clock to move, or to stand still, passes `now` (milliseconds).
    ...(options.now
      ? {
          Date: class extends Date {
            static now() {
              return options.now();
            }
          },
        }
      : {}),
    // The page's monotonic clock. It follows `now` unless a test passes `perf`
    // to make the two disagree (#51); with neither it is the real one.
    performance: {
      now: () => (options.perf ?? options.now ?? (() => performance.now()))(),
    },
    // Kept rather than run: a test fires `frames[i]()` itself, so it decides
    // when the page repaints a move (#154).
    requestAnimationFrame(callback) {
      frames.push(callback);
      return frames.length;
    },
    document,
    location: { protocol: "http:", host: "box:8080" },
    // No wake lock, which is the deployed case: the API needs a secure context
    // and the page is served over plain HTTP.
    navigator: {},
    // The note button's only way to ask for text. Defaults to answering
    // nothing, which is also what a real dialog gives back if it is
    // cancelled; a test that wants text passes its own.
    prompt: (text) => (options.prompt ? options.prompt(text) : null),
    setTimeout() {},
    clearTimeout() {},
    AbortController,
    // The banner and the level readout are repainted on a tick so a silence is
    // noticed without a message arriving to notice it. Kept rather than fired:
    // a test that wants a tick calls `intervals[0]()` itself, and every other
    // one calls paintLink directly.
    setInterval(callback) {
      intervals.push(callback);
    },
    // The page boots on load. Neither of these may resolve, or the tests would
    // be racing the page's own first render. What was asked for is kept, so a
    // tap can be checked by what it sent.
    fetch: (path, init = {}) => {
      posted.push({ path, body: init.body ? JSON.parse(init.body) : undefined });
      return options.fetch ? options.fetch(path, init) : new Promise(() => {});
    },
    WebSocket: class {
      constructor(url) {
        this.url = url;
        this.sent = [];
        sockets.push(this);
      }
      send(text) {
        this.sent.push(JSON.parse(text));
      }
      close() {
        if (this.onclose) this.onclose();
      }
    },
  });
  runInContext(SOURCE, context);
  return { context, nodes, created, sockets, posted, intervals, frames, documentListeners };
}

// -- snapshots --------------------------------------------------------------

// Written by the box itself: tests/snapshots.py runs the real App into a
// handful of states, and tests/test_snapshot_fixtures.py fails when App no
// longer produces them. These tests used to build a snapshot by hand, and the
// copy drifted from what the box sends without either suite noticing (#38).
const FIXTURES = join(here, "fixtures");
const PREFIX = "snapshot-";
const SUFFIX = ".json";
const SNAPSHOTS = Object.fromEntries(
  readdirSync(FIXTURES)
    .filter((name) => name.startsWith(PREFIX) && name.endsWith(SUFFIX))
    .map((name) => [
      name.slice(PREFIX.length, -SUFFIX.length),
      JSON.parse(readFileSync(join(FIXTURES, name), "utf8")),
    ]),
);

// The curves the box's ramp takes, and the move description each was taken
// from (#154): `moveDbAt` is held to them below.
const CURVES = JSON.parse(readFileSync(join(FIXTURES, "move-curves.json"), "utf8"));

// A copy of `base` with some fields changed. Only fields the box actually sends
// may be: a change to one it does not describes a box that does not exist, and
// that is exactly how the hand copy drifted.
function overlay(base, changes, where) {
  const result = structuredClone(base);
  for (const [key, value] of Object.entries(changes)) {
    if (!(key in base)) throw new Error(`${where}.${key} is not in the box's snapshot`);
    result[key] = value;
  }
  return result;
}

// The box's standing-down snapshot, with the parts a test is about changed.
function snapshot(recording = {}, fader = {}, buttons = undefined, openSpans = []) {
  const base = SNAPSHOTS["standing-down"];
  const snap = structuredClone(base);
  snap.recording = overlay(base.recording, recording, "recording");
  snap.fader = overlay(base.fader, fader, "fader");
  if (buttons !== undefined) {
    snap.buttons = buttons.map((button) => overlay(base.buttons[0], button, "buttons[]"));
  }
  snap.open_spans = openSpans;
  return snap;
}

function rendered(recording = {}, fader = {}) {
  const { context, nodes } = browser();
  context.render(snapshot(recording, fader));
  return nodes;
}

// -- timecode ---------------------------------------------------------------

const { context } = browser();
const timecode = context.timecode;

check("timecode: zero", timecode(0), "0:00:00.000");
check("timecode: milliseconds", timecode(0.058), "0:00:00.058");
check("timecode: seconds", timecode(12.5), "0:00:12.500");
check("timecode: minutes", timecode(599.25), "0:09:59.250");
check("timecode: an hour", timecode(3600), "1:00:00.000");
check("timecode: a whole game", timecode(7432.123), "2:03:52.123");
check("timecode: negative", timecode(-3.5), "-0:00:03.500");
// Rounding before the split, so this is a minute rather than :60.
check("timecode: carries rather than showing :60", timecode(59.9996), "0:01:00.000");
check("timecode: carries across an hour", timecode(3599.9999), "1:00:00.000");

// -- the recording tag ------------------------------------------------------

// Reaper is silent whenever it is parked, so silence alone is not a fault.
// These four have to stay tellable apart; see design.md 5.5.
const TAGS = {
  live: { known: true, label: "confirmed", className: "tag confirmed" },
  quiet: { known: true, label: "confirmed (idle)", className: "tag confirmed" },
  lost: { known: false, label: "LINK LOST", className: "tag unknown" },
  unknown: { known: false, label: "no feedback", className: "tag unknown" },
};

for (const [liveness, want] of Object.entries(TAGS)) {
  const nodes = rendered({ liveness, known: want.known, confirmed: true, position: 1.5 });
  check(`${liveness}: tag label`, nodes.get("rec-tag").textContent, want.label);
  check(`${liveness}: tag class`, nodes.get("rec-tag").className, want.className);
}

// A live link and an unknown record state are not a contradiction: Reaper
// announces /record only when it changes, so a box that started after the last
// change has heard /time and nothing about recording. The tag has to describe
// the value, not the link, or this reads as "I do not know, and I am sure".
for (const liveness of ["live", "quiet"]) {
  const nodes = rendered({ liveness, known: false, confirmed: true });
  check(`${liveness} with no record state: value`, nodes.get("rec").textContent, "unknown");
  check(`${liveness} with no record state: label`, nodes.get("rec-tag").textContent, "not yet reported");
  check(`${liveness} with no record state: class`, nodes.get("rec-tag").className, "tag unknown");
}

// The invariant behind all of it.
for (const liveness of Object.keys(TAGS)) {
  for (const known of [true, false]) {
    const nodes = rendered({ liveness, known, confirmed: true });
    if (nodes.get("rec").textContent === "unknown") {
      check(
        `${liveness}/known=${known}: an unknown value is never tagged confirmed`,
        nodes.get("rec-tag").className.includes("confirmed"),
        false,
      );
    }
  }
}

check(
  "every liveness gets its own label",
  new Set(Object.values(TAGS).map((want) => want.label)).size,
  Object.keys(TAGS).length,
);

for (const liveness of Object.keys(TAGS)) {
  const nodes = rendered({ liveness, known: TAGS[liveness].known, confirmed: true });
  // Recording is confirmed and the fader is only ever commanded. If the
  // recording tag ever borrows the commanded styling the two stop being
  // tellable apart, which design.md 5.5 forbids.
  check(
    `${liveness}: never borrows the commanded styling`,
    nodes.get("rec-tag").className.includes("commanded"),
    false,
  );
}

// -- what the recording line says -------------------------------------------

check(
  "a rolling recorder says so",
  rendered({ liveness: "live", known: true, recording: true, confirmed: true }).get("rec")
    .textContent,
  "ROLLING",
);
check(
  "a parked recorder reads stopped, not unknown",
  rendered({ liveness: "quiet", known: true, recording: false, confirmed: true }).get("rec")
    .textContent,
  "stopped",
);
check(
  "a lost link never claims to be rolling",
  rendered({ liveness: "lost", known: false, recording: true, confirmed: false }).get("rec")
    .textContent,
  "unknown",
);
check(
  "an unheard-from recorder reads unknown",
  rendered().get("rec").textContent,
  "unknown",
);

// -- the playhead -----------------------------------------------------------

check(
  "the playhead is a timecode",
  rendered({ liveness: "live", known: true, confirmed: true, position: 7432.123 }).get("rec-pos")
    .textContent,
  "at 2:03:52.123",
);
check(
  "no playhead without a believed reading",
  rendered({ liveness: "lost", known: false, confirmed: false, position: 12.0 }).get("rec-pos")
    .textContent,
  "",
);

// -- the record button ------------------------------------------------------

// Reaper's /record is a toggle. A start button that stops on the second press
// is the stop button design.md 5.9 keeps off this screen.
check(
  "a startable recorder offers the button",
  rendered({ can_start: true }).get("btn-record").disabled,
  false,
);
check(
  "a rolling recorder does not",
  rendered({ liveness: "live", known: true, recording: true, confirmed: true, can_start: false })
    .get("btn-record").disabled,
  true,
);
check(
  "a rolling recorder says so on the button",
  rendered({ liveness: "live", known: true, recording: true, confirmed: true, can_start: false })
    .get("btn-record").textContent,
  "Recording",
);
check(
  "an idle recorder offers to start",
  rendered({ can_start: true }).get("btn-record").textContent,
  "Start recording",
);
check(
  "a lost recorder does not offer the button either",
  rendered({ liveness: "lost", known: false, can_start: false }).get("btn-record").disabled,
  true,
);

// A grey record button always says why (#163). Games 2 and 3 had a grey button
// and no reason, and the operator could not tell a refusal to act on from one to
// leave alone. The wording is the box's: these are the texts it sends.
const RECORD_REASONS = [
  "Reaper has stopped answering: it says it is recording but its playhead is not moving. "
    + "Check Reaper, and start the recording there if it is not rolling.",
  "Reaper has not confirmed the start the box sent, so another tap could stop it. "
    + "Check Reaper, and start it there if it is not recording.",
  "Reaper is not answering. Open it, with its audio device running, or start the recording in Reaper.",
  "Reaper is already recording.",
  "Reaper's transport is moving but it has not said whether it is recording. "
    + "Check Reaper, and start the recording there if it is not.",
  "This log already holds a recording, and Reaper has not said whether it is still rolling. "
    + "Check Reaper, and start it there if it is not.",
  "Listening to Reaper: not heard long enough yet to tell a parked transport from a moving one. "
    + "This clears in a couple of seconds.",
];

for (const refusal of RECORD_REASONS) {
  const nodes = rendered({ can_start: false, refusal });
  check("a grey record button shows a reason: disabled", nodes.get("btn-record").disabled, true);
  check("the reason is the box's own words", nodes.get("rec-why").textContent, refusal);
}

check(
  "a live record button shows no reason",
  rendered({ can_start: true, refusal: null }).get("rec-why").textContent,
  "",
);

{
  const { context, nodes } = browser();
  const snap = snapshot({ can_start: false });
  delete snap.recording.refusal;
  context.render(snap);
  const missing = runInContext("RECORD_REASON_MISSING", context);
  check("a box that sends no reason still gets one", nodes.get("rec-why").textContent, missing);
  check("and that fallback is not empty", missing !== "", true);
}

{
  const { context, nodes } = browser();
  context.render(snapshot({ can_start: false, refusal: RECORD_REASONS[3] }));
  check("grey: the reason is up", nodes.get("rec-why").textContent, RECORD_REASONS[3]);
  context.render(snapshot({ can_start: true, refusal: null }));
  check("the reason clears when the button comes back", nodes.get("rec-why").textContent, "");
}

// -- the fader --------------------------------------------------------------

// A closed fader is -inf, which JSON cannot carry, so it arrives as null. This
// also pins the escaping: the script is inlined into a Python string, and a
// doubled backslash here would put a literal "∞" on the screen.
// The box's boot snapshot does not know where the fader is (#107), so every
// check about a number on the screen says the level is known, out loud: the
// fixture stays honest about the cold boot and the checks are about the number.
check("a sweep through unity never reads negative zero", context.faderDb(-0.004), "0.00 dB");
check("a closed fader reads as minus infinity", rendered({}, { level_known: true }).get("level").textContent, "-∞ dB");
check(
  "an open fader reads in dB",
  rendered({}, { level_known: true, db: -12.5 }).get("level").textContent,
  "-12.50 dB",
);
// A move is described once and the page draws it (#154), so these render on a
// page whose clock is held still: the sweep is then exactly where it started.
const STILL = () => 2_000_000_000;
const MOVE_AT = SNAPSHOTS["standing-down"].at;

// A move as the box describes it, started when the snapshot was taken.
function moveFrom(kind, from, to, seconds, extra = {}) {
  return {
    seq: 1,
    kind,
    by: null,
    from_db: from,
    to_db: to,
    seconds,
    started_at: MOVE_AT,
    floor_db: -60.0,
    knee: null,
    ...extra,
  };
}

function renderedStill(fader) {
  const { context, nodes } = browser({ now: STILL });
  context.render(snapshot({}, fader));
  return nodes;
}

// A close takes two seconds, so the number on its own reads as a fader that is
// not moving. The destination is shown beside it while the move is in flight.
check(
  "a fade in flight shows where it is heading",
  renderedStill({
    level_known: true,
    commanded: -300,
    db: -3.0,
    target: -32768,
    target_db: null,
    moving: true,
    move: moveFrom("fade", -3.0, null, 2.0, { by: "out" }),
  }).get("level").textContent,
  "-3.00 dB \u2192 -\u221E dB",
);
check(
  "and nothing is pointed at when the fader is settled",
  rendered({}, { level_known: true, db: -3.0 }).get("level").textContent,
  "-3.00 dB",
);
check(
  "an open in flight points at its destination too",
  renderedStill({
    level_known: true,
    commanded: -6000,
    db: -60.0,
    target: 0,
    target_db: 0.0,
    moving: true,
    move: moveFrom("ride", -60.0, 0.0, 1.5, { by: "up-slow" }),
  }).get("level").textContent,
  "-60.00 dB \u2192 0.00 dB",
);

check(
  "an unreachable Reaper says so, under the Recording panel (#172)",
  rendered({ healthy: false, error: "reaper is not listening" }).get("rec-error").textContent,
  "Reaper unreachable: reaper is not listening",
);
check("a healthy Reaper says nothing there", rendered({ healthy: true, error: null }).get("rec-error").textContent, "");
check(
  "an unhealthy Reaper does not touch the refusal line",
  rendered({ healthy: false, error: "x", can_start: true, refusal: null }).get("rec-why").textContent,
  "",
);

check(
  "an unreachable console says so",
  rendered({}, { healthy: false, error: "no route to host" }).get("fader-error").textContent,
  "Console unreachable: no route to host",
);

// -- the age of the last command (#12) ---------------------------------------

const { context: ageContext } = browser();
const ageText = ageContext.ageText;

check("age: nothing sent yet is blank", ageText(null), "");
check("age: seconds are exact and close up", ageText(3.4), "3s ago");
check("age: rounds rather than truncating", ageText(59.6), "1 min ago");
check("age: minutes once it is not urgent any more", ageText(76 * 60), "76 min ago");
check("age: unbounded, so a whole game's worth is still readable", ageText(3 * 3600 + 5 * 60), "185 min ago");

// The box sends when it last sent (#147), on its own clock, and the page counts
// the age: the gap between that and the snapshot's own `at`, plus the time since
// the snapshot arrived. A command sent `ago` seconds before the snapshot was
// taken reads as `ago` seconds old when it is painted on arrival.
function sentAgo(ago) {
  return SNAPSHOTS["standing-down"].at - ago;
}

// `since` is the page-side seconds since the snapshot arrived (#154).
check("commandAge: nothing sent yet has no age", ageContext.commandAge(null, 100, 15), null);
check("commandAge: a snapshot with no at has no age", ageContext.commandAge(90, null, 15), null);
check("commandAge: a non-numeric at has no age", ageContext.commandAge(90, "x", 15), null);
check("commandAge: a snapshot that never arrived has no age", ageContext.commandAge(90, 100, null), null);
check(
  "commandAge: box-side gap plus page-side time since arrival",
  ageContext.commandAge(90, 100, 15),
  25,
);
check("commandAge: never negative", ageContext.commandAge(100, 90, 0), 0);

check(
  "the readout carries the age of the last command, known level or not",
  rendered({}, { level_known: true, db: 0.0, sent_at: sentAgo(76 * 60) }).get("level").textContent,
  "0.00 dB - 76 min ago",
);
check(
  "the readout counts seconds close up",
  rendered({}, { level_known: true, db: 0.0, sent_at: sentAgo(3) }).get("level").textContent,
  "0.00 dB - 3s ago",
);
check(
  "no age is shown when nothing has ever been sent",
  rendered({}, { level_known: true, sent_at: null }).get("level").textContent,
  "-∞ dB",
);

{
  // #147: the age is counted on the page, so it keeps counting while the box
  // sends nothing, and with no clock estimate. The page's clock is held still
  // for the render, then moved on and the 1 s tick fired, with no new snapshot
  // in between.
  let clock = 2_000_000;
  const { context, nodes, intervals } = browser({ now: () => clock * 1000 });
  const snap = snapshot({}, { level_known: true, db: 0.0 });
  snap.at = 5000;
  snap.fader.sent_at = 5000 - 30;
  // No pong is ever sent: there is no clock estimate at all, which is the case
  // the age has to survive (#147).
  context.render(snap);
  check("age ticks: painted at the age it had on arrival", nodes.get("level").textContent, "0.00 dB - 30s ago");
  clock += 20;
  intervals[0]();
  check("age ticks: and twenty seconds on, with no new snapshot", nodes.get("level").textContent, "0.00 dB - 50s ago");
  clock += 60 * 60;
  intervals[0]();
  check("age ticks: and an hour on it still says so", nodes.get("level").textContent, "0.00 dB - 61 min ago");
}

{
  // #147, #12: the link dropping is when the age matters most. The snapshot on
  // screen arrived when it arrived, so closing the socket must not blank the
  // age or stop it climbing.
  let clock = 2_000_000;
  const { context, nodes, intervals, sockets } = browser({ now: () => clock * 1000 });
  const snap = snapshot({}, { level_known: true, db: 0.0 });
  snap.at = 5000;
  snap.fader.sent_at = 5000 - 30;
  context.render(snap);
  sockets[0].close();
  clock += 20;
  intervals[0]();
  check("age across a disconnect: still counting after the socket closes", nodes.get("level").textContent, "0.00 dB - 50s ago");
  clock += 60 * 60;
  intervals[0]();
  check("age across a disconnect: and an hour on", nodes.get("level").textContent, "0.00 dB - 61 min ago");
}

// -- the box does not know where the fader is: the level renders as unknown (#107) --

check(
  "a level that is not trusted reads as unknown rather than a confident number",
  rendered({}, { level_known: false, db: 0.0 }).get("level").textContent,
  "unknown",
);
check(
  "the age still shows so a stale belief cannot look current",
  rendered({}, { level_known: false, db: 0.0, sent_at: sentAgo(76 * 60) }).get("level").textContent,
  "unknown - 76 min ago",
);
check(
  "the tag says unknown too",
  rendered({}, { level_known: false }).get("level-tag").textContent,
  "unknown",
);
check(
  "styled as unknown, not as commanded",
  rendered({}, { level_known: false }).get("level-tag").className,
  "tag unknown",
);
check(
  "the box's own boot snapshot does not know, so the cold-boot page reads unknown",
  rendered().get("level").textContent,
  "unknown",
);
check(
  "an ordinary reading is tagged commanded, as before",
  rendered({}, { level_known: true }).get("level-tag").textContent,
  "commanded",
);

// -- handing off to StageMix (#12) ------------------------------------------

// The boot snapshot with the level known or not. The page reads one flag for
// both things it used to read two for (#107): the box does not know where the
// fader is, whether that is a cold boot or a hand-off.
function levelSnapshot(known) {
  return snapshot({}, { level_known: known });
}

{
  const { context, nodes } = browser();
  context.render(levelSnapshot(true));
  check("the confirm prompt starts closed", nodes.get("handoff-confirm").style.display, "none");
  // The script never rewrites this label (#118): it is the page's own
  // markup, like the two belief buttons below it, so the stub's default
  // stands in for whatever the page itself says.
  check("the script leaves the button's own label alone", nodes.get("btn-handoff").textContent, "");
  check("and is not disabled", nodes.get("btn-handoff").disabled, false);
}

{
  // Tapping it does not itself post anything: the box only hears about a
  // handoff once the prompt is answered (CLAUDE.md principle 4).
  const { context, nodes, posted } = browser();
  context.render(levelSnapshot(true));
  posted.length = 0; // the page's own /api/state fetch on load
  nodes.get("tab-btn-more").onclick();
  nodes.get("btn-handoff").onclick();
  check("tapping it opens the prompt rather than acting at once", nodes.get("handoff-confirm").style.display, "block");
  check("opening the prompt sends nothing", posted.length, 0);

  nodes.get("btn-handoff-yes").onclick();
  check("yes hands off", posted[0].path, "/api/handoff");
  check("and closes the prompt", nodes.get("handoff-confirm").style.display, "none");
  check("and stays on MORE (#109)", nodes.get("tab-more").style.display, "");
  check("MAIN stays hidden", nodes.get("tab-main").style.display, "none");
}

{
  // The negative answer is `still-mine` (#12): a pure log entry, never a
  // handoff.
  const { context, nodes, posted } = browser();
  context.render(levelSnapshot(true));
  posted.length = 0;
  nodes.get("tab-btn-more").onclick();
  nodes.get("btn-handoff").onclick();
  nodes.get("btn-handoff-no").onclick();
  check("no logs still-mine instead", posted[0].path, "/api/still-mine");
  check("and never hands off", posted.some((p) => p.path === "/api/handoff"), false);
  check("and closes the prompt too", nodes.get("handoff-confirm").style.display, "none");
  check("and stays on MORE too (#109)", nodes.get("tab-more").style.display, "");
  check("MAIN stays hidden here as well", nodes.get("tab-main").style.display, "none");
}

{
  // Answered elsewhere - another browser's "yes", or this tap's own already
  // having landed - the question this page was asking is answered: it is the
  // transition from known to unknown that closes it, not "unknown" on its own
  // (#118), since a hand-off while it is already unknown is a real tap with
  // nothing yet to answer it.
  const { context, nodes } = browser();
  context.render(levelSnapshot(true));
  nodes.get("btn-handoff").onclick();
  check("the prompt is open", nodes.get("handoff-confirm").style.display, "block");
  context.render(levelSnapshot(false));
  check("the known-to-unknown edge closes it", nodes.get("handoff-confirm").style.display, "none");
}

{
  // A hand-off while the level already reads unknown is a real tap that has
  // not been answered yet - the box may have restarted while StageMix had
  // the DCA - so "unknown" on its own must not be read as the answer (#118).
  const { context, nodes } = browser();
  context.render(levelSnapshot(false));
  nodes.get("btn-handoff").onclick();
  check("the prompt opens", nodes.get("handoff-confirm").style.display, "block");
  context.render(levelSnapshot(false));
  check("staying unknown does not close a prompt that is still open",
        nodes.get("handoff-confirm").style.display, "block");
}

{
  // The hand-off button is never disabled and never relabelled by the script
  // at either belief (#118): a hand-off while the level already reads
  // unknown is a real tap with a real log entry, so the button stays live.
  const { context, nodes } = browser();
  context.render(levelSnapshot(false));
  check("unknown: never disabled", nodes.get("btn-handoff").disabled, false);
  check("unknown: never relabelled", nodes.get("btn-handoff").textContent, "");
  context.render(levelSnapshot(true));
  check("known: never disabled", nodes.get("btn-handoff").disabled, false);
  check("known: never relabelled", nodes.get("btn-handoff").textContent, "");
}

{
  // The cold-boot page is the box's own boot snapshot, and it does not know -
  // and is exactly the restart-under-StageMix page #118 is about, so the
  // button stays live rather than saying there is nothing to hand off.
  const { context, nodes } = browser();
  context.render(snapshot());
  check("at cold boot the button is still live", nodes.get("btn-handoff").disabled, false);
}

// -- the arm / stand-down question: pure functions (#19) --------------------

// Mirrors app.js's own constant of the same name (a `const`, so it is not
// reachable through the sandbox's global object the way the `function`
// declarations below are - see STALE_AFTER above for the same reasoning).
const PROMPT_GUARD_MS = 700;

const { context: promptFnContext } = browser();
const { promptCopy, answerable, clockTime, dutyChip } = promptFnContext;

check(
  "promptCopy: stand-down, level known",
  promptCopy("stand-down", true),
  { question: "Band left the stands. Stand down? Fades the band out if it is up.", accept: "Stand down" },
);
check(
  "promptCopy: stand-down, level unknown - the send is a no-op, and says so",
  promptCopy("stand-down", false),
  {
    question: "Band left the stands. Stand down? Moves nothing while the fader position is unknown.",
    accept: "Stand down",
  },
);
check(
  "promptCopy: arm, level known",
  promptCopy("arm", true),
  { question: "Band in the stands. Arm? Moves nothing.", accept: "Arm" },
);
check(
  "promptCopy: arm, level unknown - the same sentence either way",
  promptCopy("arm", false),
  { question: "Band in the stands. Arm? Moves nothing.", accept: "Arm" },
);
check(
  "promptCopy: an unrecognized kind renders nothing, not a broken panel",
  promptCopy("some-future-kind", true),
  null,
);

check("answerable: never before it has been shown", answerable(null, 100), false);
check(
  "answerable: just under the guard is not yet answerable",
  answerable(0, (PROMPT_GUARD_MS - 1) / 1000),
  false,
);
check(
  "answerable: exactly the guard is answerable",
  answerable(0, PROMPT_GUARD_MS / 1000),
  true,
);
check(
  "answerable: comfortably past the guard",
  answerable(1000, 1000 + PROMPT_GUARD_MS / 1000 + 1),
  true,
);

// An independent oracle for a local HH:MM, built from calendar fields with the
// `Date` constructor directly rather than by reusing clockTime's own
// arithmetic - and read back with the same local getters clockTime uses, so
// the check holds in whatever timezone this runs in rather than assuming UTC.
function oracleClock(y, m, d, h, min) {
  const local = new Date(y, m - 1, d, h, min, 0);
  return { epoch: local.getTime() / 1000, text: String(h).padStart(2, "0") + ":" + String(min).padStart(2, "0") };
}

check("clockTime: no reading at all is blank", clockTime(null, 0), "");
check("clockTime: a reading with no offset yet is blank", clockTime(100, null), "");
{
  const at = oracleClock(2026, 9, 19, 10, 42);
  check("clockTime: matches an independently computed local time", clockTime(at.epoch, 0), at.text);
}
{
  const at = oracleClock(2026, 9, 19, 0, 7);
  check("clockTime: hour and minute are both zero-padded", clockTime(at.epoch, 0), at.text);
}
{
  // The box's own seconds plus the page's clock offset should land on the
  // same instant as the oracle, whatever that offset is.
  const at = oracleClock(2026, 9, 19, 23, 59);
  check("clockTime: box seconds plus a nonzero offset", clockTime(at.epoch - 1000, 1000), at.text);
}

check("dutyChip: armed with no time yet (a restarted box)", dutyChip({ armed: true, since: null }, 0), "ARMED");
check(
  "dutyChip: stood down with no time yet",
  dutyChip({ armed: false, since: null }, 0),
  "STOOD DOWN",
);
{
  // #19's own worked example, matching the real armed time in game 2
  // (docs/game-2.md:111: "The box was armed at 10:42:49").
  const at = oracleClock(2026, 9, 19, 10, 42);
  check("dutyChip: armed with a time, #19's own example", dutyChip({ armed: true, since: at.epoch }, 0), "ARMED " + at.text);
}
{
  // #19's own worked example for the stood-down half.
  const at = oracleClock(2026, 9, 19, 12, 51);
  check(
    "dutyChip: stood down with a time, #19's other example",
    dutyChip({ armed: false, since: at.epoch }, 0),
    "STOOD DOWN " + at.text,
  );
}
{
  // #19: boxOffset falls back to null, not NaN, when there is neither a
  // round-trip estimate nor a usable `at` to measure this snapshot's age
  // against - a malformed snapshot must render a blank chip, never a
  // confident-looking "ARMED NaN:NaN".
  const { context } = browser();
  check("boxOffset: a non-numeric at with no estimate is null", context.boxOffset({ at: "not-a-number" }), null);
  check("boxOffset: a missing at with no estimate is null", context.boxOffset({}), null);
  check(
    "boxOffset: and dutyChip renders that as a blank time, not NaN:NaN",
    dutyChip({ armed: true, since: 100 }, context.boxOffset({ at: "not-a-number" })),
    "ARMED",
  );
}

// -- where is the fader: the two belief controls (#107) -----------------------

const STATES = ["standing-down", "idle", "ready", "open", "releasing"];

for (const state of STATES) {
  for (const known of [true, false]) {
    const { context, nodes } = browser();
    context.render({ ...levelSnapshot(known), state });
    const label = `${state}, level ${known ? "known" : "unknown"}`;
    // Nothing appears, disappears or moves on a belief change: the row is in
    // the readout gap for good, and only a button with nothing to say is
    // disabled in place.
    check(`${label}: Close now is never hidden`, nodes.get("btn-close-now").style.display, undefined);
    check(`${label}: Close now is never disabled`, nodes.get("btn-close-now").disabled, false);
    check(`${label}: the ready report is never hidden`, nodes.get("btn-report-ready").style.display, undefined);
    check(`${label}: the ready report is disabled iff the level is known`,
          nodes.get("btn-report-ready").disabled, known);
  }
}

check(
  "the script never rewrites the belief buttons' labels; they are the page's own markup",
  [
    rendered({}, { level_known: true }).get("btn-close-now").textContent,
    rendered({}, { level_known: false }).get("btn-close-now").textContent,
  ],
  ["", ""],
);

{
  // Both post to their own routes, and neither navigates: the fader column is
  // on screen whatever tab is showing.
  const { context, nodes, posted } = browser();
  context.render(levelSnapshot(false));
  posted.length = 0;
  nodes.get("btn-close-now").onclick();
  check("close now posts to its own route", posted[0].path, "/api/close-now");
  nodes.get("btn-report-ready").onclick();
  check("the ready report posts to its own route", posted[1].path, "/api/report-ready");
  check("neither asks first", posted.length, 2);
}

{
  // The take-back prompt and its routes are gone (#107).
  const { context, posted } = browser();
  context.render(levelSnapshot(false));
  check("nothing on the page posts to a take-back route",
        posted.some((p) => p.path.includes("take-back")), false);
  check("and the script no longer knows the word", SOURCE.includes("take-back-"), false);
}

// -- the ramping buttons, greyed while the level is unknown (#107) --------------

// The four column buttons whose move ramps from the believed level, and the two
// snaps that do not. Keys, not actions: the column is six named, pinned
// buttons (#5).
const RAMPING_KEYS = ["up-slow", "up-ready", "out", "score-reversed"];
const SNAP_KEYS = ["up-whistle", "up-drums"];
const COLUMN_KEYS = [...RAMPING_KEYS, ...SNAP_KEYS];
const RAMPING_BLOCKED = "Greyed: they ramp from an unknown level";

// What one page shows for the fader column at a belief. The standing-down
// fixture is the unknown case; the known case is the same box with the level
// trusted.
function columnAt(known, top = {}) {
  const { context, nodes, created } = browser();
  context.render({ ...levelSnapshot(known), ...top });
  const column = (key) => created.find((node) => node.tag === "button" && node.dataset.key === key);
  return { context, nodes, created, column };
}

for (const known of [false, true]) {
  const at = `level ${known ? "known" : "unknown"}`;
  const { column } = columnAt(known);
  for (const key of RAMPING_KEYS) {
    check(`${at}: ${key} is disabled iff the level is unknown`, column(key).disabled, !known);
  }
  for (const key of SNAP_KEYS) {
    check(`${at}: ${key} (a snap open) is never disabled`, column(key).disabled, false);
  }
  for (const key of ["band-enters-stands", "last-two-minutes", "touchdown", "note"]) {
    check(`${at}: the annotation ${key} is never disabled by the belief`, column(key).disabled, false);
  }
  for (const key of COLUMN_KEYS) {
    check(`${at}: ${key} is never hidden`, column(key).style.display, undefined);
  }
}

{
  const { column } = columnAt(false);
  check("a greyed button keeps its own label; the reason lives in the column, not on it",
        [column("up-slow").textContent, column("up-ready").textContent,
         column("out").textContent, column("score-reversed").textContent],
        ["Up slow, missed the start", "Ready (band likely)", "Faded out", "Score reversed"]);
}

{
  // In place: the same four nodes, in the same order, before and after. This is
  // how level_known is pinned out of the rebuild signature. The assertion is
  // indirect - renderButtons is the only thing that creates these buttons, so
  // an unchanged created list across a belief change proves no rebuild.
  const { context, created } = browser();
  const buttonsNow = () => created.filter((node) => node.tag === "button");
  context.render(levelSnapshot(false));
  const before = buttonsNow();
  const keysBefore = before.map((node) => node.dataset.key);
  const stale = before.filter((node) => RAMPING_KEYS.includes(node.dataset.key)).map((node) => node.disabled);
  context.render(levelSnapshot(true));
  const after = buttonsNow();
  check("the four were disabled first", stale, [true, true, true, true]);
  check("changing the belief creates nothing", after.length, before.length);
  check("changing the belief reorders nothing", after.map((node) => node.dataset.key), keysBefore);
  check("every node is the very same node", after.every((node, i) => node === before[i]), true);
  check("they re-enable in place once the level is known",
        after.filter((node) => RAMPING_KEYS.includes(node.dataset.key)).map((node) => node.disabled),
        [false, false, false, false]);
  context.render(levelSnapshot(false));
  check("and grey again in place when the belief is lost", buttonsNow().length, before.length);
  check("still the same nodes", buttonsNow().every((node, i) => node === before[i]), true);
  check("and disabled again",
        buttonsNow().filter((node) => RAMPING_KEYS.includes(node.dataset.key)).map((node) => node.disabled),
        [true, true, true, true]);
}

{
  const { nodes } = columnAt(false);
  check("the column says why they are greyed, while the level is unknown",
        nodes.get("col-unknown").textContent, RAMPING_BLOCKED);
  check("and shows the line", nodes.get("col-unknown").style.display, "block");
}

{
  // The readout gap has room for one of the two lines, not both (see the
  // budget in web.py), and the refusal says the same thing in more words.
  const refusal = "the box does not know where the fader is, and this move ramps from that belief";
  const { nodes } = columnAt(false, { refusal });
  check("the note yields to a refusal", nodes.get("col-unknown").style.display, "none");
  check("and the refusal shows", nodes.get("col-refusal").style.display, "block");
  check("in the box's words", nodes.get("col-refusal").textContent, refusal);
}

{
  const { context, nodes } = columnAt(false);
  context.render({ ...levelSnapshot(true) });
  check("the note is gone once the level is known", nodes.get("col-unknown").style.display, "none");
  check("and empty, so nothing stale is left to read", nodes.get("col-unknown").textContent, "");
  context.render({ ...levelSnapshot(false), refusal: "x" });
  context.render({ ...levelSnapshot(false), refusal: null });
  check("a refusal clearing brings the note back", nodes.get("col-unknown").style.display, "block");
}

// -- the box's own snapshots --------------------------------------------------

// Each state the box wrote, rendered as it came. A change to the snapshot's
// shape arrives here as a fixture diff, and these say whether the page still
// reads it.
check(
  "the box wrote the snapshots these tests read",
  Object.keys(SNAPSHOTS).sort(),
  [
    "faults",
    "open-recording",
    "parked-unreported",
    "prompt",
    "prompt-arm",
    "releasing",
    "retargeting",
    "riding",
    "standing-down",
    "target-stored",
  ],
);

function renderedFixture(name) {
  const { context, nodes, created } = browser({ now: STILL });
  let error = null;
  try {
    context.render(structuredClone(SNAPSHOTS[name]));
  } catch (caught) {
    error = String(caught);
  }
  const button = (key) => created.find((node) => node.tag === "button" && node.dataset.key === key);
  return { nodes, error, button };
}

{
  let refused = null;
  try {
    snapshot({}, { level_unknown: true });
  } catch (caught) {
    refused = String(caught);
  }
  check(
    "a test cannot describe a field the box does not send",
    refused,
    "Error: fader.level_unknown is not in the box's snapshot",
  );
}

for (const name of Object.keys(SNAPSHOTS)) {
  const { nodes, error } = renderedFixture(name);
  check(`${name}: renders without throwing`, error, null);
  check(
    `${name}: the headline is its state`,
    nodes.get("state").textContent,
    SNAPSHOTS[name].state.replace(/-/g, " ").toUpperCase(),
  );
}

{
  // #19: a box rolled back to before this feature sends no `duty` field at
  // all. Dereferencing it unguarded would throw inside render() and silently
  // stop repainting the whole page - the same hazard orphanSpans and
  // renderFaderHalf are already written to avoid for their own fields.
  const { context, nodes } = browser();
  const without = structuredClone(SNAPSHOTS["standing-down"]);
  delete without.duty;
  let error = null;
  try {
    context.render(without);
  } catch (caught) {
    error = String(caught);
  }
  check("a snapshot with no duty field does not throw", error, null);
  check("and the rest of the page still renders", nodes.get("state").textContent, "STANDING DOWN");
}

{
  const { nodes, button } = renderedFixture("standing-down");
  // The boot fixture does not know where the fader is (#107), and the page
  // says so rather than showing the -inf the box merely starts from. (Null dB
  // is a closed fader, never a missing reading: see the readout checks above,
  // which say the level is known.)
  check("standing down: the cold-boot level reads unknown", nodes.get("level").textContent, "unknown");
  check("standing down: Close now is there and live", nodes.get("btn-close-now").disabled, false);
  check("standing down: the ready report is there and live", nodes.get("btn-report-ready").disabled, false);
  // End to end: the box's own cold-boot snapshot, not a hand-built one.
  check("standing down: the ramping buttons are greyed", button("out").disabled, true);
  check("standing down: and the column says why", nodes.get("col-unknown").style.display, "block");
  check("standing down: the snap opens are live", button("up-whistle").disabled, false);
  check("standing down: an unheard Reaper is unknown", nodes.get("rec").textContent, "unknown");
  check("standing down: and says so", nodes.get("rec-tag").textContent, "no feedback");
  check("standing down: nothing is wrong with saving", nodes.get("saving").className, "");
  check("standing down: the whole vocabulary is on screen", button("last-two-minutes").textContent, "Last two minutes (start)");
}

{
  const { nodes, button } = renderedFixture("open-recording");
  check("open: unity", nodes.get("level").textContent, "0.00 dB - 0s ago");
  check("open: rolling", nodes.get("rec").textContent, "ROLLING");
  check("open: confirmed", nodes.get("rec-tag").textContent, "confirmed");
  check("open: where", nodes.get("rec-pos").textContent, "at 0:12:34.500");
  check("open: the record button will not be pressed twice", nodes.get("btn-record").disabled, true);
  check("open: the open media timeout offers to end", button("timeout-media").textContent, "Timeout: media (end)");
}

{
  const { nodes } = renderedFixture("releasing");
  // The box's own fade snapshot, rendered as it came (#154): the sweep from
  // where it started to where it is going, and no age while it is described.
  check(
    "releasing: where it is and where it is going",
    nodes.get("level").textContent,
    "0.00 dB \u2192 -\u221e dB",
  );
  check("releasing: the tag says fading", nodes.get("level-tag").textContent, "fading");
}

{
  const { nodes } = renderedFixture("faults");
  check("faults: the console", nodes.get("fader-error").textContent, "Console unreachable: no route to host");
  check("faults: Reaper", nodes.get("rec-tag").textContent, "LINK LOST");
  check("faults: Reaper's send failure", nodes.get("rec-error").textContent, "Reaper unreachable: no route to host");
  check("faults: the log", nodes.get("saving").className, "fault");
  check("faults: the refusal is shown", nodes.get("refusal").style.display, "block");
}

// -- the arm / stand-down question, rendered from the box's own snapshots (#19) --

{
  // The "prompt" fixture: armed, open, a stand-down question raised by
  // band-exits-stands (tests/snapshots.py:prompt_open), level known.
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  check("prompt: the panel shows", nodes.get("prompt-panel").style.display, "block");
  check(
    "prompt: stand-down copy at a known level",
    nodes.get("prompt-question").textContent,
    "Band left the stands. Stand down? Fades the band out if it is up.",
  );
  check("prompt: the accept button is labelled for the kind", nodes.get("btn-prompt-accept").textContent, "Stand down");
  check(
    "prompt: the chip starts ARMED - the exact minute is whatever time this test runs, see dutyChip's own tests",
    nodes.get("duty").textContent.startsWith("ARMED "),
    true,
  );
}

{
  // The "prompt-arm" fixture: cold boot, an Arm question raised by
  // band-enters-stands, accepted and refused for want of a known level
  // (tests/snapshots.py:prompt_arm_refused). The refusal shows and the
  // question stays open under the same seq - #107 and #19 together.
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt-arm"]));
  check("prompt-arm: the panel is still open despite the refusal", nodes.get("prompt-panel").style.display, "block");
  check(
    "prompt-arm: arm copy",
    nodes.get("prompt-question").textContent,
    "Band in the stands. Arm? Moves nothing.",
  );
  check("prompt-arm: the accept button says Arm", nodes.get("btn-prompt-accept").textContent, "Arm");
  check(
    "prompt-arm: the chip is exactly STOOD DOWN - a restarted box invents no time",
    nodes.get("duty").textContent,
    "STOOD DOWN",
  );
  check("prompt-arm: the refusal is shown too", nodes.get("refusal").style.display, "block");
}

for (const name of ["standing-down", "open-recording"]) {
  // Neither fixture is asking anything: the panel is hidden and a tap on its
  // buttons - left over from some earlier question - posts nothing.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS[name]));
  check(`${name}: no question, no panel`, nodes.get("prompt-panel").style.display, "none");
  posted.length = 0;
  nodes.get("btn-prompt-accept").onclick();
  nodes.get("btn-prompt-dismiss").onclick();
  check(`${name}: a tap on a hidden panel's buttons posts nothing`, posted.length, 0);
}

// The chip is one of the two ways design.md 5.5 requires duty state to always
// be visible (the other being the why line); a blank chip on any real
// snapshot would defeat that.
for (const name of Object.keys(SNAPSHOTS)) {
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS[name]));
  check(`${name}: the duty chip is never blank`, nodes.get("duty").textContent.length > 0, true);
}

// -- the 700ms tap guard (#19) ------------------------------------------------
//
// Real-timer waits, like the other timing-sensitive checks in this suite:
// `now()` in app.js reads `Date.now()` directly rather than an injectable
// clock, since it is measured against the same wall clock a real tap on a
// real screen would be.
const guardWait = () => new Promise((resolve) => setTimeout(resolve, PROMPT_GUARD_MS + 50));

{
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  posted.length = 0;
  nodes.get("btn-prompt-accept").onclick();
  check("guard: a tap inside the window posts nothing", posted.length, 0);
  await guardWait();
  nodes.get("btn-prompt-accept").onclick();
  check("guard: a tap after the window posts exactly once", posted.length, 1);
  check("guard: to the accept route", posted[0].path, "/api/prompt/accept");
  check("guard: naming the open seq", posted[0].body.seq, SNAPSHOTS["prompt"].prompt.seq);
  check(
    "guard: and nothing else in the body besides the tap stamp every request carries",
    Object.keys(posted[0].body).filter((key) => key !== "tap").sort(),
    ["seq"],
  );
}

{
  // Dismiss is guarded the same way as accept.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  posted.length = 0;
  nodes.get("btn-prompt-dismiss").onclick();
  check("guard: dismiss inside the window posts nothing either", posted.length, 0);
  await guardWait();
  nodes.get("btn-prompt-dismiss").onclick();
  check("guard: dismiss after the window posts once", posted.length, 1);
  check("guard: to the dismiss route", posted[0].path, "/api/prompt/dismiss");
}

{
  // A new seq - a fresh question, or the same kind raised again - re-arms the
  // guard even though the previous one had already cleared.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  await guardWait();
  const later = structuredClone(SNAPSHOTS["prompt"]);
  later.prompt = { ...later.prompt, seq: later.prompt.seq + 1 };
  later.at = later.at + 1;
  context.render(later);
  posted.length = 0;
  nodes.get("btn-prompt-accept").onclick();
  check("guard: a new seq is not yet answerable", posted.length, 0);
  await guardWait();
  nodes.get("btn-prompt-accept").onclick();
  check("guard: and clears in its own turn", posted.length, 1);
  check("guard: naming the new seq", posted[0].body.seq, later.prompt.seq);
}

{
  // A refusal on the SAME seq - the box declined the accept, e.g. #107's
  // unknown-level Arm refusal - must not re-arm the guard: the operator's
  // retry tap has to go through at once, not wait another 700ms.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  await guardWait();
  const refused = structuredClone(SNAPSHOTS["prompt"]);
  refused.refusal = "the box does not know where the fader is";
  refused.at = refused.at + 1;
  context.render(refused);
  posted.length = 0;
  nodes.get("btn-prompt-accept").onclick();
  check("guard: a refusal on the same seq does not re-arm it; the retry goes through", posted.length, 1);
}

// -- the hand-off confirmation and #19's question exclude each other (#12, #19) --

{
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  check("slot: the question is showing to start", nodes.get("prompt-panel").style.display, "block");
  await guardWait();

  nodes.get("btn-handoff").onclick();
  check("slot: opening the hand-off confirmation hides the question", nodes.get("prompt-panel").style.display, "none");
  posted.length = 0;
  nodes.get("btn-prompt-accept").onclick();
  nodes.get("btn-prompt-dismiss").onclick();
  check("slot: its buttons are inert while hidden", posted.length, 0);

  nodes.get("btn-handoff-no").onclick();
  check("slot: declining the hand-off brings the question back", nodes.get("prompt-panel").style.display, "block");
  posted.length = 0; // clear the still-mine post the decline itself made
  nodes.get("btn-prompt-accept").onclick();
  check("slot: but its guard is freshly armed - an immediate tap posts nothing", posted.length, 0);
  await guardWait();
  nodes.get("btn-prompt-accept").onclick();
  check("slot: and it answers once the guard has passed again", posted.length, 1);
}

{
  // The same story for accepting the hand-off rather than declining it.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  await guardWait();
  nodes.get("btn-handoff").onclick();
  nodes.get("btn-handoff-yes").onclick();
  check("slot: accepting the hand-off brings the question back too", nodes.get("prompt-panel").style.display, "block");
  posted.length = 0; // clear the handoff post the accept itself made
  nodes.get("btn-prompt-accept").onclick();
  check("slot: freshly armed here as well", posted.length, 0);
  await guardWait();
  nodes.get("btn-prompt-accept").onclick();
  check("slot: and answers once the guard passes", posted.length, 1);
}

// -- leaving MORE cancels an unanswered hand-off confirmation (#108) -------

{
  // The confirmation now lives inside MORE, so switching tabs would hide it
  // while `handoffPromptOpen` stayed true. Nothing has been sent at that
  // point, so the switch cancels it and sends nothing.
  const { context, nodes, posted } = browser();
  context.render(levelSnapshot(true));
  posted.length = 0;
  nodes.get("tab-btn-more").onclick();
  nodes.get("btn-handoff").onclick();
  check("cancel: the confirmation is open", nodes.get("handoff-confirm").style.display, "block");
  nodes.get("tab-btn-main").onclick();
  check("cancel: leaving MORE closes it", nodes.get("handoff-confirm").style.display, "none");
  check("cancel: and sends nothing at all", posted.length, 0);
  check(
    "cancel: neither answer was posted",
    posted.some((p) => p.path === "/api/handoff" || p.path === "/api/still-mine"),
    false,
  );
}

{
  // The safety half: an abandoned, invisible confirmation must never be
  // able to suppress the box's own arm / stand-down question.
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  nodes.get("tab-btn-more").onclick();
  nodes.get("btn-handoff").onclick();
  check("cancel: opening the confirmation hides the question", nodes.get("prompt-panel").style.display, "none");
  nodes.get("tab-btn-main").onclick();
  check("cancel: leaving MORE brings the question back", nodes.get("prompt-panel").style.display, "block");
}

{
  // Coming back to MORE does not resurrect it: re-opening is one tap.
  const { context, nodes } = browser();
  context.render(levelSnapshot(true));
  nodes.get("tab-btn-more").onclick();
  nodes.get("btn-handoff").onclick();
  nodes.get("tab-btn-main").onclick();
  nodes.get("tab-btn-more").onclick();
  check("cancel: returning to MORE does not re-open it", nodes.get("handoff-confirm").style.display, "none");
  nodes.get("btn-handoff").onclick();
  check("cancel: one tap on the button re-opens it", nodes.get("handoff-confirm").style.display, "block");
}

{
  // Answering the question never moves the tab - the same rule every other
  // button now follows (#109). It must not steal the operator away from
  // wherever they were.
  const { context, nodes, posted } = browser();
  context.render(structuredClone(SNAPSHOTS["prompt"]));
  nodes.get("tab-btn-more").onclick();
  check("tab: MORE is selected", nodes.get("tab-btn-more").classList.contains("on"), true);
  await guardWait();
  nodes.get("btn-prompt-accept").onclick();
  check("tab: answering the question leaves the tab where it was", nodes.get("tab-btn-more").classList.contains("on"), true);
  check("tab: and the tap really went out", posted.length > 0, true);
}

// -- span buttons say which tap they are ------------------------------------

// The first five band buttons are instants that happen to pair up (enters /
// exits), while the quarters are genuine spans. Nothing on screen distinguished
// those two shapes, and an outline on an open span looks exactly like an
// instant that was just tapped.
const BUTTONS = [
  { key: "q1", label: "Q1", category: "GAME", kind: "span" },
  { key: "timeout-injury", label: "Timeout: injury", category: "GAME", kind: "span" },
  { key: "band-enters-stands", label: "Band enters stands", category: "BAND", kind: "instant" },
];

// An open span as the box sends it: the id to end it by, and the event it
// belongs to. The id is the box's to mint and the page never parses it.
const open = (event, seq) => ({ span_id: `${event}-${seq}`, event });

function buttons(openSpans = []) {
  const { context, created } = browser();
  context.render(snapshot({}, {}, BUTTONS, openSpans));
  const found = new Map();
  for (const node of created.filter((n) => n.tag === "button")) found.set(node.dataset.key, node);
  return found;
}

check("a closed span says it starts", buttons().get("q1").textContent, "Q1 (start)");
check("an open span says it ends", buttons([open("q1", 2)]).get("q1").textContent, "Q1 (end)");
check(
  "an instant carries no start or end",
  buttons().get("band-enters-stands").textContent,
  "Band enters stands",
);
check(
  "an instant is never marked open, whatever spans are running",
  buttons([open("q1", 2)]).get("band-enters-stands").classList.contains("on"),
  false,
);
check("an open span is highlighted", buttons([open("q1", 2)]).get("q1").classList.contains("on"), true);
check("a closed span is not highlighted", buttons().get("q1").classList.contains("on"), false);
check(
  "one open span does not open another",
  buttons([open("q1", 2)]).get("timeout-injury").textContent,
  "Timeout: injury (start)",
);
check(
  "spans are marked as spans for styling",
  buttons().get("q1").dataset.kind,
  "span",
);

// Span ids are `<key>-<seq>` and keys contain hyphens, so a key that prefixes
// another looked like it owned that key's span: with a home timeout running,
// "Official timeout" read (end) and tapping it closed the *home* timeout,
// and Halftime could never open during the exodus. Real vocabulary keys, since
// those are the pairs that collide.
const PREFIXED = [
  { key: "halftime", label: "Halftime", category: "GAME", kind: "span" },
  { key: "halftime-exodus", label: "Halftime exodus", category: "GAME", kind: "span" },
  { key: "timeout-home", label: "Timeout: home", category: "GAME", kind: "span" },
  { key: "timeout", label: "Official timeout", category: "GAME", kind: "span" },
];

function prefixed(openSpans) {
  const { context, created, posted } = browser();
  context.render(snapshot({}, {}, PREFIXED, openSpans));
  const found = new Map();
  for (const node of created.filter((n) => n.tag === "button")) found.set(node.dataset.key, node);
  // What was posted, less the tap stamp every request carries: that has tests
  // of its own below, and its clock reading differs every run.
  const tap = (key) => {
    posted.length = 0;
    found.get(key).onclick();
    return posted.map(({ path, body }) => {
      const { tap: _, ...rest } = body;
      return { path, body: rest };
    });
  };
  return { found, tap };
}

{
  const { found, tap } = prefixed([open("timeout-home", 12), open("halftime-exodus", 40)]);
  check(
    "a span key that prefixes another does not claim its open span",
    found.get("timeout").textContent,
    "Official timeout (start)",
  );
  check("nor is it highlighted by it", found.get("timeout").classList.contains("on"), false);
  check(
    "halftime is not taken for open during the exodus",
    found.get("halftime").textContent,
    "Halftime (start)",
  );
  check("the longer key still owns its own span", found.get("timeout-home").textContent,
        "Timeout: home (end)");
  check(
    "tapping the shorter key opens its own span",
    tap("timeout"),
    [{ path: "/api/span/start", body: { key: "timeout" } }],
  );
  check(
    "tapping the longer key ends the span it owns",
    tap("timeout-home"),
    [{ path: "/api/span/end", body: { span_id: "timeout-home-12" } }],
  );
}

// And the other way round: the shorter key's span says nothing about the longer.
{
  const { found, tap } = prefixed([open("halftime", 7)]);
  check("an open halftime does not end the exodus", found.get("halftime-exodus").textContent,
        "Halftime exodus (start)");
  check(
    "tapping halftime ends halftime",
    tap("halftime"),
    [{ path: "/api/span/end", body: { span_id: "halftime-7" } }],
  );
}

// -- fader buttons --------------------------------------------------------

// These both move the fader and say why. They render outside any grid, in the
// pinned column (#5), which is tapped without looking exactly as the
// annotation grids are, so they still carry the styling that tells them apart.
const MIXED = [
  { key: "band-enters-stands", label: "Band enters stands", category: "BAND", kind: "instant" },
  { key: "last-two-minutes", label: "Last two minutes", category: "GAME", kind: "span" },
  { key: "up-whistle", label: "Up on whistle", category: "FDR", kind: "instant", action: "open" },
  { key: "out", label: "Faded out", category: "FDR", kind: "instant", action: "release" },
];

function laidOut() {
  const { context, created } = browser();
  context.render(snapshot({}, {}, MIXED, []));
  return created.filter((node) => node.tag === "button");
}

function headings() {
  const { context, created } = browser();
  context.render(snapshot({}, {}, MIXED, []));
  return created.filter((node) => node.tag === "h2").map((node) => node.textContent);
}

const laid = laidOut();
const byKey = new Map(laid.map((node) => [node.dataset.key, node]));

check("an opening button is marked as one", byKey.get("up-whistle").dataset.action, "open");
check("a releasing button is marked as one", byKey.get("out").dataset.action, "release");
check(
  "an annotation button is not marked as acting",
  byKey.get("band-enters-stands").dataset.action,
  undefined,
);
check(
  "a span that does not act is not marked as acting",
  byKey.get("last-two-minutes").dataset.action,
  undefined,
);
// #5: the fader buttons move to their own pinned column, which carries no
// heading of its own - there is nothing to sort against any more.
// last-two-minutes lands in MAIN's "Game" group (Scoring and Timeouts have nothing to show and get no
// heading of their own); band-enters-stands lands in MORE under its category.
check("the fader column carries no heading", headings().includes("FDR"), false);
check("last-two-minutes lands in MAIN's Game group", headings()[0], "Game");
check("band-enters-stands lands in MORE under its category", headings()[1], "BAND");
check("only the groups with something to show get a heading", headings().length, 2);
check(
  "an acting button is still an instant, not a toggle",
  byKey.get("up-whistle").textContent,
  "Up on whistle",
);

// -- the grid is not rebuilt under the operator's finger ---------------------

// renderButtons tears the grid down and recreates it. A tap landing during that
// is lost: the node under the finger is detached between touchstart and
// touchend, and no click fires. This is the grid gameday.md describes as tapped
// without looking, by someone watching a field, and it carries the coloured
// fader buttons - so a dropped tap there is a missed open, and design.md 5.6 is
// explicit that the annotation half cannot be reconstructed afterwards.
//
// The guard used to be a flag written onto the snapshot object, which is
// replaced by a freshly parsed one on every frame, so it never survived. Reaper
// streams /time for a whole game, coalesced to a snapshot a second.

function grid(snapshots) {
  const { context, created } = browser();
  // Parsed fresh each time, exactly as a websocket frame arrives. The old guard
  // passed when handed the same object twice, which is why this matters.
  for (const snap of snapshots) context.render(JSON.parse(JSON.stringify(snap)));
  // The vocabulary's buttons: the target segments (#9) are not among them.
  return created.filter((node) => node.tag === "button" && node.dataset.preset === undefined);
}

const GRID = snapshot({}, {}, BUTTONS, []);

check("one snapshot builds the grid", grid([GRID]).length, BUTTONS.length);
check(
  "a hundred more do not build it again",
  grid(Array(100).fill(GRID)).length,
  BUTTONS.length,
);

// Not never, though. A box that restarts mid-game can serve a different
// vocabulary, and the page reconnects to it without reloading.
{
  const { context, created } = browser();
  const count = () => created.filter((node) => node.tag === "button").length;
  context.render(snapshot({}, {}, BUTTONS, []));
  const first = count();
  context.render(snapshot({}, {}, MIXED, []));
  check("a changed vocabulary is rebuilt", count() - first, MIXED.length);
  context.render(snapshot({}, {}, MIXED, []));
  check("and then left alone again", count() - first, MIXED.length);
}

// A relabelled button is a changed vocabulary too - the labels are what the
// operator reads, and the keys alone would not notice.
{
  const { context, created } = browser();
  const count = () => created.filter((node) => node.tag === "button").length;
  context.render(snapshot({}, {}, BUTTONS, []));
  const first = count();
  const relabelled = BUTTONS.map((b) => (b.key === "q1" ? { ...b, label: "First quarter" } : b));
  context.render(snapshot({}, {}, relabelled, []));
  check("a relabelled button is rebuilt", count() - first, BUTTONS.length);
}

// The per-render update path still has to work without a rebuild behind it,
// which is the half a flag-only fix would have quietly broken.
{
  const { context, created } = browser();
  context.render(snapshot({}, {}, BUTTONS, []));
  context.render(snapshot({}, {}, BUTTONS, [open("q1", 2)]));
  const buttons = created.filter((node) => node.tag === "button" && node.dataset.preset === undefined);
  const q1 = buttons.find((node) => node.dataset.key === "q1");
  check("an opened span still updates", q1.textContent, "Q1 (end)");
  check("and is still highlighted", q1.classList.contains("on"), true);
  check("without anything being rebuilt to do it", buttons.length, BUTTONS.length);
}

// -- saving -----------------------------------------------------------------

// The disk fills in the third quarter. The fader buttons still move the fader,
// so nothing else on the page looks wrong - and every annotation from then on
// is gone. The operator is the only one placed to notice, so it stays on screen
// for as long as it is true.
{
  const { context } = browser();
  const saving = context.savingBanner;
  const log = (over = {}) => ({ healthy: true, error: null, failures: 0, ...over });
  const full = "could not write game.jsonl: No space left on device";

  check("a log that is saving shows nothing", saving(log(), log()), null);
  check("a log that is not saving is a fault", saving(log({ healthy: false, error: full, failures: 1 }), log())[0],
        "fault");
  check(
    "and says why, and what it costs",
    saving(log({ healthy: false, error: full, failures: 1 }), log())[1],
    "Log not saving: " + full + ". The fader buttons still work, but annotations are being lost.",
  );
  check(
    "the log outranks the markers, which can be rebuilt from it",
    saving(log({ healthy: false, error: full, failures: 1 }), log({ healthy: false, error: "x", failures: 1 }))[0],
    "fault",
  );
  check(
    "markers that are not updating say the log is fine",
    saving(log(), log({ healthy: false, error: "could not write queue.tsv: No space left on device", failures: 1 })),
    ["warn", "Reaper markers not updating: could not write queue.tsv: No space left on device."
      + " The log is still saving; markers can be rebuilt from it."],
  );
  check(
    "a log that recovered still says what it lost",
    saving(log({ failures: 3 }), log()),
    ["note", "3 log entries were not saved earlier. Saving again now."],
  );
  check("in the singular too", saving(log({ failures: 1 }), log())[1],
        "1 log entry was not saved earlier. Saving again now.");
  check(
    "a mirror failing now outranks entries lost earlier",
    saving(log({ failures: 2 }), log({ healthy: false, error: "x", failures: 1 }))[0],
    "warn",
  );
}

{
  const { context, nodes } = browser();
  const snap = snapshot();
  snap.log = { ...snap.log, healthy: false, error: "No space left on device", failures: 1 };
  context.render(snap);
  check("the page shows it", nodes.get("saving").className, "fault");
  context.render(snapshot());
  check("and clears it when it is no longer true", nodes.get("saving").className, "");
  check("text and all", nodes.get("saving").textContent, "");
}

// -- the link banner --------------------------------------------------------

// The failure that matters in a stadium is not a socket that closes, it is one
// that half-opens: delivery stops, `onclose` never fires, and the page goes on
// showing a six-minute-old snapshot with complete confidence. So the banner has
// three states rather than two, and an open socket alone is not one of them.
const { context: linkContext } = browser();
const linkBanner = linkContext.linkBanner;
const STALE_AFTER = 37.5; // what the box sends; see web.STALE_AFTER

check("a closed socket says so", linkBanner(false, 0, STALE_AFTER)[0], "lost");
check(
  "a closed socket warns that the screen is stale",
  linkBanner(false, 0, STALE_AFTER)[1],
  "Not connected to the box \u2014 what you see may be stale",
);
// Until the box has named a threshold nothing has been delivered, and there is
// no basis for calling the link healthy. An open socket is an attempt.
check("an open socket that has delivered nothing is connecting",
      linkBanner(true, 0, null)[0], "connecting");
check("and does not claim a staleness it has no threshold for",
      linkBanner(true, 999, null)[0], "connecting");
check("a delivering link shows no banner at all", linkBanner(true, 0, STALE_AFTER), null);
// A box with nothing to report goes deliberately quiet, so quiet is not a
// fault. Same distinction recordingTag draws about a parked Reaper.
check("a quiet link inside the threshold is not a fault",
      linkBanner(true, STALE_AFTER - 0.1, STALE_AFTER), null);
check("silence past the threshold is", linkBanner(true, STALE_AFTER, STALE_AFTER)[0], "stale");
check(
  "and says how long it has been",
  linkBanner(true, 42.4, STALE_AFTER)[1],
  "No word from the box for 42s \u2014 what you see may be stale",
);

// The invariant: there is one way to show nothing, and it needs both a live
// socket and something recently delivered through it.
for (const [open, silence, staleAfter] of [
  [false, 0, STALE_AFTER],
  [false, 0, null],
  [true, 0, null],
  [true, 999, STALE_AFTER],
]) {
  check(
    `open=${open} silence=${silence} staleAfter=${staleAfter}: never reads as healthy`,
    linkBanner(open, silence, staleAfter) === null,
    false,
  );
}

// -- the link counter -------------------------------------------------------

// The half that says things are fine. A banner cannot: its absence is also what
// a page that has stopped executing looks like, and telling those apart is the
// entire point on a link assumed to be unreliable.
const linkPulse = linkContext.linkPulse;

check("nothing heard yet shows no reading", linkPulse(null, null)[1], "--");
check("and claims no health for it", linkPulse(null, null)[0], "");
check("a fresh frame reads zero", linkPulse(0, STALE_AFTER)[1], "0s");
check("and reads as live", linkPulse(0, STALE_AFTER)[0], "live");
check("seconds are whole", linkPulse(3.4, STALE_AFTER)[1], "3s");
// The count climbing is normal - the box only speaks every 15s when it has
// nothing to report. What is not normal is a count that stops changing.
check("a quiet box still reads live", linkPulse(STALE_AFTER - 0.1, STALE_AFTER)[0], "live");
check("silence past the threshold does not", linkPulse(STALE_AFTER, STALE_AFTER)[0], "stale");
check("but still says how long", linkPulse(41.6, STALE_AFTER)[1], "42s");

// A silence with no threshold to judge it against is not a fault, it is a
// socket that has not been told anything yet.
check("an unmeasured silence is never called stale", linkPulse(999, null)[0], "live");

// The counter and the banner have to agree about what is wrong. The banner is
// what shouts; the counter is what is read at a glance.
for (const silence of [0, 10, STALE_AFTER - 0.1]) {
  check(`silence=${silence}: quiet link, no banner and a live counter`,
        [linkBanner(true, silence, STALE_AFTER), linkPulse(silence, STALE_AFTER)[0]],
        [null, "live"]);
}
check("past the threshold both say so",
      [linkBanner(true, 999, STALE_AFTER)[0], linkPulse(999, STALE_AFTER)[0]],
      ["stale", "stale"]);

// -- the link banner, wired up ----------------------------------------------

// Driven through the socket callbacks the page actually installs, so the
// plumbing is covered as well as the decision above.
const keepaliveFrame = { data: JSON.stringify({ keepalive: true, stale_after: STALE_AFTER }) };
function keepaliveFrameFor(staleAfter) {
  return { data: JSON.stringify({ keepalive: true, stale_after: staleAfter }) };
}
const snapshotFrame = { data: JSON.stringify(snapshot()) };

{
  const { nodes } = browser();
  check("a page that has not connected yet does not claim it has",
        nodes.get("link").className, "lost");
  check("and its counter shows no reading rather than a huge one",
        nodes.get("pulse").textContent, "--");
}
{
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  check("an opened socket is still only connecting", nodes.get("link").className, "connecting");
}
{
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(snapshotFrame);
  check("a snapshot renders", nodes.get("state").textContent, "STANDING DOWN");
  // The threshold arrives with the keepalive, and until it does the page has
  // no number to judge a silence against.
  check("but does not settle the threshold on its own",
        nodes.get("link").className, "connecting");
}
{
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(keepaliveFrame);
  check("a delivered keepalive clears the banner", nodes.get("link").className, "");
  check("and leaves no text behind it", nodes.get("link").textContent, "");
  check("and starts the counter", nodes.get("pulse").textContent, "0s");
  check("green, which is the point of it", nodes.get("pulse").className, "live");
}
{
  // A keepalive carries no state. Rendering it would blank the whole page.
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(snapshotFrame);
  sockets[0].onmessage(keepaliveFrame);
  check("a keepalive does not blank the state", nodes.get("state").textContent, "STANDING DOWN");
  check("and does not blank the fader", nodes.get("level").textContent, "unknown");
}
{
  // Everything the old socket established goes with it, the threshold
  // included: the next one proves itself from scratch.
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(keepaliveFrame);
  sockets[0].onclose();
  check("a dropped socket says so at once", nodes.get("link").className, "lost");
  check("and the counter stops claiming a reading", nodes.get("pulse").textContent, "--");
  check("and stops being green", nodes.get("pulse").className, "");
}

{
  // A snapshot is proof of life too, so it moves the counter even though it
  // carries no threshold.
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(snapshotFrame);
  check("a snapshot starts the counter as well", nodes.get("pulse").textContent, "0s");
}

// -- the clock estimate -----------------------------------------------------

// #11: one round trip, the NTP way, and the tightest of the last few.
{
  const { context } = browser();
  const { clockSample, clockEstimate } = context;
  // Page clock 1000 s ahead; 40 ms out and 60 ms back, box read at 5000.
  const sample = clockSample(6000.0, 5000.04, 6000.1);
  check("the offset is the trip's midpoint less the box's reading",
        Math.round(sample.offset * 1000) / 1000, 1000.01);
  check("and it is out by at most half the trip", Math.round(sample.uncertainty * 1000) / 1000, 0.05);
  check("no samples, no estimate", clockEstimate([]), null);
  check("the tightest sample wins",
        clockEstimate([{ offset: 1, uncertainty: 0.3 }, { offset: 2, uncertainty: 0.01 },
                       { offset: 3, uncertainty: 0.2 }]),
        { offset: 2, uncertainty: 0.01 });
}

// Wired up: a keepalive asks, a pong answers, and the next tap carries it.
// Taps only, not the page's own first fetch of the state.
const tapsIn = (posted) => posted.filter(({ path }) => path !== "/api/state");

{
  const { context, nodes, sockets, posted: all } = browser();
  const posted = () => tapsIn(all);
  const socket = sockets[0];
  context.post("/api/arm");
  check("before any round trip a tap says it has no estimate",
        [posted()[0].body.tap.offset, posted()[0].body.tap.uncertainty], [null, null]);
  check("and still says when it happened", typeof posted()[0].body.tap.at, "number");

  socket.onmessage(keepaliveFrameFor(STALE_AFTER));
  check("a keepalive is the cue to time the link", socket.sent.length, 1);
  check("with the page's clock", typeof socket.sent[0].ping, "number");

  const sentAt = socket.sent[0].ping;
  socket.onmessage({ data: JSON.stringify({ pong: sentAt, box: 12.0 }) });
  // The stub makes an element the first time the page asks for it, and only
  // render asks for this one.
  check("a pong renders nothing", nodes.has("state"), false);
  context.post("/api/arm");
  const tap = posted()[1].body.tap;
  check("the next tap carries the estimate", tap.offset !== null && tap.uncertainty >= 0, true);
  check("measured against the box's clock", Math.abs(tap.offset - (sentAt - 12.0)) < 1, true);

  socket.close();
  context.post("/api/arm");
  check("a closed socket forgets the clock it was measured over", posted()[2].body.tap.offset, null);
}

// A trip that ended before it began is a clock that stepped, not a sample.
{
  const { context, sockets, posted } = browser();
  sockets[0].onmessage({ data: JSON.stringify({ pong: 1e12, box: 1.0 }) });
  context.post("/api/arm");
  check("a backwards round trip is not used", tapsIn(posted)[0].body.tap.offset, null);
}

// -- sending, and failing to --------------------------------------------------

// A fetch the test resolves or rejects when it chooses.
function controlled() {
  const pending = [];
  // The page's own first fetch of the state is left hanging, as the default
  // stub leaves it, so `pending` holds taps and nothing else.
  const fetch = (path) =>
    path === "/api/state"
      ? new Promise(() => {})
      : new Promise((resolve, reject) => {
          pending.push({ resolve, reject });
        });
  const answer = (snap, ok = true) => ({ ok, statusText: ok ? "OK" : "Bad Request", json: async () => snap });
  return { fetch, pending, answer };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

{
  const { fetch, pending, answer } = controlled();
  const { nodes } = browser({ fetch });
  // Arm and Stand down stand in for any static, always-wired button (#5 moved
  // the bare OPEN / FADE OUT pair off the page); what is under test here is
  // the generic sending/tap machinery, not what either one does.
  const button = nodes.get("btn-arm");
  button.onclick();
  check("#11: a tap shows as sending before the box answers", button.classList.contains("sending"), true);
  pending[0].resolve(answer(snapshot()));
  await settle();
  check("and stops when it does", button.classList.contains("sending"), false);
}

{
  const { fetch, pending, answer } = controlled();
  const { nodes } = browser({ fetch });
  const button = nodes.get("btn-stand-down");
  button.onclick();
  button.onclick();
  pending[0].resolve(answer(snapshot()));
  await settle();
  check("a second tap on the same button is still sending after the first lands",
        button.classList.contains("sending"), true);
  pending[1].resolve(answer(snapshot()));
  await settle();
  check("until it lands too", button.classList.contains("sending"), false);
}

{
  const { fetch, pending } = controlled();
  const { context, nodes } = browser({ fetch });
  const button = nodes.get("btn-arm");
  let threw = false;
  const tapped = context.post("/api/arm", undefined, button).catch(() => { threw = true; });
  pending[0].reject(new TypeError("Load failed"));
  await tapped;
  await settle();
  check("#11: a rejected fetch does not throw out of the tap", threw, false);
  check("clears the sending state", button.classList.contains("sending"), false);
  check("and says the tap did not reach the box", nodes.get("tap").className, "failed");
  check("naming why", nodes.get("tap").textContent.includes("Load failed"), true);

  context.render(snapshot());
  check("a snapshot does not wipe it: the box never knew", nodes.get("tap").className, "failed");
}

{
  const { fetch, pending, answer } = controlled();
  const { context, nodes, sockets } = browser({ fetch });
  // Timed, so the success leaves nothing behind.
  sockets[0].onmessage({ data: JSON.stringify({ pong: Date.now() / 1000, box: 1.0 }) });
  context.post("/api/arm");
  pending[0].reject(new TypeError("Load failed"));
  await settle();
  context.post("/api/arm");
  pending[1].resolve(answer(snapshot()));
  await settle();
  check("a later tap that lands clears the failure", nodes.get("tap").textContent, "");
}

{
  const { fetch, pending, answer } = controlled();
  const { context, nodes } = browser({ fetch });
  context.post("/api/arm");
  pending[0].resolve(answer({ error: "tap at must be a finite number" }, false));
  await settle();
  check("a refusal is shown as a refusal", nodes.get("refusal").textContent, "tap at must be a finite number");
  check("and says the tap was untimed, which it was", nodes.get("tap").className, "untimed");
}

{
  const { fetch, pending } = controlled();
  const { context, nodes } = browser({ fetch });
  context.post("/api/arm");
  const aborted = new Error("The operation was aborted.");
  aborted.name = "AbortError";
  pending[0].reject(aborted);
  await settle();
  check("a tap that timed out says it got no answer",
        nodes.get("tap").textContent.includes("no answer in 10s"), true);
}

// A command answers with the snapshot itself; the annotation routes wrap it.
{
  const { fetch, pending, answer } = controlled();
  const { context, nodes } = browser({ fetch });
  context.post("/api/arm");
  pending[0].resolve(answer({ ...snapshot(), state: "idle" }));
  await settle();
  check("a command's answer renders, bare", nodes.get("state").textContent, "IDLE");
  check("without being taken for a failed tap", nodes.get("tap").className === "failed", false);
  context.post("/api/annotate", { key: "note" });
  pending[1].resolve(answer({ entry: null, state: { ...snapshot(), state: "open" } }));
  await settle();
  check("an annotation's answer renders, wrapped", nodes.get("state").textContent, "OPEN");
}

// -- a span whose button is gone --------------------------------------------

// #14: retiring a button keeps the key, so an older log can still leave that
// span open. Without a control for it, it runs to the end of the timeline.
{
  const { context, created, posted, nodes } = browser();
  const snap = snapshot({}, {}, undefined, [
    { span_id: "band-in-stands-4", event: "band-in-stands", label: "Band in stands" },
    { span_id: "q1-9", event: "q1", label: "Q1" },
  ]);
  snap.buttons = snap.buttons.filter((button) => button.key !== "band-in-stands");
  context.render(snap);
  const labels = created.filter((node) => node.tag === "button").map((node) => node.textContent);
  check("an open span with no button gets one", labels.includes("End: Band in stands"), true);
  check("and it is not offered as a way to start anything",
        labels.includes("Band in stands (start)"), false);

  const ender = created.find((node) => node.textContent === "End: Band in stands");
  posted.length = 0;
  ender.onclick();
  check("tapping it ends that span by id", posted[0].path, "/api/span/end");
  check("naming the span the old log left open", posted[0].body.span_id, "band-in-stands-4");

  context.render(snap);
  check("a later snapshot does not relabel it", ender.textContent, "End: Band in stands");
  check("the heading says where it came from",
        nodes.has("tab-more-vocabulary") && created.some((node) => node.textContent === "OPEN FROM AN EARLIER RUN"),
        true);
}

{
  // A span whose button still exists is handled by that button, as before.
  const { context, created } = browser();
  context.render(snapshot({}, {}, undefined, [{ span_id: "timeout-home-9", event: "timeout-home", label: "Timeout: home" }]));
  const labels = created.filter((node) => node.tag === "button").map((node) => node.textContent);
  check("no orphan control for a span that has its own button",
        labels.some((label) => label.startsWith("End: ")), false);
}

// -- a fader tap that arrived too late ---------------------------------------

{
  const { context, nodes } = browser();
  const refusal = "That tap was not done: it took 3.0s to reach the box";
  context.render({ ...snapshot(), refusal, stale_tap: { delay: 3.0, threshold: 2.0 } });
  check("#16: a stale fader tap is refused loudly", nodes.get("refusal").className, "loud");
  check("in the box's words", nodes.get("refusal").textContent, refusal);
  context.render({ ...snapshot(), refusal: "already recording", stale_tap: null });
  check("an ordinary refusal is not shouted", nodes.get("refusal").className, "");
  context.render(snapshot());
  check("and nothing to refuse hides the line", nodes.get("refusal").style.display, "none");
}

// -- snapshots out of order -----------------------------------------------

{
  const { context, nodes, sockets } = browser();
  const at = (state, when) => ({ ...snapshot(), state, at: when });
  context.render(at("open", 20.0));
  context.render(at("idle", 10.0));
  check("#11: an older snapshot does not paint over a newer one", nodes.get("state").textContent, "OPEN");
  context.render(at("releasing", 20.0));
  check("one taken at the same moment still renders", nodes.get("state").textContent, "RELEASING");
  sockets[0].close();
  context.render(at("standing-down", 1.0));
  check("after the socket drops, a rebooted box's low clock is believed",
        nodes.get("state").textContent, "STANDING DOWN");
}

// -- every fader and MAIN button lands exactly once (#5) ---------------------
//
// FADER_COLUMN and MAIN_GROUPS are explicit, reviewed lists rather than
// derived from category or vocabulary order. A key left out of both would
// simply not render, in the fader column or anywhere else; a key spelled
// wrong in one of them would drop out of its intended home and reappear,
// unstyled, in MORE. Both failures are silent without this: nothing throws,
// the page just quietly offers one fewer button, or the wrong one.
{
  const real = SNAPSHOTS["standing-down"].buttons;
  const { context, created } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  const buttonKeys = created.filter((node) => node.tag === "button" && node.dataset.key)
    .map((node) => node.dataset.key);
  for (const button of real) {
    const count = buttonKeys.filter((key) => key === button.key).length;
    check(`${button.key} renders exactly once`, count, 1);
  }
}

// -- MAIN / MORE (#5) ---------------------------------------------------------

{
  const { context, nodes } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  check("MAIN is shown to start", nodes.get("tab-main").style.display, "");
  check("MORE is hidden to start", nodes.get("tab-more").style.display, "none");
  check("MAIN's tab button is marked on", nodes.get("tab-btn-main").classList.contains("on"), true);

  nodes.get("tab-btn-more").onclick();
  check("tapping MORE shows it", nodes.get("tab-more").style.display, "");
  check("and hides MAIN", nodes.get("tab-main").style.display, "none");
  check("MORE's tab button is marked on instead", nodes.get("tab-btn-more").classList.contains("on"), true);
  check("the left panel is tinted while MORE is showing", nodes.get("left").classList.contains("more"), true);
  nodes.get("tab-btn-main").onclick();
  check("and untinted back on MAIN", nodes.get("left").classList.contains("more"), false);
}

{
  // A tap in MORE leaves the operator on MORE (#109): the tab moves only when
  // they tap a tab button. Nothing is rebuilt to do it either.
  const { context, nodes, created } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  nodes.get("tab-btn-more").onclick();
  const before = created.filter((node) => node.tag === "button").length;
  const falseOpen = created.find((node) => node.dataset.key === "false-open");
  falseOpen.onclick();
  check("a MORE tap stays on MORE", nodes.get("tab-more").style.display, "");
  check("and MAIN stays hidden", nodes.get("tab-main").style.display, "none");
  check("MORE's tab button stays marked on", nodes.get("tab-btn-more").classList.contains("on"), true);
  check("the left panel stays tinted", nodes.get("left").classList.contains("more"), true);
  check("nothing was rebuilt", created.filter((node) => node.tag === "button").length, before);
}

{
  // Note stays on MORE like every other button. What is special about it is
  // only its prompt() data, not navigation.
  const { context, nodes, created } = browser({ prompt: () => "left tackle is limping" });
  context.render(SNAPSHOTS["standing-down"]);
  nodes.get("tab-btn-more").onclick();
  const note = created.find((node) => node.dataset.key === "note");
  note.onclick();
  check("note stays on MORE", nodes.get("tab-more").style.display, "");
}

{
  // The Control row (Arm / Stand down / Record) lives in MORE too, and a tap
  // there is no more navigation than any other (#109).
  const controls = [
    ["btn-arm", "/api/arm"],
    ["btn-stand-down", "/api/stand-down"],
    ["btn-record", "/api/record"],
  ];
  for (const [id, path] of controls) {
    const { context, nodes, posted } = browser();
    context.render(SNAPSHOTS["standing-down"]);
    nodes.get("tab-btn-more").onclick();
    posted.length = 0;
    nodes.get(id).onclick();
    check(`${id} stays on MORE`, nodes.get("tab-more").style.display, "");
    check(`${id} leaves MAIN hidden`, nodes.get("tab-main").style.display, "none");
    check(`${id} really posts ${path}`, posted.some((p) => p.path === path), true);
  }
}

// -- the fader column is split around the readout gap, in order (#5) --------

{
  const { context, created } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  const keys = created.filter((node) => node.tag === "button")
    .map((node) => node.dataset.key)
    .filter((key) => ["up-whistle", "up-drums", "up-slow", "up-ready", "score-reversed", "out"].includes(key));
  check(
    "the column keeps the reviewed order, split around the gap",
    keys,
    ["up-whistle", "up-drums", "up-slow", "up-ready", "score-reversed", "out"],
  );
}

// -- a tap expands a status chip to its full sentence, without growing the
// strip it lives in (#5) -----------------------------------------------------

{
  const { context, nodes } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  const refusal = nodes.get("refusal");
  check("a chip starts collapsed", refusal.dataset.expanded, undefined);
  refusal.onclick();
  check("a tap expands it", refusal.dataset.expanded, "1");
  refusal.onclick();
  check("a second tap collapses it again", refusal.dataset.expanded, "");
}

// -- the wake advice --------------------------------------------------------

// The Screen Wake Lock API needs a secure context and the page is plain HTTP,
// so the advice is the load-bearing half, not the fallback.
{
  // The stub has no navigator.wakeLock, which is the deployed case, so the
  // page has already fallen back by the time it has finished loading.
  const { context, nodes } = browser();
  check("no wake lock means advice, without being asked", nodes.get("wake").className, "advice");
  check(
    "and the advice names the setting that actually works",
    nodes.get("wake").textContent.includes("Auto-Lock"),
    true,
  );
  context.paintWake(true);
  check("a held lock says so instead", nodes.get("wake").className, "held");
  check(
    "and does not go on telling the operator to change a setting",
    nodes.get("wake").textContent.includes("Auto-Lock"),
    false,
  );
}

// -- the standing target level (#9) ------------------------------------------
//
// Two things are called a target. `snapshot.target` is the STANDING setting -
// the level the next open goes to, chosen on MORE > Target level and shown in
// the strip. `snapshot.fader.target` is where a move already in flight is
// heading. The tests below say which they mean.

// The standing-down fixture with the standing target changed, and optionally
// the fader. `overlay` refuses a key the box does not send.
function targetSnapshot(changes = {}, fader = {}) {
  const snap = snapshot({}, fader);
  snap.target = overlay(SNAPSHOTS["standing-down"].target, changes, "target");
  return snap;
}

const presetSegments = (created) => created.filter((node) => node.tag === "button" && node.dataset.preset !== undefined);

{
  const { context } = browser();
  check("targetChipText: unity", context.targetChipText(0), "target 0 dB");
  check("targetChipText: a whole dB down", context.targetChipText(-3), "target -3 dB");
  check("targetChipText: a fraction", context.targetChipText(-2.5), "target -2.5 dB");
  for (const db of [0, -3, -2.5, -6, 3]) {
    check(`targetChipText: ${db} is ASCII`, /^[\x20-\x7e]*$/.test(context.targetChipText(db)), true);
  }
  check(
    "targetChipText: a ride in flight to somewhere else says the target is for the next open",
    context.targetChipText(-6, true),
    "target -6 dB (next open)",
  );
}

{
  // The chip is amber only when the standing target is not the configured
  // default, so a leftover quiet setting is not forgotten.
  const { context, nodes } = browser();
  context.render(targetSnapshot());
  const chip = nodes.get("target-level");
  check("chip: reads the target at the default", chip.textContent, "target 0 dB");
  check("chip: no off-default at the default", chip.classList.contains("off-default"), false);
  context.render(targetSnapshot({ db: -3, level: -300 }));
  check("chip: reads a quieter target", chip.textContent, "target -3 dB");
  check("chip: off-default at -3", chip.classList.contains("off-default"), true);
  context.render(targetSnapshot());
  check("chip: back at the default it goes neutral again", chip.classList.contains("off-default"), false);
}

{
  // "Default" is the box's word, not the page's: a site whose default is -2
  // is neutral at -2 and amber at 0.
  const { context, nodes } = browser();
  const site = { presets_db: [-2, -5, -8], default_db: -2, db: -2, level: -200 };
  context.render(targetSnapshot(site));
  check("chip: neutral at a quiet site's own default", nodes.get("target-level").classList.contains("off-default"), false);
  context.render(targetSnapshot({ ...site, db: -5, level: -500 }));
  check("chip: amber at another preset", nodes.get("target-level").classList.contains("off-default"), true);
}

{
  // #139: the default need not be the first preset either. app.js itself does
  // not change for this - snapshot.target.default_db already carries whatever
  // the box configured - but this pins that claim rather than leaving it a
  // reading.
  const { context, nodes } = browser();
  const site = { presets_db: [3, 0, -3], default_db: 0, db: 0, level: 0 };
  context.render(targetSnapshot(site));
  check(
    "chip: neutral at a default that is not the first preset",
    nodes.get("target-level").classList.contains("off-default"),
    false,
  );
  context.render(targetSnapshot({ ...site, db: 3, level: 300 }));
  check(
    "chip: amber at the first (louder) preset, which is not the default",
    nodes.get("target-level").classList.contains("off-default"),
    true,
  );
}

{
  // The chip and the fader readout do not fight (see the section comment in
  // app.js). A ride to the old target still shows its own arrow in the readout;
  // the chip says the standing target is for the NEXT open.
  const ride = {
    level_known: true,
    commanded: -1000,
    db: -10,
    target: 0,
    target_db: 0,
    moving: true,
    move: moveFrom("ride", -10, 0, 1.5, { by: "up-slow" }),
  };
  const { context, nodes } = browser({ now: STILL });
  context.render(targetSnapshot({ db: -6, level: -600 }, ride));
  check("ride: the readout still shows where the ride is going", nodes.get("level").textContent.startsWith("-10.00 dB \u2192 0.00 dB"), true);
  check("ride: the chip says the new target is for the next open", nodes.get("target-level").textContent, "target -6 dB (next open)");

  context.render(targetSnapshot({ db: 0, level: 0 }, ride));
  check("ride: no note when the ride is to the standing target", nodes.get("target-level").textContent, "target 0 dB");

  const idle = { level_known: true, commanded: 0, db: 0, target: null, target_db: null, moving: false, move: null };
  context.render(targetSnapshot({ db: -6, level: -600 }, idle));
  check("nothing moving: no note", nodes.get("target-level").textContent, "target -6 dB");

  // READY's own ride goes to target - hold_below_db, never to the standing
  // target, so the chip carries the suffix there too even though nothing was
  // changed. Deliberate: the readout says "-inf dB -> -15.00 dB" at the same
  // moment and the chip must not seem to disagree with it.
  const ready = {
    level_known: true,
    commanded: -32768,
    db: null,
    target: -1500,
    target_db: -15,
    moving: true,
    move: moveFrom("ride", null, -15, 4.0, { by: "up-ready" }),
  };
  context.render(targetSnapshot({}, ready));
  check("READY's ride to its hold level: the readout shows the hold level", nodes.get("level").textContent.startsWith("-\u221e dB \u2192 -15.00 dB"), true);
  check("READY's ride to its hold level: the chip says the target is for the next open", nodes.get("target-level").textContent, "target 0 dB (next open)");

  const fade = {
    level_known: true,
    commanded: -1000,
    db: -10,
    target: -32768,
    target_db: null,
    moving: true,
    move: moveFrom("fade", -10, null, 2.0, { by: "out" }),
  };
  context.render(targetSnapshot({ db: -6, level: -600 }, fade));
  check("a fade to -inf is not a competing target: no note", nodes.get("target-level").textContent, "target -6 dB");
}

{
  const { context, created } = browser();
  context.render(targetSnapshot());
  const segments = presetSegments(created);
  check("segments: one per preset, in list order", segments.map((node) => node.dataset.preset), ["0", "-3", "-6"]);
  check("segments: labelled in dB", segments.map((node) => node.textContent), ["0 dB", "-3 dB", "-6 dB"]);
  check("segments: labels are ASCII", segments.every((node) => /^[\x20-\x7e]*$/.test(node.textContent)), true);
  check("segments: none is a keyed vocabulary button", segments.some((node) => node.dataset.key !== undefined), false);
}

{
  const { context, created } = browser();
  context.render(targetSnapshot({ presets_db: [-2, -5, -8], default_db: -2, db: -5, level: -500 }));
  check(
    "segments: a site's own list, in its own order",
    presetSegments(created).map((node) => node.dataset.preset),
    ["-2", "-5", "-8"],
  );
}

{
  const { context, created } = browser();
  context.render(targetSnapshot());
  const selected = () => presetSegments(created).filter((node) => node.classList.contains("selected"));
  check("selected: exactly one segment", selected().map((node) => node.dataset.preset), ["0"]);
  context.render(targetSnapshot({ db: -3, level: -300 }));
  check("selected: a snapshot at another target moves it", selected().map((node) => node.dataset.preset), ["-3"]);
  context.render(targetSnapshot({ db: -6, level: -600 }));
  check("selected: and again", selected().map((node) => node.dataset.preset), ["-6"]);
}

{
  // The tap's own feedback is the `sending` outline. Painting selected before
  // the box has said so would show a change that a refusal or a lost packet
  // did not make.
  const { context, created, posted } = browser();
  context.render(targetSnapshot());
  posted.length = 0;
  const minus3 = presetSegments(created).find((node) => node.dataset.preset === "-3");
  minus3.onclick();
  check("tap: posts to /api/target", posted.map((p) => p.path), ["/api/target"]);
  check("tap: with the level as a number", posted[0].body.db, -3);
  check("tap: and nothing else in the body but the stamp", Object.keys(posted[0].body).sort(), ["db", "tap"]);
  check("tap: is not painted selected until a snapshot says so", minus3.classList.contains("selected"), false);
  check("tap: the old segment is still the selected one", presetSegments(created)[0].classList.contains("selected"), true);
  check("tap: shows the sending outline meanwhile", minus3.classList.contains("sending"), true);
}

{
  // Same discipline as `renderedButtons`: never tear a control down under a
  // thumb, since a tap landing on a detached node fires no click.
  const { context, created } = browser();
  context.render(targetSnapshot());
  const before = created.length;
  context.render(targetSnapshot({ db: -3, level: -300 }));
  context.render(targetSnapshot({ db: -6, level: -600 }));
  check("rebuild: the same list builds nothing new", created.length, before);
  context.render(targetSnapshot({ presets_db: [-2, -5], default_db: -2, db: -2, level: -200 }));
  check("rebuild: a different list does", presetSegments(created).length, 3 + 2);
}

{
  const { context, nodes, created } = browser();
  check("before any snapshot: no segments", presetSegments(created).length, 0);
  // Nothing has asked for the chip yet, so read it the way the page would.
  const chip = context.document.getElementById("target-level");
  check("before any snapshot: the chip is empty", chip.textContent, "");
  check("before any snapshot: and neutral", chip.classList.contains("off-default"), false);
  void nodes;
}

{
  // A box that predates #9 sends no `target`. Dereferencing it unguarded would
  // throw inside render() and stop the page repainting - the same hazard the
  // duty chip's guard is there for.
  const { context, nodes, created } = browser();
  const without = structuredClone(SNAPSHOTS["standing-down"]);
  delete without.target;
  let error = null;
  try {
    context.render(without);
  } catch (caught) {
    error = String(caught);
  }
  check("a snapshot with no target does not throw", error, null);
  check("and the rest of the page still renders", nodes.get("state").textContent, "STANDING DOWN");
  check("and offers no segments", presetSegments(created).length, 0);
}

{
  // #109: a tap in MORE leaves the operator on MORE.
  const { context, nodes, created } = browser();
  context.render(targetSnapshot());
  nodes.get("tab-btn-more").onclick();
  presetSegments(created).find((node) => node.dataset.preset === "-6").onclick();
  check("a segment tap stays on MORE", nodes.get("tab-more").style.display, "");
  check("and MAIN stays hidden", nodes.get("tab-main").style.display, "none");
}

for (const [label, fader] of [["unknown", { level_known: false }], ["known", { level_known: true }]]) {
  // Changing the target moves nothing, so it is never greyed (#9). The ramp
  // buttons are, and the chip does not follow them.
  const { context, created } = browser();
  context.render(targetSnapshot({}, fader));
  check(`level ${label}: no segment is disabled`, presetSegments(created).some((node) => node.disabled), false);
}

{
  // The vocabulary paint loop must not treat a segment as a keyed button: it
  // would rewrite its label from an empty `dataset.label`.
  const { context, created } = browser();
  context.render(targetSnapshot({ db: -3, level: -300 }));
  context.render(targetSnapshot({ db: -3, level: -300 }));
  check(
    "paint loop: segment labels survive a repaint",
    presetSegments(created).map((node) => node.textContent),
    ["0 dB", "-3 dB", "-6 dB"],
  );
}

// -- a target tap that stored and did not move says so (#153) -----------------
//
// The box sends `target.stored` ({db, because}) or null; the page words it
// under the segments. Null is also what the box sends for every tap made while
// the fader is closed: storing is what the operator expects there, so the page
// says nothing at all.

const NOT_MOVED_SENTENCE = "The fader did not move.";
const pure = browser().context;

check(
  "storedNote: open says stored and that the fader did not move",
  pure.storedNote({ db: -3, because: "open" }),
  { text: "Stored: -3 dB on the next open. " + NOT_MOVED_SENTENCE, attention: true },
);
check(
  "storedNote: ready",
  pure.storedNote({ db: -6, because: "ready" }),
  { text: "Stored: -6 dB on the next open. " + NOT_MOVED_SENTENCE, attention: true },
);
check(
  "storedNote: releasing says the fade carries on",
  pure.storedNote({ db: -3, because: "releasing" }),
  { text: "Stored: -3 dB on the next open. The fade carries on to -inf.", attention: true },
);
check(
  "storedNote: unchanged",
  pure.storedNote({ db: -3, because: "unchanged" }),
  { text: "Already the target: -3 dB. Nothing changed.", attention: false },
);
check(
  "storedNote: unity reads 0 dB",
  pure.storedNote({ db: 0, because: "unchanged" }).text,
  "Already the target: 0 dB. Nothing changed.",
);
check(
  "storedNote: an unknown reason falls back to did-not-move",
  pure.storedNote({ db: -3, because: "from-a-newer-box" }),
  { text: "Stored: -3 dB on the next open. " + NOT_MOVED_SENTENCE, attention: true },
);
check("storedNote: null is empty", pure.storedNote(null), { text: "", attention: false });
check("storedNote: undefined is empty", pure.storedNote(undefined), { text: "", attention: false });
for (const because of ["open", "ready", "releasing", "unchanged", "from-a-newer-box"]) {
  check(
    `storedNote: every sentence is ASCII (${because})`,
    /^[\x20-\x7e]*$/.test(pure.storedNote({ db: -3, because }).text),
    true,
  );
}

{
  const { context, nodes } = browser({ now: STILL });
  context.render(fadeSnapshot("target-stored"));
  const note = nodes.get("target-note");
  check("target-stored: the note is the fade sentence", note.textContent, "Stored: -3 dB on the next open. The fade carries on to -inf.");
  check("target-stored: and wants attention", note.className, "attention");
  check("target-stored: the chip says it is for the next open", nodes.get("target-level").textContent, "target -3 dB (next open)");

  // The box clears a note when the fader moves or the state changes, and the
  // page follows its next snapshot (a later `at`, or the page ignores it as
  // old) with the note cleared.
  const cleared = structuredClone(SNAPSHOTS["target-stored"]);
  cleared.at += 1;
  cleared.target.stored = null;
  context.render(cleared);
  check("a snapshot with no note empties it", note.textContent, "");
  check("and drops the attention colour", note.className, "");
  check("and drops (next open) from the chip", nodes.get("target-level").textContent, "target -3 dB");
}

{
  // #153's Done-when as the maintainer kept it: a tap while the fader is closed
  // leaves no note and no (next open). The box sends `stored: null` for it.
  const { context, nodes } = browser();
  context.render(targetSnapshot({ db: -3, level: -300 }));
  check("closed: no note", nodes.get("target-note").textContent, "");
  check("closed: no attention colour", nodes.get("target-note").className, "");
  check("closed: the chip does not say (next open)", nodes.get("target-level").textContent, "target -3 dB");
}

{
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS["target-stored"]));
  const note = nodes.get("target-note");
  const written = note.writes;
  context.render(structuredClone(SNAPSHOTS["target-stored"]));
  check("an unchanged note is not rewritten (#51)", note.writes, written);
}

{
  const { context, nodes } = browser();
  const snap = structuredClone(SNAPSHOTS["target-stored"]);
  snap.target.stored = { db: -3, because: "unchanged" };
  context.render(snap);
  check("unchanged while open: dim, not attention", nodes.get("target-note").className, "");
  check("unchanged while open: the chip adds nothing", nodes.get("target-level").textContent, "target -3 dB");
}

// -- a target tap while the fader is up rides there (#128) --------------------
//
// The box does the ride; the page greys the segments while the level is unknown
// and the fader is up (a ride from an unknown level would be a guess), and the
// selected segment wears the fading colour until the box says it landed.

const TARGET_BLOCKED_COPY = runInContext("TARGET_BLOCKED", browser().context);
const riding = (created) =>
  created.filter((node) => node.tag === "button" && node.dataset.preset !== undefined && node.classList.contains("fading"));

check(
  "targetBlocked: true for open and ready with the level unknown",
  ["open", "ready"].map((name) => pure.targetBlocked(name, { level_known: false })),
  [true, true],
);
check(
  "targetBlocked: false for idle, standing down and releasing with the level unknown",
  ["idle", "standing-down", "releasing"].map((name) => pure.targetBlocked(name, { level_known: false })),
  [false, false, false],
);
check("targetBlocked: false for open with the level known", pure.targetBlocked("open", { level_known: true }), false);
check(
  "storedNote: unknown says the box does not know",
  pure.storedNote({ db: -3, because: "unknown" }).text,
  "Stored: -3 dB on the next open. The fader did not move: the box does not know where it is.",
);
check("TARGET_BLOCKED is ASCII", /^[\x20-\x7e]*$/.test(TARGET_BLOCKED_COPY), true);

{
  const { context, nodes, created } = browser();
  context.render(structuredClone(SNAPSHOTS["faults"]));
  const segments = presetSegments(created);
  check("faults (open, unknown): every segment is greyed", segments.map((node) => node.disabled), [true, true, true]);
  check("faults: the note says why", nodes.get("target-note").textContent, TARGET_BLOCKED_COPY);
  check("faults: and wants attention", nodes.get("target-note").className, "attention");

  // The level comes back: the same nodes are re-enabled in place.
  const back = structuredClone(SNAPSHOTS["faults"]);
  back.at += 1;
  back.fader.level_known = true;
  const before = created.length;
  context.render(back);
  check("level back: no segment is rebuilt", created.length, before);
  check("level back: the same segments are enabled", presetSegments(created).map((node) => node.disabled), [false, false, false]);
  check("level back: the note is gone", nodes.get("target-note").textContent, "");
}

{
  const { context, nodes, created } = browser();
  context.render(structuredClone(SNAPSHOTS["standing-down"]));
  check(
    "standing down, unknown: segments stay enabled (storing is allowed)",
    presetSegments(created).map((node) => node.disabled),
    [false, false, false],
  );
  check("standing down, unknown: no note", nodes.get("target-note").textContent, "");
}

{
  const snap = structuredClone(SNAPSHOTS["faults"]);
  snap.target.stored = { db: -3, because: "unknown" };
  const { context, nodes } = browser();
  context.render(snap);
  check(
    "the note joins the stored sentence and the greyed reason",
    nodes.get("target-note").textContent,
    "Stored: -3 dB on the next open. The fader did not move: the box does not know where it is. " + TARGET_BLOCKED_COPY,
  );
}

{
  const { context, nodes, created } = browser({ now: STILL });
  context.render(fadeSnapshot("retargeting"));
  check(
    "retargeting: the selected segment wears the fading colour",
    riding(created).map((node) => node.dataset.preset),
    ["-3"],
  );
  check(
    "retargeting: no vocabulary button does",
    created.some((node) => node.tag === "button" && node.dataset.key !== undefined && node.classList.contains("fading")),
    false,
  );
  check("retargeting: the readout tag says riding", nodes.get("level-tag").textContent, "riding");
  check("retargeting: the readout starts at the old level", nodes.get("level").textContent.startsWith("0.00 dB \u2192 -3.00 dB"), true);
  check("retargeting: the segments are not greyed", presetSegments(created).some((node) => node.disabled), false);
}

{
  // Landed-only (#154): the page's own clock running out does not end it.
  let clock = 2_000_000;
  const { context, created, intervals } = browser({ now: () => clock * 1000 });
  const ride = fadeSnapshot("retargeting");
  context.render(ride);
  clock += 10;
  intervals[0]();
  check("retargeting: still painted after the ride's time is up", riding(created).map((node) => node.dataset.preset), ["-3"]);
  context.render(landedSnapshot(ride, 10));
  check("retargeting: a snapshot without a move clears it", riding(created).length, 0);
}

check(
  "parity: a retarget curve is among the cases",
  CURVES.some((curve) => curve.name.startsWith("retarget")),
  true,
);

{
  const { context, nodes, created } = browser({ now: STILL });
  context.render(fadeSnapshot("target-stored"));
  check(
    "target-stored (releasing): the note is the fade sentence",
    nodes.get("target-note").textContent,
    "Stored: -3 dB on the next open. The fade carries on to -inf.",
  );
  check("target-stored (releasing): the chip says next open", nodes.get("target-level").textContent, "target -3 dB (next open)");
  check("target-stored (releasing): no segment wears the riding colour", riding(created).length, 0);
}

// -- MAIN after Game 3 (#155) ------------------------------------------------

// The real, regenerated standing-down snapshot, rendered once per test that
// needs a fresh page; `openSpans` is as the box sends them.
function game3(openSpans = []) {
  const { context, created, posted } = browser();
  context.render(snapshot({}, {}, undefined, openSpans));
  const buttons = created.filter((node) => node.tag === "button");
  return { context, created, posted, buttons, byKey: new Map(buttons.map((node) => [node.dataset.key, node])) };
}

const SCORING_KEYS = ["touchdown", "field-goal", "safety", "first-down", "defensive-stop"];
const TIMEOUT_KEYS = ["timeout", "timeout-home", "timeout-away", "timeout-media", "timeout-injury"];
const FADER_KEYS = ["up-whistle", "up-drums", "up-slow", "up-ready", "score-reversed", "out"];
const toneOf = (tone) => (node) => node.dataset.tone === tone;

{
  const { created } = game3();
  check(
    "MAIN's groups are Scoring, Timeouts, Game, in that order",
    created.filter((node) => node.tag === "h2").slice(0, 3).map((node) => node.textContent),
    ["Scoring", "Timeouts", "Game"],
  );
}

{
  const { buttons, byKey } = game3();
  check(
    "Scoring renders Touchdown, Field goal, Safety, First down, Defensive stop",
    buttons.filter(toneOf("score")).map((node) => node.dataset.key),
    SCORING_KEYS,
  );
  const timeouts = buttons.filter(toneOf("timeout"));
  check("Official timeout leads the Timeouts group", timeouts[0].dataset.key, "timeout");
  check("and says Official timeout", timeouts[0].textContent, "Official timeout (start)");
  check("the Timeouts group is the five timeouts", timeouts.map((node) => node.dataset.key), TIMEOUT_KEYS);
  for (const key of ["q1", "q2", "q3", "q4", "halftime", "timeout-official"]) {
    check(`no ${key} button renders`, byKey.has(key), false);
  }
  check("halftime exodus is still on MAIN", byKey.has("halftime-exodus"), true);
  check("halftime exodus is untoned", byKey.get("halftime-exodus").dataset.tone, undefined);
  check(
    "every scoring button, Safety included, carries the score tone",
    SCORING_KEYS.map((key) => byKey.get(key).dataset.tone),
    SCORING_KEYS.map(() => "score"),
  );
  check(
    "every timeout carries the timeout tone",
    TIMEOUT_KEYS.map((key) => byKey.get(key).dataset.tone),
    TIMEOUT_KEYS.map(() => "timeout"),
  );
  check(
    "nothing else carries a tone",
    [...new Set(buttons.map((node) => node.dataset.tone).filter((tone) => tone !== undefined))].sort(),
    ["score", "timeout"],
  );
  check(
    "no tone outside Scoring and Timeouts, the fader column and MORE included",
    buttons.filter((node) => node.dataset.tone && !SCORING_KEYS.concat(TIMEOUT_KEYS).includes(node.dataset.key)).length,
    0,
  );
}

{
  const { created } = game3();
  const grid = created.find(
    (node) => node.tag === "div" && node.children.some((child) => child.dataset.key === "touchdown"),
  );
  check("the scoring grid is marked for its own layout", grid.dataset.tone, "score");
}

const iconOf = (node) => node.children[0];
const svgOf = (node) => iconOf(node).innerHTML;

{
  const { byKey } = game3();
  const keyed = FADER_KEYS.concat(SCORING_KEYS);
  for (const key of keyed) {
    check(`${key} leads with an icon`, iconOf(byKey.get(key)).className, "ico");
    check(`${key}'s icon is an svg`, svgOf(byKey.get(key)).startsWith("<svg"), true);
  }
  check("all 11 fader and scoring icons are pairwise distinct", new Set(keyed.map((key) => svgOf(byKey.get(key)))).size, 11);

  const shared = new Set(TIMEOUT_KEYS.map((key) => svgOf(byKey.get(key))));
  check("the timeouts share one icon", shared.size, 1);
  check("and it differs from all 11 others", keyed.map((key) => svgOf(byKey.get(key))).includes([...shared][0]), false);

  for (const key of ["halftime-exodus", "band-enters-stands"]) {
    check(`a grey button (${key}) has one child`, byKey.get(key).children.length, 1);
    check(`and it is the label`, byKey.get(key).children[0].className, "lbl");
  }

  const everyIcon = [...new Set(keyed.concat(TIMEOUT_KEYS).map((key) => svgOf(byKey.get(key))))];
  for (const html of everyIcon) {
    check("an icon follows the button's colour", html.includes("currentColor"), true);
    check("and is hidden from assistive tech", html.includes('aria-hidden="true"'), true);
    check("and fetches nothing", /http|xmlns|<script/.test(html), false);
    check("and has no whitespace between tags", />\s+</.test(html), false);
  }
}

{
  const span = { span_id: "timeout-9", event: "timeout", label: "Official timeout" };
  const { context, created } = browser();
  context.render(snapshot({}, {}, undefined, [span]));
  const node = created.find((n) => n.tag === "button" && n.dataset.key === "timeout");
  check("an open span's button says (end)", node.textContent, "Official timeout (end)");
  check("and keeps its icon", iconOf(node).className, "ico");
  context.render(snapshot({}, {}, undefined, []));
  check("a closed span says (start) again", node.textContent, "Official timeout (start)");
  check("and still keeps its icon", iconOf(node).className, "ico");
}

{
  const { created, posted } = game3([{ span_id: "q1-9", event: "q1", label: "Q1" }]);
  const ender = created.find((node) => node.tag === "button" && node.textContent === "End: Q1");
  check("a quarter left open by an older log is offered in MORE", ender !== undefined, true);
  posted.length = 0;
  ender.onclick();
  check("and tapping it ends that span", posted[0].path, "/api/span/end");
  check("by its id", posted[0].body.span_id, "q1-9");
}

// -- a move is described once and animated here (#154) -------------------------

const ONE_UNIT_DB = 0.01; // one console unit, the finest the console takes
const MOVE_STEP = (move, elapsed) => context.moveDbAt(move, elapsed);

{
  const fade = (from, seconds = 2.0) => moveFrom("fade", from, null, seconds);
  check("moveDbAt: a fade starts where it started", MOVE_STEP(fade(0), 0), 0);
  check("moveDbAt: and is halfway to the floor at half the time", MOVE_STEP(fade(0), 1.0), -30);
  check("moveDbAt: and is -inf at its end", MOVE_STEP(fade(0), 2.0), null);
  check("moveDbAt: and after it", MOVE_STEP(fade(0), 9.0), null);
  check("moveDbAt: a negative elapsed holds the start", MOVE_STEP(fade(0), -1), 0);
  check("moveDbAt: a NaN elapsed holds the start", MOVE_STEP(fade(0), NaN), 0);
  check("moveDbAt: a fade from below the floor holds, then closes", MOVE_STEP(fade(-70), 1.0), -70);
  check("moveDbAt: and closes at its end", MOVE_STEP(fade(-70), 2.0), null);

  const knee = { db: -20.0, fraction: 0.15 };
  const ride = (from, to, seconds) => moveFrom("ride", from, to, seconds, { knee });
  check("moveDbAt: a ride from -inf holds -inf at the start", MOVE_STEP(ride(null, 0, 1.5), 0), null);
  check("moveDbAt: and reaches the knee at the knee's fraction", MOVE_STEP(ride(null, 0, 1.5), 0.225), -20);
  check("moveDbAt: and arrives", MOVE_STEP(ride(null, 0, 1.5), 1.5), 0);
  check("moveDbAt: a ride to below the floor holds -inf", MOVE_STEP(ride(null, -70, 1.5), 0.7), null);
  check("moveDbAt: and arrives", MOVE_STEP(ride(null, -70, 1.5), 1.5), -70);
  check("moveDbAt: a move whose from is its to returns it", MOVE_STEP(ride(-6, -6, 1.5), 0.5), -6);
}

// The page's curve against the console's own steps: the box wrote both, so a
// drift on either side fails here. Within one console unit at every step; the
// last step is where the move arrives.
for (const curve of CURVES) {
  let worst = 0;
  let mismatched = null;
  for (const [offset, db] of curve.steps) {
    if (offset >= curve.move.seconds) continue;
    const drawn = context.moveDbAt(curve.move, offset);
    if ((drawn === null) !== (db === null)) mismatched = [offset, db, drawn];
    else if (db !== null) worst = Math.max(worst, Math.abs(drawn - db));
  }
  check(`parity: ${curve.name}: no step is null on one side only`, mismatched, null);
  check(`parity: ${curve.name}: within one console unit`, worst <= ONE_UNIT_DB, true);
  const last = curve.steps[curve.steps.length - 1];
  check(
    `parity: ${curve.name}: arrives where the last step does`,
    context.moveDbAt(curve.move, curve.move.seconds),
    last[1],
  );
}
check("parity: the curves fixture has cases", CURVES.length > 0, true);

check("moveElapsed: the box's gap plus the page's", context.moveElapsed({ started_at: 90 }, 100, 5), 15);
check("moveElapsed: no usable at has no elapsed", context.moveElapsed({ started_at: 90 }, null, 5), null);
check("moveElapsed: no arrival has no elapsed", context.moveElapsed({ started_at: 90 }, 100, null), null);
check("moveElapsed: no start has no elapsed", context.moveElapsed({ started_at: null }, 100, 5), null);

check("secondsSince: a stamp never taken is null", context.secondsSince(null, { wall: 5, mono: 5 }), null);
check(
  "secondsSince: the larger of the two deltas",
  [
    context.secondsSince({ wall: 10, mono: 10 }, { wall: 12, mono: 15 }),
    context.secondsSince({ wall: 10, mono: 10 }, { wall: 16, mono: 11 }),
  ],
  [5, 6],
);

// The snapshot the box sends when a fade starts, with `at` the moment it began
// so the page's elapsed time is exactly the page's own.
function fadeSnapshot(name = "releasing", offset = 0) {
  const snap = structuredClone(SNAPSHOTS[name]);
  snap.at = snap.fader.move.started_at + offset;
  return snap;
}

// The snapshot the box sends once that move has landed: the move gone and the
// live values back.
function landedSnapshot(from, laterBy) {
  const snap = structuredClone(from);
  snap.at = from.at + laterBy;
  snap.fader = {
    ...from.fader,
    move: null,
    moving: false,
    commanded: -32768,
    db: null,
    target: null,
    target_db: null,
    sent_at: snap.at,
  };
  return snap;
}

const fadingNodes = (created) =>
  created.filter((node) => node.tag === "button" && node.classList.contains("fading"));

{
  let clock = 2_000_000;
  const { context: page, nodes, created, frames } = browser({ now: () => clock * 1000 });
  page.render(fadeSnapshot());
  check("a fade renders as a sweep: on arrival", nodes.get("level").textContent, "0.00 dB \u2192 -\u221e dB");
  check("a fade says fading", nodes.get("level-tag").textContent, "fading");
  check("and is styled fading", nodes.get("level-tag").className, "tag fading");
  clock += 1;
  frames[0]();
  check("a second on, after a frame", nodes.get("level").textContent, "-30.00 dB \u2192 -\u221e dB");
  check("the page asks for the next frame itself", frames.length, 2);
  check(
    "the button that started it wears the fading colour, and only that one",
    fadingNodes(created).map((node) => node.dataset.key),
    ["out"],
  );
}

{
  const { context: page, nodes } = browser();
  page.render(fadeSnapshot("riding"));
  check("a ride renders as riding", nodes.get("level-tag").textContent, "riding");
  check("and is styled fading", nodes.get("level-tag").className, "tag fading");
}

{
  // The in-flight state ends on the box's word and on nothing else: not on the
  // page's own timer running out, which only says the page has not heard.
  let clock = 2_000_000;
  const { context: page, nodes, created, intervals } = browser({ now: () => clock * 1000 });
  const fading = fadeSnapshot();
  page.render(fading);
  clock += 10;
  intervals[0]();
  check("fading ends only on the box's word: still fading", nodes.get("level-tag").textContent, "fading");
  check("the button is still painted", fadingNodes(created).map((node) => node.dataset.key), ["out"]);
  check("and the readout says how late", nodes.get("level").textContent, "\u2192 -\u221e dB - 8s late");
  page.render(landedSnapshot(fading, 10));
  check("the landed snapshot ends it: tag", nodes.get("level-tag").textContent, "commanded");
  check("and styles it commanded", nodes.get("level-tag").className, "tag commanded");
  check("no button is painted", fadingNodes(created).length, 0);
  check("and the settled readout is back", nodes.get("level").textContent, "-\u221e dB - 0s ago");
}

{
  const { context: page, nodes } = browser({ now: STILL });
  page.render(fadeSnapshot("releasing", 2.5));
  check(
    "within the grace the readout points at the destination without a sweep",
    nodes.get("level").textContent,
    "\u2192 -\u221e dB",
  );
  check("and does not say late", nodes.get("level").textContent.includes("late"), false);
}

{
  // A link that drops mid-fade is exactly when the fade must not look finished.
  let clock = 2_000_000;
  const { context: page, nodes, created, intervals, sockets } = browser({ now: () => clock * 1000 });
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ keepalive: true, stale_after: 37.5 }) });
  sockets[0].onmessage({ data: JSON.stringify(fadeSnapshot()) });
  clock += 1;
  sockets[0].close();
  clock += 5;
  intervals[0]();
  check("a link drop mid-fade: the banner says lost", nodes.get("link").className, "lost");
  check("the tag still says fading", nodes.get("level-tag").textContent, "fading");
  check("the button is still painted", fadingNodes(created).map((node) => node.dataset.key), ["out"]);
  check("and the readout says late", nodes.get("level").textContent.includes("late"), true);
}

{
  // Half-open: the socket never closes and nothing arrives.
  let clock = 2_000_000;
  const { nodes, created, intervals, sockets } = browser({ now: () => clock * 1000 });
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ keepalive: true, stale_after: 37.5 }) });
  sockets[0].onmessage({ data: JSON.stringify(fadeSnapshot()) });
  clock += 37.5 + 5;
  intervals[0]();
  check("a half-open link mid-fade: the banner says stale", nodes.get("link").className, "stale");
  check("the tag still says fading", nodes.get("level-tag").textContent, "fading");
  check("the button is still painted", fadingNodes(created).map((node) => node.dataset.key), ["out"]);
  check("and the readout says late", nodes.get("level").textContent.includes("late"), true);
}

{
  const { context: page, nodes, created } = browser({ now: STILL });
  const fading = fadeSnapshot();
  page.render(fading);
  const open = structuredClone(SNAPSHOTS["open-recording"]);
  open.at = fading.at + 1;
  page.render(open);
  check("a snap back to open mid-fade ends the animation: tag", nodes.get("level-tag").textContent, "commanded");
  check("and no button is painted", fadingNodes(created).length, 0);
}

{
  const { context: page, created } = browser({ now: STILL });
  page.render(fadeSnapshot());
  const riding = fadeSnapshot("riding");
  riding.at += 1;
  riding.fader.move.seq = 2;
  riding.fader.move.started_at = riding.at;
  page.render(riding);
  check(
    "a new move replaces the old one",
    fadingNodes(created).map((node) => node.dataset.key),
    ["up-slow"],
  );
}

{
  const { context: page, nodes } = browser({ now: STILL });
  page.render(fadeSnapshot("releasing", 1.0));
  check("a page that connects mid-fade joins the sweep", nodes.get("level").textContent, "-30.00 dB \u2192 -\u221e dB");
}

{
  const { context: page, nodes } = browser({ now: STILL });
  const fading = fadeSnapshot();
  page.render(landedSnapshot(fading, 3));
  page.render(fading);
  check("a held-up response cannot restart a landed fade", nodes.get("level-tag").textContent, "commanded");
}

{
  const { context: page, nodes } = browser({ now: STILL });
  page.render(fadeSnapshot());
  check(
    "the in-flight tag is never styled commanded or confirmed",
    ["commanded", "confirmed"].some((word) => nodes.get("level-tag").className.includes(word)),
    false,
  );
}

{
  const { context: page, frames } = browser({ now: STILL });
  page.render(fadeSnapshot());
  page.render(fadeSnapshot());
  check("one animation frame at a time", frames.length, 1);
}
{
  const { context: page, frames } = browser({ now: STILL });
  page.render(structuredClone(SNAPSHOTS["standing-down"]));
  check("no frame is asked for when nothing is moving", frames.length, 0);
}
{
  const { context: page, frames } = browser({ now: STILL });
  page.render(fadeSnapshot("releasing", 5));
  check("none once past the end of the move", frames.length, 0);
}

{
  const { context: page, nodes } = browser({ now: STILL });
  const snap = snapshot({}, { level_known: false, moving: true, move: moveFrom("fade", 0.0, null, 2.0) });
  page.render(snap);
  check("a level that is not known never shows a move", nodes.get("level").textContent.startsWith("unknown"), true);
  check("and is tagged unknown", nodes.get("level-tag").textContent, "unknown");
}

// -- the page keeps its buttons and its text still (#51) ---------------------

{
  const { context: page, nodes, created } = browser({ now: STILL });
  page.render(structuredClone(SNAPSHOTS.prompt));
  for (const node of [...created, nodes.get("btn-record"), nodes.get("btn-prompt-accept")]) node.writes = 0;
  const again = structuredClone(SNAPSHOTS.prompt);
  again.at += 1;
  page.render(again);
  const written = [...created, nodes.get("btn-record"), nodes.get("btn-prompt-accept")]
    .filter((node) => node.writes > 0)
    .map((node) => node.dataset.key || node.id || node.tag);
  check("rendering an unchanged snapshot writes no button text", written, []);
}

{
  const { context: page, nodes, created } = browser({ now: STILL });
  page.render(structuredClone(SNAPSHOTS["open-recording"]));
  const before = created.length;
  const lean = structuredClone(SNAPSHOTS["open-recording"]);
  delete lean.buttons;
  lean.at += 1;
  page.render(lean);
  check("a snapshot without buttons keeps the ones on screen: nothing rebuilt", created.length, before);
  check("MAIN is still populated", nodes.get("tab-main").children.length > 0, true);
  check(
    "and an open span of a known event is not listed as an orphan",
    created.some((node) => node.dataset.orphan === "1"),
    false,
  );
}

{
  const { context: page, created } = browser({ now: STILL });
  const lean = structuredClone(SNAPSHOTS["standing-down"]);
  delete lean.buttons;
  let error = null;
  try {
    page.render(lean);
  } catch (caught) {
    error = String(caught);
  }
  check("a snapshot without buttons, before any, does not throw", error, null);
  check("and builds none", created.filter((node) => node.dataset.key !== undefined).length, 0);
}

{
  // A lean push can overtake the first full snapshot: the older full one still
  // has to give the page its buttons, without being painted over the newer.
  const { context: page, nodes, created } = browser({ now: STILL });
  const lean = structuredClone(SNAPSHOTS["standing-down"]);
  delete lean.buttons;
  lean.at += 1;
  page.render(lean);
  check("a lean snapshot first builds no vocabulary button", created.filter((node) => node.dataset.key !== undefined).length, 0);
  page.render(structuredClone(SNAPSHOTS["standing-down"]));
  check(
    "an older snapshot's buttons are still taken: the fader column is built",
    created.some((node) => node.dataset.key === "out"),
    true,
  );
  check("and the newer snapshot stays on screen", nodes.get("state").textContent, "STANDING DOWN");
}

{
  const { sockets } = browser();
  check("the page asks for its buttons once", sockets[0].url.endsWith("/ws?buttons=once"), true);
}

{
  // The wall clock stepped back an hour must not delay the stale banner.
  let wall = 2_000_000;
  let perf = 1000;
  const { nodes, intervals, sockets } = browser({ now: () => wall * 1000, perf: () => perf * 1000 });
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ keepalive: true, stale_after: 37.5 }) });
  wall -= 3600;
  perf += 50;
  intervals[0]();
  check("a wall clock stepped back does not delay the stale banner", nodes.get("link").className, "stale");
}

{
  // A monotonic clock that paused (a device asleep) must not hide a silence.
  let wall = 2_000_000;
  const perf = 1000;
  const { nodes, intervals, sockets } = browser({ now: () => wall * 1000, perf: () => perf * 1000 });
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ keepalive: true, stale_after: 37.5 }) });
  wall += 50;
  intervals[0]();
  check("a monotonic clock that paused does not hide a silence", nodes.get("link").className, "stale");
}

// Every state the box can produce: a grey record button always has a reason (#163).
for (const name of Object.keys(SNAPSHOTS)) {
  const { nodes } = renderedFixture(name);
  if (nodes.get("btn-record").disabled) {
    check(`${name}: a grey record button shows a reason`, nodes.get("rec-why").textContent !== "", true);
  }
}

{
  const { nodes } = renderedFixture("parked-unreported");
  check("parked and unreported: the button is live", nodes.get("btn-record").disabled, false);
  check("parked and unreported: not yet reported", nodes.get("rec-tag").textContent, "not yet reported");
  check("parked and unreported: no reason", nodes.get("rec-why").textContent, "");
}

// -- what code the box runs (#157) ----------------------------------------------

{
  const chip = browser().context.provenanceChip;
  const checkout = (changes) => ({
    source: "checkout",
    dirty: false,
    where: "main @ 0123456",
    error: null,
    ...changes,
  });
  check("no provenance says nothing", [chip(undefined), chip(null)], [null, null]);
  check("a clean checkout says nothing", chip(checkout({})), null);
  check("a clean checkout on another branch says nothing", chip(checkout({ where: "157-fix @ 0123456" })), null);
  check("not a checkout says nothing", chip({ source: "not-a-checkout", dirty: null, where: null, error: null }), null);
  check(
    "a dirty tree is a warning that names where",
    chip(checkout({ dirty: true, where: "157-fix @ 0123456 (worktree)" })),
    [
      "warn",
      "Unreviewed code running: uncommitted changes on 157-fix @ 0123456 (worktree). "
        + "The log names the commit, not the changes.",
    ],
  );
  check(
    "an unknown is a quiet note with its reason",
    chip({ source: "unknown", dirty: null, where: null, error: "git did not answer within 5s" }),
    ["note", "Running code not identified: git did not answer within 5s. It may include uncommitted changes."],
  );
}

{
  const { nodes } = renderedFixture("standing-down");
  check("standing down: a clean checkout shows no chip", nodes.get("provenance").className, "");
  check("standing down: and no text", nodes.get("provenance").textContent, "");
}

{
  const { nodes } = renderedFixture("faults");
  check("faults: a dirty tree shows the amber chip", nodes.get("provenance").className, "warn");
  check(
    "faults: and says so first",
    nodes.get("provenance").textContent.startsWith("Unreviewed code running"),
    true,
  );
}

{
  const { context, nodes } = browser();
  const snap = snapshot();
  delete snap.provenance;
  let error = null;
  try {
    context.render(snap);
  } catch (caught) {
    error = String(caught);
  }
  check("a snapshot with no provenance does not throw", error, null);
  check("and the rest of the page still renders", nodes.get("state").textContent, "STANDING DOWN");
}

{
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS.faults));
  const writes = nodes.get("provenance").writes;
  context.render(structuredClone(SNAPSHOTS.faults));
  check("the same chip is not rewritten", nodes.get("provenance").writes, writes);
}

{
  const { context, nodes } = browser();
  context.render(structuredClone(SNAPSHOTS.faults));
  nodes.get("provenance").onclick();
  check("a tap expands the chip", nodes.get("provenance").dataset.expanded, "1");
  nodes.get("provenance").onclick();
  check("and a second tap collapses it", nodes.get("provenance").dataset.expanded, "");
}

// -- the console's address (#73) --------------------------------------------------

{
  const chip = browser().context.consoleChip;
  const found = (reach, detail = null) => ({ reach, detail, checked_at: 1, trigger: "arm" });
  check("no console block says nothing", [chip(undefined), chip(null)], [null, null]);
  check("an answer is the healthy state and has no chip", chip(found("answered")), null);
  check("not checked says nothing", chip(found("not-checked")), null);
  check(
    "nothing there is a fault that says the operator has the fader",
    chip(found("nothing-there")),
    [
      "fault",
      "Nothing answered at the console address at the last check (no ARP reply). Fader moves may not be reaching anything. "
        + "Check the cable and the console IP; the operator has the fader.",
    ],
  );
  check(
    "no answer is a quiet note",
    chip(found("no-answer")),
    ["note", "Console did not answer ping. It may ignore ping. Fader moves are still sent, unconfirmed."],
  );
  check(
    "could not check carries its reason",
    chip(found("could-not-check", "ping is not installed or not on PATH")),
    ["note", "Could not check the console: ping is not installed or not on PATH."],
  );
}

{
  const { context, nodes } = browser();
  const snap = snapshot();
  delete snap.console;
  let error = null;
  try {
    context.render(snap);
  } catch (caught) {
    error = String(caught);
  }
  check("a snapshot with no console block does not throw", error, null);
  check("and shows no chip", nodes.get("console-reach").className, "");
}

{
  const { context, nodes } = browser();
  const snap = snapshot();
  snap.console = { reach: "nothing-there", detail: null, checked_at: 1, trigger: "startup" };
  context.render(snap);
  check("nothing there shows the red chip", nodes.get("console-reach").className, "fault");
  check(
    "and says so first",
    nodes.get("console-reach").textContent.startsWith("Nothing answered at the console address"),
    true,
  );
  nodes.get("console-reach").onclick();
  check("a tap expands the chip", nodes.get("console-reach").dataset.expanded, "1");
  snap.console = { reach: "answered", detail: null, checked_at: 2, trigger: "keepalive" };
  context.render(snap);
  check("an answer afterwards clears it", [nodes.get("console-reach").className, nodes.get("console-reach").textContent], ["", ""]);
}

// -- swiping between MAIN and MORE (#156) ----------------------------------

// The thresholds are read from the page, so a retune moves the tests with it;
// the three that are pinned by number below are the ones decided in the issue.
const swipeProbe = browser().context;
const swipeConst = (name) => runInContext(name, swipeProbe);
const SWIPE_MIN_PX = swipeConst("SWIPE_MIN_PX");
const SWIPE_DOMINANCE = swipeConst("SWIPE_DOMINANCE");
const SWIPE_MAX_MS = swipeConst("SWIPE_MAX_MS");
const SWIPE_NEXT = swipeConst("SWIPE_NEXT");
const SWIPE_PREVIOUS = swipeConst("SWIPE_PREVIOUS");
const NOT_A_SWIPE = swipeConst("NOT_A_SWIPE");

check("swipe: the distance is two minimum tap targets", SWIPE_MIN_PX, 88);
check("swipe: horizontal at least twice vertical", SWIPE_DOMINANCE, 2);
check("swipe: max duration", SWIPE_MAX_MS, 500);

const LEFTWARD = { dx: -2 * SWIPE_MIN_PX, dy: 0 };
const RIGHTWARD = { dx: 2 * SWIPE_MIN_PX, dy: 0 };
const VERTICAL = { dx: 0, dy: 3 * SWIPE_MIN_PX };
const NO_MOVE = { dx: 0, dy: 0 };
const ORIGIN = { x: 300, y: 300 };
const SWIPE_FAST_MS = SWIPE_MAX_MS / 2;

const swipeOf = (move, ms = SWIPE_FAST_MS) =>
  swipeProbe.swipeDirection({ ...ORIGIN, t: 0 }, { x: ORIGIN.x + move.dx, y: ORIGIN.y + move.dy, t: ms });

check("swipe: leftward is next", swipeOf(LEFTWARD), SWIPE_NEXT);
check("swipe: rightward is previous", swipeOf(RIGHTWARD), SWIPE_PREVIOUS);
check("swipe: one px short is not a swipe", swipeOf({ dx: -(SWIPE_MIN_PX - 1), dy: 0 }), NOT_A_SWIPE);
check("swipe: exactly the threshold is", swipeOf({ dx: -SWIPE_MIN_PX, dy: 0 }), SWIPE_NEXT);
check("swipe: too steep is not", swipeOf({ dx: -2 * SWIPE_MIN_PX, dy: 2 * SWIPE_MIN_PX }), NOT_A_SWIPE);
check(
  "swipe: on the dominance line is",
  swipeOf({ dx: -2 * SWIPE_MIN_PX, dy: (2 * SWIPE_MIN_PX) / SWIPE_DOMINANCE }),
  SWIPE_NEXT,
);
check("swipe: a vertical scroll is not", swipeOf(VERTICAL), NOT_A_SWIPE);
check("swipe: one ms too slow is not", swipeOf(LEFTWARD, SWIPE_MAX_MS + 1), NOT_A_SWIPE);
check("swipe: exactly the max duration is", swipeOf(LEFTWARD, SWIPE_MAX_MS), SWIPE_NEXT);
check("swipe: no movement is not", swipeOf(NO_MOVE), NOT_A_SWIPE);

check("swipe: MAIN next is MORE", swipeProbe.tabAfter("main", SWIPE_NEXT), "more");
check("swipe: MORE previous is MAIN", swipeProbe.tabAfter("more", SWIPE_PREVIOUS), "main");
check("swipe: past the last tab is nothing", swipeProbe.tabAfter("more", SWIPE_NEXT), null);
check("swipe: before the first is nothing", swipeProbe.tabAfter("main", SWIPE_PREVIOUS), null);
check("swipe: not a swipe goes nowhere", swipeProbe.tabAfter("main", NOT_A_SWIPE), null);

// A touch event as the page sees it. A touch is {identifier, clientX, clientY}.
function touchEvent(target, touches, changedTouches, timeStamp) {
  return {
    target,
    touches,
    changedTouches,
    timeStamp,
    cancelable: true,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

// The browser's bubbling: a touch event is targeted at the node the touch
// began on and runs every listener up the chain, then the document's.
function dispatch(world, target, type, event) {
  for (let node = target; node; node = node.parentNode) {
    for (const { fn } of node.listeners[type] ?? []) fn(event);
  }
  for (const { fn } of world.documentListeners[type] ?? []) fn(event);
  return event;
}

const touchAt = (point, identifier = 0) => ({ identifier, clientX: point.x, clientY: point.y });

// One finger down at ORIGIN at t=0 and up again `ms` later; returns the end
// event so a test can read `defaultPrevented`.
function gesture(world, target, move, ms = SWIPE_FAST_MS) {
  const from = touchAt(ORIGIN);
  const to = touchAt({ x: ORIGIN.x + move.dx, y: ORIGIN.y + move.dy });
  dispatch(world, target, "touchstart", touchEvent(target, [from], [from], 0));
  return dispatch(world, target, "touchend", touchEvent(target, [], [to], ms));
}

// The page on MAIN or MORE with the real vocabulary rendered.
function swipeWorld(tab = "main") {
  const world = browser();
  world.context.render(snapshot({}, {}, undefined, []));
  world.buttons = world.created.filter((node) => node.tag === "button");
  world.byKey = new Map(world.buttons.map((node) => [node.dataset.key, node]));
  if (tab === "more") world.nodes.get("tab-btn-more").onclick();
  world.posted.length = 0;
  return world;
}

const onMore = (world) => world.nodes.get("tab-more").style.display === "";
const onMain = (world) => world.nodes.get("tab-main").style.display === "";

{
  const world = swipeWorld();
  const end = gesture(world, world.byKey.get("touchdown"), LEFTWARD);
  check("swipe: a leftward swipe in the panel goes to MORE", onMore(world), true);
  check("swipe: MAIN is hidden", world.nodes.get("tab-main").style.display, "none");
  check("swipe: MORE's tab button is on", world.nodes.get("tab-btn-more").classList.contains("on"), true);
  check("swipe: the left panel is tinted", world.nodes.get("left").classList.contains("more"), true);
  check("swipe: the swiped-over button's tap is suppressed", end.defaultPrevented, true);
  check("swipe: and nothing was posted", world.posted.length, 0);
}

{
  const world = swipeWorld("more");
  const end = gesture(world, world.byKey.get("false-open"), RIGHTWARD);
  check("swipe: a rightward swipe on MORE comes back to MAIN", onMain(world), true);
  check("swipe: MAIN's tab button is on", world.nodes.get("tab-btn-main").classList.contains("on"), true);
  check("swipe: the return is also suppressed", end.defaultPrevented, true);
  check("swipe: and nothing was posted on the way back", world.posted.length, 0);
}

{
  const world = swipeWorld();
  const button = world.byKey.get("touchdown");
  const end = gesture(world, button, NO_MOVE);
  check("swipe: a tap in the panel leaves the tab alone", onMain(world), true);
  check("swipe: a tap in the panel is not suppressed", end.defaultPrevented, false);
  button.onclick();
  check("swipe: and still fires", world.posted.map((p) => p.path), ["/api/annotate"]);
}

{
  const world = swipeWorld();
  const end = gesture(world, world.byKey.get("touchdown"), VERTICAL);
  check("swipe: a vertical scroll in the panel changes nothing", onMain(world), true);
  check("swipe: a vertical scroll is not suppressed", end.defaultPrevented, false);
}

{
  // DONE WHEN: a gesture that starts in the fader column never changes tab.
  const lateral = { MAIN: LEFTWARD, MORE: RIGHTWARD };
  for (const key of FADER_KEYS) {
    for (const [tab, move] of Object.entries(lateral)) {
      const world = swipeWorld(tab.toLowerCase());
      const end = gesture(world, world.byKey.get(key), move);
      check(`swipe: ${key} on ${tab} keeps the tab`, tab === "MAIN" ? onMain(world) : onMore(world), true);
      check(`swipe: ${key} on ${tab} is not suppressed`, end.defaultPrevented, false);
    }
    const world = swipeWorld();
    const button = world.byKey.get(key);
    const end = gesture(world, button, NO_MOVE);
    check(`swipe: a tap on ${key} leaves the tab alone`, onMain(world), true);
    check(`swipe: a tap on ${key} is not suppressed`, end.defaultPrevented, false);
    button.onclick();
    check(`swipe: a tap on ${key} still fires`, world.posted.map((p) => p.body?.key), [key]);
  }
  for (const [tab, move] of Object.entries(lateral)) {
    const world = swipeWorld(tab.toLowerCase());
    const end = gesture(world, world.nodes.get("btn-close-now"), move);
    check(`swipe: Close now on ${tab} keeps the tab`, tab === "MAIN" ? onMain(world) : onMore(world), true);
    check(`swipe: Close now on ${tab} is not suppressed`, end.defaultPrevented, false);
  }
}

{
  const world = swipeWorld();
  const heard = (node) => Object.keys(node.listeners).filter((type) => type.startsWith("touch"));
  const column = ["fader-column", "fader-top", "fader-bottom", "readout", "belief", "btn-close-now", "btn-report-ready"];
  const nodesToCheck = column.map((id) => world.nodes.get(id)).concat(FADER_KEYS.map((k) => world.byKey.get(k)));
  check("swipe: nothing listens for touches in the fader column or above it", nodesToCheck.flatMap(heard), []);
  check(
    "swipe: nor on the document",
    Object.keys(world.documentListeners).filter((type) => type.startsWith("touch")),
    [],
  );
}

{
  const world = swipeWorld();
  const listeners = world.nodes.get("left").listeners;
  check("swipe: touchstart is passive", listeners.touchstart.map((l) => l.options.passive), [true]);
  check("swipe: touchend may prevent default", listeners.touchend.map((l) => l.options.passive), [false]);
  check("swipe: there is no touchmove listener", listeners.touchmove, undefined);
}

{
  // #108: a swipe is a tab change, so it cancels the confirmation like a tap.
  const world = swipeWorld("more");
  world.nodes.get("btn-handoff").onclick();
  check("swipe: the confirmation is open", world.nodes.get("handoff-confirm").style.display, "block");
  world.posted.length = 0;
  gesture(world, world.nodes.get("btn-handoff"), RIGHTWARD);
  check("swipe: leaving MORE closes the confirmation", world.nodes.get("handoff-confirm").style.display, "none");
  check("swipe: and sends nothing at all", world.posted.length, 0);
}

{
  const world = browser();
  world.context.render(structuredClone(SNAPSHOTS["prompt"]));
  world.nodes.get("tab-btn-more").onclick();
  world.nodes.get("btn-handoff").onclick();
  check("swipe: opening the confirmation hides the question", world.nodes.get("prompt-panel").style.display, "none");
  gesture(world, world.nodes.get("btn-handoff"), RIGHTWARD);
  check("swipe: leaving MORE brings the question back", world.nodes.get("prompt-panel").style.display, "block");
}

{
  const world = swipeWorld("more");
  world.nodes.get("btn-handoff").onclick();
  const end = gesture(world, world.nodes.get("btn-handoff"), LEFTWARD);
  check("swipe: past the last tab stays on MORE", onMore(world), true);
  check("swipe: past the last tab keeps the confirmation", world.nodes.get("handoff-confirm").style.display, "block");
  check("swipe: past the last tab still suppresses the tap", end.defaultPrevented, true);
}

{
  const world = swipeWorld("more");
  world.nodes.get("btn-handoff").onclick();
  gesture(world, world.byKey.get("up-drums"), RIGHTWARD);
  check("swipe: a fader-column gesture on MORE leaves the confirmation open", world.nodes.get("handoff-confirm").style.display, "block");
}

{
  // A second finger abandons the gesture.
  const world = swipeWorld();
  const target = world.byKey.get("touchdown");
  const [a, b] = [touchAt(ORIGIN, 0), touchAt({ x: ORIGIN.x + 20, y: ORIGIN.y }, 1)];
  dispatch(world, target, "touchstart", touchEvent(target, [a], [a], 0));
  dispatch(world, target, "touchstart", touchEvent(target, [a, b], [b], 1));
  const to = touchAt({ x: ORIGIN.x + LEFTWARD.dx, y: ORIGIN.y }, 0);
  const end = dispatch(world, target, "touchend", touchEvent(target, [b], [to], SWIPE_FAST_MS));
  check("swipe: a second finger abandons it", onMain(world), true);
  check("swipe: and the tap is not suppressed", end.defaultPrevented, false);
}

{
  const world = swipeWorld();
  const target = world.byKey.get("touchdown");
  const from = touchAt(ORIGIN);
  dispatch(world, target, "touchstart", touchEvent(target, [from], [from], 0));
  dispatch(world, target, "touchcancel", touchEvent(target, [], [from], 1));
  const to = touchAt({ x: ORIGIN.x + LEFTWARD.dx, y: ORIGIN.y });
  dispatch(world, target, "touchend", touchEvent(target, [], [to], SWIPE_FAST_MS));
  check("swipe: a cancelled touch is not a swipe", onMain(world), true);
}

// -- report -----------------------------------------------------------------

console.log(`${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 0 : 1);
