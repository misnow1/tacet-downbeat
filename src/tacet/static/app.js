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

function renderButtons(buttons) {
  const byCategory = {};
  for (const button of buttons) (byCategory[button.category] ||= []).push(button);
  const host = $("buttons");
  host.innerHTML = "";
  for (const [category, items] of Object.entries(byCategory)) {
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

function render(next) {
  if (!next) return;
  snapshot = next;
  $("state").textContent = next.state.replace(/-/g, " ").toUpperCase();
  $("why").textContent = next.why;
  showRefusal(next.refusal);

  const fader = next.fader;
  $("level").textContent = fader.db === null ? "-\u221E dB" : fader.db.toFixed(2) + " dB";
  $("fader-error").textContent = fader.healthy ? "" : "Console unreachable: " + fader.error;

  // Reaper is silent whenever it is parked, so silence alone is not a fault.
  // "quiet" is a believed reading from a stopped Reaper; "lost" is silence
  // where the /time stream should have been, and that one is a fault.
  const rec = next.recording;
  const REC_TAG = {
    live: ["confirmed", "confirmed"],
    quiet: ["confirmed", "confirmed (idle)"],
    lost: ["unknown", "LINK LOST"],
    unknown: ["unknown", "no feedback"],
  };
  const [recClass, recLabel] = REC_TAG[rec.liveness] || REC_TAG.unknown;
  $("rec").textContent = !rec.known ? "unknown" : (rec.recording ? "ROLLING" : "stopped");
  const tag = $("rec-tag");
  tag.textContent = recLabel;
  tag.className = "tag " + recClass;
  $("rec-pos").textContent =
    rec.confirmed && rec.position !== null ? "at " + timecode(rec.position) : "";

  if (!snapshot.buttonsRendered) { renderButtons(next.buttons); snapshot.buttonsRendered = true; }
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

function connect() {
  const socket = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  socket.onopen = () => { $("link").style.display = "none"; };
  socket.onmessage = event => render(JSON.parse(event.data));
  socket.onclose = () => { $("link").style.display = "block"; setTimeout(connect, 1000); };
  socket.onerror = () => socket.close();
}
fetch("/api/state").then(r => r.json()).then(render);
connect();
