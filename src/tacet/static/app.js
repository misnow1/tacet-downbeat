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

// MAIN: the whole GAME category, split into the three groups the hallway test
// was run against.
const MAIN_GROUPS = [
  ["Game", ["q1", "q2", "q3", "q4", "halftime", "halftime-exodus", "last-two-minutes"]],
  ["Scoring", ["touchdown", "field-goal", "first-down", "defensive-stop"]],
  ["Timeouts", ["timeout-home", "timeout-away", "timeout-media", "timeout-official", "timeout-injury", "timeout"]],
];

// What the fader column and MAIN together account for, so MORE is "whatever
// is left" rather than a third list that can drift out of step with the other
// two - see the completeness check in tests/test_app_js.mjs.
const PLACED_ELSEWHERE = new Set([...FADER_COLUMN, ...MAIN_GROUPS.flatMap(([, keys]) => keys)]);

function buildButtonNode(item) {
  const node = document.createElement("button");
  node.textContent = buttonLabel(item.label, item.kind, false);
  node.dataset.key = item.key;
  node.dataset.kind = item.kind;
  node.dataset.label = item.label;
  if (item.action) node.dataset.action = item.action;
  node.onclick = () => activate(item, node);
  return node;
}

function buildGrid(items) {
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const item of items) grid.appendChild(buildButtonNode(item));
  return grid;
}

function appendHeadedGrid(host, heading, items) {
  if (!items.length) return;
  const h = document.createElement("h2");
  h.textContent = heading;
  host.appendChild(h);
  host.appendChild(buildGrid(items));
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
  for (const [heading, keys] of MAIN_GROUPS) {
    appendHeadedGrid(host, heading, keys.map((key) => byKey.get(key)).filter(Boolean));
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
// Reaper is also silent whenever it is parked, so silence alone is not a fault:
// "quiet" is a believed reading from a stopped Reaper, while "lost" is silence
// where the /time stream should have been, and only that one is.
// A fader level for the screen. Null is -inf - a closed fader, not a missing
// reading - because JSON cannot carry -inf and a blank there would read as a
// link problem rather than a closed DCA.
function faderDb(db) {
  return db === null ? "-∞ dB" : db.toFixed(2) + " dB";
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

function recordingTag(liveness, known) {
  if (liveness === "lost") return ["unknown", "LINK LOST"];
  if (liveness === "unknown") return ["unknown", "no feedback"];
  if (!known) return ["unknown", "not yet reported"];
  return ["confirmed", liveness === "quiet" ? "confirmed (idle)" : "confirmed"];
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

function isOlder(next, at) {
  return at !== null && typeof next.at === "number" && next.at < at;
}

function render(next) {
  if (!next) return;
  if (isOlder(next, renderedAt)) return;
  if (typeof next.at === "number") renderedAt = next.at;
  snapshot = next;
  $("state").textContent = next.state.replace(/-/g, " ").toUpperCase();
  $("why").textContent = next.why;
  // #19: ARMED / STOOD DOWN and since when, on the box's own clock. Guarded
  // like renderFaderHalf and orphanSpans below: a box rolled back to before
  // #19 sends no `duty` at all, and that must not throw and stop the whole
  // render - only leave the chip showing whatever it last did.
  if (next.duty) $("duty").textContent = dutyChip(next.duty, boxOffset(next));
  // A fader tap that arrived too late was not done. Nothing moved, so nothing
  // else on the page changes to say so, and the operator has to decide again
  // (#16).
  showRefusal(next.refusal, Boolean(next.stale_tap));
  const saving = savingBanner(next.log, next.mirror);
  $("saving").className = saving ? saving[0] : "";
  $("saving").textContent = saving ? saving[1] : "";

  const fader = next.fader;
  // While a fade runs the number on the left sweeps, so the destination is
  // shown beside it: a close takes two seconds and "-3.00 dB" on its own reads
  // as a fader that is not moving. Null dB is -inf, never a missing reading.
  // Compared as console units, not dB: -inf has no number to compare with.
  const arrived = fader.target === null || fader.target === fader.commanded;
  // The box does not know where the fader is (#107): at every cold boot, and
  // again after a hand-off to StageMix (#12). `commanded` is only a belief
  // until an absolute command says otherwise, and the number must say so
  // rather than sit there looking confident - the game 2 hazard this whole
  // feature exists for.
  const value = fader.level_known
    ? (arrived ? faderDb(fader.db) : faderDb(fader.db) + " \u2192 " + faderDb(fader.target_db))
    : "unknown";
  const age = ageText(fader.age);
  $("level").textContent = age ? value + " - " + age : value;
  const levelTag = $("level-tag");
  levelTag.textContent = fader.level_known ? "commanded" : "unknown";
  levelTag.className = "tag " + (fader.level_known ? "commanded" : "unknown");
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
  $("btn-report-ready").disabled = fader.level_known;
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
  record.disabled = !rec.can_start;
  record.textContent = rec.known && rec.recording ? "Recording" : "Start recording";

  $("rec-pos").textContent =
    rec.confirmed && rec.position !== null ? "at " + timecode(rec.position) : "";

  // Rebuilt only when the vocabulary actually changes. renderButtons tears the
  // grid down and recreates it, and a tap landing during that is lost - the node
  // under the finger is detached between touchstart and touchend, so no click
  // fires. This is the grid tapped without looking by someone watching a field,
  // and it carries the fader buttons, so a dropped tap there is a missed open.
  const orphans = orphanSpans(next);
  const signature = buttonSignature(next.buttons) + JSON.stringify(orphans.map(span => span.span_id));
  if (signature !== renderedButtons) {
    renderButtons(next.buttons, orphans);
    renderedButtons = signature;
  }
  for (const node of document.querySelectorAll(
    "#fader-top button, #fader-bottom button, #tab-main button, #tab-more-vocabulary button"
  )) {
    // An orphan's label says what it does and never changes.
    if (node.dataset.orphan) continue;
    const open = openSpan(next, node.dataset.key) !== undefined;
    node.classList.toggle("on", open);
    node.textContent = buttonLabel(node.dataset.label, node.dataset.kind, open);
    // In this paint loop and not in buildButtonNode, so that the belief stays
    // out of the rebuild signature above: a belief change re-enables these in
    // place, and never tears the grid down under a thumb.
    node.disabled = !fader.level_known && RAMPING_ACTIONS.has(node.dataset.action);
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
  $("btn-prompt-accept").textContent = copy.accept;
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
for (const id of ["link", "refusal", "tap", "saving"]) {
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
  const silence = link.seen === null ? null : now() - link.seen;
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
  link.seen = now();
  if (message.keepalive) link.staleAfter = message.stale_after;
  paintLink();
}

function connect() {
  const socket = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
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
setInterval(paintLink, LINK_TICK_MS);
connect();
holdWake();
