// The operator page's logic, tested against a stubbed browser.
//
// The same bargain as reaper/test_tacet_mirror.lua: the logic here is ours and
// gets tested, while whether a real browser behaves the way this stub pretends
// is what the first-run checklist in docs/handoff.md is for.
//
//     node tests/test_app_js.mjs      (or: make test-js)

import { readFileSync } from "node:fs";
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

function browser() {
  const nodes = new Map();
  const created = [];
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
      // The page only ever asks for "#buttons button". The stub ignores the
      // selector and answers with every button it has been asked to make.
      querySelectorAll: () => created.filter((node) => node.tag === "button"),
    },
    location: { protocol: "http:", host: "box:8080" },
    setTimeout() {},
    // The page boots on load. Neither of these may resolve, or the tests would
    // be racing the page's own first render.
    fetch: () => new Promise(() => {}),
    WebSocket: class {
      constructor(url) {
        this.url = url;
      }
      close() {}
    },
  });
  runInContext(SOURCE, context);
  return { context, nodes, created };
}

function snapshot(recording = {}, fader = {}, buttons = [], openSpans = []) {
  return {
    state: "standing-down",
    why: "Standing down.",
    refusal: null,
    detector_enabled: false,
    fader: { commanded: -32768, db: null, confirmed: false, healthy: true, error: null, ...fader },
    recording: {
      known: false,
      recording: false,
      position: null,
      confirmed: false,
      liveness: "unknown",
      healthy: true,
      ...recording,
    },
    buttons,
    open_spans: openSpans,
  };
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
  live: ["confirmed", "tag confirmed"],
  quiet: ["confirmed (idle)", "tag confirmed"],
  lost: ["LINK LOST", "tag unknown"],
  unknown: ["no feedback", "tag unknown"],
};

for (const [liveness, [label, className]] of Object.entries(TAGS)) {
  const nodes = rendered({ liveness, known: true, confirmed: true, position: 1.5 });
  check(`${liveness}: tag label`, nodes.get("rec-tag").textContent, label);
  check(`${liveness}: tag class`, nodes.get("rec-tag").className, className);
}

check(
  "every liveness gets its own label",
  new Set(Object.values(TAGS).map(([label]) => label)).size,
  Object.keys(TAGS).length,
);

for (const liveness of Object.keys(TAGS)) {
  const nodes = rendered({ liveness, known: true, confirmed: true });
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

// -- the fader --------------------------------------------------------------

// A closed fader is -inf, which JSON cannot carry, so it arrives as null. This
// also pins the escaping: the script is inlined into a Python string, and a
// doubled backslash here would put a literal "∞" on the screen.
check("a closed fader reads as minus infinity", rendered().get("level").textContent, "-∞ dB");
check(
  "an open fader reads in dB",
  rendered({}, { db: -12.5 }).get("level").textContent,
  "-12.50 dB",
);
check(
  "an unreachable console says so",
  rendered({}, { healthy: false, error: "no route to host" }).get("fader-error").textContent,
  "Console unreachable: no route to host",
);

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

function buttons(openSpans = []) {
  const { context, created } = browser();
  context.render(snapshot({}, {}, BUTTONS, openSpans));
  const found = new Map();
  for (const node of created.filter((n) => n.tag === "button")) found.set(node.dataset.key, node);
  return found;
}

check("a closed span says it starts", buttons().get("q1").textContent, "Q1 (start)");
check("an open span says it ends", buttons(["q1-2"]).get("q1").textContent, "Q1 (end)");
check(
  "an instant carries no start or end",
  buttons().get("band-enters-stands").textContent,
  "Band enters stands",
);
check(
  "an instant is never marked open, whatever spans are running",
  buttons(["q1-2"]).get("band-enters-stands").classList.contains("on"),
  false,
);
check("an open span is highlighted", buttons(["q1-2"]).get("q1").classList.contains("on"), true);
check("a closed span is not highlighted", buttons().get("q1").classList.contains("on"), false);
check(
  "one open span does not open another",
  buttons(["q1-2"]).get("timeout-injury").textContent,
  "Timeout: injury (start)",
);
check(
  "spans are marked as spans for styling",
  buttons().get("q1").dataset.kind,
  "span",
);

// -- fader buttons --------------------------------------------------------

// These both move the fader and say why. They sit in the same grid as the
// annotation buttons, which is tapped without looking, so they have to be
// marked for the styling that tells them apart - and put where they are
// reached in a hurry.
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
check("the fader category comes first", headings()[0], "FDR");
check("the rest keep the vocabulary order", headings().slice(1), ["BAND", "GAME"]);
check(
  "an acting button is still an instant, not a toggle",
  byKey.get("up-whistle").textContent,
  "Up on whistle",
);

// -- report -----------------------------------------------------------------

console.log(`${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 0 : 1);
