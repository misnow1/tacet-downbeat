"""Run the box: web UI, console control, annotation log, Reaper transport.

    tacet-serve --console-host 10.0.0.5 --dca 3 --log ~/games/2026-09-13.jsonl

Everything is optional except the console. Without `--reaper-host` there is no
transport control and recording state reads as unknown, which is honest rather
than broken.

Most of that repeats every game. Put the site values in a config file and only
the game is left to type:

    tacet-serve --log ~/games/2026-09-13.jsonl

A `tacet.toml` in the working directory is picked up on its own; otherwise name
one with `--config PATH` or `$TACET_CONFIG`, and `~/.config/tacet/tacet.toml`
is the last place looked. The file is optional and every flag still wins over
it -- `--dca 4` drives DCA 4 whatever the file says. `tacet.toml.example` in the
repo root lists every key; `tacet.config` documents the rules; docs/gameday.md
is the runbook.

Because those values no longer have to appear on the command line, the box
prints a banner at startup naming the console, the DCA, Reaper, the log and the
queue, followed by the pre-kickoff checklist. The `config` row in it says which
file was used, or `none (flags only)`. Everything in the banner is what the box
was told, never what it has confirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from pathlib import Path

from aiohttp import web as aiohttp_web

from . import config, dm7, mirror, reaper, web
from .annotations import AnnotationLog, TornTail, find_prior_anchor, find_torn_tail, set_aside_path
from .annotations import Entry as AnnotationEntry
from .app import App

#: How long a Ctrl-C stays armed, waiting for the one that confirms it.
#:
#: A confirmation that never expires is its own trap across a three-hour game: a
#: stray Ctrl-C in the first quarter must not combine with an unrelated one in
#: the fourth to end the capture. After this, the next press warns again.
STOP_CONFIRM_SECONDS = 5.0

#: What the first Ctrl-C says. The two things worth knowing are both things this
#: does *not* do, because both are easy to assume it does.
STOP_WARNING = f"""
Stopping the box does not stop the recording. Reaper keeps rolling and is
stopped in Reaper, deliberately (design.md 5.9).

It does not move the fader either. The console keeps whatever level it was last
commanded, and the operator has the iPad.

Press Ctrl-C again within {STOP_CONFIRM_SECONDS:.0f}s to stop.
"""


def confirms_stop(pressed_at: float, armed_at: float | None, *, window: float = STOP_CONFIRM_SECONDS) -> bool:
    """Whether this Ctrl-C is the second of a pair, and so means it.

    Pure, so the rule is testable without a signal, a clock or a subprocess.
    """
    return armed_at is not None and pressed_at - armed_at <= window


class _Feedback(asyncio.DatagramProtocol):
    """Reaper's OSC feedback. Unlike the console, this link talks back."""

    def __init__(self, app: App) -> None:
        self._app = app

    def datagram_received(self, data: bytes, _addr: object) -> None:
        self._app.handle_recorder_packet(data)


def build(args: argparse.Namespace) -> tuple[App, AnnotationLog, mirror.MirrorQueue | None]:
    queue = mirror.MirrorQueue(args.queue).open() if args.queue else None
    log = AnnotationLog(args.log, mirror=queue).open()
    console = dm7.Dm7Client(
        args.console_host,
        args.console_port,
        dca=args.dca,
        quantized=args.quantized,
    )
    recorder = reaper.ReaperClient(args.reaper_host, args.reaper_port) if args.reaper_host else None
    app = App(
        console=console,
        log=log,
        recorder=recorder,
        fade_seconds=args.fade,
        slow_open_seconds=args.slow_open,
    )
    return app, log, queue


async def _run(args: argparse.Namespace) -> None:
    app, log, queue = build(args)
    server = web.create_app(app)

    loop = asyncio.get_running_loop()
    transport = None
    if args.reaper_host:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Feedback(app), local_addr=(args.listen, args.reaper_feedback_port)
        )
        print(f"listening for Reaper feedback on {args.listen}:{args.reaper_feedback_port}")

    runner = aiohttp_web.AppRunner(server)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, args.listen, args.http_port)
    await site.start()
    # The banner already said where the page will be and what the fader can and
    # cannot tell you. This line is the different fact that it actually bound.
    print(f"serving on {args.listen}:{args.http_port} - ready")

    # Handled rather than left to KeyboardInterrupt. The interrupt used to
    # arrive inside `runner.cleanup()` - which waits on websocket handlers, so
    # any connected browser made that the common case - and took out the rest of
    # the shutdown with it, closing neither the log nor the queue and printing a
    # page of traceback at whoever was standing there. Nothing was lost, because
    # both fsync per line, but that was the earlier decision saving this one.
    stop = asyncio.Event()
    armed: float | None = None

    def on_interrupt() -> None:
        nonlocal armed
        pressed = loop.time()
        if confirms_stop(pressed, armed):
            print("stopping")
            stop.set()
        else:
            armed = pressed
            print(STOP_WARNING)

    loop.add_signal_handler(signal.SIGINT, on_interrupt)

    try:
        await stop.wait()
    finally:
        # Leave the operator in control: never fade on the way out.
        loop.remove_signal_handler(signal.SIGINT)
        # Nested, so a cleanup that fails cannot skip the ones after it. That is
        # exactly what the interrupt used to do.
        try:
            await runner.cleanup()
        finally:
            if transport is not None:
                transport.close()
            log.close()
            if queue is not None:
                queue.close()


#: Width of the startup banner's rules. Wide enough for a long path plus its
#: explanation, narrow enough for a terminal on a shelf in a press box.
BANNER_WIDTH = 74

#: Column the values line up in, so the labels read as a column and not as prose.
BANNER_LABEL_WIDTH = 12

#: Indent for the numbered checklist under the rule.
BANNER_STEP_INDENT = 4

#: Bind addresses the banner has something to say about. `0.0.0.0` is not an
#: address anyone can open, and `127.0.0.1` is the one that looks like a
#: firewall problem from the iPad (docs/gameday.md).
ALL_INTERFACES = web.DEFAULT_HOST
#: Not `reaper.DEFAULT_HOST`, which is the same string meaning something else
#: entirely -- where Reaper is. This one is about what a browser can open.
LOOPBACK = "127.0.0.1"

#: What the config row says when there is no config file. Pinned here because
#: docs/gameday.md quotes it in the table of things that go wrong.
NO_CONFIG = "none (flags only)"


def _rule(character: str = "-") -> str:
    return character * BANNER_WIDTH


def _row(label: str, value: str) -> str:
    return f"  {label:<{BANNER_LABEL_WIDTH}}{value}"


def _note(text: str) -> str:
    """A continuation under a row: a caveat about the value, not a value."""
    return _row("", text)


def _page_lines(listen: str, port: int) -> list[str]:
    """How to reach the page, and the two bind addresses worth a warning.

    `0.0.0.0` is a wildcard rather than somewhere to point a browser, so
    printing it as a URL sends the operator to a dead link.
    """
    if listen == ALL_INTERFACES:
        return [
            _row("page", f"http://<this box>:{port}"),
            _note(f"bound to {ALL_INTERFACES}; use the box's address on the VLAN"),
        ]
    if listen == LOOPBACK:
        return [
            _row("page", f"http://{listen}:{port}"),
            _note("this machine only - the iPad cannot reach it, which looks"),
            _note("exactly like a firewall problem and is not one"),
        ]
    return [_row("page", f"http://{listen}:{port}")]


def _checklist(args: argparse.Namespace) -> list[str]:
    """The things that are already true by the time this runs, or should be.

    Order follows docs/gameday.md: Reaper, then the script, then the box. The
    box is what is printing this, so every line here is a thing to confirm
    rather than a thing to go and do.
    """
    steps: list[str] = []
    if args.reaper_host:
        # The ports this box will actually use, not the documented defaults:
        # if they have been changed, the defaults are the wrong thing to check.
        steps.append(
            f"Reaper up, OSC device on: listen {args.reaper_port}, device {args.reaper_feedback_port}, feedback on"
        )
    if args.queue:
        steps.append("tacet_mirror.lua running in Reaper, watching exactly this queue:")
        steps.append(f"  {args.queue}")
    # A step nobody can carry out is worse than a shorter list, so the page step
    # only promises what is actually wired up. The rows above already say what
    # is missing; repeating it as a numbered step would be saying it twice.
    if args.reaper_host and args.queue:
        steps.append("open the page, tap Start recording, confirm a marker lands")
    elif args.reaper_host:
        steps.append("open the page and tap Start recording")
    else:
        steps.append("open the page")
    steps.append("arm when the band is in the stands")

    lines = ["  before kickoff"]
    number = 0
    pad = " " * BANNER_STEP_INDENT
    for step in steps:
        # A continuation line (the queue path) hangs under its step rather than
        # taking a number of its own.
        if step.startswith("  "):
            lines.append(f"{pad}   {step.strip()}")
            continue
        number += 1
        lines.append(f"{pad}{number}  {step}")
    return lines


def _prior_anchor_lines(prior: AnnotationEntry) -> list[str]:
    """The loud block for a log that already holds a recording.

    Appending is not a fault -- the log is append-only and nothing is lost -- so
    this warns rather than refusing. A box that will not start twenty minutes
    before kickoff is worse than a log that needs splitting afterwards, and the
    operator is the supervisor here, not the fallback.

    What it must not do is stay quiet. `markers.find_anchor` takes the *first*
    recording in a log, so today's entries would be positioned against that one:
    positive, plausible, and wrong by however long ago it was.
    """
    return [
        _rule(),
        _row("WARNING", "this log already contains a recording"),
        _note(f"the earlier one started {prior.wall}"),
        _note("markers derived from this log anchor to THAT recording, so"),
        _note("today's entries would be placed wrong, and nothing downstream"),
        _note("would report it"),
        _note("a fresh game wants a fresh --log; carry on only if you meant"),
        _note("to append to this one"),
    ]


def _torn_lines(name: str, path: Path | str, torn: TornTail, *, keeps_entries: bool) -> list[str]:
    """The block for a file whose last run ended mid-write.

    Repaired on open rather than refused: a box that will not start is worse,
    and nothing is thrown away. Said out loud because the fragment was the last
    thing written before a crash, which is worth knowing about on its own.
    """
    lines = [
        _rule(),
        _row("WARNING", f"{name} ends in a torn write ({len(torn.tail)} bytes)"),
        _note("the last run stopped mid-write: a crash, a kill, or a full disk"),
    ]
    if keeps_entries and torn.is_entry():
        lines.append(_note("it is a whole entry missing its newline; it is kept"))
    else:
        lines.append(_note(f"it is moved to {set_aside_path(path)}"))
        lines.append(_note("everything before it is intact"))
    return lines


def startup_lines(
    args: argparse.Namespace,
    config_path: Path | None,
    prior_anchor: AnnotationEntry | None = None,
    *,
    torn_log: TornTail | None = None,
    torn_queue: TornTail | None = None,
) -> list[str]:
    """The banner, as a list of lines. Pure, so the wording is testable.

    Exists because the values no longer have to appear on the command line. A
    box configured from a file is a box whose settings are invisible at the
    moment they matter most, so it says them out loud instead.

    Everything here is what the box was *told*. None of it is confirmed: the
    console cannot answer at all, and Reaper has not been asked yet.
    """
    lines = [
        _rule("="),
        _row("tacet", "band DCA - Phase 0/1, the detector drives nothing"),
        _row("config", str(config_path) if config_path else NO_CONFIG),
        _rule(),
        _row("console", f"{args.console_host}:{args.console_port}   DCA {args.dca}"),
        _note("commanded, never confirmed - the DM7's OSC is write-only"),
    ]
    if args.quantized:
        lines.append(_note("fader values snapped to Table 1"))
    lines.append(_row("fade", f"{args.fade:.1f}s close, fast open, {args.slow_open:.1f}s ride-in"))

    if args.reaper_host:
        lines.append(
            _row(
                "reaper",
                f"{args.reaper_host}   send {args.reaper_port}   feedback {args.reaper_feedback_port}",
            )
        )
    else:
        lines.append(_row("reaper", "not configured"))
        lines.append(_note("no transport control; recording state stays unknown"))

    lines.append(_row("log", str(args.log)))
    if args.queue:
        lines.append(_row("queue", str(args.queue)))
    else:
        lines.append(_row("queue", "not set"))
        lines.append(_note("annotations are logged, but no markers reach Reaper"))

    lines.extend(_page_lines(args.listen, args.http_port))
    if prior_anchor is not None:
        lines.extend(_prior_anchor_lines(prior_anchor))
    if torn_log is not None:
        lines.extend(_torn_lines("log", args.log, torn_log, keeps_entries=True))
    if torn_queue is not None and args.queue:
        lines.extend(_torn_lines("queue", args.queue, torn_queue, keeps_entries=False))
    lines.append(_rule())
    lines.extend(_checklist(args))
    lines.append(_rule("="))
    return lines


#: Which config keys stand in for which flags. The whole of this tool's half of
#: the config file; `tests/test_config.py` checks it against the schema.
CONFIG_MAPPING = {
    "console_host": "console.host",
    "console_port": "console.port",
    "dca": "console.dca",
    "quantized": "console.quantized",
    "reaper_host": "reaper.host",
    "reaper_port": "reaper.send_port",
    "reaper_feedback_port": "reaper.receive_port",
    "log": "capture.log",
    "queue": "capture.queue",
    "fade": "fader.fade_seconds",
    "slow_open": "fader.slow_open_seconds",
    "listen": "ui.listen",
    "http_port": "ui.port",
}

#: Needed before the box can run, from wherever. Not `required=True`, because
#: argparse enforces that before the config file has been read; `config.require`
#: does it afterwards and names the config key too.
REQUIRED = ("console_host", "dca", "log")


def parser() -> argparse.ArgumentParser:
    # Raw, or argparse reflows the docstring into one paragraph and the example
    # command lines -- the part someone actually came to --help to copy -- come
    # out wrapped mid-flag.
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    config.add_config_argument(p)
    p.add_argument("--console-host", help="the DM7's For Mixer Control IP")
    p.add_argument("--console-port", type=int, default=dm7.DEFAULT_PORT)
    p.add_argument("--dca", type=int, help="the band DCA number")
    p.add_argument("--log", type=Path, help="annotation log (JSONL)")
    p.add_argument("--queue", type=Path, help="mirror queue for the Reaper script")
    p.add_argument("--reaper-host", help="omit to run without transport control")
    p.add_argument("--reaper-port", type=int, default=reaper.DEFAULT_SEND_PORT)
    p.add_argument("--reaper-feedback-port", type=int, default=reaper.DEFAULT_RECEIVE_PORT)
    p.add_argument("--listen", default=web.DEFAULT_HOST)
    p.add_argument("--http-port", type=int, default=web.DEFAULT_PORT)
    p.add_argument("--fade", type=float, default=dm7.DEFAULT_FADE_SECONDS)
    p.add_argument(
        "--slow-open",
        type=float,
        default=dm7.DEFAULT_SLOW_OPEN_SECONDS,
        metavar="SECONDS",
        help="ride-in for the 'up slow' button, used when the start was missed",
    )
    # BooleanOptionalAction, not store_true: a config file that sets
    # quantized = true has to be refusable from the command line, or the
    # precedence rule is a lie for this one flag.
    p.add_argument(
        "--quantized",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="snap fader values to Table 1; set this if the console rounds",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args, config_path = config.resolve_or_exit(p, CONFIG_MAPPING, argv)
    config.require(p, args, CONFIG_MAPPING, *REQUIRED)
    # Said out loud, because a box configured from a file has no visible
    # command line: "why is it driving DCA 3" otherwise has no answer on the
    # day. Printed before anything binds, so it survives a failure to start.
    # Read before the log is opened for appending, so "already contains a
    # recording" still means *before this run*, and a torn end is reported as
    # the last run left it rather than as the repair on open leaves it.
    banner = startup_lines(
        args,
        config_path,
        find_prior_anchor(args.log),
        torn_log=find_torn_tail(args.log),
        torn_queue=find_torn_tail(args.queue) if args.queue else None,
    )
    for line in banner:
        print(line)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
