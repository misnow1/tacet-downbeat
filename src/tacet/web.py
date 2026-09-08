"""The operator UI: a thin aiohttp shell over `tacet.app`.

Everything that decides anything lives in `tacet.app`; this module turns HTTP
into method calls and pushes snapshots down a websocket. Keeping it thin is what
lets the operator flow be tested without a server.

The page is served as one self-contained string. It fetches nothing off the
network: the control VLAN has no route to the internet, and a game is not the
time to discover that a CDN was a single point of failure. A test asserts it.

There is no stop route, and a test asserts that too (design.md 5.9).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import annotations as ann
from .app import App

#: How often a snapshot that differs only in playhead position is pushed out.
#:
#: Reaper streams `/time` at about 11 Hz while the transport rolls, and each
#: packet produces a snapshot. Pushing every one of them costs roughly 30 KB/s
#: per connected browser for three hours, over whatever wifi a stadium has, to
#: animate a number nobody reads at that resolution. Everything that is not the
#: playhead still goes out immediately.
POSITION_BROADCAST_INTERVAL = 1.0


def _without_position(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    recording = {k: v for k, v in snapshot["recording"].items() if k != "position"}
    return {**snapshot, "recording": recording}


def should_broadcast(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    elapsed: float,
    interval: float = POSITION_BROADCAST_INTERVAL,
) -> bool:
    """Whether this snapshot is worth waking every browser for.

    Pure, so the coalescing rule is testable without a socket or a clock.

    Only the playhead is ever held back. A fader move, a state change or a lost
    recorder goes out at once regardless of the interval: a missed downbeat is
    unrecoverable, and CLAUDE.md wants faults visible, not averaged.
    """
    if previous is None:
        return True
    if _without_position(previous) != _without_position(current):
        return True
    return elapsed >= interval


class _Hub:
    """Holds the app and the connected browsers, and pushes snapshots at them."""

    def __init__(self, app: App, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.sockets: list[web.WebSocketResponse] = []
        self._pending: set[asyncio.Task[None]] = set()
        self._monotonic = monotonic
        self._last: dict[str, Any] | None = None
        self._last_sent = 0.0

    def broadcast(self) -> None:
        # Fire and forget: a slow or wedged browser must never stall a fader
        # move. Tasks are held until done so they are not collected mid-send.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        snapshot = self.app.snapshot()
        now = self._monotonic()
        if not should_broadcast(self._last, snapshot, elapsed=now - self._last_sent):
            return
        self._last = snapshot
        self._last_sent = now
        payload = json.dumps(snapshot)
        for socket in list(self.sockets):
            if socket.closed:
                continue
            task = loop.create_task(_send(socket, payload))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)


_HUB = web.AppKey("tacet_hub", _Hub)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


async def _state(request: web.Request) -> web.Response:
    return web.json_response(request.app[_HUB].app.snapshot())


def _command_route(name: str) -> Any:
    async def handler(request: web.Request) -> web.Response:
        app = request.app[_HUB].app
        await getattr(app, name)()
        return web.json_response(app.snapshot())

    return handler


async def _record(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    await app.start_recording()
    return web.json_response(app.snapshot())


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": f"malformed JSON: {exc}"}),
            content_type="application/json",
        ) from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "expected a JSON object"}),
            content_type="application/json",
        )
    return payload


def _bad_request(message: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": message}), content_type="application/json")


async def _annotate(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    payload = await _body(request)
    key = payload.get("key")
    if not isinstance(key, str):
        raise _bad_request("'key' is required")
    try:
        entry = await app.annotate(key, data=payload.get("data"))
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    return web.json_response({"entry": entry.as_dict(), "state": app.snapshot()})


async def _span_start(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    payload = await _body(request)
    key = payload.get("key")
    if not isinstance(key, str):
        raise _bad_request("'key' is required")
    try:
        span_id = await app.start_span(key)
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    return web.json_response({"span_id": span_id, "state": app.snapshot()})


async def _span_end(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    payload = await _body(request)
    span_id = payload.get("span_id")
    if not isinstance(span_id, str):
        raise _bad_request("'span_id' is required")
    try:
        await app.end_span(span_id)
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    return web.json_response({"state": app.snapshot()})


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse(heartbeat=10.0)
    await socket.prepare(request)
    hub = request.app[_HUB]
    hub.sockets.append(socket)
    try:
        await socket.send_str(json.dumps(hub.app.snapshot()))
        async for message in socket:
            if message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        if socket in hub.sockets:
            hub.sockets.remove(socket)
    return socket


async def _send(socket: web.WebSocketResponse, payload: str) -> None:
    """A browser that has gone away is not an error worth raising."""
    with contextlib.suppress(ConnectionResetError, RuntimeError):
        await socket.send_str(payload)


def create_app(app: App) -> web.Application:
    server = web.Application()
    hub = _Hub(app)
    server[_HUB] = hub
    app.on_change(hub.broadcast)

    server.add_routes(
        [
            web.get("/", _index),
            web.get("/api/state", _state),
            web.post("/api/arm", _command_route("arm")),
            web.post("/api/stand-down", _command_route("stand_down")),
            web.post("/api/trigger", _command_route("trigger")),
            web.post("/api/release", _command_route("release")),
            web.post("/api/record", _record),
            web.post("/api/annotate", _annotate),
            web.post("/api/span/start", _span_start),
            web.post("/api/span/end", _span_end),
            web.get("/ws", _websocket),
        ]
    )
    return server


#: The page inlines its script rather than linking it. The control VLAN has no
#: route to the internet and a game is not the time to discover that a second
#: request was a single point of failure; `test_the_page_does_not_load_anything
#: _off_the_network` holds that line. The script lives in its own file anyway,
#: so node can test it - see `tests/test_app_js.mjs`.
_SCRIPT_PLACEHOLDER = "__TACET_SCRIPT__"
_SCRIPT = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8").rstrip("\n")

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>tacet-downbeat</title>
<style>
:root{--bg:#14161a;--panel:#1e2128;--line:#2c313b;--text:#e8eaed;--dim:#9aa3b0;
--open:#2e7d32;--fade:#b26a00;--warn:#c62828;--ok:#2e7d32}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-text-size-adjust:100%;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
header{padding:14px 16px;border-bottom:1px solid var(--line)}
#state{font-size:26px;font-weight:700;letter-spacing:.02em}
#why{color:var(--dim);margin-top:4px}
#refusal{color:#ffb4a9;margin-top:6px;display:none}
main{padding:16px;display:grid;gap:16px;max-width:900px;margin:0 auto}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
button{font:inherit;font-weight:600;color:var(--text);background:var(--panel);
border:1px solid var(--line);border-radius:12px;padding:18px 12px;cursor:pointer;
touch-action:manipulation;-webkit-tap-highlight-color:transparent}
button:active{transform:translateY(1px)}
button.big{font-size:20px;padding:26px 12px}
button.open{background:var(--open);border-color:var(--open)}
button.fade{background:var(--fade);border-color:var(--fade)}
button.on{outline:2px solid var(--text)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.label{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.value{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:99px;
border:1px solid var(--line);color:var(--dim);margin-left:6px;vertical-align:middle}
.tag.commanded{border-color:var(--fade);color:#ffca7a}
.tag.confirmed{border-color:var(--ok);color:#9fd8a2}
.tag.unknown{border-color:var(--warn);color:#ff9d94}
h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;
margin:4px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.grid button{padding:14px 10px;font-size:14px;font-weight:500}
#link{padding:8px 16px;text-align:center;font-size:13px;color:#fff;background:var(--warn);
display:none}
</style></head><body>
<div id="link">Disconnected from the box &mdash; what you see may be stale</div>
<header><div id="state">&hellip;</div><div id="why"></div><div id="refusal"></div></header>
<main>
  <div class="row">
    <button class="big open" id="btn-trigger">OPEN</button>
    <button class="big fade" id="btn-release">FADE OUT</button>
  </div>
  <div class="row">
    <div class="panel"><div class="label">Fader</div>
      <div class="value"><span id="level">&mdash;</span><span class="tag commanded">commanded</span></div>
      <div id="fader-error" style="color:#ff9d94;font-size:13px;margin-top:4px"></div></div>
    <div class="panel"><div class="label">Recording</div>
      <div class="value"><span id="rec">&mdash;</span><span class="tag" id="rec-tag">unknown</span></div>
      <div id="rec-pos" style="color:var(--dim);font-size:13px;margin-top:4px"></div></div>
  </div>
  <div class="row">
    <button id="btn-arm">Arm</button>
    <button id="btn-stand-down">Stand down</button>
  </div>
  <div class="row"><button id="btn-record">Start recording</button><div></div></div>
  <div id="buttons"></div>
</main>
<script>
__TACET_SCRIPT__
</script></body></html>
""".replace(_SCRIPT_PLACEHOLDER, _SCRIPT)
