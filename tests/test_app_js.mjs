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
  return {
    id,
    textContent: "",
    className: "",
    innerHTML: "",
    style: {},
    dataset: {},
    onclick: null,
    disabled: false,
    classList: {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      contains: (name) => classes.has(name),
    },
    appendChild() {},
  };
}

function browser(options = {}) {
  const nodes = new Map();
  const created = [];
  const sockets = [];
  const posted = [];
  const context = createContext({
    console,
    document: {
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, element(id));
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
      // The page re-takes its wake lock when the tab comes back. Nothing here
      // ever fires it; what is tested is the branch it calls into.
      addEventListener() {},
      hidden: false,
    },
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
    // The banner is repainted on a tick so a silence is noticed without a
    // message arriving to notice it. Never fired here; paintLink is called
    // directly instead.
    setInterval() {},
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
  return { context, nodes, created, sockets, posted };
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

// -- the fader --------------------------------------------------------------

// A closed fader is -inf, which JSON cannot carry, so it arrives as null. This
// also pins the escaping: the script is inlined into a Python string, and a
// doubled backslash here would put a literal "∞" on the screen.
// The box's boot snapshot does not know where the fader is (#107), so every
// check about a number on the screen says the level is known, out loud: the
// fixture stays honest about the cold boot and the checks are about the number.
check("a closed fader reads as minus infinity", rendered({}, { level_known: true }).get("level").textContent, "-∞ dB");
check(
  "an open fader reads in dB",
  rendered({}, { level_known: true, db: -12.5 }).get("level").textContent,
  "-12.50 dB",
);
// A close takes two seconds, so the number on its own reads as a fader that is
// not moving. The destination is shown beside it while the move is in flight.
check(
  "a fade in flight shows where it is heading",
  rendered({}, { level_known: true, commanded: -300, db: -3.0, target: -32768, target_db: null, moving: true })
    .get("level")
    .textContent,
  "-3.00 dB \u2192 -\u221E dB",
);
check(
  "and nothing is pointed at when the fader is settled",
  rendered({}, { level_known: true, db: -3.0 }).get("level").textContent,
  "-3.00 dB",
);
check(
  "a fade that has arrived does not point at itself",
  rendered({}, { level_known: true, commanded: -32768, db: null, target: -32768, target_db: null, moving: true })
    .get("level")
    .textContent,
  "-\u221E dB",
);
check(
  "an open in flight points at its destination too",
  rendered({}, { level_known: true, commanded: -6000, db: -60.0, target: 0, target_db: 0.0, moving: true })
    .get("level")
    .textContent,
  "-60.00 dB \u2192 0.00 dB",
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

check(
  "the readout carries the age of the last command, known level or not",
  rendered({}, { level_known: true, db: 0.0, age: 76 * 60 }).get("level").textContent,
  "0.00 dB - 76 min ago",
);
check(
  "no age is shown when nothing has ever been sent",
  rendered({}, { level_known: true, age: null }).get("level").textContent,
  "-∞ dB",
);

// -- the box does not know where the fader is: the level renders as unknown (#107) --

check(
  "a level that is not trusted reads as unknown rather than a confident number",
  rendered({}, { level_known: false, db: 0.0 }).get("level").textContent,
  "unknown",
);
check(
  "the age still shows so a stale belief cannot look current",
  rendered({}, { level_known: false, db: 0.0, age: 76 * 60 }).get("level").textContent,
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
  nodes.get("btn-handoff").onclick();
  check("tapping it opens the prompt rather than acting at once", nodes.get("handoff-confirm").style.display, "block");
  check("opening the prompt sends nothing", posted.length, 0);

  nodes.get("btn-handoff-yes").onclick();
  check("yes hands off", posted[0].path, "/api/handoff");
  check("and closes the prompt", nodes.get("handoff-confirm").style.display, "none");
  check("and returns to MAIN", nodes.get("tab-main").style.display, "");
}

{
  // The negative answer is `still-mine` (#12): a pure log entry, never a
  // handoff.
  const { context, nodes, posted } = browser();
  context.render(levelSnapshot(true));
  posted.length = 0;
  nodes.get("btn-handoff").onclick();
  nodes.get("btn-handoff-no").onclick();
  check("no logs still-mine instead", posted[0].path, "/api/still-mine");
  check("and never hands off", posted.some((p) => p.path === "/api/handoff"), false);
  check("and closes the prompt too", nodes.get("handoff-confirm").style.display, "none");
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
  for (const key of ["band-enters-stands", "q1", "touchdown", "note"]) {
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
  ["faults", "open-recording", "prompt", "releasing", "standing-down"],
);

function renderedFixture(name) {
  const { context, nodes, created } = browser();
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
  check("standing down: the whole vocabulary is on screen", button("q1").textContent, "Q1 (start)");
}

{
  const { nodes, button } = renderedFixture("open-recording");
  check("open: unity", nodes.get("level").textContent, "0.00 dB - 0s ago");
  check("open: rolling", nodes.get("rec").textContent, "ROLLING");
  check("open: confirmed", nodes.get("rec-tag").textContent, "confirmed");
  check("open: where", nodes.get("rec-pos").textContent, "at 0:12:34.500");
  check("open: the record button will not be pressed twice", nodes.get("btn-record").disabled, true);
  check("open: the open quarter offers to end", button("q2").textContent, "Q2 (end)");
}

{
  const { nodes } = renderedFixture("releasing");
  check(
    "releasing: where it is and where it is going",
    nodes.get("level").textContent,
    "0.00 dB \u2192 -\u221e dB - 0s ago",
  );
}

{
  const { nodes } = renderedFixture("faults");
  check("faults: the console", nodes.get("fader-error").textContent, "Console unreachable: no route to host");
  check("faults: Reaper", nodes.get("rec-tag").textContent, "LINK LOST");
  check("faults: the log", nodes.get("saving").className, "fault");
  check("faults: the refusal is shown", nodes.get("refusal").style.display, "block");
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
// "Timeout: unspecified" read (end) and tapping it closed the *home* timeout,
// and Halftime could never open during the exodus. Real vocabulary keys, since
// those are the pairs that collide.
const PREFIXED = [
  { key: "halftime", label: "Halftime", category: "GAME", kind: "span" },
  { key: "halftime-exodus", label: "Halftime exodus", category: "GAME", kind: "span" },
  { key: "timeout-home", label: "Timeout: home", category: "GAME", kind: "span" },
  { key: "timeout", label: "Timeout: unspecified", category: "GAME", kind: "span" },
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
    "Timeout: unspecified (start)",
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
  { key: "q1", label: "Q1", category: "GAME", kind: "span" },
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
  byKey.get("q1").dataset.action,
  undefined,
);
// #5: the fader buttons move to their own pinned column, which carries no
// heading of its own - there is nothing to sort against any more. q1 lands in
// MAIN's "Game" group (Scoring and Timeouts have nothing to show and get no
// heading of their own); band-enters-stands lands in MORE under its category.
check("the fader column carries no heading", headings().includes("FDR"), false);
check("q1 lands in MAIN's Game group", headings()[0], "Game");
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
  return created.filter((node) => node.tag === "button");
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
  const buttons = created.filter((node) => node.tag === "button");
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
  context.render(snapshot({}, {}, undefined, [{ span_id: "q1-9", event: "q1", label: "Q1" }]));
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
  // A tap in MORE is navigation, not a mode change: it returns to MAIN once
  // answered for, without switching tabs having rebuilt anything under it.
  const { context, nodes, created } = browser();
  context.render(SNAPSHOTS["standing-down"]);
  nodes.get("tab-btn-more").onclick();
  const before = created.filter((node) => node.tag === "button").length;
  const falseOpen = created.find((node) => node.dataset.key === "false-open");
  falseOpen.onclick();
  check("MORE returns to MAIN once its tap is answered for", nodes.get("tab-main").style.display, "");
  check("nothing was rebuilt to do it", created.filter((node) => node.tag === "button").length, before);
}

{
  // Note is the one exception: its prompt() is still open when this runs, so
  // switching tabs out from under it would close a dialog the operator has
  // not finished with.
  const { context, nodes, created } = browser({ prompt: () => "left tackle is limping" });
  context.render(SNAPSHOTS["standing-down"]);
  nodes.get("tab-btn-more").onclick();
  const note = created.find((node) => node.dataset.key === "note");
  note.onclick();
  check("note does not return to MAIN", nodes.get("tab-more").style.display, "");
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

// -- report -----------------------------------------------------------------

console.log(`${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 0 : 1);
