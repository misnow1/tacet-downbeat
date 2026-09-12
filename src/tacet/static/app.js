const $ = id => document.getElementById(id);
let snapshot = null;

async function post(path, body) {
  const options = {method: "POST"};
  if (body) { options.headers = {"Content-Type": "application/json"};
              options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => null);
  if (!response.ok) { showRefusal((payload && payload.error) || response.statusText); return null; }
  render(payload.state || payload);
  return payload;
}

function showRefusal(text) {
  const node = $("refusal");
  node.textContent = text || "";
  node.style.display = text ? "block" : "none";
}

// A category is reached in a hurry if tapping something in it moves the fader.
function actsOnTheFader(items) {
  return items.some((item) => item.action) ? 1 : 0;
}

// What the grid currently on screen was built from. Comparing this rather than
// setting a flag is what keeps the rebuild honest: the vocabulary is not fixed
// for the life of a page, because a box that restarts mid-game can serve a
// different ann.BUTTONS and the page reconnects to it without reloading.
let renderedButtons = null;

// Only the parts a rebuild would change. Deliberately not the whole snapshot -
// this has to survive a playhead moving a thousand times an hour without
// noticing.
function buttonSignature(buttons) {
  return JSON.stringify(
    buttons.map(item => [item.key, item.label, item.category, item.kind, item.action || ""]));
}

function renderButtons(buttons) {
  const byCategory = {};
  for (const button of buttons) (byCategory[button.category] ||= []).push(button);
  const host = $("buttons");
  host.innerHTML = "";
  // Categories that move the fader come first, nearest the big buttons. The
  // sort is stable, so everything else keeps the vocabulary's own order.
  const categories = Object.entries(byCategory);
  categories.sort((a, b) => actsOnTheFader(b[1]) - actsOnTheFader(a[1]));
  for (const [category, items] of categories) {
    const heading = document.createElement("h2");
    heading.textContent = category;
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const item of items) {
      const node = document.createElement("button");
      node.textContent = buttonLabel(item.label, item.kind, false);
      node.dataset.key = item.key;
      node.dataset.kind = item.kind;
      node.dataset.label = item.label;
      if (item.action) node.dataset.action = item.action;
      node.onclick = () => activate(item, node);
      grid.appendChild(node);
    }
    host.appendChild(heading); host.appendChild(grid);
  }
}

// Span buttons toggle: the first tap opens the region, the second closes it.
// Instants fire once. The highlight alone cannot carry that difference - it
// looks the same as an instant that was just tapped - so the button says which
// tap it is about to be.
function buttonLabel(label, kind, open) {
  if (kind !== "span") return label;
  return label + (open ? " (end)" : " (start)");
}

function activate(item, node) {
  if (item.kind !== "span") {
    const data = item.key === "note"
      ? {text: prompt("Note") || ""} : undefined;
    if (item.key === "note" && !data.text) return;
    post("/api/annotate", {key: item.key, data});
    return;
  }
  const open = (snapshot.open_spans || []).find(id => id.startsWith(item.key + "-"));
  if (open) post("/api/span/end", {span_id: open});
  else post("/api/span/start", {key: item.key});
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

function recordingTag(liveness, known) {
  if (liveness === "lost") return ["unknown", "LINK LOST"];
  if (liveness === "unknown") return ["unknown", "no feedback"];
  if (!known) return ["unknown", "not yet reported"];
  return ["confirmed", liveness === "quiet" ? "confirmed (idle)" : "confirmed"];
}

function render(next) {
  if (!next) return;
  snapshot = next;
  $("state").textContent = next.state.replace(/-/g, " ").toUpperCase();
  $("why").textContent = next.why;
  showRefusal(next.refusal);

  const fader = next.fader;
  // While a fade runs the number on the left sweeps, so the destination is
  // shown beside it: a close takes two seconds and "-3.00 dB" on its own reads
  // as a fader that is not moving. Null dB is -inf, never a missing reading.
  // Compared as console units, not dB: -inf has no number to compare with.
  const arrived = fader.target === null || fader.target === fader.commanded;
  $("level").textContent = arrived
    ? faderDb(fader.db)
    : faderDb(fader.db) + " \u2192 " + faderDb(fader.target_db);
  $("fader-error").textContent = fader.healthy ? "" : "Console unreachable: " + fader.error;

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
  const signature = buttonSignature(next.buttons);
  if (signature !== renderedButtons) {
    renderButtons(next.buttons);
    renderedButtons = signature;
  }
  for (const node of document.querySelectorAll("#buttons button")) {
    const open = (next.open_spans || []).some(id => id.startsWith(node.dataset.key + "-"));
    node.classList.toggle("on", open);
    node.textContent = buttonLabel(node.dataset.label, node.dataset.kind, open);
  }
}

$("btn-trigger").onclick = () => post("/api/trigger");
$("btn-release").onclick = () => post("/api/release");
$("btn-arm").onclick = () => post("/api/arm");
$("btn-stand-down").onclick = () => post("/api/stand-down");
$("btn-record").onclick = () => post("/api/record");

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
    // A keepalive carries no state. Rendering it would blank the page.
    if (!message.keepalive) render(message);
  };
  socket.onclose = () => {
    // Everything the old socket established is gone with it, the threshold
    // included: the next one has to prove itself from scratch.
    link = {open: false, staleAfter: null, seen: null};
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
setInterval(paintLink, LINK_TICK_MS);
connect();
holdWake();
