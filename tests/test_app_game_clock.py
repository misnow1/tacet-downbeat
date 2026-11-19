"""The game clock inside the App (#235): what it logs, what it shows, and what it cannot do.

The reader is context for the operator and never a gate, so half of what is
asserted here is an absence: no packet to the console, no machine change, and
no way for a reader that throws to stop the rest of the box.
"""

import ast
import contextlib
import inspect
import io
import json
import textwrap

from tacet import annotations as ann
from tacet import app as tacet_app
from tacet import faults, state
from tacet import game_clock as gc

from . import rtd_frames as real
from .test_app import AppTestCase

SOURCE = real.GAME_5_SOURCE
OTHER_SOURCE = "192.168.0.26"
START = 5000.0
PORT = 21000


class Raising(gc.Reader):
    """A reader with a bug in it: raises while `broken`, as the real one is built never to."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = True
        self.forgotten = 0

    def feed(self, datagram):
        if self.broken:
            raise RuntimeError("bug in decoding")
        return super().feed(datagram)

    def forget_display(self) -> None:
        self.forgotten += 1
        super().forget_display()


class BrokenView(gc.Reader):
    def view(self, *, now: float) -> gc.ClockView:
        raise ValueError("bad view")


class TestGameClockInTheApp(AppTestCase):
    def make(self, reader=None, **kwargs):
        self.t = [START]
        app = self.build(monotonic=lambda: self.t[0], **kwargs)
        if reader is not None:
            app._game_clock = reader
        return app

    def at(self, seconds: float) -> None:
        self.t[0] = START + seconds

    def feed(self, app, payload: bytes, *, at: float, source: str = SOURCE) -> None:
        self.at(at)
        app.rtd_received(payload, source)

    def named(self, *keys):
        return [e for e in self.entries() if e.event in keys]

    # -- display only -----------------------------------------------------

    async def test_rtd_alone_sends_nothing_to_the_console(self):
        app = self.make()
        await app.arm()
        machine = app.machine
        fader = json.dumps(app.snapshot()["fader"], sort_keys=True)
        sent = list(self.console_sender.packets)
        to_reaper = list(self.reaper_sender.packets)
        for _number, offset, payload in real.GAME_5_KICKOFF + real.GAME_5_HALFTIME:
            self.feed(app, payload, at=offset)
        for junk in (b"", b"garbage", real.G5_14632[:100], b"\x16" * 50, b"x" * 2000):
            self.feed(app, junk, at=200)
        self.at(300)
        app.rtd_tick()
        app.rtd_failed("socket lost")
        self.assertEqual(self.console_sender.packets, sent)
        self.assertEqual(self.reaper_sender.packets, to_reaper)
        self.assertIs(app.machine, machine)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(json.dumps(app.snapshot()["fader"], sort_keys=True), fader)

    async def test_the_snapshot_carries_the_clock_and_a_box_without_rtd_says_never(self):
        app = self.make()
        self.assertEqual(app.snapshot()["game_clock"]["status"], "never")
        self.feed(app, real.G5_14625, at=1)
        block = app.snapshot()["game_clock"]
        self.assertEqual((block["status"], block["clock"], block["running"]), ("live", "0:00", False))
        self.assertEqual(block["source"], SOURCE)

    # -- the defences -----------------------------------------------------

    async def test_reader_exception_latches_rtd_reader_once_and_the_next_datagram_is_read(self):
        reader = Raising()
        app = self.make(reader)
        heard = []
        app.on_fault(lambda change, fault, present: heard.append((change, fault.kind)))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            for i in range(5):
                self.feed(app, real.G5_14625, at=i)
        (fault,) = app.snapshot()["faults"]
        self.assertEqual(fault["kind"], "rtd-reader")
        self.assertIn("bug in decoding", fault["message"])
        self.assertEqual(heard, [(faults.Change.RAISED, faults.FaultKind.RTD_READER)])
        self.assertEqual(len(self.named(ann.FAULT_RAISED)), 1)
        self.assertEqual(reader.forgotten, 5)
        # The traceback is printed for the first error of the latch only.
        self.assertEqual(errors.getvalue().count("Traceback"), 1)
        # The bug goes away: the next datagram is read as normal and the clock shows.
        reader.broken = False
        self.feed(app, real.G5_14625, at=6)
        block = app.snapshot()["game_clock"]
        self.assertEqual((block["status"], block["clock"]), ("live", "0:00"))
        # Still latched until the operator clears it, and clearing says it is no longer present.
        self.assertEqual(len(app.snapshot()["faults"]), 1)
        await app.clear_fault(fault["id"])
        (cleared,) = self.named(ann.FAULT_CLEARED)
        self.assertFalse(cleared.data["still_present"])
        self.assertEqual(app.snapshot()["faults"], [])

    async def test_a_reader_still_failing_says_so_on_clearing(self):
        app = self.make(Raising())
        with contextlib.redirect_stderr(io.StringIO()):
            self.feed(app, real.G5_14625, at=1)
        (fault,) = app.snapshot()["faults"]
        await app.clear_fault(fault["id"])
        (cleared,) = self.named(ann.FAULT_CLEARED)
        self.assertTrue(cleared.data["still_present"])

    async def test_a_reader_that_raises_does_not_stop_the_fader(self):
        app = self.make(Raising())
        await app.arm()
        with contextlib.redirect_stderr(io.StringIO()):
            self.feed(app, real.G5_14625, at=1)
        await app.trigger()
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def test_snapshot_survives_a_view_that_raises(self):
        app = self.make(BrokenView())
        block = app.snapshot()["game_clock"]
        self.assertEqual(block["status"], "down")
        self.assertIn("bad view", block["detail"])
        self.assertIsNone(block["clock"])
        # And what every fader POST returns still comes back, after a move.
        await app.arm()
        await app.trigger()
        self.assertEqual(app.snapshot()["state"], "open")
        self.assertEqual(app.snapshot()["game_clock"]["status"], "down")

    async def test_the_tick_and_failure_paths_catch_too(self):
        class BadPoll(gc.Reader):
            def poll(self, *, now: float):
                raise RuntimeError("poll bug")

            def fail(self, detail: str, *, now: float):
                raise RuntimeError("fail bug")

        app = self.make(BadPoll())
        with contextlib.redirect_stderr(io.StringIO()):
            app.rtd_tick()
            app.rtd_failed("x")
        (fault,) = app.snapshot()["faults"]
        self.assertEqual(fault["kind"], "rtd-reader")

    async def test_an_error_while_latching_does_not_escape(self):
        class Worst(Raising):
            def forget_display(self) -> None:
                raise RuntimeError("cannot even reset")

        app = self.make(Worst())
        with contextlib.redirect_stderr(io.StringIO()):
            self.feed(app, real.G5_14625, at=1)
            self.feed(app, real.G5_14625, at=2)

    # -- the log ----------------------------------------------------------

    async def test_status_and_source_changes_are_logged_once(self):
        app = self.make()
        self.feed(app, real.G5_14625, at=1)
        for i in range(2, 6):
            self.feed(app, real.TIME_OF_DAY, at=i)
        (live,) = self.named(ann.RTD_STATUS)
        self.assertEqual(live.data["status"], "live")
        self.assertEqual(live.data["previous"], "never")
        self.assertEqual(live.data["source"], SOURCE)
        self.assertEqual(live.data["port"], PORT)
        self.assertEqual(live.data["datagrams"], 1)
        self.assertEqual(self.named(ann.RTD_SOURCE), [])
        # A different source, a long time later.
        self.feed(app, real.TIME_OF_DAY, at=100, source=OTHER_SOURCE)
        self.feed(app, real.TIME_OF_DAY, at=101, source=OTHER_SOURCE)
        statuses = [e.data["status"] for e in self.named(ann.RTD_STATUS)]
        self.assertEqual(statuses, ["live", "stale", "live"])
        (moved,) = self.named(ann.RTD_SOURCE)
        self.assertEqual((moved.data["old"], moved.data["new"]), (SOURCE, OTHER_SOURCE))

    async def test_tick_logs_stale_without_a_packet(self):
        app = self.make()
        self.feed(app, real.G5_14625, at=1)
        heard: list[gc.Transition] = []
        app.on_rtd(heard.append)
        self.at(2)
        app.rtd_tick()
        self.assertEqual(heard, [])
        self.at(1 + gc.STALE_SECONDS)
        app.rtd_tick()
        app.rtd_tick()
        (edge,) = heard
        assert isinstance(edge, gc.StatusChanged)
        self.assertEqual(edge.view.status, gc.Status.STALE)
        stale = self.named(ann.RTD_STATUS)[-1]
        self.assertEqual(stale.data["status"], "stale")
        self.assertEqual(app.snapshot()["game_clock"]["status"], "stale")

    async def test_the_events_become_session_markers(self):
        for key in (ann.RTD_STATUS, ann.RTD_SOURCE):
            event = ann.EVENTS[key]
            self.assertFalse(event.button)
            self.assertEqual(event.category, ann.Category.SESSION)
            self.assertEqual(event.kind, ann.Kind.INSTANT)

    async def test_listen_failure_is_down_logged_and_not_a_refusal(self):
        app = self.make()
        app.rtd_failed("[Errno 48] Address already in use")
        block = app.snapshot()["game_clock"]
        self.assertEqual((block["status"], block["detail"]), ("down", "[Errno 48] Address already in use"))
        (entry,) = self.named(ann.RTD_STATUS)
        self.assertEqual(entry.data["status"], "down")
        self.assertEqual(entry.data["detail"], "[Errno 48] Address already in use")
        # The fader is untouched.
        await app.arm()
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_the_port_the_receiver_reports_is_the_one_logged(self):
        app = self.make()
        app.rtd_listening(40404)
        self.feed(app, real.G5_14625, at=1)
        (entry,) = self.named(ann.RTD_STATUS)
        self.assertEqual(entry.data["port"], 40404)

    # -- the page ---------------------------------------------------------

    async def test_pushes_are_rate_limited(self):
        app = self.make()
        pushes = []
        app.on_change(lambda: pushes.append(self.t[0]))
        self.feed(app, real.G5_14625, at=0)
        first = len(pushes)
        self.assertGreaterEqual(first, 1)
        # A burst of changes inside the window: one push at most.
        for i, clock in enumerate(("14:59", "14:58", "14:57", "14:56")):
            self.feed(app, real.frame(0, clock), at=0.01 * (i + 1))
        self.assertEqual(len(pushes), first)
        # The tick carries what was held back.
        self.at(1)
        app.rtd_tick()
        self.assertEqual(len(pushes), first + 1)
        self.at(2)
        app.rtd_tick()
        self.assertEqual(len(pushes), first + 1)
        # A change after the window goes straight out.
        self.feed(app, real.frame(0, "14:55"), at=2.5)
        self.assertEqual(len(pushes), first + 2)

    async def test_an_unchanged_datagram_does_not_push(self):
        app = self.make()
        self.feed(app, real.G5_14625, at=0)
        pushes = []
        app.on_change(lambda: pushes.append(1))
        for i in range(1, 6):
            self.feed(app, real.G5_14625, at=i)
        self.assertEqual(pushes, [])

    # -- nothing else may touch it ----------------------------------------

    def test_only_rtd_methods_and_snapshot_touch_the_reader(self):
        source = textwrap.dedent(inspect.getsource(tacet_app.App))
        tree = ast.parse(source)
        touching = set()
        for method in ast.walk(tree):
            if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Attribute) and node.attr == "_game_clock":
                    touching.add(method.name)
        self.assertEqual(
            touching,
            {
                "__init__",
                "rtd_received",
                "rtd_tick",
                "rtd_failed",
                "_rtd_error",
                "_game_clock_view",
            },
        )
        # And the snapshot reaches it only through the guard.
        snapshot = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "snapshot")
        calls = {
            n.func.attr for n in ast.walk(snapshot) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertIn("_game_clock_view", calls)
