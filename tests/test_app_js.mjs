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
  return {
    id,
    textContent: "",
    className: "",
    innerHTML: "",
    style: {},
    dataset: {},
    onclick: null,
    classList: { toggle() {} },
    appendChild() {},
  };
}

function browser() {
  const nodes = new Map();
  const context = createContext({
    console,
    document: {
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, element(id));
        return nodes.get(id);
      },
      createElement: (tag) => element(tag),
      querySelectorAll: () => [],
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
  return { context, nodes };
}

function snapshot(recording = {}, fader = {}) {
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
    buttons: [],
    open_spans: [],
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

// -- report -----------------------------------------------------------------

console.log(`${checks} checks, ${failures} failures`);
process.exit(failures === 0 ? 0 : 1);
