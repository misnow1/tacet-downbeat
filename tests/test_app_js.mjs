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

function browser() {
  const nodes = new Map();
  const created = [];
  const sockets = [];
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
      // The page re-takes its wake lock when the tab comes back. Nothing here
      // ever fires it; what is tested is the branch it calls into.
      addEventListener() {},
      hidden: false,
    },
    location: { protocol: "http:", host: "box:8080" },
    // No wake lock, which is the deployed case: the API needs a secure context
    // and the page is served over plain HTTP.
    navigator: {},
    setTimeout() {},
    // The banner is repainted on a tick so a silence is noticed without a
    // message arriving to notice it. Never fired here; paintLink is called
    // directly instead.
    setInterval() {},
    // The page boots on load. Neither of these may resolve, or the tests would
    // be racing the page's own first render.
    fetch: () => new Promise(() => {}),
    WebSocket: class {
      constructor(url) {
        this.url = url;
        sockets.push(this);
      }
      close() {
        if (this.onclose) this.onclose();
      }
    },
  });
  runInContext(SOURCE, context);
  return { context, nodes, created, sockets };
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
      can_start: true,
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
  context.render(snapshot({}, {}, BUTTONS, ["q1-2"]));
  const buttons = created.filter((node) => node.tag === "button");
  const q1 = buttons.find((node) => node.dataset.key === "q1");
  check("an opened span still updates", q1.textContent, "Q1 (end)");
  check("and is still highlighted", q1.classList.contains("on"), true);
  check("without anything being rebuilt to do it", buttons.length, BUTTONS.length);
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

// -- the link banner, wired up ----------------------------------------------

// Driven through the socket callbacks the page actually installs, so the
// plumbing is covered as well as the decision above.
const keepaliveFrame = { data: JSON.stringify({ keepalive: true, stale_after: STALE_AFTER }) };
const snapshotFrame = { data: JSON.stringify(snapshot()) };

{
  const { nodes } = browser();
  check("a page that has not connected yet does not claim it has",
        nodes.get("link").className, "lost");
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
}
{
  // A keepalive carries no state. Rendering it would blank the whole page.
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(snapshotFrame);
  sockets[0].onmessage(keepaliveFrame);
  check("a keepalive does not blank the state", nodes.get("state").textContent, "STANDING DOWN");
  check("and does not blank the fader", nodes.get("level").textContent, "-\u221E dB");
}
{
  // Everything the old socket established goes with it, the threshold
  // included: the next one proves itself from scratch.
  const { nodes, sockets } = browser();
  sockets[0].onopen();
  sockets[0].onmessage(keepaliveFrame);
  sockets[0].onclose();
  check("a dropped socket says so at once", nodes.get("link").className, "lost");
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
