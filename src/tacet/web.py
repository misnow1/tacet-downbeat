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
import math
import time
import traceback
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from . import annotations as ann
from . import taps
from .app import App

#: How often a snapshot that differs only in playhead position is pushed out.
#:
#: Reaper streams `/time` at about 11 Hz while the transport rolls, and each
#: packet produces a snapshot. Pushing every one of them costs roughly 30 KB/s
#: per connected browser for three hours, over whatever wifi a stadium has, to
#: animate a number nobody reads at that resolution. Everything that is not the
#: playhead still goes out immediately.
POSITION_BROADCAST_INTERVAL = 1.0

#: How often the box re-reads its own state and pushes it if anything changed.
#:
#: Snapshots are otherwise pushed only when something happens - a packet, a
#: tap, a fade step - and some faults are the absence of anything happening.
#: Reaper dying mid-recording stops its packets, and with them every push, so
#: the page showed ROLLING, tagged confirmed, indefinitely. This bounds how
#: long a fault that is a silence takes to reach the screen, on top of the
#: silence it takes to be one (`reaper.DEFAULT_FEEDBACK_TIMEOUT`). Most ticks
#: find nothing new and `should_broadcast` sends nothing.
TICK_INTERVAL = 1.0

#: How often the box says "still here" on a socket it has nothing to report on.
#:
#: An open socket proves nothing on stadium wifi. The link half-opens, delivery
#: stops, `onclose` never fires, and the page goes on showing a snapshot from
#: six minutes ago with complete confidence. aiohttp's own ping frames
#: (`WS_PING_INTERVAL`) solve the mirror image of this - they are how the box
#: notices a browser that went away - but a browser does not surface ping or
#: pong to script, so the page cannot see them. This is the one it can see.
KEEPALIVE_INTERVAL = 15.0

#: How many keepalives may go missing before the page stops trusting what it is
#: showing. Two, so one lost frame on a bad link is not a fault; the half is so
#: a keepalive that is merely late is not one either.
KEEPALIVE_LOSSES_BEFORE_STALE = 2.5

#: When the page gives up on what it is showing. Derived rather than written
#: down twice: the page is told this number instead of keeping its own copy, so
#: the threshold cannot drift away from the interval it comes from.
STALE_AFTER = KEEPALIVE_INTERVAL * KEEPALIVE_LOSSES_BEFORE_STALE

#: How often aiohttp pings the browser, which is how the box notices a browser
#: that has gone away without closing. The other direction of the same problem.
WS_PING_INTERVAL = 10.0

#: A frame that carries no state, only the fact that the link delivered it.
#: `keepalive` marks it so the page renders a snapshot and not this.
KEEPALIVE_FRAME = json.dumps({"keepalive": True, "stale_after": STALE_AFTER})


#: Fields of a snapshot that change without anything having happened: when it
#: was taken, which is different every time it is asked for.
_UNREMARKABLE = ("at",)


def _remarkable(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snapshot.items() if k not in _UNREMARKABLE}


def _without_position(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    recording = {k: v for k, v in snapshot["recording"].items() if k != "position"}
    return {**_remarkable(snapshot), "recording": recording}


def pong_for(text: str, now: float) -> str | None:
    """The answer to a page asking the box's clock, or None if `text` is not a
    well-formed ask. Pure.

    The page sends `{"ping": <its clock>}` each time a keepalive arrives, and
    estimates its offset from the round trip (#11). The ping is echoed so the
    page needs no bookkeeping to pair them up.
    """
    try:
        message = json.loads(text)
    except ValueError:
        return None
    ping = message.get("ping") if isinstance(message, dict) else None
    if isinstance(ping, bool) or not isinstance(ping, int | float) or not math.isfinite(ping):
        return None
    return json.dumps({"pong": ping, "box": now})


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

    A snapshot identical to the last one sent never goes out: the state tick
    asks every second, and the page already has it.
    """
    if previous is None:
        return True
    if _remarkable(previous) == _remarkable(current):
        return False
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

#: Configuration for the same reason as the keepalive below: so a test can
#: watch the tick notice a silence without waiting seconds for it.
_TICK = web.AppKey("tacet_tick_interval", float)

#: Configuration rather than a constant, so a link that wants a different
#: cadence can have one without the number being edited into the source - and so
#: the loop can be watched repeating in a test without the test taking a minute.
_KEEPALIVE = web.AppKey("tacet_keepalive_interval", float)

#: The largest request body read. aiohttp's default is a megabyte, fsynced into
#: the log if it parses; the biggest thing the page sends is a note, which
#: `annotations.MAX_DATA_TEXT` bounds far below this.
MAX_REQUEST_BYTES = 16 * 1024

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


async def _state(request: web.Request) -> web.Response:
    return web.json_response(request.app[_HUB].app.snapshot())


def _command_route(name: str) -> Any:
    async def handler(request: web.Request) -> web.Response:
        app = request.app[_HUB].app
        received = app.now()
        tap = _tap(await _optional_body(request), received)
        await getattr(app, name)(tap=tap)
        return web.json_response(app.snapshot())

    return handler


async def _record(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    received = app.now()
    tap = _tap(await _optional_body(request), received)
    await app.start_recording(tap=tap)
    return web.json_response(app.snapshot())


def _tap(payload: Mapping[str, Any], received: float) -> taps.TapTiming:
    """The request's tap on the box's clock. `received` is read before the body,
    so the time spent reading it is not counted as the page's."""
    try:
        return taps.timing(taps.read_tap(payload), received)
    except taps.TapError as exc:
        raise _bad_request(str(exc)) from exc


async def _optional_body(request: web.Request) -> dict[str, Any]:
    """A command's body, which may be absent: they carry nothing but a tap
    stamp, and curl, or a page cached from before #11, sends none."""
    if not request.can_read_body:
        return {}
    return await _body(request)


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
    received = app.now()
    payload = await _body(request)
    key = payload.get("key")
    if not isinstance(key, str):
        raise _bad_request("'key' is required")
    tap = _tap(payload, received)
    try:
        entry = await app.annotate(key, data=payload.get("data"), tap=tap)
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    # A null entry is one the log refused. Not an error status: the tap was
    # acted on, and the state below carries the log fault to the page. An entry
    # is accepted, not yet saved; a later failure arrives the same way (#41).
    saved = entry.as_dict() if entry is not None else None
    return web.json_response({"entry": saved, "state": app.snapshot()})


async def _span_start(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    received = app.now()
    payload = await _body(request)
    key = payload.get("key")
    if not isinstance(key, str):
        raise _bad_request("'key' is required")
    tap = _tap(payload, received)
    try:
        span_id = await app.start_span(key, tap=tap)
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    return web.json_response({"span_id": span_id, "state": app.snapshot()})


async def _span_end(request: web.Request) -> web.Response:
    app = request.app[_HUB].app
    received = app.now()
    payload = await _body(request)
    span_id = payload.get("span_id")
    if not isinstance(span_id, str):
        raise _bad_request("'span_id' is required")
    tap = _tap(payload, received)
    try:
        await app.end_span(span_id, tap=tap)
    except ann.AnnotationError as exc:
        raise _bad_request(str(exc)) from exc
    return web.json_response({"state": app.snapshot()})


def _prompt_seq(payload: Mapping[str, Any]) -> int:
    """Which prompt an answer is for. A bool is an int to Python and not to the
    page, so it is refused explicitly rather than read as 1."""
    if "seq" not in payload:
        raise _bad_request("'seq' is required")
    seq = payload["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise _bad_request(f"'seq' must be a whole number, got {seq!r}")
    return seq


def _target_db(payload: Mapping[str, Any]) -> float:
    """Which level the standing target is being set to (#9), in dB. A bool is
    a number to Python and not to the page, and JSON's NaN and Infinity are not
    levels, so both are refused here. Whether it is one of the *presets* is not
    this function's business: that is a refusal on the snapshot, not a 400."""
    if "db" not in payload:
        raise _bad_request("'db' is required")
    db = payload["db"]
    if isinstance(db, bool) or not isinstance(db, int | float) or not math.isfinite(db):
        raise _bad_request(f"'db' must be a number, got {db!r}")
    return float(db)


async def _set_target(request: web.Request) -> web.Response:
    """Change the standing target level (#9). Stores the value and moves
    nothing, in every state, so it is neither stale-checked nor refused by
    state; a level that is not a preset comes back as a refusal on the
    snapshot with a 200, like any other tap the box declines."""
    app = request.app[_HUB].app
    received = app.now()
    payload = await _body(request)
    db = _target_db(payload)
    tap = _tap(payload, received)
    await app.set_target(db, tap=tap)
    return web.json_response(app.snapshot())


def _prompt_route(name: str) -> Any:
    """An answer to the arm / stand-down question (#19). A `seq` that is not the
    open prompt's is not an error: it is the ordinary race between two
    browsers, or an answer that crossed a replacement, and the box does nothing
    and says where things stand."""

    async def handler(request: web.Request) -> web.Response:
        app = request.app[_HUB].app
        received = app.now()
        payload = await _body(request)
        seq = _prompt_seq(payload)
        tap = _tap(payload, received)
        await getattr(app, name)(seq, tap=tap)
        return web.json_response(app.snapshot())

    return handler


async def _keepalive(socket: web.WebSocketResponse, *, interval: float = KEEPALIVE_INTERVAL) -> None:
    """Say "still here" on a socket the box has nothing to report on."""
    while True:
        await asyncio.sleep(interval)
        if socket.closed:
            return
        await _send(socket, KEEPALIVE_FRAME)


async def _tick(hub: _Hub, *, interval: float = TICK_INTERVAL) -> None:
    """Re-read the state on a clock, so a fault that is a silence still reaches
    the page. The first read is immediate, so the hub knows what the page is
    shown on connect and the first tick after it does not resend that."""
    while True:
        # A tick that died would leave everything else looking healthy and put
        # the frozen ROLLING straight back, so one bad read does not end it.
        try:
            hub.broadcast()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(interval)


async def _ticking(server: web.Application) -> AsyncIterator[None]:
    ticker = asyncio.ensure_future(_tick(server[_HUB], interval=server[_TICK]))
    yield
    ticker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse(heartbeat=WS_PING_INTERVAL)
    await socket.prepare(request)
    hub = request.app[_HUB]
    hub.sockets.append(socket)
    # The keepalive goes first, so the page learns how long to wait before
    # distrusting a silence on the same round trip as its first snapshot. Until
    # it has been told, it reports itself as connecting rather than connected.
    keeper = asyncio.ensure_future(_keepalive(socket, interval=request.app[_KEEPALIVE]))
    try:
        await socket.send_str(KEEPALIVE_FRAME)
        await socket.send_str(json.dumps(hub.app.snapshot()))
        async for message in socket:
            if message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
            if message.type is WSMsgType.TEXT:
                # Answered at once, before anything else gets a turn: the time
                # this waits is counted against the estimate's uncertainty.
                pong = pong_for(message.data, hub.app.now())
                if pong is not None:
                    await _send(socket, pong)
    finally:
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper
        if socket in hub.sockets:
            hub.sockets.remove(socket)
    return socket


async def _send(socket: web.WebSocketResponse, payload: str) -> None:
    """A browser that has gone away is not an error worth raising."""
    with contextlib.suppress(ConnectionResetError, RuntimeError):
        await socket.send_str(payload)


async def _close_sockets(server: web.Application) -> None:
    """Hang up on every browser before the server goes away.

    Without this the shutdown waits on handlers parked in `async for message in
    socket`, which do not return until their socket does. The browser wants the
    close anyway: a clean hangup puts the page straight into its reconnecting
    banner, where a silent disappearance would leave it looking healthy and
    stale until the keepalive threshold expired.
    """
    for socket in list(server[_HUB].sockets):
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await socket.close(code=WSCloseCode.GOING_AWAY, message=b"box stopping")


def create_app(
    app: App,
    *,
    keepalive_interval: float = KEEPALIVE_INTERVAL,
    tick_interval: float = TICK_INTERVAL,
) -> web.Application:
    server = web.Application(client_max_size=MAX_REQUEST_BYTES)
    hub = _Hub(app)
    server[_HUB] = hub
    server[_KEEPALIVE] = keepalive_interval
    server[_TICK] = tick_interval
    server.cleanup_ctx.append(_ticking)
    server.on_shutdown.append(_close_sockets)
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
            # Where the fader is, and who has it (#12, #107): all four take only
            # a tap, the same as arm/stand-down/trigger/release, so
            # `_command_route` covers them without a handler of its own.
            web.post("/api/handoff", _command_route("handoff")),
            web.post("/api/close-now", _command_route("close_now")),
            web.post("/api/report-ready", _command_route("report_ready")),
            web.post("/api/still-mine", _command_route("confirm_still_mine")),
            web.post("/api/annotate", _annotate),
            web.post("/api/span/start", _span_start),
            web.post("/api/span/end", _span_end),
            # The arm / stand-down question (#19): one route for each answer,
            # like /api/handoff and /api/still-mine.
            web.post("/api/target", _set_target),
            web.post("/api/prompt/accept", _prompt_route("accept_prompt")),
            web.post("/api/prompt/dismiss", _prompt_route("dismiss_prompt")),
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
/* #5: black text on a bright amber (about 11:1) rather than white on the old,
   darker amber (about 3.9:1) - the close direction has to read at a glance in
   noon sun, and colour alone must not be the only thing saying which way a
   button goes (see the arrows below). */
:root{--bg:#14161a;--panel:#1e2128;--line:#2c313b;--text:#e8eaed;--dim:#9aa3b0;
--open:#2e7d32;--fade:#ffb300;--fade-text:#1a1400;--warn:#c62828;--ok:#2e7d32}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-text-size-adjust:100%}
/* Landscape, right thumb (#5): the fader column is a fixed-width flex sibling
   of everything else, so nothing that grows on the left - the why line, an
   expanded status chip, MAIN's content - can ever move a fader button. */
.app{display:flex;min-height:100vh;min-height:100dvh}
.left{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;overflow-y:auto;
padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
/* MORE is reached less often and never mid-play; the tint says at a glance
   which tab is showing without having to read either tab button. */
.left.more{background:#1a1d24}
header{padding:14px 16px;border-bottom:1px solid var(--line)}
#state{font-size:26px;font-weight:700;letter-spacing:.02em}
/* The state and the link counter share a line: the counter is the one element
   that says things are fine, and no banner cannot, since a page that has
   stopped executing shows no banner either. */
.headline{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
#pulse{font-size:13px;color:var(--dim);font-variant-numeric:tabular-nums;
white-space:nowrap}
#pulse::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;
background:var(--line);margin-right:6px}
#pulse.live::before{background:var(--ok)}
#pulse.stale::before{background:var(--warn)}
#why{color:var(--dim);margin-top:4px}
/* The status strip: a fixed 44px so nothing below it - the prompt slot, the
   tabs, the fader column across the flex row - ever moves under a thumb about
   to tap again. Each chip is short by default (ellipsised) rather than by
   rewritten text, so `showRefusal` and friends still say things in the box's
   own words; a tap expands one to its full sentence, in flow, which only ever
   pushes other things inside .left - never the fader column, a flex sibling
   of the whole panel. */
#strip{min-height:44px;display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:6px 16px}
#strip > div{flex:0 1 auto;max-width:100%;padding:4px 10px;border-radius:8px;font-size:12px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
#strip > div[data-expanded="1"]{white-space:normal;overflow:visible;text-overflow:clip;
font-size:13px}
/* #19: the duty chip. Always shown, and never one of the tap-to-expand chips
   above - it has nothing to expand into, so it gets `cursor:default` rather
   than the `cursor:pointer` `#strip > div` sets for those. The `#strip >`
   here, not just `#duty`, is what actually wins that fight: an id alone is
   lower specificity than `#strip > div`'s id-plus-type, and would silently
   lose. Colour is deliberately neutral - this page already spends colour on
   fader direction (see the open/release buttons below), and ARMED is not
   "open". */
#strip > #duty{color:var(--dim);cursor:default}
/* #9: the standing target's chip, straight after the duty chip and held to the
   same `#strip >` rule for the same reason. Neutral while the target is the
   configured default; amber only when it is not, so a leftover quiet setting is
   not forgotten. The amber is the page's "attention" colour and the chip says
   the same thing in words ("target -3 dB"), so it is never colour alone. This
   is where the target is *shown*; where it is *changed* is MORE. */
#strip > #target-level{color:var(--dim);cursor:default}
#strip > #target-level.off-default{background:var(--fade);color:var(--fade-text);font-weight:700}
#refusal{color:#ffb4a9;display:none}
#refusal.loud{color:#fff;background:var(--warn);font-weight:700}
/* The page's own word on its last tap. The box cannot say a tap did not reach
   it, so this is not wiped by a snapshot the way the refusal line is. */
#tap{display:none}
#tap.failed{display:block;color:#fff;background:var(--warn)}
#tap.untimed{display:block;color:#ffca7a}
/* #19's arm / stand-down question is alone in this slot (the hand-off
   confirmation moved into MORE, #108), so it stays a fixed height whether
   or not the question is showing, and nothing below it ever moves. */
#prompt{min-height:88px;padding:0 16px;display:flex;align-items:center}
/* #19's question panel. Built on .panel but with its own budget: this
   question recurs every quarter change and has to fit the fixed 88px like
   #readout does. Ignoring .panel's own border+padding (1+12 top, 1+12
   bottom = 26px), against the longest of the four sentences in PROMPT_COPY:
     question line   28   13px font, 14px line-height, up to two lines,
                          clipped by max-height rather than left to wrap
                          further
     gap             6    the flex gap between the question and the answers
     answer row      28   12px font, 14px line-height, 6px padding top and
                          bottom, 1px border top and bottom
   26 + 28 + 6 + 28 = 88, exactly the slot budget. Unverified against the real
   sentences on a real screen; see docs/handoff.md 3. */
#prompt-panel{display:flex;flex-direction:column;gap:6px;width:100%}
#prompt-question{font-size:13px;line-height:14px;max-height:28px;overflow:hidden}
#prompt-answers{display:flex;gap:8px}
#prompt-answers button{flex:1;padding:6px 10px;font-size:12px;line-height:14px}
/* Recording is a status check worth glancing at regardless of which tab is
   open, and it used to crowd MORE's last row of buttons when it lived there. */
#recording-status{margin:0 16px 12px}
.tabs{display:flex;gap:8px;padding:0 16px}
.tab-btn{flex:1;padding:10px;border-radius:10px 10px 0 0;background:var(--panel);
border:1px solid var(--line);border-bottom:none;color:var(--dim);font-weight:700}
.tab-btn.on{color:var(--text)}
.tabpanel{padding:12px 16px}
button{font:inherit;font-weight:600;color:var(--text);background:var(--panel);
border:1px solid var(--line);border-radius:12px;padding:18px 12px;cursor:pointer;
touch-action:manipulation;-webkit-tap-highlight-color:transparent}
button:active{transform:translateY(1px)}
button:disabled{opacity:.45;cursor:not-allowed}
button:disabled:active{transform:none}
button.on{outline:2px solid var(--text)}
/* #5: a 4px dashed white ring rather than the amber inset used everywhere
   else - amber on an amber button is invisible, and this is the one ring that
   has to show on every button colour, including the fader column's own. */
button.sending{outline:4px dashed #fff;outline-offset:-4px}
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
/* The Control grid's own gap is 8px; the same here puts the confirmation the
   same distance below the row as the buttons sit from each other. */
#handoff-confirm{margin-top:8px}
/* MORE > Target level (#9): one segment per preset, wrapping if a site offers
   more than fit a row. 96 x 72 pt each, the size the UX review settled on for a
   thumb: 96px wide is the label ("-3 dB") with room either side, 72px tall
   matches the fader column's smallest button (score-reversed), so a tap here is
   as easy to land as the ones that matter more. The selected segment is solid
   and NEUTRAL, deliberately. Green and amber are the fader's direction (#5),
   and this control moves nothing: it stores the level the next open goes to,
   so it must not borrow the colour that means "up" or "down". */
#target-control{display:flex;flex-wrap:wrap;gap:8px}
#target-control button{width:96px;height:72px;font-size:16px;font-weight:700}
#target-control button.selected{background:var(--text);color:var(--bg);border-color:var(--text)}
/* Spans are a different kind of control from instants and must not look like
   them: one tap of these opens a region and the next one closes it. Not
   scoped to .grid: the fader column's buttons carry the same data attributes
   outside any grid, and must read the same way. */
button[data-kind="span"]{border-style:dashed}
/* These move the fader as well as recording why, so they must not read as more
   annotation buttons: the grid, and the fader column, are tapped without
   looking. */
button[data-action]{border-width:2px;font-weight:700}
/* Direction is never colour alone (#5): a CSS arrow says which way the fader
   is going, black-on-amber or white-on-green either way. Green for everything
   that sends it up, including READY's ride to the hold level short of target -
   `open-slow` riding in rather than snapping, or `ready` stopping short of
   target, are differences in the gesture, not the direction, that the label
   already says in words. Listed rather than matched on a prefix so a new
   action has to be given a colour and an arrow deliberately, not inherit one. */
button[data-action="open"],
button[data-action="open-slow"],
button[data-action="ready"]{border-color:var(--open);color:var(--open)}
button[data-action="open"]::before,
button[data-action="open-slow"]::before,
button[data-action="ready"]::before{content:"\\2191\\a0"}
button[data-action="release"]{border-color:var(--fade);color:var(--fade)}
button[data-action="release"]::before{content:"\\2193\\a0"}
/* Three link states, not two. An open socket that has not delivered anything
   is a connection attempt and reads as one; a silence past the box's own
   threshold is the dangerous state, because nothing else on the page looks
   wrong while it is happening. */
#link{color:#fff;display:none}
#link.connecting{display:block;background:var(--fade);color:var(--fade-text)}
#link.stale,#link.lost{display:block;background:var(--warn)}
/* Whether annotations are reaching the disk. Up for as long as it is true, and
   not dismissable: a full disk leaves everything else on the page looking fine. */
#saving{display:none}
#saving.fault{display:block;background:var(--warn);color:#fff}
#saving.warn{display:block;background:var(--fade);color:var(--fade-text)}
#saving.note{display:block;color:#ffca7a}
#wake{text-align:center;font-size:13px;color:var(--dim);padding:8px 16px}
#wake.advice{color:#ffca7a}
/* The pinned fader column (#5): full height, on the thumb edge, never
   scrolled and never rebuilt by a tab switch. Heights and order are a
   reviewed, hallway-tested list (annotations.py's FADER_COLUMN in app.js),
   not automatic - a seventh fader reason is data on an existing button. */
#fader-column{flex:0 0 280px;width:280px;display:flex;flex-direction:column;gap:12px;
padding:env(safe-area-inset-top) 10px env(safe-area-inset-bottom);
border-left:1px solid var(--line);background:var(--bg)}
#fader-top,#fader-bottom{display:contents}
#fader-column button{width:100%;flex:0 0 auto;font-size:16px}
#fader-column button[data-key="up-whistle"]{height:112px}
#fader-column button[data-key="up-drums"]{height:112px}
#fader-column button[data-key="up-slow"]{height:80px}
#fader-column button[data-key="up-ready"]{height:80px}
#fader-column button[data-key="score-reversed"]{height:72px}
#fader-column button[data-key="out"]{height:136px}
/* The readout gap: exactly 96px of room, never more asked for. The column is 592
   of buttons + 6 gaps of 12 + this = 760px, which fits a 768px-tall landscape
   iPad; growing it makes the page scroll, the failure #5 was built to remove
   (#107 put the belief row in here without changing that). Level/target/refusal
   are repeated here because the header carrying #refusal is on the far side of
   the screen from this thumb; the age of the last command (#12) rides on
   #level itself. Budget, top to bottom:
     level line     ~22.5   flex:none, one line, never wrapped. 18px at 1.2 is
                            21.6, but baseline alignment makes the flex line
                            taller than that
     error line     15      12px at 15px. NOT a duplicate: "Console unreachable"
                            is shown nowhere else on the page, so it never yields
     refusal line   15      12px at 15px. The header chip carries its full text,
                            so this is the line that yields
     column note    15      12px at 15px, the same yielding class (#107). Says why
                            the ramping buttons are grey. NEVER shown with the
                            refusal - app.js paints it only when there is none, so
                            it takes that line's place and is not a fifth row
     belief row     43.6    13px padding twice + 15.6 line + 2 border, flex:none,
                            pinned to the bottom
   Fixed parts (level + belief) are 66.1, leaving 29.9 for the two lines. Both at
   once want 30, so the worst case is 96.1 against 96: 0.1px short, taken out of
   the refusal line or the note, whichever is showing, which are the only ones
   allowed to shrink. The note and the refusal are mutually exclusive, so the
   worst case with the note is the same 96.1 and no worse. One line showing
   leaves ~14.9 spare; none, ~29.9. The protection is flex-shrink on those two
   lines, with the level line and the belief row flex:none. overflow:hidden
   on #readout is only the backstop: a deficit beyond what they can absorb
   would clip the bottom, which is where the belief row sits, so keep the
   sum above under 96 if any of these numbers change.
   The level is 18px, not the 22px it used to be, so the fade destination stays
   inside the ellipsis: during a close it reads "-3.00 dB -> -inf dB", which at
   22px is ~181px against ~183px available and would start clipping the
   destination during the two seconds it exists for. At 18px there is ~35px of
   headroom. Do not raise it back without redoing that sum. */
#readout{flex:1 1 96px;min-height:96px;display:flex;flex-direction:column;
justify-content:flex-start;overflow:hidden}
#readout .value{display:flex;align-items:baseline;flex:none;font-size:18px;line-height:1.2}
#readout #level{flex:0 1 auto;min-width:0;overflow:hidden;white-space:nowrap;
text-overflow:ellipsis}
#readout .tag{flex:none}
#readout #fader-error{color:#ff9d94;font-size:12px;line-height:15px;flex:0 0 auto;
min-height:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#col-refusal{color:#ffb4a9;font-size:12px;line-height:15px;display:none;flex:0 1 auto;
min-height:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
/* Why four of the column's buttons are grey (#107): a standing note, not a
   fault, so the dim colour rather than the refusal's. Same yielding class as
   the refusal line, and never shown with it (see the budget above). */
#col-unknown{color:var(--dim);font-size:12px;line-height:15px;display:none;flex:0 1 auto;
min-height:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
/* The two answers to "where is the fader" (#107): always here, never shown or
   hidden, never moved. The script only ever disables one in place, so a thumb
   that finds the row finds it in the same place every time. One row, no
   heading, one line of label each (so 43.6px tall), pinned to the
   bottom of the readout gap. Close now is its natural width and the ready
   report takes the rest, so its label has room without wrapping. */
#belief{display:flex;gap:8px;flex:none;margin-top:auto}
#belief button{width:auto;flex:1 1 0;min-width:0;padding:13px 6px;font-size:13px;line-height:1.2;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#belief #btn-close-now{flex:0 0 auto}
</style></head><body>
<div class="app">
<div class="left" id="left">
<header>
  <div class="headline"><div id="state">&hellip;</div><div id="pulse">--</div></div>
  <div id="why"></div>
</header>
<div id="strip">
  <div id="duty"></div>
  <div id="target-level"></div>
  <div id="link" class="connecting">Connecting to the box</div>
  <div id="saving"></div>
  <div id="refusal"></div>
  <div id="tap"></div>
</div>
<div id="prompt">
  <!-- #19: the arm / stand-down question the box raises from
       band-exits-stands, the start of halftime-exodus, or band-enters-stands.
       The accept button's label is filled in by script, per the question's
       kind; it ships empty because nothing has answered that yet. -->
  <div class="panel" id="prompt-panel" style="display:none">
    <div id="prompt-question"></div>
    <div id="prompt-answers">
      <button id="btn-prompt-dismiss">Not yet</button>
      <button id="btn-prompt-accept"></button>
    </div>
  </div>
</div>
<div class="panel" id="recording-status"><div class="label">Recording</div>
  <div class="value"><span id="rec">&mdash;</span><span class="tag" id="rec-tag">unknown</span></div>
  <div id="rec-pos" style="color:var(--dim);font-size:13px;margin-top:4px"></div></div>
<div class="tabs">
  <button id="tab-btn-main" class="tab-btn on">Main</button>
  <button id="tab-btn-more" class="tab-btn">More</button>
</div>
<div id="tab-main" class="tabpanel"></div>
<div id="tab-more" class="tabpanel" style="display:none">
  <div id="tab-more-vocabulary"></div>
  <h2>Control</h2>
  <div class="grid">
    <button id="btn-arm">Arm</button>
    <button id="btn-stand-down">Stand down</button>
    <button id="btn-record">Start recording</button>
    <button id="btn-handoff">StageMix has it</button>
  </div>
  <!-- Announce, don't surprise (CLAUDE.md principle 4): handing off is a mode
       change, so it asks before it happens rather than firing on one tap. "No"
       is `still-mine` (#12) - a pure log entry, answered from here rather than
       a button in a grid, that changes nothing. It sits directly under the
       Control row, where the button that opens it is (#108); a sibling of the
       grid rather than inside it, so its buttons do not inherit `.grid
       button`. Leaving MORE cancels it unanswered - see selectTab in app.js. -->
  <div class="panel" id="handoff-confirm" style="display:none">
    <div>Hand off to StageMix? The commanded level will read as unknown until you say where it really is.</div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button id="btn-handoff-yes">Yes, hand off</button>
      <button id="btn-handoff-no">No, still mine</button>
    </div>
  </div>
  <!-- #9: the standing target level, from fader.presets. Below the hand-off
       confirmation, never between it and its button (#108). The segments are
       built by script from the snapshot; storing a level moves nothing and a
       tap here leaves the operator on MORE (#109). -->
  <h2>Target level</h2>
  <div id="target-control"></div>
</div>
<div id="wake"></div>
</div>
<div id="fader-column">
  <div id="fader-top"></div>
  <div id="readout">
    <div class="value"><span id="level">&mdash;</span><span class="tag commanded" id="level-tag">commanded</span></div>
    <div id="fader-error"></div>
    <div id="col-refusal"></div>
    <div id="col-unknown"></div>
    <div id="belief">
      <button id="btn-close-now">Close now</button>
      <button id="btn-report-ready">It's at ready level</button>
    </div>
  </div>
  <div id="fader-bottom"></div>
</div>
</div>
<script>
__TACET_SCRIPT__
</script></body></html>
""".replace(_SCRIPT_PLACEHOLDER, _SCRIPT)
