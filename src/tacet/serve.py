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
from pathlib import Path

from aiohttp import web as aiohttp_web

from . import dm7, mirror, reaper, web
from .annotations import AnnotationLog
from .app import App


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

    try:
        await asyncio.Event().wait()
    finally:
        # Leave the operator in control: never fade on the way out.
        await runner.cleanup()
        if transport is not None:
            transport.close()
        log.close()
        if queue is not None:
            queue.close()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--console-host", required=True, help="the DM7's For Mixer Control IP")
    p.add_argument("--console-port", type=int, default=dm7.DEFAULT_PORT)
    p.add_argument("--dca", type=int, required=True, help="the band DCA number")
    p.add_argument("--log", type=Path, required=True, help="annotation log (JSONL)")
    p.add_argument("--queue", type=Path, help="mirror queue for the Reaper script")
    p.add_argument("--reaper-host", help="omit to run without transport control")
    p.add_argument("--reaper-port", type=int, default=reaper.DEFAULT_SEND_PORT)
    p.add_argument("--reaper-feedback-port", type=int, default=reaper.DEFAULT_RECEIVE_PORT)
    p.add_argument("--listen", default=web.DEFAULT_HOST)
    p.add_argument("--http-port", type=int, default=web.DEFAULT_PORT)
    p.add_argument("--fade", type=float, default=dm7.DEFAULT_FADE_SECONDS)
    p.add_argument(
        "--quantized",
        action="store_true",
        help="snap fader values to Table 1; set this if the console rounds",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
