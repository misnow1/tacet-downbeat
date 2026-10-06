"""Run the box: web UI, console control, annotation log, Reaper transport.

    tacet-serve --console-host 10.0.0.5 --dca 3 --log ~/games/<YYYY-MM-DD>.jsonl

Everything is optional except the console and the log. Without `--reaper-host`
there is no transport control and recording state reads as unknown, which is
honest rather than broken.

Most of that repeats every game. Put the site values in a config file and only
the game is left to type:

    tacet-serve --log ~/games/<YYYY-MM-DD>.jsonl

`--log` is never taken from the file. It is the one value that changes every
game, so it is typed every game.

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

To find out whether a command would start, without starting it:

    tacet-serve --log ~/games/<YYYY-MM-DD>.jsonl --check

prints the same banner, or the same refusal, and exits: zero if the box would
have started. Nothing binds and nothing is sent to the console's OSC port or to
Reaper; one ping goes to the console's address (#73). The log and queue are only
read. `--check` is a flag and never a config key, since a file that set it
would stop the box from ever starting.

The box refuses to start without room for a whole game's recording (#53): set
`capture.audio_path` and `capture.channels`, or pass `--no-disk-check`. See
`tacet.disk`.

The banner's `code` row says what commit and branch is running, and whether the
tree is dirty (#157). The log's first entry of every run, `box-started`, records
the same. git is asked once, here, before the event loop exists.

The `ping` row says whether anything answered an ICMP ping at the console's
address (#73): presence at the address, never the DM7, the port or a delivered
fader move. It is asked once here, before the loop exists, then again at every
arm and every few minutes by `tacet.reach`, which also keeps the laptop's ARP
entry for the console warm. A warning, never a refusal: the operator has the
fader.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import textwrap
import time
from pathlib import Path

from aiohttp import web as aiohttp_web

from . import config, disk, dm7, mirror, provenance, reach, reaper, taps, targets, web
from .annotations import (
    AnnotationLog,
    CorruptLogError,
    TornTail,
    find_clock_reset,
    find_open_spans,
    find_prior_anchor,
    find_torn_tail,
    lookup,
    set_aside_path,
)
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


def build(
    args: argparse.Namespace,
    code: provenance.Provenance,
    *,
    console_check: reach.Check | None = None,
) -> tuple[App, AnnotationLog, mirror.MirrorQueue | None]:
    # First, so a preset above the cap, or a default that is not one of the
    # presets, raises before the log or the queue is opened. Held here as well
    # as in the config file (#9, #139): a flag can name values the file never
    # saw.
    levels = targets.build(args.presets, args.max_target, args.default_target)
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
        hold_below_db=args.hold_below_db,
        ready_ride_seconds=args.ready_ride,
        retarget_ride_seconds=args.retarget_ride,
        stale_tap_seconds=args.stale_tap,
        target_levels=levels,
        provenance=code,
        console_check=console_check,
    )
    # The run's first entry, before anything is served (#157), and straight
    # after it what the startup ping found (#73).
    app.log_box_started()
    if console_check is not None:
        app.console_checked(console_check)
    return app, log, queue


def _watch_stopped(app: App, host: str, task: asyncio.Future[None]) -> None:
    """A dead background task is a fault in its own right (#41): say so."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        failed = reach.CHECK_STOPPED.format(error=f"{type(error).__name__}: {error}")
        app.console_checked(
            reach.Check(reach.Reach.COULD_NOT_CHECK, reach.Trigger.KEEPALIVE, host, app.now(), detail=failed)
        )


async def _run(
    args: argparse.Namespace,
    code: provenance.Provenance,
    console_check: reach.Check | None = None,
    tools: reach.Tools | None = None,
) -> None:
    app, log, queue = build(args, code, console_check=console_check)
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
    # page of traceback at whoever was standing there. Nothing was lost then,
    # because both files were fsynced per line. They are written on a thread now
    # (#41), which makes closing the log - it waits for that thread - matter more.
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

    # The ping that keeps the console's ARP entry warm, and checks at every arm
    # (#73). For the life of the box, armed or not: an operator open from
    # STANDING DOWN (#89) must not meet a cold entry after halftime. Started
    # one interval after the startup check, which already warmed it. It holds
    # no sender and writes no OSC; it waits for any fader move to end.
    watch = reach.Watch(
        args.console_host,
        tools if tools is not None else reach.find_tools(reach.platform_of(sys.platform)),
        runner=reach.AsyncioRunner(),
        busy=lambda: app.fader_moving,
        on_result=app.console_checked,
        first_due=time.monotonic() + reach.KEEPALIVE_SECONDS,
    )
    app.on_armed(lambda: watch.request(reach.Trigger.ARM))
    watch_task: asyncio.Future[None] = asyncio.ensure_future(watch.run())
    watch_task.add_done_callback(lambda task: _watch_stopped(app, args.console_host, task))

    try:
        await stop.wait()
    finally:
        # Leave the operator in control: never fade on the way out.
        loop.remove_signal_handler(signal.SIGINT)
        # First, so no child ping outlives the box.
        watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watch_task
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
#: firewall problem from the iPad (docs/box.md).
ALL_INTERFACES = web.DEFAULT_HOST
#: Not `reaper.DEFAULT_HOST`, which is the same string meaning something else
#: entirely -- where Reaper is. This one is about what a browser can open.
LOOPBACK = "127.0.0.1"

#: What the config row says when there is no config file. Pinned here because
#: docs/troubleshooting.md quotes it in the table of things that go wrong.
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


def _target_row(args: argparse.Namespace) -> str:
    """Where an open goes, the levels the page offers and the cap on them (#9).
    The configured default is said first, which is the first preset only when
    nothing names another (#139)."""
    presets = " / ".join(f"{db:.1f}" for db in args.presets)
    default = args.presets[0] if args.default_target is None else args.default_target
    return _row("target", f"{default:.1f} dB   presets {presets}   cap {args.max_target:.1f} dB")


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


#: The chip the page shows for a dirty tree. Must equal app.js's PROVENANCE_DIRTY;
#: tests/test_runbook.py holds the two together.
PAGE_DIRTY_CHIP = "Unreviewed code running"
DIRTY_WARNING = "the box is running uncommitted changes"
UNKNOWN_CODE_WARNING = "cannot tell what code is running"


def _code_warning_lines(code: provenance.Provenance | None) -> list[str]:
    """Warned, not refused (#157): a box that will not start before kickoff is
    worse than one running a fix the operator knows about. Silent is worse."""
    if code is None:
        return []
    if code.dirty:
        return [
            _rule(),
            _row("WARNING", DIRTY_WARNING),
            _note("the log records the commit, not the changes made since it"),
            _note(f'the page shows "{PAGE_DIRTY_CHIP}" while this runs'),
            _note("commit, then restart, to leave a record of what ran"),
        ]
    if code.source is provenance.Source.UNKNOWN:
        return [
            _rule(),
            _row("WARNING", UNKNOWN_CODE_WARNING),
            _note(str(code.error)),
            _note("it may include uncommitted changes; the page says so too"),
        ]
    return []


#: What the banner says when nothing answered at the console's address (#73).
#: Quoted in docs/troubleshooting.md.
NOTHING_THERE_WARNING = "nothing answered at the console address"

#: Room for a value after the label column and the rule's indent.
BANNER_VALUE_WIDTH = BANNER_WIDTH - BANNER_LABEL_WIDTH - 2


def _wrapped_row(label: str, text: str) -> list[str]:
    """A row whose value may be long: the rest hangs under it as notes."""
    first, *rest = textwrap.wrap(text, BANNER_VALUE_WIDTH) or [""]
    return [_row(label, first), *(_note(line) for line in rest)]


def _ping_lines(check: reach.Check | None) -> list[str]:
    """The `ping` row and what it does and does not prove (#73)."""
    if check is None:
        return []
    lines = _wrapped_row("ping", check.summary())
    if check.reach is reach.Reach.ANSWERED:
        lines.append(_note("something is at this address; not proof it is the DM7"))
        lines.append(_note("and not proof the port is right"))
    elif check.reach is reach.Reach.NO_ANSWER:
        lines.append(_note("the console may ignore ping, or not be there"))
        lines.append(_note("fader moves are still sent"))
    return lines


def _ping_warning_lines(check: reach.Check | None) -> list[str]:
    """Warned, not refused (#73): the console may be coming up later, and the
    operator has the fader either way."""
    if check is None or check.reach is not reach.Reach.NOTHING_THERE:
        return []
    return [
        _rule(),
        _row("WARNING", NOTHING_THERE_WARNING),
        _note(f"at {check.host}: wrong address, wrong adapter,"),
        _note("cable out, or console off"),
        _note("the box starts anyway; fader moves sent now go nowhere"),
        _note("fix it, or carry on if the console is coming up later"),
    ]


def _clock_reset_lines(last: AnnotationEntry) -> list[str]:
    """The block for a log written before this machine's clock last restarted.

    Warned rather than refused, like a reused log: nothing is lost, and a box
    that will not start before kickoff is worse. But offsets in the log are
    measured on a clock that no longer exists, and nothing downstream checks.
    """
    return [
        _rule(),
        _row("WARNING", "the clock has restarted since this log was written"),
        _note(f"its last entry was at {last.wall}; the machine has rebooted since"),
        _note("entries placed by arithmetic across the reboot will be wrong;"),
        _note("ones stamped with Reaper's playhead are not affected"),
        _note("a fresh game wants a fresh --log"),
    ]


#: Said once under the open spans when any of them has no button (#155): the
#: page offers its "End" button in MORE, not beside the buttons that start it.
RETIRED_SPAN_NOTE = "a retired one is ended from OPEN FROM AN EARLIER RUN in MORE"


def _open_span_lines(spans: list[AnnotationEntry]) -> list[str]:
    """The block for spans an earlier run left open in this log.

    Warned rather than refused: a box restarted mid-quarter resumes its own
    quarter, which is right. On a log meant to be fresh it is last game's `q4`
    still running on this game's page.
    """
    lines = [
        _rule(),
        _row("WARNING", f"this log has {len(spans)} span(s) still open from an earlier run"),
    ]
    lines.extend(_note(f"{span.label}, started {span.wall}") for span in spans)
    lines.append(_note('their buttons will read "(end)"; if this is a restart mid-game,'))
    lines.append(_note("that is right. If it is a new game, it wants a fresh --log"))
    if any(not lookup(span.event).button for span in spans):
        lines.append(_note(RETIRED_SPAN_NOTE))
    return lines


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
    clock_reset: AnnotationEntry | None = None,
    open_spans: list[AnnotationEntry] | None = None,
    space: disk.Verdict | None = None,
    code: provenance.Provenance | None = None,
    console_check: reach.Check | None = None,
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
        *([_row("code", code.summary())] if code is not None else []),
        _row("config", str(config_path) if config_path else NO_CONFIG),
        _rule(),
        _row("console", f"{args.console_host}:{args.console_port}   DCA {args.dca}"),
        _note("commanded, never confirmed - the DM7's OSC is write-only"),
        *_ping_lines(console_check),
    ]
    if args.quantized:
        lines.append(_note("fader values snapped to Table 1"))
    lines.append(_row("fade", f"{args.fade:.1f}s close, fast open, {args.slow_open:.1f}s ride-in"))
    lines.append(_note(f"a fader tap arriving over {args.stale_tap:.1f}s late is not executed"))
    lines.append(_target_row(args))

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
    if space is not None:
        lines.append(_row("disk", space.summary))
        lines.extend(_note(note) for note in space.notes)

    lines.extend(_page_lines(args.listen, args.http_port))
    if prior_anchor is not None:
        lines.extend(_prior_anchor_lines(prior_anchor))
    if clock_reset is not None:
        lines.extend(_clock_reset_lines(clock_reset))
    if open_spans:
        lines.extend(_open_span_lines(open_spans))
    if torn_log is not None:
        lines.extend(_torn_lines("log", args.log, torn_log, keeps_entries=True))
    if torn_queue is not None and args.queue:
        lines.extend(_torn_lines("queue", args.queue, torn_queue, keeps_entries=False))
    lines.extend(_code_warning_lines(code))
    lines.extend(_ping_warning_lines(console_check))
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
    "queue": "capture.queue",
    "audio_path": "capture.audio_path",
    "channels": "capture.channels",
    "game_hours": "capture.game_hours",
    "fade": "fader.fade_seconds",
    "slow_open": "fader.slow_open_seconds",
    "hold_below_db": "fader.hold_below_db",
    "ready_ride": "fader.ready_ride_seconds",
    "retarget_ride": "fader.retarget_ride_seconds",
    "stale_tap": "fader.stale_tap_seconds",
    "presets": "fader.presets",
    "default_target": "fader.default_target_db",
    "max_target": "fader.max_target_db",
    "listen": "ui.listen",
    "http_port": "ui.port",
}

#: Needed before the box can run, from wherever. Not `required=True`, because
#: argparse enforces that before the config file has been read; `config.require`
#: does it afterwards and names the config key too.
REQUIRED = ("console_host", "dca")

#: What the box says without `--log`. Required separately from `REQUIRED`
#: because no config file can supply it (#20), so the generic message - which
#: offers a config key - would send someone looking for one.
LOG_REQUIRED = "--log is required on the command line, a fresh one each game: --log ~/games/<YYYY-MM-DD>.jsonl"

#: Named when the three target keys disagree, so the message covers every
#: way the value could have arrived - the file, the flag, or neither.
TARGET_REFUSAL = (
    "--presets / --max-target / --default-target (fader.presets, fader.max_target_db, fader.default_target_db)"
)


def parser() -> argparse.ArgumentParser:
    # Raw, or argparse reflows the docstring into one paragraph and the example
    # command lines -- the part someone actually came to --help to copy -- come
    # out wrapped mid-flag.
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    config.add_config_argument(p)
    p.add_argument("--console-host", help="the DM7's For Mixer Control IP")
    p.add_argument("--console-port", type=config.port, default=dm7.DEFAULT_PORT)
    p.add_argument("--dca", type=int, help="the band DCA number")
    p.add_argument("--log", type=Path, help="annotation log (JSONL); required, and never taken from the config")
    p.add_argument("--queue", type=Path, help="mirror queue for the Reaper script")
    p.add_argument("--audio-path", type=Path, help="where Reaper records; checked for room for a whole game")
    p.add_argument("--channels", type=int, help="tracks this game records, for the disk check")
    p.add_argument("--game-hours", type=float, default=disk.DEFAULT_GAME_HOURS, help="game length to leave room for")
    # Flag only, like --check: a file that turned a safety check off would be
    # wrong every game after it was written.
    p.add_argument(
        disk.OVERRIDE_FLAG,
        dest="no_disk_check",
        action="store_true",
        help="start even without room for a whole game, or with nowhere to check",
    )
    p.add_argument("--reaper-host", help="omit to run without transport control")
    p.add_argument("--reaper-port", type=config.port, default=reaper.DEFAULT_SEND_PORT)
    p.add_argument("--reaper-feedback-port", type=config.port, default=reaper.DEFAULT_RECEIVE_PORT)
    p.add_argument("--listen", default=web.DEFAULT_HOST)
    p.add_argument("--http-port", type=config.port, default=web.DEFAULT_PORT)
    p.add_argument("--fade", type=float, default=dm7.DEFAULT_FADE_SECONDS)
    p.add_argument(
        "--slow-open",
        type=float,
        default=dm7.DEFAULT_SLOW_OPEN_SECONDS,
        metavar="SECONDS",
        help="ride-in for the 'up slow' button, used when the start was missed",
    )
    p.add_argument(
        "--hold-below-db",
        type=float,
        default=dm7.DEFAULT_HOLD_BELOW_DB,
        metavar="DB",
        help="how far below target the READY hold level sits (#6)",
    )
    p.add_argument(
        "--ready-ride",
        type=float,
        default=dm7.DEFAULT_READY_RIDE_SECONDS,
        metavar="SECONDS",
        help="ride from idle to the READY hold level",
    )
    p.add_argument(
        "--retarget-ride",
        type=float,
        default=dm7.DEFAULT_RETARGET_RIDE_SECONDS,
        metavar="SECONDS",
        help="ride to a new target while the fader is up (#128)",
    )
    p.add_argument(
        "--stale-tap",
        type=float,
        default=taps.DEFAULT_STALE_TAP_SECONDS,
        metavar="SECONDS",
        help="a fader tap that arrives later than this is logged and not executed",
    )
    p.add_argument(
        "--presets",
        type=config.db_list,
        default=targets.DEFAULT_PRESETS_DB,
        metavar="DB,DB,...",
        help=(
            "target levels the page offers, the first the default unless --default-target names "
            "another (--presets=-3,-6 if it starts with a minus)"
        ),
    )
    p.add_argument(
        "--default-target",
        type=float,
        default=None,
        metavar="DB",
        help="the preset every open goes to until the operator picks another (default: the first preset)",
    )
    p.add_argument(
        "--max-target",
        type=float,
        default=targets.DEFAULT_MAX_TARGET_DB,
        metavar="DB",
        help="cap: a preset above this refuses to start; set by the on-site ring-out",
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
    # Not in CONFIG_MAPPING, and never: it chooses what the tool does, like
    # verify_dm7's --fade. A config file that set it would never start the box.
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "print the banner, or the refusal, and exit without starting: zero if it would start. "
            "Nothing binds and nothing is sent to the console's OSC port or to Reaper; "
            "one ping goes to the console's address"
        ),
    )
    return p


def _disk_plan(args: argparse.Namespace) -> disk.Plan:
    return disk.Plan(
        audio_path=args.audio_path,
        channels=args.channels,
        hours=args.game_hours,
        log_path=args.log,
        queue_path=args.queue,
        override=args.no_disk_check,
    )


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args, config_path = config.resolve_or_exit(p, CONFIG_MAPPING, argv)
    config.require(p, args, CONFIG_MAPPING, *REQUIRED)
    if args.log is None:
        p.error(LOG_REQUIRED)
    # A preset above the cap, or a default that is not one of the presets, is
    # refused here, like any other bad flag, rather than as a traceback from
    # `build` after the banner has said all is well. This is also the only
    # place a file's default and a flag's preset list can be checked against
    # each other, since config.resolve validates each source on its own.
    try:
        targets.build(args.presets, args.max_target, args.default_target)
    except targets.TargetError as exc:
        p.error(f"{TARGET_REFUSAL}: {exc}")
    # Said out loud, because a box configured from a file has no visible
    # command line: "why is it driving DCA 3" otherwise has no answer on the
    # day. Printed before anything binds, so it survives a failure to start.
    # Read before the log is opened for appending, so "already contains a
    # recording" still means *before this run*, and a torn end is reported as
    # the last run left it rather than as the repair on open leaves it.
    #
    # A log that cannot be read stops the box here, before anything binds, as
    # the command-line error it is - the fix is a different --log - rather than
    # as a traceback from somewhere inside the startup.
    try:
        prior_anchor = find_prior_anchor(args.log)
        clock_reset = find_clock_reset(args.log, now=time.monotonic())
        open_spans = find_open_spans(args.log)
    except CorruptLogError as exc:
        p.error(f"--log {args.log} cannot be read: {exc}. Nothing in it was changed.")
    # Refused like any other command-line error, before the banner (#53).
    space = disk.check(_disk_plan(args))
    if space.refusal is not None:
        p.error(space.refusal)
    # What code this is (#157). Asked here, once, before the loop exists (#41), and
    # after every refusal so a box that will not start never waits on git.
    # Read-only, so --check asks too and prints what a start prints.
    code = provenance.probe()
    # Whether anything answers at the console's address (#73). Asked here, once,
    # before the loop exists, and after every refusal so a box that will not
    # start never pings. One ping and one table read; no OSC. --check asks too:
    # a typo'd address is the cheapest thing to catch before the day.
    tools = reach.find_tools(reach.platform_of(sys.platform))
    console_check = reach.probe(args.console_host, tools)
    banner = startup_lines(
        args,
        config_path,
        prior_anchor,
        torn_log=find_torn_tail(args.log),
        torn_queue=find_torn_tail(args.queue) if args.queue else None,
        clock_reset=clock_reset,
        open_spans=open_spans,
        space=space,
        code=code,
        console_check=console_check,
    )
    for line in banner:
        print(line)
    # Everything above only reads, which is what makes this the whole of
    # --check (#79): the same refusals, the same banner, and nothing opened.
    if args.check:
        return 0
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args, code, console_check, tools))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
