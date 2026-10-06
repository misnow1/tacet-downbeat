const $ = id => document.getElementById(id);
let snapshot = null;

// -- taps -------------------------------------------------------------------

// How long a tap waits for the box before the page says it did not get there.
// A request can still arrive after this; the page cannot unsend it.
const TAP_TIMEOUT_MS = 10000;

// Page-side, so they survive every snapshot render: the box never hears about a
// tap that did not reach it, and so can never say so itself (#11).
const TAP_FAILED = "Tap did not reach the box";
const TAP_FAILED_ADVICE = ". Check the fader, and tap again if it did not happen."
  + " A fader tap that gets there late is not acted on.";
const TAP_UNTIMED = "Sent before the page had timed its link to the box, "
  + "so how late that tap arrived is not known.";

// Every tap says when it happened by the page's clock, and how that clock
// relates to the box's, so the box can log when it was tapped and not only when
// it arrived. On stadium wifi those are seconds apart (#11).
function tapStamp() {
  const estimate = clockEstimate(clockSamples);
  return {
    at: now(),
    offset: estimate ? estimate.offset : null,
    uncertainty: estimate ? estimate.uncertainty : null,
  };
}

// Shown on the tapped button from the tap until the box answers or the request
// fails, so a tap is visibly received and nobody taps again to find out. A
// count, not a flag: a second tap on the same button must not clear the first.
function sending(node, on) {
  if (!node) return;
  const count = Math.max(0, Number(node.dataset.sending || 0) + (on ? 1 : -1));
  node.dataset.sending = String(count);
  node.classList.toggle("sending", count > 0);
}

function showTapNote(kind, text) {
  const node = $("tap");
  node.className = text ? kind : "";
  node.textContent = text || "";
}

function tapFailure(error) {
  const why = error && error.name === "AbortError"
    ? "no answer in " + TAP_TIMEOUT_MS / 1000 + "s"
    : (error && error.message) || "the request failed";
  return TAP_FAILED + " (" + why + ")" + TAP_FAILED_ADVICE;
}

async function post(path, body, node) {
  const payload = {...(body || {}), tap: tapStamp()};
  const options = {method: "POST", headers: {"Content-Type": "application/json"},
                   body: JSON.stringify(payload)};
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  if (controller) options.signal = controller.signal;
  const timer = controller ? setTimeout(() => controller.abort(), TAP_TIMEOUT_MS) : null;
  sending(node, true);
  let response;
  let result;
  try {
    response = await fetch(path, options);
    result = await response.json().catch(() => null);
  } catch (error) {
    // A wifi drop, a box that has gone, a timeout. Never swallowed: the tap
    // would look like it did nothing, and the next snapshot would not say
    // otherwise, because the box never knew.
    showTapNote("failed", tapFailure(error));
    return null;
  } finally {
    clearTimeout(timer);
    sending(node, false);
  }
  // It reached the box, which clears a failure however the box answered.
  showTapNote("untimed", payload.tap.offset === null ? TAP_UNTIMED : "");
  if (!response.ok) { showRefusal((result && result.error) || response.statusText); return null; }
  render(snapshotIn(result));
  return result;
}

// The snapshot in a response. The command routes answer with the snapshot
// itself and the others wrap it as `state` - and a snapshot has a `state` of
// its own, a string, which `result.state || result` used to hand to `render`,
// which threw. Nobody saw: the rejection went nowhere and a push repainted.
function snapshotIn(result) {
  if (!result) return null;
  return result.state !== null && typeof result.state === "object" ? result.state : result;
}

function showRefusal(text, loud = false) {
  const node = $("refusal");
  node.textContent = text || "";
  node.style.display = text ? "block" : "none";
  node.className = text && loud ? "loud" : "";
  // Repeated in the fader column's readout gap (#5): the header carrying
  // #refusal is on the far side of the screen from the thumb that needs to
  // read it.
  const col = $("col-refusal");
  col.textContent = text || "";
  col.style.display = text ? "block" : "none";
}

// -- layout: the pinned fader column, and MAIN/MORE beside it (#5) ----------
//
// Held in landscape, right thumb. Both lists below are explicit and ordered
// rather than derived from category or vocabulary order: a button's position
// is a reviewed, hallway-tested fact, not something that should shift because
// a new key happened to be declared before it in annotations.py. A future
// fader reason is data on an existing button, not a new button - the column
// holds exactly these six, and a seventh will not fit.
//
// Split around the readout gap, which is its own fixed slot in the HTML
// (level, target, and the refusal duplicate) rather than a seventh entry here.
const FADER_COLUMN_TOP = ["up-whistle", "up-drums", "up-slow", "up-ready"];
const FADER_COLUMN_BOTTOM = ["score-reversed", "out"];
const FADER_COLUMN = [...FADER_COLUMN_TOP, ...FADER_COLUMN_BOTTOM];

// Commands that ramp from what the box believes the level is. Disabled in
// place while that belief is not trusted (#107) - never hidden, never moved,
// and never relabelled: `up-slow`'s label already fills its one permitted
// line, so an explanation on the button would wrap it out of a pinned height.
// The column says it once instead, in the readout gap.
const RAMPING_ACTIONS = new Set(["open-slow", "ready", "release"]);
const RAMPING_BLOCKED = "Greyed: they ramp from an unknown level";

const SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
  + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
const SVG_CLOSE = "</svg>";
const svg = (body) => SVG_OPEN + body + SVG_CLOSE;

// Icons are inline SVG because the page fetches nothing. They are drawn in
// currentColor so each follows its button's hue, and #154's fading colour once
// that lands. They carry no xmlns, and no whitespace sits between tags, so a
// button's textContent stays exactly its label.
//
// One per fader button and one per scoring type (#155): each is a different
// thing to do, so each is a different shape to find under glare or at night.
const KEY_ICONS = {
  "up-whistle": svg('<circle cx="9" cy="15" r="6"/><path d="M13.5 11H22v4h-7"/><circle cx="9" cy="15" r="1.6" fill="currentColor" stroke="none"/><path d="M4 4l2 2.5M9 2.5V6M14 4l-2 2.5"/>'),
  "up-drums": svg('<ellipse cx="12" cy="12" rx="9" ry="3.5"/><path d="M3 12v5.5c0 1.9 4 3.5 9 3.5s9-1.6 9-3.5V12"/><path d="M5 2.5l6 7.5M19 2.5l-6 7.5"/>'),
  "up-slow": svg('<path d="M3 20C10 20 12 7 20 6"/><path d="M16.5 3.6L20 6l-2.8 3.4"/>'),
  "up-ready": svg('<path d="M3 5h18" stroke-dasharray="2.5 3"/><path d="M3 20h3l4-9h11"/>'),
  "out": svg('<path d="M3 4h4l10 15h4"/><path d="M3 21h18" stroke-opacity=".45"/>'),
  "score-reversed": svg('<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>'),
  "touchdown": svg('<path d="M4 20C4 11 11 4 20 4c0 9-7 16-16 16z"/><path d="M8.5 15.5l7-7M10 12l2 2M12 10l2 2"/>'),
  "field-goal": svg('<path d="M12 22v-9M5 13h14M5 13V3M19 13V3"/>'),
  "safety": svg('<path d="M2.5 12h6M5.5 9v6"/><path d="M12.5 8.5a3.5 3.5 0 1 1 6.3 2.1L12.5 18.5h7.5"/>'),
  "first-down": svg('<path d="M6 22V2"/><path d="M6 3h13v8H6"/><path d="M12 5.5l1.5-1v5"/>'),
  "defensive-stop": svg('<path d="M12 2.5l8 3v6c0 5-3.5 8.6-8 10-4.5-1.4-8-5-8-10v-6z"/><path d="M8.5 12h7"/>'),
};
// One shared by every timeout: they are one kind of thing, told apart by label.
const TONE_ICONS = {
  timeout: svg('<circle cx="12" cy="13.5" r="7.5"/><path d="M12 13.5V9.5M9.5 2.5h5M12 2.5V6M18.5 6.5L20 5"/>'),
};

// MAIN: the order is the Game 3 debrief's (#155). The tone belongs to this
// reviewed placement rather than to Category, because GAME covers all three
// groups; null means grey.
const MAIN_GROUPS = [
  ["Scoring", "score", ["touchdown", "field-goal", "safety", "first-down", "defensive-stop"]],
  ["Timeouts", "timeout", ["timeout", "timeout-home", "timeout-away", "timeout-media", "timeout-injury"]],
  ["Game", null, ["halftime-exodus", "last-two-minutes"]],
];

// What the fader column and MAIN together account for, so MORE is "whatever
// is left" rather than a third list that can drift out of step with the other
// two - see the completeness check in tests/test_app_js.mjs.
const PLACED_ELSEWHERE = new Set([...FADER_COLUMN, ...MAIN_GROUPS.flatMap(([, , keys]) => keys)]);

// The span each vocabulary button's words live in, so the paint loop can relabel
// a span "(start)"/"(end)" without wiping the icon beside it (#155).
const LABELS = new WeakMap();
function labelOf(node) { return LABELS.get(node) || node; }

function buildButtonNode(item, tone = null) {
  const node = document.createElement("button");
  const icon = KEY_ICONS[item.key] || (tone && TONE_ICONS[tone]) || null;
  if (icon) {
    const glyph = document.createElement("span");
    glyph.className = "ico";
    glyph.innerHTML = icon;
    node.appendChild(glyph);
  }
  const label = document.createElement("span");
  label.className = "lbl";
  label.textContent = buttonLabel(item.label, item.kind, false);
  node.appendChild(label);
  LABELS.set(node, label);
  node.dataset.key = item.key;
  node.dataset.kind = item.kind;
  node.dataset.label = item.label;
  if (item.action) node.dataset.action = item.action;
  if (tone) node.dataset.tone = tone;
  node.onclick = () => activate(item, node);
  return node;
}

function buildGrid(items, tone = null) {
  const grid = document.createElement("div");
  grid.className = "grid";
  if (tone) grid.dataset.tone = tone;
  for (const item of items) grid.appendChild(buildButtonNode(item, tone));
  return grid;
}

function appendHeadedGrid(host, heading, items, tone = null) {
  if (!items.length) return;
  const h = document.createElement("h2");
  h.textContent = heading;
  host.appendChild(h);
  host.appendChild(buildGrid(items, tone));
}

// Spans an older log left open whose event is no longer a button (#14). The
// key is never deleted, so the box can still end them - but without this there
// is nothing on the page to end them with, and they run to the end of the
// timeline as regions.
function orphanSpans(snap) {
  const offered = new Set(((snap && snap.buttons) || []).map(button => button.key));
  return (((snap && snap.open_spans) || [])).filter(span => !offered.has(span.event));
}

function renderFaderHalf(hostId, keys, byKey) {
  const host = $(hostId);
  host.innerHTML = "";
  for (const key of keys) {
    const item = byKey.get(key);
    // A box running older code might not offer every key yet; skip rather
    // than throw, so the rest of the column still works.
    if (item) host.appendChild(buildButtonNode(item));
  }
}

function renderFaderColumn(buttons) {
  const byKey = new Map(buttons.map((item) => [item.key, item]));
  renderFaderHalf("fader-top", FADER_COLUMN_TOP, byKey);
  renderFaderHalf("fader-bottom", FADER_COLUMN_BOTTOM, byKey);
}

function renderMainTab(buttons) {
  const byKey = new Map(buttons.map((item) => [item.key, item]));
  const host = $("tab-main");
  host.innerHTML = "";
  for (const [heading, tone, keys] of MAIN_GROUPS) {
    appendHeadedGrid(host, heading, keys.map((key) => byKey.get(key)).filter(Boolean), tone);
  }
}

function renderMoreTab(buttons, orphans) {
  const rest = buttons.filter((item) => !PLACED_ELSEWHERE.has(item.key));
  const byCategory = {};
  for (const item of rest) (byCategory[item.category] ||= []).push(item);
  const host = $("tab-more-vocabulary");
  host.innerHTML = "";
  // Vocabulary order, the same way the fader column and MAIN's own groups are
  // reviewed rather than sorted - MORE is reached less often and never mid-play,
  // so it does not need MAIN's hand-picked order, only a stable one.
  for (const [category, items] of Object.entries(byCategory)) {
    appendHeadedGrid(host, category, items);
  }
  if (!orphans.length) return;
  const heading = document.createElement("h2");
  heading.textContent = "OPEN FROM AN EARLIER RUN";
  host.appendChild(heading);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const span of orphans) {
    const node = document.createElement("button");
    node.textContent = "End: " + span.label;
    node.dataset.kind = "span";
    // Not keyed by event: this ends one span by id, and its label is fixed.
    node.dataset.orphan = "1";
    node.onclick = () => post("/api/span/end", {span_id: span.span_id}, node);
    grid.appendChild(node);
  }
  host.appendChild(grid);
}

// What the three lists currently on screen were built from. Comparing this
// rather than setting a flag is what keeps the rebuild honest: the vocabulary
// is not fixed for the life of a page, because a box that restarts mid-game
// can serve a different ann.BUTTONS and the page reconnects to it without
// reloading. One signature for all three: they are built together from the
// same list, and switching MAIN/MORE never touches any of them (see
// `paintTabs`), so nothing here needs to know which tab is showing.
let renderedButtons = null;

// Only the parts a rebuild would change. Deliberately not the whole snapshot -
// this has to survive a playhead moving a thousand times an hour without
// noticing.
function buttonSignature(buttons) {
  return JSON.stringify(
    buttons.map(item => [item.key, item.label, item.category, item.kind, item.action || ""]));
}

function renderButtons(buttons, orphans) {
  renderFaderColumn(buttons);
  renderMainTab(buttons);
  renderMoreTab(buttons, orphans);
}

// -- the standing target level (#9) ---------------------------------------------
//
// Two things in a snapshot are called a target, and they are not the same.
// `snapshot.target` is the STANDING setting: the level the next open goes to and
// the READY hold is measured from. It is what this section shows (the chip in
// the strip) and changes (the segments in MORE > Target level). A tap stores
// the value always, and rides the fader there while it is up (#128). When it
// only stored while the fader is up the box says so and why
// (`snapshot.target.stored`), the page words that under the segments, and the
// box clears it (a later tap, a fader move, a state change). While the fader is
// closed the box sends null: storing is what the operator expects there, so
// there is no note (#153). The segments are greyed in place while the level is
// unknown and the fader is up, because a ride from an unknown level would be a
// guess (#107), and the selected one wears the fading colour until the box says
// the ride landed (#154's landed-only rule; `fadingKey` ends only on a snapshot
// without `fader.move`). `snapshot.fader.target` is where a move
// already in flight is heading, null when nothing is moving; the fader readout
// below draws that as the arrow in "-10.00 dB -> 0.00 dB" (from `fader.move`
// since #154, which carries the same destination). A move in flight can
// head somewhere other than the standing target: a ride that began before a
// change keeps going to its old destination, and Ready's own ride goes to its
// hold level (target - hold_below_db), never to the target. While that lasts
// the two disagree, and the chip says "(next open)" rather than let the readout
// and the chip seem to contradict each other. That is deliberate for Ready even
// when nothing was changed. A fade's destination is -inf, which is not a
// competing target, so it adds nothing to the chip.
//
// Taps here leave the operator on MORE (#109) like every other tap in it. The
// selected segment is painted from the snapshot on every render and never
// optimistically: a refused or lost tap must not look like a change. The tap's
// own feedback is the `sending` outline `post` puts on the node.

// The sentences under the segments when a tap stored and did not move (#153).
// The box sends only the reason. ASCII only: `-inf`, not the infinity sign.
const NOT_MOVED = "The fader did not move.";
// No `closed` entry: the box never sends one, because a tap while the fader is
// closed has no note (#153).
const STORED_COPY = {
  open: NOT_MOVED,
  ready: NOT_MOVED,
  releasing: "The fade carries on to -inf.",
  unknown: "The fader did not move: the box does not know where it is.",
};
// Reasons whose note wants attention, and whose chip says (next open).
const STORED_ATTENTION = new Set(["open", "ready", "releasing", "unknown"]);

// The `by` the box gives a move a target tap started (the log's `target-set`).
const TARGET_SET_KEY = "target-set";
// The states where a target tap rides, so an unknown level blocks it.
const RETARGET_RIDES = new Set(["open", "ready"]);
const TARGET_BLOCKED = "Greyed: a new target would ride from an unknown level. Close it now, or open, first.";

// Pure: whether the segments are greyed. Storing is always allowed, so only the
// states where a tap would ride, with the level not known.
function targetBlocked(stateName, fader) {
  return !fader.level_known && RETARGET_RIDES.has(stateName);
}

// Pure: the note under the segments, `{text, attention}`. A reason this page
// does not know (a newer box) reads as not-moved, which is true of every
// store-only case and the safe side, as MOVE_TAG_FALLBACK is.
function storedNote(stored) {
  if (!stored) return {text: "", attention: false};
  if (stored.because === "unchanged") {
    return {text: "Already the target: " + String(stored.db) + " dB. Nothing changed.", attention: false};
  }
  const copy = STORED_COPY[stored.because] || NOT_MOVED;
  return {text: "Stored: " + String(stored.db) + " dB on the next open. " + copy, attention: true};
}

// Pure: the chip's text. `nextOpenOnly` is true while a ride is heading
// somewhere other than the standing target.
function targetChipText(db, nextOpenOnly = false) {
  return "target " + String(db) + " dB" + (nextOpenOnly ? " (next open)" : "");
}

// What the segments on screen were built from, compared before any rebuild for
// the reason `renderedButtons` is: a tap landing on a node that was just
// detached fires no click, and this control is one the operator taps while
// looking at the field.
let renderedPresets = null;
// Kept here, not read back from the host: the stub cannot list a host's
// children, and this saves a DOM query on every render.
let presetNodes = [];

function buildPresetNode(db) {
  const node = document.createElement("button");
  node.textContent = String(db) + " dB";
  // Deliberately no `dataset.key`: the vocabulary's own completeness check
  // counts buttons by key, and this is not one of them.
  node.dataset.preset = String(db);
  node.onclick = () => post("/api/target", {db}, node);
  return node;
}

function renderTargetControl(target, fader, stateName) {
  const signature = JSON.stringify(target.presets_db);
  if (signature !== renderedPresets) {
    const host = $("target-control");
    host.innerHTML = "";
    presetNodes = target.presets_db.map(buildPresetNode);
    for (const node of presetNodes) host.appendChild(node);
    renderedPresets = signature;
  }
  // Painted here, never in the rebuild signature, so greying in place never
  // tears the segments down under a thumb (the rule #107's column follows).
  const blocked = targetBlocked(stateName, fader);
  const riding = fadingKey(fader) === TARGET_SET_KEY;
  for (const node of presetNodes) {
    const selected = Number(node.dataset.preset) === target.db;
    node.classList.toggle("selected", selected);
    node.classList.toggle("fading", selected && riding);
    setDisabled(node, blocked);
  }
}

function paintTarget(target, fader, stateName) {
  // A ride to somewhere other than the standing target, and not a fade.
  const ridingElsewhere = fader.target !== null && fader.target_db !== null && fader.target !== target.level;
  const stored = target.stored || null;
  const nextOpenOnly = ridingElsewhere || (stored !== null && STORED_ATTENTION.has(stored.because));
  const chip = $("target-level");
  chip.textContent = targetChipText(target.db, nextOpenOnly);
  chip.classList.toggle("off-default", target.db !== target.default_db);
  renderTargetControl(target, fader, stateName);
  const note = storedNote(stored);
  const blocked = targetBlocked(stateName, fader);
  const noteNode = $("target-note");
  setText(noteNode, [note.text, blocked ? TARGET_BLOCKED : ""].filter(Boolean).join(" "));
  noteNode.className = note.attention || blocked ? "attention" : "";
}

// -- MAIN / MORE ---------------------------------------------------------
//
// The fader column and the status strip are visible in both; only the two
// vocabulary panels swap. The tab moves only when the operator taps a tab
// button (#109): a tap inside MORE leaves them on MORE, so a run of taps there
// does not bounce them back to MAIN between each one. Navigation, not a mode
// change, so nothing here is announced or logged.
let activeTab = "main";

function paintTabs() {
  $("tab-main").style.display = activeTab === "main" ? "" : "none";
  $("tab-more").style.display = activeTab === "more" ? "" : "none";
  $("tab-btn-main").classList.toggle("on", activeTab === "main");
  $("tab-btn-more").classList.toggle("on", activeTab === "more");
  // #5: the left panel is tinted while MORE is showing, so which tab is
  // active reads at a glance without having to read either tab button.
  $("left").classList.toggle("more", activeTab === "more");
}

// The one entry point that moves the tab. Switching tabs cancels an
// unanswered hand-off confirmation (#108): the confirmation lives inside MORE,
// so leaving would otherwise hide it while `handoffPromptOpen` stayed true,
// and an invisible confirmation would go on suppressing the box's own arm /
// stand-down question (`paintPrompt`). Nothing has been sent at that point, so
// cancelling changes nothing on the console, and re-opening is one tap on the
// button right there in MORE. `paintSlot` is a function declaration below, so
// it is callable from here whatever the order they are defined in;
// `handoffPromptOpen` is a `let` declared further down, so selectTab must not
// be called during script evaluation (it would hit the temporal dead zone),
// only from handlers.
function selectTab(tab) {
  activeTab = tab;
  handoffPromptOpen = false;
  paintTabs();
  paintSlot();
}

// No call sites since #109; retained deliberately, not an oversight. It is
// the one place that moves the tab for us, should anything need to again -
// now a thin wrapper, so it cancels a hand-off confirmation like the buttons.
function returnToMain() {
  selectTab("main");
}

$("tab-btn-main").onclick = () => selectTab("main");
$("tab-btn-more").onclick = () => selectTab("more");

// Span buttons toggle: the first tap opens the region, the second closes it.
// Instants fire once. The highlight alone cannot carry that difference - it
// looks the same as an instant that was just tapped - so the button says which
// tap it is about to be.
function buttonLabel(label, kind, open) {
  if (kind !== "span") return label;
  return label + (open ? " (end)" : " (start)");
}

// The open span a button owns, if any. Matched on the event the box names, never
// by parsing the id: ids are `<key>-<seq>` and keys contain hyphens, so a prefix
// match let "timeout" claim a running "timeout-home" and close it.
function openSpan(snap, key) {
  return ((snap && snap.open_spans) || []).find(span => span.event === key);
}

function activate(item, node) {
  if (item.kind !== "span") {
    const data = item.key === "note"
      ? {text: prompt("Note") || ""} : undefined;
    if (item.key === "note" && !data.text) return;
    post("/api/annotate", {key: item.key, data}, node);
  } else {
    const open = openSpan(snapshot, item.key);
    if (open) post("/api/span/end", {span_id: open.span_id}, node);
    else post("/api/span/start", {key: item.key}, node);
  }
}

// Reaper's /time is a float of seconds and says nothing about how Reaper is
// displaying its own timeline, so the formatting is entirely ours. Rounding to
// whole milliseconds first keeps 59.9996 from rendering as :60.000.
function timecode(seconds) {
  const sign = seconds < 0 ? "-" : "";
  const total = Math.round(Math.abs(seconds) * 1000);
  const ms = total % 1000;
  const whole = (total - ms) / 1000;
  const h = Math.floor(whole / 3600);
  const m = Math.floor(whole / 60) % 60;
  const s = whole % 60;
  const pad = (v, n) => String(v).padStart(n, "0");
  return sign + h + ":" + pad(m, 2) + ":" + pad(s, 2) + "." + pad(ms, 3);
}

// How much to trust the recording line above it - which is about the value, not
// about the link. Those come apart: Reaper announces /record only when it
// changes, so a box that started after the last change has a perfectly live
// link and still no idea whether Reaper is rolling. Tagging that "confirmed"
// because the link was up read as a contradiction, and painted a reassuring
// colour over something unknown.
//
// Reaper streams meters whenever its audio device runs, parked or rolling, and
// /time only while the transport moves, so a parked Reaper is not a fault:
// "quiet" is a believed reading from a stopped Reaper, while "lost" is no /time
// where the stream should have been, and only that one is (#163).
// A fader level for the screen. Null is -inf - a closed fader, not a missing
// reading - because JSON cannot carry -inf and a blank there would read as a
// link problem rather than a closed DCA.
function faderDb(db) {
  if (db === null) return "-\u221e dB";
  // A sweep passing through unity can round to a negative zero.
  const text = db.toFixed(2);
  return (text === "-0.00" ? "0.00" : text) + " dB";
}

// The age of the last thing actually sent to the console - a snap, a ramp
// step, or a close - in the box's own words (#12). Shown whether or not the
// level is known: game 2 spent 76 minutes
// with StageMix on the DCA and the page reading a confident number that had
// not been true since 14:15, and the age is what would have said so. Seconds
// close up, so a page that just tapped does not say "0 min ago"; minutes
// further out, since nobody needs second-level precision on a number that
// might be an hour old.
function ageText(seconds) {
  if (seconds === null) return "";
  const whole = Math.round(seconds);
  if (whole < 60) return whole + "s ago";
  return Math.round(seconds / 60) + " min ago";
}

// Seconds since the box last sent, with no clock estimate in it (#147). The box
// stamps both `sentAt` and the snapshot's `at` on one monotonic clock, so the
// gap between them is exact; `since` is how long ago the snapshot arrived by
// this page's own clock (`secondsSince`), also exact. Only transit time is
// uncounted. Nothing here converts between the two clocks, so it keeps
// climbing on a link that has never timed a round trip - the degraded case the
// #12 age is for. Null when nothing has been sent, or the snapshot has no
// usable `at` or arrival time. Never negative, whatever the clocks do.
function commandAge(sentAt, at, since) {
  if (sentAt === null || !Number.isFinite(at) || since === null) return null;
  return Math.max(0, (at - sentAt) + since);
}

// -- a move in flight (#154) ---------------------------------------------------
//
// The box describes a fade or a ride once, in `fader.move`, and holds the rest
// of the fader block still until the move ends. The page draws the sweep from
// that description and counts the time itself. Nothing arrives per ramp step.

// The tag while a move is in flight, by the move's kind. Never `commanded` or
// `confirmed`: a number still changing is neither, and the two must stay
// tellable apart from it. A kind this page does not know (a newer box) reads
// as the fallback rather than as nothing.
const MOVE_TAGS = {fade: "fading", ride: "riding"};
const MOVE_TAG_FALLBACK = "moving";

// How far past its expected end a move may run before the readout says it is
// late. Inside this the readout points at the destination without a sweep; a
// snapshot in flight on the wifi is normal for that long.
const MOVE_LATE_SECONDS = 1.0;
const MOVE_LATE_WORD = "late";

// Mirror of `dm7.ramp_steps` in dB, with null for -inf, so the page draws what
// the console is being told. `tests/fixtures/move-curves.json` holds the two
// together: the box writes the steps and the description, and the page's
// checks hold this to within one console unit of every step. A close to -inf
// ramps to the floor and steps the rest; a rise from -inf starts at the floor;
// a taper only shapes a rise that crosses its knee. Display only: the console
// is never told anything from here.
function moveDbAt(move, elapsed) {
  const from = move.from_db;
  const to = move.to_db;
  if (!(elapsed > 0)) return from;
  if (elapsed >= move.seconds || from === to) return to;
  let start = from;
  let end = to;
  if (to === null) {
    if (from <= move.floor_db) return from;
    end = move.floor_db;
  } else if (from === null) {
    if (to <= move.floor_db) return from;
    start = move.floor_db;
  }
  const fraction = elapsed / move.seconds;
  const knee = move.knee;
  if (knee && start < knee.db && knee.db < end) {
    if (fraction <= knee.fraction) return start + (knee.db - start) * (fraction / knee.fraction);
    return knee.db + (end - knee.db) * ((fraction - knee.fraction) / (1 - knee.fraction));
  }
  return start + (end - start) * fraction;
}

// Seconds into the move, with no clock estimate in it: the same pattern as
// `commandAge`. The box stamps `started_at` and the snapshot's `at` on one
// clock; `since` is how long ago the snapshot arrived by this page's clock.
// The only error is the transit time of the snapshot that carried the
// description, and it runs the animation that much behind the console. Null
// when either end is unusable.
function moveElapsed(move, at, since) {
  if (!Number.isFinite(move.started_at) || !Number.isFinite(at) || since === null) return null;
  return (at - move.started_at) + since;
}

// The fader readout and its tag, from the snapshot's fader block. Pure.
//
// While a move is described the line sweeps toward its destination, and the
// age is not shown: the move is the news. Past the move's expected end the
// sweep stops and the line points at the destination; past that by
// MOVE_LATE_SECONDS it says how late. It never goes back to a settled number
// on its own: the in-flight state ends only when a snapshot arrives without the
// move, because that is the box saying the move landed. The page's timer
// running out says only that the page has not heard (fail visible, #154).
function levelReadout(fader, at, since) {
  const age = ageText(commandAge(fader.sent_at, at, since));
  if (!fader.level_known) {
    // The box does not know where the fader is (#107): at every cold boot, and
    // again after a hand-off to StageMix (#12). `commanded` is only a belief
    // until an absolute command says otherwise, and the number must say so
    // rather than sit there looking confident - the game 2 hazard this whole
    // feature exists for.
    return {text: age ? "unknown - " + age : "unknown", tag: "unknown", tagClass: "tag unknown"};
  }
  const move = fader.move;
  if (!move) {
    const value = faderDb(fader.db);
    return {text: age ? value + " - " + age : value, tag: "commanded", tagClass: "tag commanded"};
  }
  const elapsed = moveElapsed(move, at, since);
  const over = elapsed === null ? 0 : elapsed - move.seconds;
  const destination = faderDb(move.to_db);
  let text;
  if (over < 0) {
    text = faderDb(moveDbAt(move, elapsed === null ? 0 : elapsed)) + " \u2192 " + destination;
  } else {
    text = "\u2192 " + destination;
    if (over >= MOVE_LATE_SECONDS) text += " - " + Math.round(over) + "s " + MOVE_LATE_WORD;
  }
  return {text, tag: MOVE_TAGS[move.kind] || MOVE_TAG_FALLBACK, tagClass: "tag fading"};
}

// The vocabulary key of the button that started the move in flight, or null:
// a bare command (the Release route, stand-down's fade) names none, and so
// colours no button, while the tag still says it.
function fadingKey(fader) {
  return fader.level_known && fader.move ? fader.move.by : null;
}

// The fader readout and the tag beside it. Painted from the snapshot on screen
// both when a snapshot arrives, on every animation frame while a move is
// running, and on the page's own timer, because the age and the lateness are
// the page's to count (#147, #154). The box going quiet is exactly when nothing
// arrives, and the number must keep moving on the screen anyway.
function paintLevel() {
  if (!snapshot) return;
  const since = secondsSince(snapshotArrivedAt, pageStamp());
  const readout = levelReadout(snapshot.fader, snapshot.at, since);
  $("level").textContent = readout.text;
  const tag = $("level-tag");
  tag.textContent = readout.tag;
  tag.className = readout.tagClass;
}

// At most one frame requested at a time, and only while a move is described
// and not yet past its end. Past the end the 1 s tick carries the late counter.
let framePending = false;
function scheduleMoveFrame() {
  if (framePending || !snapshot || typeof requestAnimationFrame !== "function") return;
  const fader = snapshot.fader;
  const move = fader.level_known ? fader.move : null;
  if (!move) return;
  const elapsed = moveElapsed(move, snapshot.at, secondsSince(snapshotArrivedAt, pageStamp()));
  if (elapsed === null || elapsed >= move.seconds) return;
  framePending = true;
  requestAnimationFrame(() => {
    framePending = false;
    paintLevel();
    scheduleMoveFrame();
  });
}

// Said when the box sends no reason for a grey record button, as one that
// predates the field would not (#163). Still a reason: never a bare grey button.
const RECORD_REASON_MISSING = "The box did not say why. "
  + "Check Reaper, and start the recording there if it is not rolling.";

// Why the record button is grey, in the box's own words (#163). A grey button
// with no reason was games 2 and 3: the operator could not tell a refusal they
// should act on from one they should leave alone.
function recordReason(rec) {
  if (rec.can_start) return "";
  return typeof rec.refusal === "string" && rec.refusal ? rec.refusal : RECORD_REASON_MISSING;
}

// Said under the Recording panel when a send to Reaper failed, as the console's
// is under the readout (#172). Quoted in docs/troubleshooting.md.
const RECORDER_ERROR_PREFIX = "Reaper unreachable: ";

function recordingTag(liveness, known) {
  if (liveness === "lost") return ["unknown", "LINK LOST"];
  if (liveness === "unknown") return ["unknown", "no feedback"];
  if (!known) return ["unknown", "not yet reported"];
  return ["confirmed", liveness === "quiet" ? "confirmed (idle)" : "confirmed"];
}

// #157: the chip's head words. Quoted in serve.py's banner and the docs; tests hold them together.
const PROVENANCE_DIRTY = "Unreviewed code running";
const PROVENANCE_UNKNOWN = "Running code not identified";

// What code the box runs (#157), or null when there is nothing to say: a clean
// checkout on any branch, not a checkout, or a box from before #157 that sends
// no `provenance`.
function provenanceChip(p) {
  if (!p) return null;
  if (p.source === "unknown") {
    return ["note", PROVENANCE_UNKNOWN + ": " + p.error + ". It may include uncommitted changes."];
  }
  if (p.source !== "checkout" || p.dirty !== true) return null;
  return ["warn", PROVENANCE_DIRTY + ": uncommitted changes on " + p.where
    + ". The log names the commit, not the changes."];
}

// #73: the console chip's head words. Quoted in the docs; tests hold them together.
const CONSOLE_NOTHING_THERE = "Nothing answered at the console address at the last check";
const CONSOLE_NO_ANSWER = "Console did not answer ping";
const CONSOLE_NOT_CHECKED = "Could not check the console";

// What the last ping at the console's address found (#73), or null when there
// is nothing to say. No chip is the healthy state: an answer, no check yet, or
// a box from before #73 that sends no `console`. It is presence at an address
// and never the DM7, the port or a delivered move, so it never says "confirmed".
function consoleChip(c) {
  if (!c) return null;
  if (c.reach === "nothing-there") {
    return ["fault", CONSOLE_NOTHING_THERE + " (no ARP reply). Fader moves may not be reaching anything. "
      + "Check the cable and the console IP; the operator has the fader."];
  }
  if (c.reach === "no-answer") {
    return ["note", CONSOLE_NO_ANSWER + ". It may ignore ping. Fader moves are still sent, unconfirmed."];
  }
  if (c.reach === "could-not-check") {
    return ["note", CONSOLE_NOT_CHECKED + ": " + c.detail + "."];
  }
  return null;
}

// Whether annotations are reaching the disk. A full disk leaves the fader
// buttons working and nothing else on the page looking wrong, while every
// annotation from then on is lost - the half nothing can recover afterwards
// (design.md 5.6). So it stays up for as long as it is true, and a log that
// recovered still says what it cost. The log outranks the Reaper markers,
// which are rebuilt from it; a fault now outranks entries lost earlier.
function savingBanner(log, mirror) {
  if (!log.healthy) {
    return ["fault", "Log not saving: " + log.error
      + ". The fader buttons still work, but annotations are being lost."];
  }
  if (!mirror.healthy) {
    return ["warn", "Reaper markers not updating: " + mirror.error
      + ". The log is still saving; markers can be rebuilt from it."];
  }
  if (log.failures > 0) {
    const entries = log.failures === 1 ? " log entry was" : " log entries were";
    return ["note", log.failures + entries + " not saved earlier. Saving again now."];
  }
  return null;
}

// When the snapshot on screen was taken, by the box's clock. A POST response
// held up on the wifi used to paint an older state over pushes that had
// overtaken it, and nothing corrected it until something else changed (#11).
let renderedAt = null;

// When, by the page's own clock, the snapshot on screen arrived (a `pageStamp`).
// The readout's age and a move's progress count from it (#147, #154), so they
// need no estimate of the box's clock.
let snapshotArrivedAt = null;

// The vocabulary the page last had. The box sends it once per socket (#51) and
// leaves it out of later pushes; a snapshot without one is read against this.
let lastButtons = null;

// Pure: `next` with the buttons it lacks filled in from `buttons`. A snapshot
// that carries its own is taken as it is; with none to fall back on, it stays
// without, and the page builds no buttons until it is told some.
function withButtons(next, buttons) {
  if (next.buttons || !buttons) return next;
  return {...next, buttons};
}

// Writes only when the text differs. A paint that rewrites a button's label
// every snapshot churns the DOM for nothing, and on a button under a thumb it
// is the churn the rebuild signature above exists to avoid (#51).
function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// The same for `disabled`: assigned only when it changes.
function setDisabled(node, disabled) {
  if (node.disabled !== disabled) node.disabled = disabled;
}

function isOlder(next, at) {
  return at !== null && typeof next.at === "number" && next.at < at;
}

function render(received) {
  if (!received) return;
  // Taken before the staleness check: a full snapshot that a lean push
  // overtook is old for everything but its buttons, which the page may not yet
  // have, and without them a fresh page has an empty fader column (#51).
  if (received.buttons) lastButtons = received.buttons;
  if (isOlder(received, renderedAt)) {
    if (renderedButtons === null && snapshot) paintButtons(withButtons(snapshot, lastButtons));
    return;
  }
  const next = withButtons(received, lastButtons);
  if (typeof next.at === "number") renderedAt = next.at;
  snapshot = next;
  snapshotArrivedAt = pageStamp();
  $("state").textContent = next.state.replace(/-/g, " ").toUpperCase();
  $("why").textContent = next.why;
  // #19: ARMED / STOOD DOWN and since when, on the box's own clock. Guarded
  // like renderFaderHalf and orphanSpans below: a box rolled back to before
  // #19 sends no `duty` at all, and that must not throw and stop the whole
  // render - only leave the chip showing whatever it last did.
  if (next.duty) $("duty").textContent = dutyChip(next.duty, boxOffset(next));
  // #9: guarded the same way. A box that predates the standing target sends no
  // `target`, and that must not throw and stop the rest of the render.
  if (next.target) paintTarget(next.target, next.fader, next.state);
  // A fader tap that arrived too late was not done. Nothing moved, so nothing
  // else on the page changes to say so, and the operator has to decide again
  // (#16).
  showRefusal(next.refusal, Boolean(next.stale_tap));
  // #157: guarded like duty and target - a box from before it sends no field.
  const code = provenanceChip(next.provenance);
  $("provenance").className = code ? code[0] : "";
  setText($("provenance"), code ? code[1] : "");
  // #73: guarded the same way. Never in the fader column, which has no room.
  const reachChip = consoleChip(next.console);
  $("console-reach").className = reachChip ? reachChip[0] : "";
  setText($("console-reach"), reachChip ? reachChip[1] : "");
  const saving = savingBanner(next.log, next.mirror);
  $("saving").className = saving ? saving[0] : "";
  $("saving").textContent = saving ? saving[1] : "";

  const fader = next.fader;
  paintLevel();
  scheduleMoveFrame();
  $("fader-error").textContent = fader.healthy ? "" : "Console unreachable: " + fader.error;
  // Said once, in the column, rather than on each button: see RAMPING_ACTIONS.
  // Suppressed while a refusal is showing - that line says the same thing in
  // more words, and the readout gap has room for one of them, not both.
  const blocked = !fader.level_known && !next.refusal;
  $("col-unknown").textContent = blocked ? RAMPING_BLOCKED : "";
  $("col-unknown").style.display = blocked ? "block" : "none";

  // The two belief controls are always on the page and never hidden (#107):
  // nothing here may appear, disappear or move on a belief change, only a
  // button that has nothing to say stops being tappable, in place. Close now is
  // absolute and always right, so it is never disabled. "It's at the ready
  // level" is a report about a belief the box does not yet have, so it only
  // means something while the level is unknown.
  setDisabled($("btn-report-ready"), fader.level_known);
  // The hand-off button is never disabled and never relabelled here (#118).
  // A hand-off while the level already reads unknown is a real tap with a real
  // log entry - the box may have restarted while StageMix had the DCA, and
  // that entry is the only record of it - so the label is the page's own
  // markup, like the two belief buttons below it.
  // Whatever this page's prompt was asking is answered when the level goes
  // from known to unknown - another browser's "yes", or this one's own tap
  // landing. The edge, not the state: since #118 "unknown" on its own no
  // longer means the question has been answered.
  if (handoffPromptOpen && lastLevelKnown === true && !fader.level_known) {
    handoffPromptOpen = false;
  }
  lastLevelKnown = fader.level_known;
  // Unconditional, so the #19 question also repaints on every ordinary
  // snapshot - a level or kind change can change its copy without its seq
  // changing at all - and not only on the known-to-unknown edge just above.
  paintSlot();

  const rec = next.recording;
  const [recClass, recLabel] = recordingTag(rec.liveness, rec.known);
  $("rec").textContent = !rec.known ? "unknown" : (rec.recording ? "ROLLING" : "stopped");
  const tag = $("rec-tag");
  tag.textContent = recLabel;
  tag.className = "tag " + recClass;
  // /record is a toggle at Reaper's end, so once it is rolling the button must
  // not invite a second press. The box refuses it anyway; this is the
  // affordance, not the guard.
  const record = $("btn-record");
  setDisabled(record, !rec.can_start);
  setText(record, rec.known && rec.recording ? "Recording" : "Start recording");
  setText($("rec-why"), recordReason(rec));
  setText($("rec-error"), rec.healthy === false ? RECORDER_ERROR_PREFIX + rec.error : "");

  $("rec-pos").textContent =
    rec.confirmed && rec.position !== null ? "at " + timecode(rec.position) : "";

  paintButtons(next);
}

function paintButtons(next) {
  const fader = next.fader;
  // Rebuilt only when the vocabulary actually changes. renderButtons tears the
  // grid down and recreates it, and a tap landing during that is lost - the node
  // under the finger is detached between touchstart and touchend, so no click
  // fires. This is the grid tapped without looking by someone watching a field,
  // and it carries the fader buttons, so a dropped tap there is a missed open.
  const orphans = orphanSpans(next);
  if (next.buttons) {
    const signature = buttonSignature(next.buttons) + JSON.stringify(orphans.map(span => span.span_id));
    if (signature !== renderedButtons) {
      renderButtons(next.buttons, orphans);
      renderedButtons = signature;
    }
  }
  // The button that started a move in flight, painted until the box says the
  // move has landed (#154). By key, so only a vocabulary button is ever painted.
  const fading = fadingKey(fader);
  for (const node of document.querySelectorAll(
    "#fader-top button, #fader-bottom button, #tab-main button, #tab-more-vocabulary button"
  )) {
    // An orphan's label says what it does and never changes, and a target
    // segment is painted by `paintTarget`, not from the vocabulary (#9).
    if (node.dataset.orphan || node.dataset.preset !== undefined) continue;
    const open = openSpan(next, node.dataset.key) !== undefined;
    node.classList.toggle("on", open);
    node.classList.toggle("fading", fading !== null && node.dataset.key === fading);
    setText(labelOf(node), buttonLabel(node.dataset.label, node.dataset.kind, open));
    // In this paint loop and not in buildButtonNode, so that the belief stays
    // out of the rebuild signature above: a belief change re-enables these in
    // place, and never tears the grid down under a thumb.
    setDisabled(node, !fader.level_known && RAMPING_ACTIONS.has(node.dataset.action));
  }
}

// The bare OPEN / FADE OUT pair came off the page (#5): the reason buttons in
// the fader column do the same move and also say why. The routes stay, for
// anything that wants to drive the box without the vocabulary - verify_dm7,
// say, or the detector once Phase 2 is declared.
for (const [id, path] of [["btn-arm", "/api/arm"], ["btn-stand-down", "/api/stand-down"],
                          ["btn-record", "/api/record"]]) {
  const node = $(id);
  node.onclick = () => post(path, undefined, node);
}

// -- handing off to StageMix (#12) --------------------------------------------
//
// A mode change asks rather than firing on one tap (CLAUDE.md principle 4:
// "announce, don't surprise"). The confirmation renders under the button that
// opens it, in MORE (#108). "No" is `still-mine`: a pure log entry, answered
// from here rather than a button in a grid, that changes nothing on the box.
let handoffPromptOpen = false;
// null until the first snapshot is painted, so nothing is treated as a
// known-to-unknown transition on load (#118).
let lastLevelKnown = null;

function paintHandoffPrompt() {
  $("handoff-confirm").style.display = handoffPromptOpen ? "block" : "none";
}

// -- the arm / stand-down question (#19) -------------------------------------
//
// The box raises this from `band-exits-stands`, the start of
// `halftime-exodus`, or `band-enters-stands` (tacet.prompts); the page only
// renders what it is told and answers with one tap. It has the #prompt slot to
// itself, but is still held back while a hand-off confirmation is open,
// through `paintSlot`, which the hand-off handlers and `selectTab` call
// instead of `paintHandoffPrompt` directly - see `paintSlot` for why.

// How long an answer is ignored after the question appears on screen, so a
// tap already in flight toward some other button cannot land on a question
// that has just popped into the same slot (CLAUDE.md principle 4: announce,
// don't surprise - the corollary is that the first instant after the
// announcement is not yet a considered answer).
const PROMPT_GUARD_MS = 700;

// The box carries no copy of its own (`tacet.prompts` is deliberately clock-
// and word-free); this is the page's translation, keyed on `prompt.kind`. The
// unknown-level stand-down sentence is confirmed wording, not a guess: the
// send really is a no-op at an unknown level (design.md 5.3), and saying so
// is what keeps the operator from expecting a fade that will not happen.
const PROMPT_COPY = {
  "stand-down": {
    known: "Band left the stands. Stand down? Fades the band out if it is up.",
    unknown: "Band left the stands. Stand down? Moves nothing while the fader position is unknown.",
    accept: "Stand down",
  },
  "arm": {
    known: "Band in the stands. Arm? Moves nothing.",
    unknown: "Band in the stands. Arm? Moves nothing.",
    accept: "Arm",
  },
};

// Pure: what to show for a question of this `kind`, or null for a kind this
// page does not recognise - a newer box asking a question this page predates.
// Rendering nothing is the safe failure; a half-built panel is not.
function promptCopy(kind, levelKnown) {
  const copy = PROMPT_COPY[kind];
  if (!copy) return null;
  return {question: levelKnown ? copy.known : copy.unknown, accept: copy.accept};
}

// Pure: whether an answer tapped `at` is acted on, given the question was
// shown `shownAt`. `shownAt` is null before any question has been shown.
function answerable(shownAt, at) {
  return shownAt !== null && (at - shownAt) * 1000 >= PROMPT_GUARD_MS;
}

// The open question's seq, and when this page put it on screen - page-side
// state, the same shape as `handoffPromptOpen` and `lastLevelKnown` above.
// Cleared whenever there is nothing to show, so a stale seq can never answer
// a question that is not the one on screen.
let promptSeq = null;
let promptShownAt = null;

// The hand-off confirmation and the question exclude each other. The operator
// opened the confirmation deliberately and is mid-decision on a two-step
// confirmation, and a freshly guarded question must not compete for that
// attention. `paintPrompt` treats a currently-open hand-off exactly like
// "nothing to ask": the guard clears, so the question reappears, freshly
// armed, the moment the hand-off confirmation is answered or cancelled (a tab
// switch, `selectTab`) and this runs again.
function paintSlot() {
  paintHandoffPrompt();
  paintPrompt();
}

function paintPrompt() {
  const prompt = handoffPromptOpen ? null : (snapshot && snapshot.prompt);
  const copy = prompt ? promptCopy(prompt.kind, snapshot.fader.level_known) : null;
  if (!copy) {
    promptSeq = null;
    promptShownAt = null;
    $("prompt-panel").style.display = "none";
    return;
  }
  if (prompt.seq !== promptSeq) {
    promptSeq = prompt.seq;
    promptShownAt = now();
  }
  $("prompt-question").textContent = copy.question;
  setText($("btn-prompt-accept"), copy.accept);
  $("prompt-panel").style.display = "block";
}

// A guarded tap is a silent no-op: no toast, nothing shown, because it is not
// a refusal the box made, only a tap this page declined to send yet.
function answerPrompt(path, node) {
  if (promptSeq === null || !answerable(promptShownAt, now())) return;
  post(path, {seq: promptSeq}, node);
}

$("btn-prompt-accept").onclick = () => answerPrompt("/api/prompt/accept", $("btn-prompt-accept"));
$("btn-prompt-dismiss").onclick = () => answerPrompt("/api/prompt/dismiss", $("btn-prompt-dismiss"));

// -- the duty chip (#19) ------------------------------------------------------
//
// ARMED / STOOD DOWN plus the time of the last change, in the strip (#5), on
// the box's own monotonic clock like every other timestamp it sends - so it
// is converted through the same round-trip estimate as a tap (`tapStamp`
// above), falling back to this snapshot's own age when there is no estimate
// yet.

// Pure: `boxSeconds` on the box's clock, converted to the page's local
// HH:MM, or "" if either half is missing - a restarted box's null `since` in
// particular, which must never show an invented or a boot time.
function clockTime(boxSeconds, offset) {
  if (boxSeconds === null || offset === null) return "";
  const local = new Date((boxSeconds + offset) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(local.getHours()) + ":" + pad(local.getMinutes());
}

// Pure: the chip's text for this duty state.
function dutyChip(duty, offset) {
  const word = duty.armed ? "ARMED" : "STOOD DOWN";
  const time = clockTime(duty.since, offset);
  return time ? word + " " + time : word;
}

// The best clock offset available for one snapshot: the round-trip estimate
// (#11) if a keepalive has already been timed, else this snapshot's own age
// against the page's clock - what a tap falls back to before the first
// estimate exists. Null, like isOlder's own guard on `next.at` above, when
// there is no estimate and `snap.at` is not a usable number either - so a
// malformed `at` renders a blank chip rather than the NaN:NaN a confident-
// looking wrong time would be.
function boxOffset(snap) {
  const estimate = clockEstimate(clockSamples);
  if (estimate) return estimate.offset;
  return typeof snap.at === "number" ? now() - snap.at : null;
}

$("btn-handoff").onclick = () => { handoffPromptOpen = true; paintSlot(); };

$("btn-handoff-yes").onclick = () => {
  handoffPromptOpen = false;
  paintSlot();
  post("/api/handoff", undefined, $("btn-handoff-yes"));
};

$("btn-handoff-no").onclick = () => {
  handoffPromptOpen = false;
  paintSlot();
  post("/api/still-mine", undefined, $("btn-handoff-no"));
};

// The two answers to "where is the fader" (#107). Neither asks first: Close now
// is one packet to -inf and is always safe, and It's at ready level moves
// nothing at all. Not navigation either - both live in the fader column, which
// is on screen whatever tab is showing.
for (const [id, path] of [["btn-close-now", "/api/close-now"], ["btn-report-ready", "/api/report-ready"]]) {
  const node = $(id);
  node.onclick = () => post(path, undefined, node);
}

// -- expand: a tap reveals a status chip's full sentence without growing the
// strip it lives in (#5) -----------------------------------------------------
//
// The strip is a fixed 44px so nothing under it moves. A dataset flag rather
// than a class: the functional colour classes above (loud, failed, fault...)
// are overwritten wholesale on every render, and a class toggled here would be
// wiped the next time one of those runs.
for (const id of ["link", "refusal", "tap", "saving", "provenance", "console-reach"]) {
  $(id).onclick = () => {
    const node = $(id);
    node.dataset.expanded = node.dataset.expanded ? "" : "1";
  };
}

// -- the page's clock against the box's -------------------------------------

// How many round trips the estimate is chosen from. One a keepalive, so about
// two minutes of them: long enough to have caught a quiet moment on the wifi,
// short enough that a clock the device has since corrected ages out.
const CLOCK_SAMPLES_KEPT = 8;

// One round trip, the NTP way. The page sent its clock, the box answered with
// its own, the page noted when the answer landed. Whatever the split between
// the two directions, the box read its clock somewhere inside that trip, so
// the midpoint is the best guess and half the trip is how far out it can be.
function clockSample(sentAt, boxAt, receivedAt) {
  return {offset: (sentAt + receivedAt) / 2 - boxAt, uncertainty: (receivedAt - sentAt) / 2};
}

// The tightest of the samples kept, or null before there is one.
function clockEstimate(samples) {
  let best = null;
  for (const sample of samples) {
    if (best === null || sample.uncertainty < best.uncertainty) best = sample;
  }
  return best;
}

let clockSamples = [];

function askTheClock(socket) {
  try {
    socket.send(JSON.stringify({ping: now()}));
  } catch {
    // A socket that cannot send is about to close, and the link banner says so.
  }
}

function receivePong(message) {
  const sample = clockSample(message.pong, message.box, now());
  // A trip that ended before it began is the device's clock stepping mid-way.
  if (!(sample.uncertainty >= 0)) return;
  clockSamples = [...clockSamples, sample].slice(-CLOCK_SAMPLES_KEPT);
}

// -- the link to the box ----------------------------------------------------

// How long to wait before trying the socket again. Fast, and deliberately not
// backed off: there is one box, a handful of browsers, and a page that is wrong
// for ten seconds is worse than a few wasted connection attempts.
const RECONNECT_MS = 1000;

// How often the banner is repainted, so a silence that has gone on too long
// gets noticed without a message having to arrive in order to notice it. That
// is the whole point - the dangerous case is the one where nothing arrives.
const LINK_TICK_MS = 1000;

const now = () => Date.now() / 1000;

// A point on this page's two clocks at once (#51, #154). "Seconds since" is
// the larger of the two deltas, because each fails differently: the wall clock
// can be stepped (a network time sync, a manual change) and the monotonic clock
// can stop while the device sleeps. Taking the larger can only show a silence
// or a lateness early, never late, which is the safe side for a page whose job
// is to say it has not heard. Taps, pings and the duty chip keep `now()`: they
// compare against the box's wall-clock offsets, not elapsed time.
function pageStamp() {
  const wall = now();
  const mono = typeof performance === "object" && performance ? performance.now() / 1000 : wall;
  return {wall, mono};
}

// Null for a stamp never taken, so "nothing yet" stays a distinct fact.
function secondsSince(stamp, at) {
  if (stamp === null) return null;
  return Math.max(at.wall - stamp.wall, at.mono - stamp.mono);
}

// What is known about the connection, as distinct from what the box last said
// through it. `staleAfter` is null until the box names it, which is what
// separates "connecting" from "connected": an open socket that has never
// delivered a frame has not proven anything yet.
let link = {open: false, staleAfter: null, seen: null};

// An open socket does not prove a working link, and that is the failure that
// matters in a stadium. Wifi degrades as the stands fill, TCP half-opens,
// `onclose` never fires, and the page goes on showing a snapshot from six
// minutes ago with complete confidence. The server's websocket pings prove
// liveness to aiohttp but a browser does not surface ping or pong to script, so
// the box sends a keepalive the page can see and this watches the gap between
// arrivals.
//
// Same distinction recordingTag draws about Reaper: a box that is deliberately
// quiet is not a fault - it goes quiet whenever nothing changes - while a
// keepalive that did not arrive is. Hence the threshold rather than a timer on
// any silence at all.
function linkBanner(open, silence, staleAfter) {
  if (!open) return ["lost", "Not connected to the box \u2014 what you see may be stale"];
  if (staleAfter === null) return ["connecting", "Connecting to the box"];
  if (silence >= staleAfter) {
    return ["stale",
            "No word from the box for " + Math.round(silence)
              + "s \u2014 what you see may be stale"];
  }
  return null;
}

// The counter beside the state, which is the half that says things are fine.
// A banner cannot: its absence is also what a page that has stopped executing
// looks like, and on a link this unreliable that ambiguity is the whole
// problem.
//
// It is a number rather than a light because it has two separate things to
// prove, and a light conflates them. The digits change every second even when
// the box has nothing to report, which is the renderer proving it still runs;
// and they drop back to zero on each keepalive, at most 15s apart, which is the
// link proving it still delivers. A counter that has stopped moving is a dead
// page. A counter climbing past the threshold is a dead link. Those are
// different faults and they now look different.
//
// It says nothing about the console, deliberately. OSC is write-only and a
// datagram into a black hole succeeds, so no indicator here can honestly claim
// the DM7 heard anything (design.md 5.3).
function linkPulse(silence, staleAfter) {
  if (silence === null) return ["", "--"];
  const text = Math.round(silence) + "s";
  if (staleAfter !== null && silence >= staleAfter) return ["stale", text];
  return ["live", text];
}

function paintLink() {
  // Null rather than a huge number, so "nothing has arrived on this socket yet"
  // stays a distinct fact from "nothing has arrived for a long time".
  const silence = secondsSince(link.seen, pageStamp());
  const banner = linkBanner(link.open, silence, link.staleAfter);
  const node = $("link");
  node.className = banner ? banner[0] : "";
  node.textContent = banner ? banner[1] : "";

  const [pulseClass, pulseText] = linkPulse(silence, link.staleAfter);
  const pulse = $("pulse");
  pulse.className = pulseClass;
  pulse.textContent = pulseText;
}

// Any frame proves the link, not just a keepalive: a box busy pushing snapshots
// is plainly alive. Only the keepalive carries how long to wait, so there is one
// copy of that number and it lives next to the interval it is derived from.
function noteFrame(message) {
  link.seen = pageStamp();
  if (message.keepalive) link.staleAfter = message.stale_after;
  paintLink();
}

function connect() {
  const socket = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws?buttons=once");
  socket.onopen = () => { link.open = true; paintLink(); };
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    noteFrame(message);
    // Neither a keepalive nor a pong carries state. Rendering one would blank
    // the page. Each keepalive is the cue to time the link again.
    if (message.pong !== undefined) receivePong(message);
    else if (message.keepalive) askTheClock(socket);
    else render(message);
  };
  socket.onclose = () => {
    // Everything the old socket established is gone with it, the threshold
    // included: the next one has to prove itself from scratch. So is the
    // clock: a box that rebooted counts from zero again, and its snapshots
    // would all look older than the last one shown.
    link = {open: false, staleAfter: null, seen: null};
    clockSamples = [];
    renderedAt = null;
    // Deliberately not reset, unlike renderedAt: the snapshot still on screen
    // arrived when it arrived, so its age stays true, and a dropped link is
    // exactly when the age has to keep climbing (#12, #147).
    paintLink();
    setTimeout(connect, RECONNECT_MS);
  };
  socket.onerror = () => socket.close();
}

// -- keeping the screen on --------------------------------------------------

// An iPad that locks its screen stops being an operator interface: the page
// goes away, the socket drops, and nobody finds out until someone looks down at
// a black slab in the middle of a drive.
//
// The Screen Wake Lock API is the right answer and is usually not available
// here. It needs a secure context, and the page is served over plain HTTP on a
// VLAN with no route to anything that issues certificates. So it is asked for,
// in case that changes, and when it is missing the page says the thing that
// does work today rather than letting the screen go out without comment.
const WAKE_HELD = "Screen held awake by this page.";
const WAKE_ADVICE = "This page cannot hold the screen on. Set Auto-Lock to Never"
  + " (Settings > Display and Brightness) or the device will sleep mid-game.";

let wakeLock = null;

function paintWake(held) {
  const node = $("wake");
  node.className = held ? "held" : "advice";
  node.textContent = held ? WAKE_HELD : WAKE_ADVICE;
}

async function holdWake() {
  if (!navigator.wakeLock) { paintWake(false); return; }
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.onrelease = () => { wakeLock = null; };
  } catch {
    wakeLock = null;
  }
  paintWake(wakeLock !== null);
}

// A wake lock is dropped whenever the document is hidden and is not handed back
// when it returns. Without this, one glance at another app turns the hold off
// for the rest of the game.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && wakeLock === null) holdWake();
});

fetch("/api/state").then(r => r.json()).then(render);
paintLink();
paintTabs();
paintSlot();
// The level readout rides the same tick: its age, and a move's lateness, have to
// count up while the box says nothing (#147, #154).
setInterval(() => { paintLink(); paintLevel(); }, LINK_TICK_MS);
connect();
holdWake();
