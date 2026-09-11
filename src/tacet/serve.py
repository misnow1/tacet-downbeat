"""Run the box: web UI, console control, annotation log, Reaper transport.

    tacet-serve --console-host 10.0.0.5 --dca 3 --log ~/games/2026-09-13.jsonl

Everything is optional except the console. Without `--reaper-host` there is no
transport control and recording state reads as unknown, which is honest rather
than broken.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from pathlib import Path

from aiohttp import web as aiohttp_web

from . import config, dm7, mirror, reaper, web
from .annotations import AnnotationLog
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
    app = App(console=console, log=log, recorder=recorder, fade_seconds=args.fade)
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
    print(f"UI on http://{args.listen}:{args.http_port}  (log: {log.path})")
    print("the fader is commanded, never confirmed; the DM7 cannot answer")

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
    "listen": "ui.listen",
    "http_port": "ui.port",
}

#: Needed before the box can run, from wherever. Not `required=True`, because
#: argparse enforces that before the config file has been read; `config.require`
#: does it afterwards and names the config key too.
REQUIRED = ("console_host", "dca", "log")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
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
    # Said out loud: a config picked up from the working directory is otherwise
    # invisible, and "why is it driving DCA 3" has no answer on the day.
    print(f"config: {config_path}" if config_path else "config: none (flags only)")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
