import asyncio
import contextlib
import math
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tacet import annotations as ann
from tacet import app as tacet_app
from tacet import dm7, mirror, moves, osc, prompts, reach, reaper, state, taps, targets, web
from tacet import provenance as prov
from tests.disk import Disk
from tests.reach_fixtures import HOST, FakeRunner, GatedRunner, ManualClock
from tests.reaper_stream import listened_parked, meter_packet, mid_take_stream, refresh_reply, rolling_with_refresh
from tests.test_annotations import Gate, Killed


class FakeSender:
    def __init__(self):
        self.packets = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)

    def levels(self):
        out = []
        for packet in self.packets:
            message = osc.decode_packet(packet)
            assert isinstance(message, osc.Message)
            if message.args:
                out.append(message.args[0])
        return out

    def addresses(self):
        out = []
        for packet in self.packets:
            message = osc.decode_packet(packet)
            assert isinstance(message, osc.Message)
            out.append(message.address)
        return out


class FailingSender:
    def send(self, packet: bytes) -> None:
        from tacet.net import TransportError

        raise TransportError("console unreachable")


class FlakySender(FakeSender):
    """Delivers `fail_after` packets in total, then fails every send until
    healed. None delivers everything."""

    def __init__(self, fail_after: int | None = None):
        super().__init__()
        self.fail_after: int | None = fail_after

    def send(self, packet: bytes) -> None:
        from tacet.net import TransportError

        if self.fail_after is not None and len(self.packets) >= self.fail_after:
            raise TransportError("console unreachable")
        super().send(packet)

    def heal(self) -> None:
        self.fail_after = None


class NotAnOsErrorSender(FlakySender):
    """Fails the way `sendto` does for an out-of-range port (#72): with an
    `OverflowError`, which is neither an `OSError` nor a `TransportError`."""

    def send(self, packet: bytes) -> None:
        if self.fail_after is not None and len(self.packets) >= self.fail_after:
            raise OverflowError("sendto(): port must be 0-65535.")
        FakeSender.send(self, packet)


class LoopClock:
    """A console clock that is never late (#92).

    Since #40 a drive that wakes late skips to the newest due step, so on a slow
    runner a short fade on the real clock can send fewer packets than a test
    set its failure for, finish cleanly, and never fail. Sleeping here advances
    this clock by exactly what was asked and yields to the loop, so every step
    is sent however slowly the machine runs.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Never less than the next representable time. A drive can be left a
        # remainder far below this clock's resolution once it reads a few
        # seconds, and adding that would leave the clock where it was: a sleep
        # that never ends.
        self.now = max(self.now + seconds, math.nextafter(self.now, math.inf))
        await asyncio.sleep(0)


#: Long enough for a cancelled move's task to wake and run its cleanup - it
#: needs a loop iteration or two, not wall time - and short next to every ramp
#: the tests below use, so the newer move is still running when it is checked.
SUPERSEDED_WAKES = 0.03

#: How long to let a move run before replacing it, so the fader has left where
#: it started. A move from where the fader already is has nowhere to go, and
#: finishes - and settles - at once.
UNDER_WAY = 0.1

#: Notifies a whole fade may cost the page (#154): the command, the
#: annotation's own, the move landing and FADE_COMPLETE. Never one per step.
FADE_NOTIFIES_MAX = 4

#: Two ramp rates for the same fade: the page must not be able to tell.
FADE_TICK_HZ_LOW = 50.0
FADE_TICK_HZ_HIGH = 1000.0

#: How long a test waits for the log's writer thread to report through a push.
#: A healthy thread takes milliseconds; this only bounds a test that is broken.
WRITER_REPORTS = 5.0


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.console_sender = FakeSender()
        self.reaper_sender = FakeSender()

    def build(
        self,
        *,
        console_sender=None,
        fade=0.05,
        monotonic=None,
        reaper_sender=None,
        opener=None,
        sync=None,
        queue=None,
        console_class=dm7.Dm7Client,
        stale_tap_seconds=taps.DEFAULT_STALE_TAP_SECONDS,
        steady=False,
        level_known=True,
        tick_hz=200.0,
        provenance=None,
        console_check=None,
    ):
        """`level_known` defaults to True, unlike the production machine: a
        hundred-odd tests here are about something other than a cold boot, and
        a box that refuses to arm would make each of them open with a ritual.
        The tests about not knowing say `level_known=False` out loud.

        `steady` puts the console on a `LoopClock`, for any test that makes
        the console fail after a number of packets: on the real clock that
        number depends on how fast the runner is (#92)."""
        options = {} if opener is None else {"opener": opener}
        if sync is not None:
            options["sync"] = sync
        self.log = ann.AnnotationLog(self.root / "game.jsonl", mirror=queue, **options)
        self.log.open()
        self.addCleanup(self.log.close)
        timing = {}
        if steady:
            loop_clock = LoopClock()
            timing = {"monotonic": loop_clock.monotonic, "sleep": loop_clock.sleep}
        self.console = console_class(
            "192.0.2.1",
            dca=3,
            sender=console_sender or self.console_sender,
            tick_hz=tick_hz,
            **timing,
        )
        clock = monotonic if monotonic is not None else time.monotonic
        self._clock = clock
        self.reaper = reaper.ReaperClient(sender=reaper_sender or self.reaper_sender, monotonic=clock)
        return tacet_app.App(
            console=self.console,
            log=self.log,
            recorder=self.reaper,
            fade_seconds=fade,
            monotonic=clock,
            stale_tap_seconds=stale_tap_seconds,
            machine=state.Machine(level_known=level_known),
            provenance=provenance,
            console_check=console_check,
        )

    #: The operator's whole cycle, from a cold boot (#107): every move the page asks for.
    OPERATOR_CYCLE = ("close_now", "arm", "trigger", "release", "handoff", "report_ready", "trigger", "close_now")

    #: What a ping can find, one after another: answered, no reply, nothing there, could not check.
    PING_OUTCOMES = (
        reach.ProcessOutput(0, b"time=1.0 ms", b""),
        reach.ProcessOutput(2, b"", b""),
        reach.ProcessOutput(68, b"", b"Host is down"),
        reach.ProcessOutput(64, b"", b"bad"),
    )

    async def run_operator_cycle(self, *, watched):
        """The cycle on its own console, with or without a console watch beside it.

        Returns `(sender, pings)`. After every operator step - once its fade has
        landed - a watched run lets the watch run a check and asserts the check
        put NOTHING on the wire, so a check that re-sent the commanded level
        (the keepalive hazard, #12) fails here and not only at the end. The
        console is on a steady clock so both runs send the same steps.
        """
        sender = FakeSender()
        saved_root = self.root
        self.root = saved_root / ("watched" if watched else "plain")
        self.root.mkdir()
        try:
            app = self.build(level_known=False, steady=True, console_sender=sender)
        finally:
            self.root = saved_root
        if not watched:
            for name in self.OPERATOR_CYCLE:
                await getattr(app, name)()
                await app.wait_for_fade()
            return sender, []
        clock = ManualClock()
        runner = FakeRunner()
        watch = reach.Watch(
            HOST,
            reach.Tools(platform=reach.Platform.MACOS, ping="/sbin/ping", arp="/usr/sbin/arp"),
            runner=runner,
            busy=lambda: app.fader_moving,
            on_result=app.console_checked,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            first_due=0.0,
        )
        app.on_armed(lambda: watch.request(reach.Trigger.ARM))
        task = asyncio.ensure_future(watch.run())
        await clock.settle()
        for number, name in enumerate(self.OPERATOR_CYCLE):
            runner.ping = self.PING_OUTCOMES[number % len(self.PING_OUTCOMES)]
            await getattr(app, name)()
            await app.wait_for_fade()
            before = len(sender.packets)
            checked = len(runner.calls)
            await clock.advance(reach.KEEPALIVE_SECONDS)
            self.assertGreater(len(runner.calls), checked, f"no check ran after {name}")
            self.assertEqual(len(sender.packets), before, f"a console check wrote to the console after {name}")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return sender, [c for c in runner.calls if c[0] == "/sbin/ping"]

    async def assert_checks_add_nothing_to_the_cycle(self):
        plain, _ = await self.run_operator_cycle(watched=False)
        watched, pings = await self.run_operator_cycle(watched=True)
        self.assertGreaterEqual(len(plain.packets), 5)
        self.assertGreaterEqual(len(pings), len(self.OPERATOR_CYCLE))
        # The same levels, in the same order, as the cycle with no watch at all.
        self.assertEqual(watched.levels(), plain.levels())
        for address in watched.addresses():
            self.assertEqual(address, dm7.fader_address(3))

    def reaper_parked(self, app):
        """Reaper open with its audio device running, transport parked, and
        listened to for a full timeout: the box will not call a transport
        parked before that (#163)."""
        listened_parked(self.reaper, self._clock())
        app.handle_recorder_packet(meter_packet())

    def reaper_answers_stopped(self, app):
        """The box's first contact with Reaper, and Reaper's answer to its
        refresh: not recording (#172)."""
        app.handle_recorder_packet(meter_packet())
        for packet in refresh_reply(recording=False, playing=False):
            app.handle_recorder_packet(packet)

    def expect_push(self, app, condition):
        """A future for the first push to satisfy `condition`.

        Watching starts here, so ask before the tap: the writer thread can
        report - and the page be pushed - before the tap's own await returns.
        Nothing flushes, so the push is the writer's own doing.
        """
        pushed = asyncio.get_running_loop().create_future()

        def listener():
            if not pushed.done():
                snapshot = app.snapshot()
                if condition(snapshot):
                    pushed.set_result(snapshot)

        app.on_change(listener)
        return pushed

    async def pushed(self, push):
        return await asyncio.wait_for(push, WRITER_REPORTS)

    def entries(self):
        # Written on the log's own thread (#41), so wait for it first.
        self.log.flush()
        return list(ann.read_entries(self.root / "game.jsonl"))

    def keys(self):
        return [e.event for e in self.entries()]


class TestOperatorInputIsCheckedFirst(AppTestCase):
    """Refused before anything moves. A fader button with bad data used to move
    the fader, then fail writing the reason (#36)."""

    async def test_annotate_refuses_box_only_events(self):
        app = self.build()
        with self.assertRaises(ann.NotAButtonError):
            await app.annotate(ann.ANCHOR_EVENT)
        self.assertNotIn(ann.ANCHOR_EVENT, self.keys())

    async def test_start_span_refuses_box_only_events(self):
        app = self.build()
        with self.assertRaises(ann.NotAButtonError):
            await app.start_span(tacet_app.COMMANDED)
        self.assertNotIn(tacet_app.COMMANDED, self.keys())

    async def test_annotate_refuses_non_object_data_before_moving(self):
        app = self.build()
        await app.arm()
        with self.assertRaises(ann.DataError):
            await app.annotate("up-drums", data="hello")
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console_sender.packets, [])
        self.assertNotIn(tacet_app.COMMANDED, self.keys())

    async def test_what_is_logged_is_the_checked_data(self):
        app = self.build()
        await app.annotate("note", data={"text": "band sounds thin"})
        self.assertEqual(self.entries()[-1].data, {"text": "band sounds thin"})


class TestAFailingDisk(AppTestCase):
    """The disk fills in the third quarter.

    A log write used to raise out of the request after the fader had already
    moved: no push, a bare 500, and a page still showing IDLE at -inf with the
    console at unity. Inside a fade it was worse - the exception ended the fade
    task before FADE_COMPLETE, and the machine sat in RELEASING. Nothing a log
    write does may stop the fader, the machine or the page.

    Since #41 the write fails on the log's own thread, after the tap has been
    answered, and has to find its own way to the page.
    """

    def setUp(self):
        super().setUp()
        self.disk = Disk()

    def build(self, **options):
        return super().build(opener=self.disk.open, **options)

    def watch(self, app):
        pushed = []
        app.on_change(lambda: pushed.append(app.snapshot()))
        return pushed

    async def test_a_failed_deferred_write_reaches_the_page(self):
        app = self.build()
        await app.arm()
        self.disk.full = True
        push = self.expect_push(app, lambda snapshot: not snapshot["log"]["healthy"])
        entry = await app.annotate("up-drums")
        # Accepted: the tap was answered before the disk was tried.
        self.assertIsNotNone(entry)
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        pushed = await self.pushed(push)
        self.assertIn("No space left on device", pushed["log"]["error"])

    async def test_a_failing_log_does_not_strand_a_fade(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        self.disk.full = True
        await app.release()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)

    async def test_a_failing_log_does_not_stop_arming(self):
        app = self.build()
        self.disk.full = True
        await app.arm()
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_a_span_that_was_not_saved_says_so_and_is_not_open(self):
        app = self.build()
        self.disk.full = True
        push = self.expect_push(app, lambda snapshot: not snapshot["log"]["healthy"])
        self.assertIsNotNone(await app.start_span("last-two-minutes"))
        pushed = await self.pushed(push)
        self.assertEqual(pushed["open_spans"], [])

    async def test_an_end_that_was_not_saved_reopens_the_span_to_retap(self):
        app = self.build()
        span = await app.start_span("last-two-minutes")
        self.log.flush()
        self.disk.full = True
        push = self.expect_push(app, lambda snapshot: not snapshot["log"]["healthy"] and snapshot["open_spans"])
        self.assertIsNotNone(await app.end_span(span))
        pushed = await self.pushed(push)
        self.assertEqual(pushed["open_spans"][0]["span_id"], span)
        self.log.flush()
        self.disk.full = False
        self.assertIsNotNone(await app.end_span(span))
        self.log.flush()
        self.assertEqual(len(app.snapshot()["open_spans"]), 0)

    async def test_the_page_says_when_saving_resumes_and_what_it_cost(self):
        app = self.build()
        self.disk.full = True
        await app.annotate("note", data={"text": "lost"})
        await app.annotate("note", data={"text": "also lost"})
        self.log.flush()
        self.disk.full = False
        await app.annotate("note", data={"text": "saved"})
        self.log.flush()
        log = app.snapshot()["log"]
        self.assertEqual((log["healthy"], log["error"], log["failures"]), (True, None, 2))

    async def test_a_healthy_log_says_so(self):
        log = self.build().snapshot()["log"]
        self.assertEqual((log["healthy"], log["error"], log["failures"]), (True, None, 0))
        self.assertEqual(log["path"], str(self.root / "game.jsonl"))

    async def test_a_failing_mirror_is_shown_and_costs_no_log_entry(self):
        queue_disk = Disk()
        queue = mirror.MirrorQueue(self.root / "queue.tsv", opener=queue_disk.open).open()
        self.addCleanup(queue.close)
        app = super().build(queue=queue)
        queue_disk.full = True
        entry = await app.annotate("note", data={"text": "kept"})
        self.assertIsNotNone(entry)
        self.assertIn("note", self.keys())  # flushes
        snapshot = app.snapshot()
        self.assertTrue(snapshot["log"]["healthy"])
        self.assertFalse(snapshot["mirror"]["healthy"])
        self.assertIn("No space left on device", snapshot["mirror"]["error"])


class TestTheLogWriterCannotHoldUpTheFader(AppTestCase):
    """#41: a FADE tap wrote two entries, each fsyncing the log and the queue,
    on the event loop, before the fade started. On Linux an fsync is 2-20 ms
    with 100 ms outliers, and one on a NAS that stalls freezes every command."""

    async def test_a_slow_fsync_does_not_delay_the_fader(self):
        gate = Gate()
        app = self.build(sync=gate)
        # Registered after the log's own close, so it runs first: the log waits
        # for its writer on close, and the writer is waiting on this gate.
        self.addCleanup(gate.open)
        await app.arm()
        self.assertTrue(gate.entered.wait(Gate.HOLD_SECONDS))

        await app.annotate("up-drums")
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        await app.annotate("out")
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        await app.annotate("up-whistle")
        self.assertEqual(self.console.commanded_level, dm7.UNITY)

        # Every move made while the first sync was still stuck.
        self.assertFalse(gate.opened.is_set())
        gate.open()
        self.assertIn("up-whistle", self.keys())

    async def test_a_dead_writer_is_shown_on_the_page(self):
        def sync(fd):
            raise Killed

        patcher = mock.patch.object(threading, "excepthook", lambda _: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = self.build(sync=sync)
        push = self.expect_push(app, lambda snapshot: not snapshot["log"]["healthy"])
        await app.arm()
        pushed = await self.pushed(push)
        self.assertEqual(pushed["log"]["error"], ann.WRITER_STOPPED)
        # The fader still answers, and the page keeps saying so.
        await app.annotate("up-drums")
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertEqual(app.snapshot()["log"]["error"], ann.WRITER_STOPPED)


class TestARecordSendThatFails(AppTestCase):
    """Sending `/record` raised out of the request: a 500, and no refusal to
    say what happened."""

    async def test_it_is_refused_visibly_and_logs_no_start(self):
        app = self.build(reaper_sender=FailingSender())
        self.reaper_parked(app)
        await app.start_recording()
        snapshot = app.snapshot()
        self.assertIn("console unreachable", snapshot["refusal"])
        self.assertNotIn(tacet_app.RECORDING_STARTED, self.keys())
        self.assertNotIn(tacet_app.RECORDING_REQUESTED, self.keys())

    async def test_it_can_be_tried_again(self):
        # Nothing reached Reaper, so there is nothing a second tap could stop.
        app = self.build(reaper_sender=FailingSender())
        self.reaper_parked(app)
        await app.start_recording()
        self.assertTrue(app.snapshot()["recording"]["can_start"])

    async def test_it_notifies(self):
        app = self.build(reaper_sender=FailingSender())
        self.reaper_parked(app)
        pushed = []
        app.on_change(lambda: pushed.append(app.snapshot()))
        await app.start_recording()
        self.assertTrue(pushed)


class TestArming(AppTestCase):
    async def test_arming_moves_to_idle_and_is_logged(self):
        app = self.build()
        await app.arm()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIn("armed", self.keys())

    async def test_arming_does_not_command_the_fader(self):
        app = self.build()
        await app.arm()
        self.assertEqual(self.console_sender.packets, [])

    async def test_standing_down_from_idle_is_logged(self):
        app = self.build()
        await app.arm()
        await app.stand_down()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertIn("stood-down", self.keys())


def _after(*commands):
    """The machine each command leaves, from boot. Built with the real state
    machine so no test hand-assembles a machine that cannot occur."""
    machines = [state.Machine(level_known=True)]
    for command in commands:
        machines.append(state.step(machines[-1], state.Event(command)).machine)
    return machines


def _after_from(machine):
    """Every machine one command from `machine`, to reach states beyond boot."""
    return [state.step(machine, state.Event(command)).machine for command in state.Command]


class TestSessionEntries(unittest.TestCase):
    """#50, the pure half: which session entries a transition earns.

    Decided from the machine before and after, so a stand-down is logged when
    it happens rather than when it was asked for.
    """

    C = state.Command

    def entries_for(self, *commands):
        machines = _after(*commands)
        return tacet_app.session_entries(machines[-2], machines[-1])

    def test_arming(self):
        self.assertEqual(self.entries_for(self.C.ARM), (tacet_app.ARMED,))

    def test_standing_down_while_closed_happens_at_once(self):
        self.assertEqual(self.entries_for(self.C.ARM, self.C.STAND_DOWN), (tacet_app.STOOD_DOWN,))

    def test_standing_down_while_open_is_only_a_request(self):
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.STAND_DOWN)
        self.assertEqual(entries, (tacet_app.STAND_DOWN_REQUESTED,))

    def test_standing_down_while_fading_is_only_a_request(self):
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.RELEASE, self.C.STAND_DOWN)
        self.assertEqual(entries, (tacet_app.STAND_DOWN_REQUESTED,))

    def test_asking_twice_is_one_request(self):
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.STAND_DOWN, self.C.STAND_DOWN)
        self.assertEqual(entries, ())

    def test_the_fade_landing_is_the_stand_down(self):
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.STAND_DOWN, self.C.FADE_COMPLETE)
        self.assertEqual(entries, (tacet_app.STOOD_DOWN,))

    def test_a_snap_back_cancels_the_request(self):
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.STAND_DOWN, self.C.TRIGGER)
        self.assertEqual(entries, (tacet_app.STAND_DOWN_CANCELLED,))

    def test_an_ordinary_close_earns_nothing(self):
        for commands in ((self.C.TRIGGER,), (self.C.TRIGGER, self.C.RELEASE), (self.C.RELEASE,)):
            with self.subTest(commands=commands):
                self.assertEqual(self.entries_for(self.C.ARM, *commands), ())
        landing = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.RELEASE, self.C.FADE_COMPLETE)
        self.assertEqual(landing, ())

    def test_not_knowing_the_level_is_handed_off_and_knowing_it_again_is_took_back(self):
        # #107 inverted the flag (True is now "known"), so both lines are pinned
        # against the direction they read in.
        known = state.Machine(level_known=True)
        unknown = state.Machine(level_known=False)
        self.assertEqual(tacet_app.session_entries(known, unknown), (tacet_app.HANDED_OFF,))
        self.assertEqual(tacet_app.session_entries(unknown, known), (tacet_app.TOOK_BACK,))
        self.assertEqual(tacet_app.session_entries(known, known), ())
        self.assertEqual(tacet_app.session_entries(unknown, unknown), ())

    def test_the_first_absolute_tap_after_boot_earns_took_back(self):
        # A cold boot never handed anything off; the key is kept, and so is
        # the entry, because it is the moment the box started knowing.
        boot = state.Machine()
        opened = state.step(boot, state.Event(self.C.TRIGGER)).machine
        self.assertEqual(tacet_app.session_entries(boot, opened), (tacet_app.ARMED, tacet_app.TOOK_BACK))
        closed = state.step(boot, state.Event(self.C.CLOSE_NOW)).machine
        self.assertEqual(tacet_app.session_entries(boot, closed), (tacet_app.TOOK_BACK,))

    def test_no_prompt_key_ever_comes_out_of_session_entries(self):
        # #19: a prompt is a question about duty, not a duty state. Nothing in
        # the machine's own transitions may write one; the shell does that.
        prompt_keys = {
            prompts.PROMPT_RAISED,
            prompts.PROMPT_ACCEPTED,
            prompts.PROMPT_DISMISSED,
            prompts.PROMPT_RESOLVED,
            prompts.PROMPT_WITHDRAWN,
        }
        starts = [state.Machine(level_known=known) for known in (True, False)]
        starts += [after for start in list(starts) for after in _after_from(start)]
        for before in starts:
            for command in state.Command:
                after = state.step(before, state.Event(command)).machine
                with self.subTest(state=before.state, command=command):
                    self.assertFalse(prompt_keys & set(tacet_app.session_entries(before, after)))

    def test_a_failed_move_earns_nothing(self):
        # move-failed is written by the shell, with the level the fader stopped at.
        entries = self.entries_for(self.C.ARM, self.C.TRIGGER, self.C.STAND_DOWN, self.C.MOVE_FAILED)
        self.assertEqual(entries, ())

    def test_a_handoff_that_changed_nothing_still_earns_its_entry(self):
        unknown = state.Machine(level_known=False)
        known = state.Machine(level_known=True)
        self.assertEqual(tacet_app.session_entries(unknown, unknown, self.C.HANDOFF), (tacet_app.HANDED_OFF,))
        # None is what _command passes for a refused event, and for everything
        # the machines can speak for themselves (#117, #118).
        self.assertEqual(tacet_app.session_entries(unknown, unknown), ())
        self.assertEqual(tacet_app.session_entries(unknown, unknown, self.C.STAND_DOWN), ())
        # Never twice for one event: the diff and the command agree.
        self.assertEqual(tacet_app.session_entries(known, unknown, self.C.HANDOFF), (tacet_app.HANDED_OFF,))


class TestAStandDownIsLoggedWhenItLands(AppTestCase):
    """#50: `stood-down` used to be written at the tap, before the fade.

    A trigger that snapped back meanwhile left a log saying the box stood down,
    with nothing to contradict it.
    """

    async def test_a_cancelled_stand_down_is_not_logged_as_stood_down(self):
        app = self.build(fade=1.0)
        await app.arm()
        await app.trigger()
        await app.stand_down()
        await app.trigger()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.OPEN)
        keys = self.keys()
        self.assertNotIn(tacet_app.STOOD_DOWN, keys)
        self.assertLess(keys.index(tacet_app.STAND_DOWN_REQUESTED), keys.index(tacet_app.STAND_DOWN_CANCELLED))

    async def test_a_stand_down_during_a_song_is_logged_when_the_fade_lands(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.stand_down()
        self.assertIn(tacet_app.STAND_DOWN_REQUESTED, self.keys())
        self.assertNotIn(tacet_app.STOOD_DOWN, self.keys())

        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        keys = self.keys()
        self.assertEqual(keys[-1], tacet_app.STOOD_DOWN)
        self.assertEqual(self.entries()[-1].data["state"], state.State.STANDING_DOWN.value)
        # The fade is reported delivered before the stand-down it made possible.
        self.assertEqual(keys[-2], tacet_app.MOVE_LANDED)

    async def test_a_stand_down_whose_fade_fails_is_never_logged_as_stood_down(self):
        sender = FlakySender()
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.trigger()
        sender.fail_after = len(sender.packets) + 2
        await app.stand_down()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.RELEASING)
        keys = self.keys()
        self.assertIn(tacet_app.STAND_DOWN_REQUESTED, keys)
        self.assertIn(tacet_app.MOVE_FAILED, keys)
        self.assertNotIn(tacet_app.STOOD_DOWN, keys)

    async def test_a_stand_down_with_nothing_to_fade_lands_at_once(self):
        # The open's send failed, so the level is unknown (#116); a stand-down
        # while unknown changes the duty state at once and sends nothing, so
        # it lands without a fade to wait for.
        app = self.build(console_sender=FailingSender())
        await app.arm()
        await app.trigger()
        await app.stand_down()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(self.keys()[-1], tacet_app.STOOD_DOWN)

    async def test_a_second_tap_while_fading_writes_no_second_request(self):
        app = self.build(fade=1.0)
        await app.arm()
        await app.trigger()
        await app.stand_down()
        await app.stand_down()
        self.assertEqual(self.keys().count(tacet_app.STAND_DOWN_REQUESTED), 1)
        await app.trigger()


class TestDeliveryIsRecordedWhenTheMoveEnds(AppTestCase):
    """#50: `delivered` was read before a fade or ride-in had sent anything, so
    it described the previous send.

    A move the box awaits (the snap open) is delivered or not by the time its
    `commanded` entry is written. A move that runs on in the background is
    neither yet: `delivered` is null, and the move ends in `move-landed` or
    `move-failed`, unless a newer move replaces it.
    """

    def commanded(self):
        return [e for e in self.entries() if e.event == tacet_app.COMMANDED]

    def landed(self):
        return [e for e in self.entries() if e.event == tacet_app.MOVE_LANDED]

    async def test_a_snap_open_is_delivered_when_it_is_logged(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        self.assertIs(self.commanded()[-1].data["delivered"], True)
        self.assertEqual(self.landed(), [])

    async def test_a_fade_is_undecided_when_logged_and_landed_when_done(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release()
        self.assertIsNone(self.commanded()[-1].data["delivered"])
        self.assertEqual(self.landed(), [])

        await app.wait_for_fade()
        landed = self.landed()
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].data["target"], dm7.MINUS_INF)
        self.assertEqual(landed[0].data["level"], dm7.MINUS_INF)

    async def test_a_ride_in_is_undecided_when_logged_and_landed_when_done(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-slow")
        self.assertIsNone(self.commanded()[-1].data["delivered"])

        await app.wait_for_fade()
        landed = self.landed()
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0].data["target"], dm7.UNITY)
        self.assertEqual(landed[0].data["level"], dm7.UNITY)

    async def test_a_failed_fade_is_failed_and_never_landed(self):
        sender = FlakySender()
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.trigger()
        sender.fail_after = len(sender.packets) + 2
        await app.release()
        await app.wait_for_fade()
        self.assertIsNone(self.commanded()[-1].data["delivered"])
        self.assertIn(tacet_app.MOVE_FAILED, self.keys())
        self.assertEqual(self.landed(), [])

    def assert_timed(self, entry):
        # #40: a stall shows as lateness on the move it delayed. Real clocks
        # here, so only the shape is pinned; the numbers are tested in test_dm7.
        self.assertIsInstance(entry.data["worst_lateness"], float)
        self.assertGreaterEqual(entry.data["worst_lateness"], 0.0)
        self.assertIsInstance(entry.data["skipped_steps"], int)

    def assert_untimed(self, entry):
        self.assertIsNone(entry.data["worst_lateness"])
        self.assertIsNone(entry.data["skipped_steps"])

    async def test_a_snap_open_is_timed_when_it_is_logged(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        self.assert_timed(self.commanded()[-1])

    async def test_a_fade_is_timed_when_it_lands_and_not_before(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release()
        self.assert_untimed(self.commanded()[-1])
        await app.wait_for_fade()
        self.assert_timed(self.landed()[-1])

    async def test_a_ride_in_is_timed_when_it_lands_and_not_before(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-slow")
        self.assert_untimed(self.commanded()[-1])
        await app.wait_for_fade()
        self.assert_timed(self.landed()[-1])

    async def test_a_failed_fade_is_timed_as_far_as_it_got(self):
        sender = FlakySender()
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.trigger()
        sender.fail_after = len(sender.packets) + 2
        await app.release()
        await app.wait_for_fade()
        failed = [e for e in self.entries() if e.event == tacet_app.MOVE_FAILED]
        self.assert_timed(failed[-1])

    async def test_a_superseded_fade_never_lands(self):
        app = self.build(fade=1.0)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.sleep(UNDER_WAY)
        await app.trigger()
        await asyncio.sleep(SUPERSEDED_WAKES)
        await app.wait_for_fade()
        self.assertEqual(self.landed(), [])
        self.assertEqual([e.data["delivered"] for e in self.commanded()], [True, None, True])


class TestFader(AppTestCase):
    async def test_trigger_opens_the_fader_to_unity(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console_sender.levels()[-1], dm7.UNITY)

    async def test_the_fader_move_is_logged_with_its_level(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        commanded = [e for e in self.entries() if e.event == "commanded"]
        self.assertTrue(commanded)
        self.assertEqual(commanded[-1].data["level"], dm7.UNITY)

    async def test_a_fade_logs_its_target_not_just_where_it_started(self):
        # The ramp is asynchronous, so the level at command time is still unity.
        # Reading the log back, `target` is the half that is unambiguous.
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release()
        await app.wait_for_fade()
        fade = [e for e in self.entries() if e.data.get("command") == "fade"][-1]
        self.assertEqual(fade.data["level"], dm7.UNITY)
        self.assertEqual(fade.data["target"], dm7.MINUS_INF)
        self.assertIsNone(fade.data["target_db"])

    async def test_release_fades_to_silence_and_lands_in_idle(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release()
        await app.wait_for_fade()
        self.assertEqual(self.console_sender.levels()[-1], dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_a_trigger_during_the_fade_snaps_back_and_cancels_completion(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.release()
        self.assertEqual(app.machine.state, state.State.RELEASING)
        await app.trigger()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console_sender.levels()[-1], dm7.UNITY)

    async def test_every_console_packet_is_a_fader_level_write(self):
        # Faders only, never mutes - asserted at the level the operator drives,
        # from a cold boot through every move the page can ask for (#107).
        app = self.build(level_known=False)
        await app.close_now()
        await app.arm()
        await app.trigger()
        await app.release()
        await app.wait_for_fade()
        await app.handoff()
        await app.report_ready()
        await app.trigger()
        await app.close_now()
        self.assertGreaterEqual(len(self.console_sender.packets), 5)
        for address in self.console_sender.addresses():
            self.assertEqual(address, dm7.fader_address(3))

    async def test_every_console_packet_is_a_fader_level_write_with_a_console_watch_attached(self):
        await self.assert_checks_add_nothing_to_the_cycle()


class TestDetectorGate(AppTestCase):
    async def test_a_detector_trigger_is_refused_and_moves_nothing(self):
        app = self.build()
        await app.arm()
        outcome = await app.trigger(source=state.Source.DETECTOR)
        self.assertIsNotNone(outcome.refusal)
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_the_refusal_reaches_the_snapshot(self):
        app = self.build()
        await app.arm()
        await app.trigger(source=state.Source.DETECTOR)
        self.assertIsNotNone(app.snapshot()["refusal"])


class TestFailures(AppTestCase):
    async def test_a_console_failure_is_surfaced_not_swallowed(self):
        app = self.build(console_sender=FailingSender())
        await app.arm()
        await app.trigger()
        snapshot = app.snapshot()
        self.assertFalse(snapshot["fader"]["healthy"])
        self.assertIn("unreachable", snapshot["fader"]["error"])

    async def test_the_app_keeps_working_after_a_console_failure(self):
        app = self.build(console_sender=FailingSender())
        await app.arm()
        await app.trigger()
        # The open's send failed, and it was absolute, so the level goes back
        # to unknown (#116). A stand-down while unknown is never refused, but
        # it is also never a fade waiting on a console that is not answering:
        # it changes the duty state at once and sends nothing, so it lands
        # straight in STANDING DOWN rather than requesting one.
        await app.stand_down()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertIn(tacet_app.STOOD_DOWN, self.keys())


class TestASenderErrorOfAnyKindIsAFailedMove(AppTestCase):
    """#72: an `OverflowError` escaped every `except TransportError`.

    The open raised out of the app while the page showed a healthy fader, and a
    fade or ride-in task died with nothing logged and nothing shown. Each move
    must end unhealthy, with `move-failed` in the log.
    """

    def assert_failed_visibly(self, app, target):
        snapshot = app.snapshot()
        self.assertFalse(snapshot["fader"]["healthy"])
        self.assertIn("port must be 0-65535", snapshot["fader"]["error"])
        failed = [e for e in self.entries() if e.event == tacet_app.MOVE_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].data["target"], target)

    async def test_an_open(self):
        app = self.build(console_sender=NotAnOsErrorSender(fail_after=0))
        await app.arm()
        await app.trigger()
        self.assert_failed_visibly(app, dm7.UNITY)

    async def test_a_fade(self):
        sender = NotAnOsErrorSender()
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.trigger()
        sender.fail_after = len(sender.packets) + 1
        await app.release()
        await app.wait_for_fade()
        self.assert_failed_visibly(app, dm7.MINUS_INF)

    async def test_a_ride_in(self):
        app = self.build(console_sender=NotAnOsErrorSender(fail_after=1), steady=True)
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assert_failed_visibly(app, dm7.UNITY)


class TestAFailedMoveCanBeRetried(AppTestCase):
    """#27: a send error mid-move used to leave the machine stuck.

    A fade that died partway stayed RELEASING, so FADE OUT sent nothing and the
    only way out was OPEN - slamming a half-closed band to unity. A failed open
    stayed OPEN at -inf, and OPEN sent nothing either.
    """

    async def fail_a_fade_partway(self, sender):
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.trigger()
        # The open is itself a short ramp; count what it sent rather than
        # assume, so the failure lands inside the fade and not at its start.
        opened = len(sender.packets)
        sender.fail_after = opened + 3
        await app.release()
        await app.wait_for_fade()
        return app, opened

    async def test_a_fade_that_failed_midway_can_be_faded_again(self):
        sender = FlakySender()
        app, opened = await self.fail_a_fade_partway(sender)
        self.assertEqual(len(sender.packets), opened + 3)
        stuck_at = self.console.commanded_level
        self.assertLess(stuck_at, dm7.UNITY)
        self.assertGreater(stuck_at, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.RELEASING)

        sender.heal()
        await app.release()
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)
        # Straight down from where it stuck; never back up through unity.
        retried = sender.levels()[opened + 3 :]
        self.assertTrue(retried)
        self.assertTrue(all(level <= stuck_at for level in retried))

    async def test_a_failed_open_can_be_retried(self):
        sender = FlakySender(fail_after=0)
        app = self.build(console_sender=sender)
        await app.arm()
        await app.trigger()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)

        sender.heal()
        await app.trigger()
        self.assertEqual(sender.levels()[-1], dm7.UNITY)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)

    async def test_a_failed_ride_in_can_be_retried(self):
        sender = FlakySender(fail_after=2)
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertLess(self.console.commanded_level, dm7.UNITY)

        sender.heal()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.UNITY)

    async def test_a_healthy_fade_is_not_restarted_by_tapping_again(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        opened = len(self.console_sender.packets)
        await app.release()
        await app.release()
        await app.release()
        await app.wait_for_fade()
        # One close: every level after the open is strictly lower than the last.
        levels = self.console_sender.levels()[opened:]
        self.assertEqual(levels, sorted(levels, reverse=True))
        self.assertEqual(len(levels), len(set(levels)))

    async def test_a_failed_move_is_logged(self):
        # Otherwise the log's `commanded` entry reads as a move that happened.
        await self.fail_a_fade_partway(FlakySender())
        failed = [e for e in self.entries() if e.event == tacet_app.MOVE_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].data["target"], dm7.MINUS_INF)
        self.assertEqual(failed[0].data["level"], self.console.commanded_level)

    async def test_the_page_says_to_tap_again(self):
        app, _ = await self.fail_a_fade_partway(FlakySender())
        self.assertIn("did not finish", app.snapshot()["why"])

    async def test_a_relative_move_that_failed_does_not_un_know_the_level(self):
        # #116: a fade is relative, so its failure must not un-know the
        # level - or the retry this whole class is about would itself be
        # refused for want of a known level.
        app, _ = await self.fail_a_fade_partway(FlakySender())
        self.assertTrue(app.machine.level_known)

    async def test_a_failed_ride_in_does_not_un_know_the_level_either(self):
        sender = FlakySender(fail_after=2)
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertTrue(app.machine.stalled)
        self.assertTrue(app.machine.level_known)


class TestRecordIsNotAStopButton(AppTestCase):
    """Reaper's /record is a toggle, so the button had to stop being one.

    design.md 5.9: each home game is a single irreplaceable sample, and a stop
    button does not belong on a screen being tapped by someone watching a
    field. A start button that stops on the second press is that button.
    """

    def record_packets(self):
        return self.reaper_sender.addresses()

    async def test_tapping_start_twice_never_stops_the_recording(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        # One command reached Reaper, not two. The second would have stopped it.
        self.assertEqual(self.record_packets().count("/record"), 1)

    async def test_the_second_tap_says_why_it_did_nothing(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertIn("already recording", app.snapshot()["refusal"])

    async def test_a_refused_tap_does_not_log_a_second_start(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertEqual(self.keys().count(tacet_app.RECORDING_STARTED), 1)
        self.assertEqual(self.keys().count(tacet_app.RECORDING_REQUESTED), 1)

    async def test_a_parked_reaper_streaming_meters_can_be_started(self):
        # The ordinary pre-game case on this rig: Reaper open and parked, with
        # its audio device running, so it streams meters and says nothing about
        # the transport (#163).
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertEqual(self.record_packets().count("/record"), 1)
        self.assertEqual(self.keys().count(tacet_app.RECORDING_REQUESTED), 1)
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_a_silent_reaper_is_not_sent_a_start(self):
        # Reaper closed: a datagram to a port nobody is bound to does not raise,
        # so a send would write an anchor for a take that never existed.
        app = self.build()
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertNotIn(tacet_app.RECORDING_STARTED, self.keys())
        self.assertNotIn(tacet_app.RECORDING_REQUESTED, self.keys())
        self.assertEqual(app.snapshot()["recording"]["refusal"], reaper.RECORD_REFUSED_SILENT)

    async def test_a_reaper_that_reported_a_stop_can_be_started(self):
        app = self.build()
        self.reaper_answers_stopped(app)
        await app.start_recording()
        self.assertIn("/record", self.record_packets())

    async def test_a_moving_transport_of_unknown_record_state_is_refused(self):
        """A box restarted mid-game hears /time and no transport change, so it
        cannot tell a safe send from one that ends the recording. Reaper has not
        answered the box's refresh, and its cap (#172) has passed."""
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        for _ in range(int(reaper.REFRESH_ANSWER_SECONDS / 0.5) + 1):
            app.handle_recorder_packet(osc.encode_message("/time", 12.0))
            clock[0] += 0.5
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertEqual(app.snapshot()["refusal"], reaper.RECORD_REFUSED_MOVING)

    async def test_a_lost_recorder_is_refused(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        clock[0] += 60.0
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertIn("stopped answering", app.snapshot()["refusal"])

    async def test_the_refusal_clears_once_reaper_has_stopped(self):
        # Otherwise the screen keeps saying "already recording" at a recorder
        # that has since stopped - the same contradiction as a confirmed tag on
        # an unknown value.
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertIsNotNone(app.snapshot()["refusal"])
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_a_successful_start_clears_an_earlier_refusal(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        await app.start_recording()
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_the_snapshot_tells_the_page_when_to_disable_the_button(self):
        app = self.build()
        self.reaper_parked(app)
        self.assertTrue(app.snapshot()["recording"]["can_start"])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        self.assertFalse(app.snapshot()["recording"]["can_start"])

    async def test_two_taps_with_no_feedback_between_send_one_record(self):
        # #28. The existing double-tap test answers /record 1 between the taps;
        # over stalled wifi both taps arrive before Reaper has said anything.
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        await app.start_recording()
        self.assertEqual(self.record_packets().count("/record"), 1)
        self.assertEqual(app.snapshot()["refusal"], reaper.RECORD_REFUSED_UNANSWERED)

    async def test_two_taps_on_a_live_rig_that_is_not_recording_send_one_record(self):
        # The press-box rig: never silent, and after the workaround take it has
        # said it is not recording, which on its own permits a send.
        app = self.build()
        self.reaper_answers_stopped(app)
        app.handle_recorder_packet(osc.encode_message("/time", 4.8))
        await app.start_recording()
        await app.start_recording()
        self.assertEqual(self.record_packets().count("/record"), 1)

    async def test_the_button_is_disabled_while_the_start_is_unanswered(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertFalse(app.snapshot()["recording"]["can_start"])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        self.assertFalse(app.snapshot()["recording"]["can_start"])

    async def test_only_one_request_for_a_double_tap(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        await app.start_recording()
        self.assertEqual(self.keys().count(tacet_app.RECORDING_REQUESTED), 1)

    async def test_the_snapshot_carries_the_refusal_record_refusal_returns(self):
        """The page shows `recording.refusal` beside the grey button, so it must
        be exactly what the box would refuse with, read on one clock (#163)."""
        clock = [1000.0]

        def fresh(app):
            pass

        def meters(app):
            self.reaper_parked(app)

        def clocked(app):
            self.reaper_parked(app)
            app.handle_recorder_packet(osc.encode_message("/time", 4.5))

        def rolling(app):
            app.handle_recorder_packet(osc.encode_message("/record", 1.0))

        def lost(app):
            rolling(app)
            clock[0] += 60.0

        for label, setup, prior in (
            ("fresh", fresh, False),
            ("meters", meters, False),
            ("meters and a clock", clocked, False),
            ("recording", rolling, False),
            ("lost", lost, False),
            ("prior recording and meters", meters, True),
        ):
            with self.subTest(label):
                clock[0] = 1000.0
                if prior:
                    with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
                        seeded.record(tacet_app.RECORDING_STARTED)
                app = self.build(monotonic=lambda: clock[0])
                setup(app)
                recording = app.snapshot()["recording"]
                self.assertEqual(recording["refusal"], app.record_refusal)
                self.assertEqual(recording["can_start"], recording["refusal"] is None)

    async def test_the_snapshot_carries_the_refusal_when_latched(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        self.reaper_parked(app)
        await app.start_recording()
        recording = app.snapshot()["recording"]
        self.assertEqual(recording["refusal"], reaper.RECORD_REFUSED_UNANSWERED)
        self.assertEqual(recording["refusal"], app.record_refusal)
        self.assertFalse(recording["can_start"])

    async def test_a_box_restarted_mid_take_sends_no_record(self):
        # The log already holds the take's anchor; Reaper is rolling and, being
        # told nothing new, announces nothing. Meters and the clock stream.
        with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
            seeded.record(tacet_app.RECORDING_STARTED)
        app = self.build()
        for tick in range(5):
            self.reaper_parked(app)
            app.handle_recorder_packet(osc.encode_message("/time", 10.0 + tick))
        await app.start_recording()
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())

    async def test_a_box_started_mid_take_on_a_fresh_log_sends_no_record_at_any_prefix(self):
        # No prior anchor, so only what the box has heard can protect the take.
        # The stream is fed one packet at a time, meter first, with a tap after
        # each: a `/record` here stops the game's recording.
        clock = [100.0]
        app = self.build(monotonic=lambda: clock[0])
        for at, packet in mid_take_stream(0.0):
            clock[0] = at
            app.handle_recorder_packet(packet)
            await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertNotIn(tacet_app.RECORDING_STARTED, self.keys())

    async def test_a_log_that_already_holds_a_recording_refuses_an_unknown_record_state(self):
        with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
            seeded.record(tacet_app.RECORDING_STARTED)
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertEqual(app.snapshot()["recording"]["refusal"], reaper.RECORD_REFUSED_PRIOR)

    async def test_a_prior_recording_does_not_refuse_once_reaper_has_reported(self):
        with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
            seeded.record(tacet_app.RECORDING_STARTED)
        app = self.build()
        self.reaper_parked(app)
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        await app.start_recording()
        self.assertEqual(self.record_packets().count("/record"), 1)

    async def test_a_relaunched_reaper_is_not_shown_rolling(self):
        # Quit mid-take and relaunched parked: meters flow again but the last
        # transport word is a stale "recording" (#163).
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        app.handle_recorder_packet(osc.encode_message("/play", 1.0))
        app.handle_recorder_packet(osc.encode_message("/time", 5.0))
        clock[0] += 30.0
        self.reaper_parked(app)
        recording = app.snapshot()["recording"]
        self.assertFalse(recording["known"])
        self.assertEqual(recording["liveness"], "lost")
        self.assertIn("stopped answering", recording["refusal"])

    async def test_a_held_record_refusal_does_not_churn_the_snapshot(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        self.reaper_parked(app)
        await app.start_recording()
        before = app.snapshot()
        clock[0] += 5.0
        self.reaper_parked(app)
        after = app.snapshot()
        self.assertEqual(before["recording"]["refusal"], after["recording"]["refusal"])
        self.assertFalse(web.should_broadcast(before, after, elapsed=5.0))

    async def test_there_is_still_no_way_to_ask_reaper_to_stop(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertNotIn("/stop", self.record_packets())


class TestFaderButtons(AppTestCase):
    """The fader buttons move the fader and say why, in one tap.

    The move is recoverable afterwards from the post-DCA reference channel
    (design.md 9); the reason is not. So the reason is what does the acting,
    and cannot be the tap that got skipped.
    """

    async def test_up_on_whistle_opens_the_fader(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(app.snapshot()["fader"]["commanded"], dm7.UNITY)

    async def test_up_on_whistle_also_records_why(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        self.assertIn("up-whistle", self.keys())

    async def test_the_move_records_the_reason_that_caused_it(self):
        # The commanded entry carries the button that triggered it, so a log
        # read back later says why the fader moved, not just that it did.
        app = self.build()
        await app.arm()
        await app.annotate("up-drums")
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertEqual(commanded[-1].data["detail"], "up-drums")

    async def test_faded_out_releases(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.annotate("out")
        self.assertIn(app.machine.state, (state.State.RELEASING, state.State.IDLE))
        self.assertIn("out", self.keys())

    async def test_an_annotation_with_no_action_leaves_the_fader_alone(self):
        app = self.build()
        await app.arm()
        before = app.snapshot()["fader"]["commanded"]
        await app.annotate("drumline-cadence")
        self.assertEqual(app.snapshot()["fader"]["commanded"], before)
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_a_fader_button_while_standing_down_arms_and_opens(self):
        """#89: it used to be refused, so a forgotten Arm was a missed
        downbeat. The operator heard the whistle; the band is playing."""
        app = self.build()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertIn("up-whistle", self.keys())
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_the_arming_that_open_did_is_logged_and_said(self):
        app = self.build()
        await app.annotate("up-whistle")
        keys = self.keys()
        self.assertIn(tacet_app.ARMED, keys)
        self.assertLess(keys.index(tacet_app.ARMED), keys.index("up-whistle"))
        self.assertIn("Armed by that", app.snapshot()["why"])

    async def test_a_fade_button_while_standing_down_closes_and_arms_nothing(self):
        app = self.build()
        await app.annotate("up-whistle")
        await app.stand_down()
        await app.annotate("out")
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(self.keys().count(tacet_app.ARMED), 1)

    async def test_the_snapshot_tells_the_page_which_buttons_act(self):
        app = self.build()
        buttons = {b["key"]: b["action"] for b in app.snapshot()["buttons"]}
        self.assertEqual(buttons["up-whistle"], "open")
        self.assertEqual(buttons["out"], "release")
        self.assertIsNone(buttons["drumline-cadence"])


class TestSnapshot(AppTestCase):
    async def test_the_fader_is_never_reported_as_confirmed(self):
        # The DM7 cannot answer. Rendering commanded as confirmed would be the
        # silent degradation the design forbids.
        app = self.build()
        await app.arm()
        await app.trigger()
        self.assertFalse(app.snapshot()["fader"]["confirmed"])

    async def test_recording_is_unknown_until_reaper_says_otherwise(self):
        app = self.build()
        recording = app.snapshot()["recording"]
        self.assertFalse(recording["known"])
        self.assertFalse(recording["confirmed"])

    async def test_recording_is_confirmed_once_reaper_reports_it(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        recording = app.snapshot()["recording"]
        self.assertTrue(recording["known"])
        self.assertTrue(recording["recording"])
        self.assertTrue(recording["confirmed"])

    async def test_a_parked_reaper_still_reports_stopped_not_unknown(self):
        """Reaper is silent whenever it is parked (measured 2026-09-08).

        Reading that silence as a lost link put the UI into "no feedback" for
        the whole pre-game window, which is where the operator most wants to
        know the recorder is there.
        """
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        app.handle_recorder_packet(osc.encode_message("/play", 0.0))

        clock[0] += 60.0  # a minute of Reaper sitting there saying nothing
        recording = app.snapshot()["recording"]
        self.assertEqual(recording["liveness"], "quiet")
        self.assertTrue(recording["known"])
        self.assertFalse(recording["recording"])

    async def test_reaper_dying_mid_recording_is_a_visible_fault(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        self.assertTrue(app.snapshot()["recording"]["recording"])

        clock[0] += 60.0  # the /time stream should have been arriving
        recording = app.snapshot()["recording"]
        self.assertEqual(recording["liveness"], "lost")
        self.assertFalse(recording["known"])
        self.assertFalse(recording["confirmed"])

    async def test_a_stale_reading_never_claims_reaper_is_rolling(self):
        # The property that makes believing a parked reading safe at all.
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        clock[0] += 60.0
        recording = app.snapshot()["recording"]
        self.assertFalse(recording["known"] and recording["recording"])

    async def test_the_snapshot_carries_the_why_line(self):
        app = self.build()
        self.assertTrue(app.snapshot()["why"])

    async def test_the_snapshot_offers_only_button_events(self):
        app = self.build()
        keys = {b["key"] for b in app.snapshot()["buttons"]}
        self.assertIn("touchdown", keys)
        # Retired as a button, kept as a key (#14).
        self.assertNotIn("touchdown-sequence", keys)
        self.assertNotIn("commanded", keys)
        self.assertNotIn("armed", keys)


class TestAnnotation(AppTestCase):
    async def test_an_annotation_is_logged(self):
        app = self.build()
        await app.annotate("touchdown")
        self.assertIn("touchdown", self.keys())

    async def test_a_note_carries_its_text(self):
        app = self.build()
        await app.annotate("note", data={"text": "band sounds thin"})
        note = [e for e in self.entries() if e.event == "note"][-1]
        self.assertEqual(note.data["text"], "band sounds thin")

    async def test_an_unknown_event_is_refused_without_crashing(self):
        app = self.build()
        with self.assertRaises(ann.UnknownEventError):
            await app.annotate("no-such-event")

    async def test_an_operator_cannot_start_a_retired_span(self):
        app = self.build()
        before = len(self.entries())
        with self.assertRaises(ann.NotAButtonError):
            await app.start_span("q1")
        self.assertEqual(len(self.entries()), before)

    async def test_a_quarter_left_open_by_an_older_log_is_offered_and_ends(self):
        # #155: the key is never deleted, so a q2 an older log left open is
        # still ended through the page's OPEN FROM AN EARLIER RUN.
        older = ann.AnnotationLog(self.root / "game.jsonl")
        older.open()
        span = older.start_span("q2")
        older.close()
        app = self.build()
        snapshot = app.snapshot()
        self.assertIn({"span_id": span, "event": "q2", "label": "Q2"}, snapshot["open_spans"])
        self.assertNotIn("q2", {button["key"] for button in snapshot["buttons"]})
        self.assertIsNotNone(await app.end_span(span))
        self.assertEqual(app.snapshot()["open_spans"], [])

    async def test_spans_open_and_close(self):
        app = self.build()
        span = await app.start_span("last-two-minutes")
        self.assertIn(
            {"span_id": span, "event": "last-two-minutes", "label": "Last two minutes"}, app.snapshot()["open_spans"]
        )
        await app.end_span(span)
        self.assertEqual(app.snapshot()["open_spans"], [])

    async def test_an_open_span_names_the_button_that_owns_it(self):
        # The page matches a button to its span on this, and never parses the
        # id -- a prefix match let "timeout" claim "timeout-home-12".
        app = self.build()
        home = await app.start_span("timeout-home")
        exodus = await app.start_span("halftime-exodus")
        keys = {button["key"] for button in app.snapshot()["buttons"]}
        self.assertEqual(
            app.snapshot()["open_spans"],
            [
                {"span_id": home, "event": "timeout-home", "label": "Timeout: home"},
                {"span_id": exodus, "event": "halftime-exodus", "label": "Halftime exodus"},
            ],
        )
        for span in app.snapshot()["open_spans"]:
            self.assertIn(span["event"], keys)


class TestRecorder(AppTestCase):
    async def test_start_recording_commands_reaper_and_logs_the_request(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertEqual(self.reaper_sender.addresses(), [reaper.DEFAULT_ADDRESSES.record])
        self.assertIn("recording-requested", self.keys())
        self.assertNotIn("recording-started", self.keys())

    async def test_there_is_no_stop(self):
        # design.md 5.9: a stop button does not belong on this screen.
        app = self.build()
        for forbidden in ("stop", "stop_recording", "abort"):
            self.assertFalse(hasattr(app, forbidden), forbidden)

    async def test_the_app_works_without_a_recorder(self):
        self.log = ann.AnnotationLog(self.root / "game.jsonl")
        self.log.open()
        self.addCleanup(self.log.close)
        app = tacet_app.App(
            console=dm7.Dm7Client("192.0.2.1", sender=self.console_sender),
            log=self.log,
            recorder=None,
        )
        await app.arm()
        self.assertFalse(app.snapshot()["recording"]["known"])


if __name__ == "__main__":
    unittest.main()


class TestThePlayheadIsStamped(AppTestCase):
    """Every entry carries Reaper's own position when Reaper is streaming it.

    `markers.position_of` prefers it over arithmetic on our clock, which is
    what stops a three-hour game accumulating drift and what makes a restarted
    recording untidy rather than wrong.
    """

    def entries(self):
        self.log.close()
        return list(ann.read_entries(self.log.path))

    def last(self):
        return self.entries()[-1]

    async def test_an_annotation_carries_the_reported_position(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 1234.5))
        await app.annotate("band-enters-stands")
        self.assertEqual(self.last().project_seconds, 1234.5)

    async def test_it_is_reaper_s_number_and_not_extrapolated(self):
        # Never adjusted forward by the time since the packet arrived: that
        # would put our clock back into the answer.
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 90.0))
        await app.annotate("drumline-cadence")
        self.assertEqual(self.last().project_seconds, 90.0)

    async def test_nothing_is_stamped_before_reaper_has_said_anything(self):
        app = self.build()
        await app.annotate("band-enters-stadium")
        self.assertIsNone(self.last().project_seconds)

    async def test_a_stale_position_is_not_stamped(self):
        # Reaper is silent whenever it is parked, so an old reading is wherever
        # the transport was last seen. Stamping it would place a marker at a
        # confidently wrong point; unstamped falls back to the arithmetic.
        now = [100.0]
        app = self.build(monotonic=lambda: now[0])
        app.handle_recorder_packet(osc.encode_message("/time", 55.0))
        now[0] += reaper.DEFAULT_FEEDBACK_TIMEOUT + 1.0
        await app.annotate("band-exits-stands")
        self.assertIsNone(self.last().project_seconds)

    async def test_a_position_that_stopped_updating_is_not_stamped_while_other_feedback_flows(self):
        # This rig meters continuously while parked (reaper.md), so the link
        # never goes quiet. After the throwaway take `/time` stops at 4.8s, and
        # every entry made while parked used to be stamped 4.8 - which
        # `markers.position_of` prefers to the arithmetic that would have
        # placed it correctly (#35).
        now = [100.0]
        app = self.build(monotonic=lambda: now[0])
        app.handle_recorder_packet(osc.encode_message("/time", 4.8))
        for _ in range(4):
            now[0] += 1.0
            app.handle_recorder_packet(osc.encode_message("/track/1/vu", 0.5))
        self.assertEqual(app.snapshot()["recording"]["liveness"], "live")
        await app.annotate("band-exits-stands")
        self.assertIsNone(self.last().project_seconds)

    async def test_spans_are_stamped_at_both_ends(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 10.0))
        span = await app.start_span("last-two-minutes")
        app.handle_recorder_packet(osc.encode_message("/time", 900.0))
        await app.end_span(span)
        entries = self.entries()
        self.assertEqual(entries[-2].project_seconds, 10.0)
        self.assertEqual(entries[-1].project_seconds, 900.0)

    async def test_a_fader_move_is_stamped(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 42.0))
        await app.arm()
        await app.annotate("up-drums")
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertTrue(commanded)
        self.assertEqual(commanded[-1].project_seconds, 42.0)

    async def test_commanded_entries_carry_the_keys_the_pilot_check_reads(self):
        from tacet import pilot

        app = self.build()
        await app.arm()
        await app.annotate("up-drums")
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertTrue(commanded)
        for key in (
            pilot.DATA_COMMAND,
            pilot.DATA_DETAIL,
            pilot.DATA_LEVEL,
            pilot.DATA_TARGET,
            pilot.DATA_TARGET_DB,
            pilot.DATA_DELIVERED,
        ):
            self.assertIn(key, commanded[-1].data)

    async def test_no_recorder_means_no_stamp(self):
        self.build()  # sets up the console and the log
        app = tacet_app.App(console=self.console, log=self.log, recorder=None)
        await app.annotate("band-enters-stands")
        self.assertIsNone(self.last().project_seconds)


class TestTheBoxAsksReaper(AppTestCase):
    """#172: the box asks Reaper for its transport state when it first hears
    it, so the record state is reported rather than inferred."""

    def build_timed(self):
        self.t = [100.0]
        return self.build(monotonic=lambda: self.t[0])

    def hear(self, app, packet, at):
        self.t[0] = at
        app.handle_recorder_packet(packet)

    def asked(self):
        return self.reaper_sender.addresses().count("/action")

    async def test_the_box_asks_reaper_once_when_it_first_hears_it(self):
        app = self.build_timed()
        self.hear(app, meter_packet(), 100.0)
        self.assertEqual(self.asked(), 1)
        self.hear(app, meter_packet(), 100.09)
        self.assertEqual(self.asked(), 1)

    async def test_a_box_started_beside_a_parked_reaper_is_live_once_reaper_answers(self):
        app = self.build_timed()
        self.hear(app, meter_packet(), 100.0)
        self.assertFalse(app.snapshot()["recording"]["can_start"])
        for k, packet in enumerate(refresh_reply(recording=False, playing=False)):
            self.hear(app, packet, 100.04 + k * 0.001)
        self.t[0] = 100.1
        recording = app.snapshot()["recording"]
        self.assertTrue(recording["can_start"])
        self.assertTrue(recording["known"])
        self.assertFalse(recording["recording"])

    async def test_a_box_started_mid_take_learns_it_is_recording(self):
        app = self.build_timed()
        self.hear(app, meter_packet(), 100.0)
        self.hear(app, osc.encode_message("/time", 10.0), 100.02)
        for k, packet in enumerate(refresh_reply(recording=True, playing=True)):
            self.hear(app, packet, 100.04 + k * 0.001)
        self.t[0] = 100.1
        recording = app.snapshot()["recording"]
        self.assertTrue(recording["known"])
        self.assertTrue(recording["recording"])
        self.assertEqual(recording["refusal"], reaper.RECORD_REFUSED_ROLLING)
        await app.start_recording()
        self.assertNotIn("/record", self.reaper_sender.addresses())

    async def test_an_entry_made_during_the_refresh_stall_is_not_stamped(self):
        app = self.build_timed()
        self.hear(app, osc.encode_message("/time", 10.0), 100.0)
        for at in (100.2, 100.4, 100.6):
            self.hear(app, meter_packet(), at)
        await app.annotate("note")
        self.log.close()
        entry = [e for e in ann.read_entries(self.log.path) if e.event == "note"][-1]
        self.assertIsNone(entry.project_seconds)


class TestARefreshInFlightGuardsTheStart(AppTestCase):
    """#172: while the box's refresh is unanswered, a start is refused as
    listening, for at most `REFRESH_ANSWER_SECONDS`."""

    def hear(self, app, packet, at):
        self.t[0] = at
        app.handle_recorder_packet(packet)

    async def test_a_start_is_refused_while_the_refresh_is_unanswered(self):
        self.t = [100.0]
        app = self.build(monotonic=lambda: self.t[0])
        for at in (100.0, 100.5, 101.0, 101.5, 102.0, 102.5):
            self.hear(app, meter_packet(), at)
        # Two seconds of meters and no `/time`: #163 alone would permit it.
        recording = app.snapshot()["recording"]
        self.assertFalse(recording["can_start"])
        self.assertEqual(recording["refusal"], reaper.RECORD_REFUSED_LISTENING)
        await app.start_recording()
        self.assertNotIn("/record", self.reaper_sender.addresses())

    async def test_an_unanswered_refresh_stops_refusing_after_the_cap(self):
        self.t = [100.0]
        app = self.build(monotonic=lambda: self.t[0])
        at = 100.0
        while at <= 100.0 + reaper.REFRESH_ANSWER_SECONDS:
            self.hear(app, meter_packet(), at)
            at += 0.5
        self.assertTrue(app.snapshot()["recording"]["can_start"])

    async def test_an_answered_refresh_lifts_the_guard_in_the_same_tick(self):
        self.t = [100.0]
        app = self.build(monotonic=lambda: self.t[0])
        self.hear(app, meter_packet(), 100.0)
        self.assertFalse(app.snapshot()["recording"]["can_start"])
        for packet in refresh_reply(recording=False, playing=False):
            self.hear(app, packet, 100.04)
        self.assertTrue(app.snapshot()["recording"]["can_start"])

    async def test_a_failed_refresh_send_is_on_the_snapshot(self):
        app = self.build(reaper_sender=FailingSender())
        app.handle_recorder_packet(meter_packet())
        recording = app.snapshot()["recording"]
        self.assertFalse(recording["healthy"])
        self.assertIn("unreachable", recording["error"])

    async def test_a_healthy_recorder_has_no_error(self):
        app = self.build()
        app.handle_recorder_packet(meter_packet())
        self.assertIsNone(app.snapshot()["recording"]["error"])


class TestTheAnchorIsReapersConfirmation(AppTestCase):
    """#158: the anchor is written when Reaper reports a take starting, never on
    the tap, which is only a request."""

    def named(self, key):
        return [e for e in self.entries() if e.event == key]

    def record(self, app, on=1.0):
        app.handle_recorder_packet(osc.encode_message("/record", on))

    async def test_a_take_started_in_reaper_writes_exactly_one_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        self.record(app)
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 1)
        self.assertEqual(self.named(tacet_app.RECORDING_REQUESTED), [])

    async def test_a_tapped_start_and_its_confirmation_write_one_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.record(app)
        self.record(app)
        requests = self.named(tacet_app.RECORDING_REQUESTED)
        anchors = self.named(tacet_app.RECORDING_STARTED)
        self.assertEqual((len(requests), len(anchors)), (1, 1))
        self.assertLess(requests[0].seq, anchors[0].seq)

    async def test_a_tap_writes_the_request_and_not_the_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertEqual(len(self.named(tacet_app.RECORDING_REQUESTED)), 1)
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])

    async def test_a_start_reaper_never_confirms_leaves_no_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        app.handle_recorder_packet(meter_packet())
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])

    async def test_a_second_take_writes_a_second_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        self.record(app)
        self.record(app, 0.0)
        self.record(app)
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 2)

    async def test_a_stop_writes_no_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        self.record(app, 0.0)
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])

    def relaunch(self, app, clock, *, answers_stop):
        """A take rolls and is answered; Reaper quits and comes back parked a
        minute later. Its first packet draws a refresh."""
        self.record(app)
        self.record(app)
        clock[0] += 60.0
        app.handle_recorder_packet(meter_packet())
        if answers_stop:
            self.record(app, 0.0)
        for _ in range(12):
            clock[0] += 0.5
            app.handle_recorder_packet(meter_packet())

    async def test_a_take_restarted_in_a_relaunched_reaper_writes_an_anchor(self):
        # The refresh the relaunch drew is never answered, and the new take is
        # reported more than the cap after it: a new take, not a reply.
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        self.relaunch(app, clock, answers_stop=False)
        self.record(app)
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 2)
        self.assertEqual(self.named(tacet_app.RECORDING_FOUND), [])

    async def test_a_take_restarted_in_a_relaunched_reaper_that_answered_stop_writes_an_anchor(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        self.relaunch(app, clock, answers_stop=True)
        self.record(app)
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 2)
        self.assertEqual(self.named(tacet_app.RECORDING_FOUND), [])

    async def test_the_anchor_carries_no_tap_even_inside_a_tapped_context(self):
        app = self.build()
        self.reaper_parked(app)
        with tacet_app._tapped(late(0.2)):
            self.record(app)
        (anchor,) = self.named(tacet_app.RECORDING_STARTED)
        self.assertNotIn("tap", anchor.data)

    async def test_the_anchor_is_never_stamped_with_a_playhead(self):
        clock = [1000.0]
        app = self.build(monotonic=lambda: clock[0])
        self.record(app)
        clock[0] += 0.1
        app.handle_recorder_packet(osc.encode_message("/time", 77.0))
        clock[0] += 0.1
        self.record(app, 0.0)
        clock[0] += 0.1
        self.record(app)
        anchors = self.named(tacet_app.RECORDING_STARTED)
        self.assertEqual(len(anchors), 2)
        for anchor in anchors:
            self.assertIsNone(anchor.project_seconds)

    async def test_the_tap_is_on_the_request_and_not_on_the_anchor(self):
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording(tap=late(0.2))
        self.record(app)
        (request,) = self.named(tacet_app.RECORDING_REQUESTED)
        (anchor,) = self.named(tacet_app.RECORDING_STARTED)
        self.assertIn("tap", request.data)
        self.assertNotIn("tap", anchor.data)

    async def test_the_anchor_says_reaper_confirmed_it(self):
        app = self.build()
        self.reaper_parked(app)
        self.record(app)
        (anchor,) = self.named(tacet_app.RECORDING_STARTED)
        self.assertEqual(anchor.data, {ann.ANCHOR_CONFIRMED_BY: ann.CONFIRMED_BY_REAPER})

    async def test_a_log_holding_only_a_request_refuses_an_unknown_record_state(self):
        with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
            seeded.record(tacet_app.RECORDING_REQUESTED)
        app = self.build()
        self.reaper_parked(app)
        await app.start_recording()
        self.assertNotIn("/record", self.reaper_sender.addresses())
        self.assertEqual(app.snapshot()["recording"]["refusal"], reaper.RECORD_REFUSED_PRIOR)


class TestAFoundTake(AppTestCase):
    """#158: a take the box finds already rolling is logged as found, stamped
    with Reaper's position, and never as an anchor."""

    def run_stream(self, app, stream):
        for at, packet in stream:
            self.t[0] = at
            app.handle_recorder_packet(packet)

    def build_timed(self, seed=None):
        self.t = [100.0]
        if seed is not None:
            with ann.AnnotationLog(self.root / "game.jsonl") as seeded:
                seeded.record(seed)
        return self.build(monotonic=lambda: self.t[0])

    def named(self, key):
        return [e for e in self.entries() if e.event == key]

    def first_time_after_the_stall(self, **kwargs):
        for _, packet in rolling_with_refresh(100.0, **kwargs):
            decoded = osc.decode_packet(packet)
            if isinstance(decoded, osc.Message) and decoded.address == "/time":
                return decoded.args[0]
        raise AssertionError("no /time in the stream")

    async def test_a_box_started_mid_take_writes_a_found_entry_and_no_anchor(self):
        app = self.build_timed()
        self.run_stream(app, rolling_with_refresh(100.0))
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])
        self.assertEqual(len(self.named(tacet_app.RECORDING_FOUND)), 1)

    async def test_the_found_entry_is_stamped_with_the_first_clock_after_the_reply(self):
        app = self.build_timed()
        self.run_stream(app, rolling_with_refresh(100.0))
        (found,) = self.named(tacet_app.RECORDING_FOUND)
        self.assertEqual(found.project_seconds, self.first_time_after_the_stall())
        self.assertEqual(found.data, {ann.ANCHOR_CONFIRMED_BY: ann.CONFIRMED_BY_REAPER})

    async def test_a_box_restarted_on_its_own_log_mid_take_writes_no_second_anchor(self):
        app = self.build_timed(seed=tacet_app.RECORDING_STARTED)
        self.run_stream(app, rolling_with_refresh(100.0))
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 1)
        self.assertEqual(len(self.named(tacet_app.RECORDING_FOUND)), 1)

    async def test_a_take_stopped_before_its_clock_arrives_writes_nothing(self):
        app = self.build_timed()
        self.run_stream(app, [(100.0, meter_packet())])
        self.run_stream(
            app, [(100.04 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=True, playing=True))]
        )
        self.run_stream(app, [(100.1, osc.encode_message("/record", 0.0)), (100.2, osc.encode_message("/time", 5.0))])
        self.assertEqual(self.named(tacet_app.RECORDING_FOUND), [])

    async def test_a_refresh_after_a_take_the_box_saw_start_writes_one_anchor_and_one_found(self):
        # The rig with its audio device off: the start burst is the first
        # packet of the run, so the box sees the take start and then asks.
        app = self.build_timed()
        self.run_stream(app, [(100.0, osc.encode_message("/record", 1.0))])
        replies = [(100.04 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=True, playing=True))]
        self.run_stream(app, replies)
        self.run_stream(app, [(101.5, osc.encode_message("/time", 1.5))])
        self.assertEqual(len(self.named(tacet_app.RECORDING_STARTED)), 1)
        self.assertEqual(len(self.named(tacet_app.RECORDING_FOUND)), 1)

    async def test_a_refresh_saying_stopped_writes_nothing(self):
        app = self.build_timed()
        self.run_stream(app, [(100.0, meter_packet())])
        self.run_stream(
            app, [(100.04 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=False, playing=False))]
        )
        self.run_stream(app, [(100.5, osc.encode_message("/time", 1.0))])
        self.assertEqual(self.named(tacet_app.RECORDING_FOUND), [])
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])

    async def test_the_found_entry_is_not_stamped_with_a_clock_from_before_the_reply(self):
        app = self.build_timed()
        self.run_stream(app, [(100.0, osc.encode_message("/time", 500.0))])
        replies = [(100.04 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=True, playing=True))]
        self.run_stream(app, replies)
        self.run_stream(app, [(101.5, osc.encode_message("/time", 501.5))])
        (found,) = self.named(tacet_app.RECORDING_FOUND)
        self.assertEqual(found.project_seconds, 501.5)

    async def test_a_slow_reply_that_starts_a_new_run_still_writes_the_found_entry(self):
        app = self.build_timed()
        self.run_stream(app, [(100.0, meter_packet())])
        replies = [(103.0 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=True, playing=True))]
        self.run_stream(app, replies)
        self.run_stream(app, [(103.5, osc.encode_message("/time", 7.0))])
        self.assertEqual(len(self.named(tacet_app.RECORDING_FOUND)), 1)
        self.assertEqual(self.named(tacet_app.RECORDING_STARTED), [])

    async def test_a_found_take_waiting_for_its_clock_is_dropped_by_a_gap(self):
        app = self.build_timed()
        self.run_stream(app, [(100.0, meter_packet())])
        replies = [(100.04 + k * 0.001, p) for k, p in enumerate(refresh_reply(recording=True, playing=True))]
        self.run_stream(app, replies)
        self.run_stream(app, [(105.0, osc.encode_message("/time", 9.0))])
        self.assertEqual(self.named(tacet_app.RECORDING_FOUND), [])

    async def test_a_found_entry_carries_no_tap(self):
        app = self.build_timed()
        self.run_stream(app, rolling_with_refresh(100.0))
        (found,) = self.named(tacet_app.RECORDING_FOUND)
        self.assertNotIn("tap", found.data)


class TestTheExpectedFaderStateIsVisible(AppTestCase):
    """The page carries where the fader is going, not only where it was.

    Still expectation and never confirmation - the DM7's OSC is write-only, so
    `confirmed` stays false whatever this says.
    """

    async def test_nothing_is_moving_so_there_is_no_target(self):
        app = self.build()
        fader = app.snapshot()["fader"]
        self.assertIsNone(fader["target"])
        self.assertIsNone(fader["target_db"])

    async def test_a_close_shows_where_it_is_heading(self):
        app = self.build(fade=0.4)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        fader = app.snapshot()["fader"]
        self.assertEqual(fader["target"], dm7.MINUS_INF)
        # -inf has no JSON spelling; the page renders null as "-oo dB".
        self.assertIsNone(fader["target_db"])
        await app.wait_for_fade()

    async def test_the_target_is_gone_once_the_fade_finishes(self):
        app = self.build(fade=0.05)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.wait_for_fade()
        self.assertIsNone(app.snapshot()["fader"]["target"])

    async def test_the_expectation_is_never_dressed_up_as_confirmation(self):
        app = self.build(fade=0.4)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        self.assertFalse(app.snapshot()["fader"]["confirmed"])
        await app.wait_for_fade()

    async def test_a_fade_notifies_a_bounded_number_of_times_whatever_its_step_count(self):
        # #154: the page is told about a fade by description, not per ramp
        # step, so a finer ramp costs the page nothing.
        counts = {}
        packets = {}
        for tick_hz in (FADE_TICK_HZ_LOW, FADE_TICK_HZ_HIGH):
            sender = FakeSender()
            app = self.build(fade=0.2, tick_hz=tick_hz, console_sender=sender)
            notifies: list[int] = []
            await app.arm()
            await app.annotate("up-drums")
            app.on_change(lambda notifies=notifies: notifies.append(1))
            sender_before = len(sender.packets)
            await app.annotate("out")
            await app.wait_for_fade()
            counts[tick_hz] = len(notifies)
            packets[tick_hz] = len(sender.packets) - sender_before
        self.assertEqual(counts[FADE_TICK_HZ_LOW], counts[FADE_TICK_HZ_HIGH])
        self.assertLessEqual(counts[FADE_TICK_HZ_HIGH], FADE_NOTIFIES_MAX)
        self.assertGreater(packets[FADE_TICK_HZ_HIGH], 10 * counts[FADE_TICK_HZ_HIGH])

    async def test_no_description_survives_a_finished_fade(self):
        app = self.build(fade=0.05)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.wait_for_fade()
        await asyncio.sleep(0)
        self.assertIsNone(app._move)

    async def test_snapping_back_to_open_clears_the_description(self):
        # A trigger during the close cancels the fade; nothing should still be
        # describing a move that is no longer happening.
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.annotate("up-whistle")
        self.assertIsNone(app._move)
        self.assertIsNone(app.snapshot()["fader"]["target"])


class TestUpSlowRidesIn(AppTestCase):
    """`up-slow` is a gesture, not only a reason.

    design.md section 4, in the practice the box reproduces: "If the first
    phrase is missed, the operator brings the mix up more slowly to disguise
    the late entry." It used to snap open exactly like the other two.
    """

    def entries(self):
        self.log.close()
        return list(ann.read_entries(self.log.path))

    async def test_the_other_open_buttons_still_snap(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-drums")
        self.assertEqual(app._console.commanded_level, dm7.UNITY)
        self.assertIsNone(app._move)

    async def test_up_slow_does_not_arrive_immediately(self):
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        await app.annotate("up-slow")
        # Still on its way up: a snap would already be at unity.
        self.assertLess(app._console.commanded_level, dm7.UNITY)
        await app.wait_for_fade()

    async def test_up_slow_rides_in_on_the_taper(self):
        # #8: fast through the bottom, slow through the top.
        app = self.build()
        app._slow_open_seconds = 0.2
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        steps = dm7.ramp_steps(dm7.MINUS_INF, dm7.UNITY, 0.2, tick_hz=200.0, taper=dm7.RIDE_IN_TAPER)
        tapered = [level for _, level in steps]
        sent = self.console_sender.levels()
        # Only levels on the taper, in order, all the way up. Not necessarily
        # every one: on the real clock a late wake-up skips a step (#40, #85),
        # and test_dm7 pins the exact curve on a clock that is never late.
        remaining = iter(tapered)
        self.assertTrue(all(level in remaining for level in sent), sent)
        self.assertEqual(sent[-1], dm7.UNITY)
        self.assertGreater(len(sent), 1, "a ride-in, not a snap")

    async def test_up_slow_gets_all_the_way_there(self):
        app = self.build()
        app._slow_open_seconds = 0.2
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, dm7.UNITY)

    async def test_the_ride_in_shows_where_it_is_going(self):
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        await app.annotate("up-slow")
        fader = app.snapshot()["fader"]
        self.assertEqual(fader["target"], dm7.UNITY)
        await app.wait_for_fade()
        self.assertIsNone(app.snapshot()["fader"]["target"])

    async def test_a_ride_in_notifies_a_bounded_number_of_times(self):
        notifies = []
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        app.on_change(lambda: notifies.append(1))
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertLessEqual(len(notifies), FADE_NOTIFIES_MAX)

    async def test_the_annotation_is_not_held_back_by_the_ramp(self):
        # The whole reason the ride-in runs as a task. `annotate` moves the
        # fader before writing the log, so awaiting a 1.5s ramp would timestamp
        # the tap - and stamp its playhead - where the ramp ended.
        app = self.build()
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.annotate("up-slow")
        self.log.close()
        events = [entry.event for entry in ann.read_entries(self.log.path)]
        self.assertIn("up-slow", events)
        app._cancel_move()

    async def test_a_close_during_the_ride_in_takes_over(self):
        app = self.build(fade=0.2)
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("out")
        # After the superseded ride-in has had its turn to clean up, not before:
        # that cleanup is what used to clear the close's target (#34).
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.snapshot()["fader"]["target"], dm7.MINUS_INF)
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, dm7.MINUS_INF)

    async def test_up_on_whistle_during_a_ride_in_snaps_to_unity(self):
        # #45, decided: the whistle means the band is about to play.
        app = self.build()
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        self.assertLess(app._console.commanded_level, dm7.UNITY)

        await app.annotate("up-whistle")
        # At unity when the tap returns: the snap is awaited like any other.
        self.assertEqual(app._console.commanded_level, dm7.UNITY)
        self.assertIsNone(app.snapshot()["fader"]["target"])
        await asyncio.sleep(SUPERSEDED_WAKES)
        await app.wait_for_fade()

        # The ride-in sent nothing after the snap and never reported landing.
        levels = self.console_sender.levels()
        self.assertEqual(levels[-1], dm7.UNITY)
        self.assertEqual(levels.count(dm7.UNITY), 1)
        self.assertNotIn(tacet_app.MOVE_LANDED, [e.event for e in self.entries()])
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertEqual([e.data["detail"] for e in commanded], ["up-slow", "up-whistle"])
        self.assertIs(commanded[-1].data["delivered"], True)

    async def test_up_on_drums_during_a_ride_in_snaps_too(self):
        app = self.build()
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("up-drums")
        self.assertEqual(app._console.commanded_level, dm7.UNITY)
        await asyncio.sleep(SUPERSEDED_WAKES)

    async def test_up_slow_during_a_ride_in_does_not_restart_it(self):
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("up-slow")
        self.assertLess(app._console.commanded_level, dm7.UNITY)
        await app.wait_for_fade()
        # One climb: never back down to the start of a second ride-in.
        levels = self.console_sender.levels()
        self.assertEqual(levels, sorted(levels))
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertEqual(len(commanded), 1)

    async def test_up_on_whistle_after_the_ride_in_landed_sends_nothing(self):
        app = self.build()
        app._slow_open_seconds = 0.1
        await app.arm()
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertFalse(app.machine.riding_in)
        sent = len(self.console_sender.packets)
        await app.annotate("up-whistle")
        self.assertEqual(len(self.console_sender.packets), sent)
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED]
        self.assertEqual(len(commanded), 1)

    async def test_up_slow_is_still_an_open_to_the_machine(self):
        app = self.build()
        app._slow_open_seconds = 0.2
        await app.arm()
        await app.annotate("up-slow")
        self.assertEqual(app.snapshot()["state"], state.State.OPEN.value)
        await app.wait_for_fade()


class TestUpReadyRidesToTheHoldLevel(AppTestCase):
    """#6: ready, then go. Something good happened for the home team; the
    fader rides to a hold level short of target and waits to see whether the
    band starts, rather than all the way to the open level."""

    def hold_level(self, app):
        return dm7.clamp(dm7.UNITY - round(app._hold_below_db * dm7.UNITS_PER_DB))

    async def test_up_ready_is_a_state_of_its_own(self):
        app = self.build()
        app._ready_ride_seconds = 0.2
        await app.arm()
        await app.annotate("up-ready")
        self.assertEqual(app.snapshot()["state"], state.State.READY.value)
        await app.wait_for_fade()

    async def test_up_ready_does_not_arrive_immediately(self):
        app = self.build()
        app._ready_ride_seconds = 0.4
        await app.arm()
        await app.annotate("up-ready")
        # Still on its way up: a snap would already be at the hold level.
        self.assertLess(app._console.commanded_level, self.hold_level(app))
        await app.wait_for_fade()

    async def test_up_ready_stops_short_of_target(self):
        app = self.build()
        app._ready_ride_seconds = 0.2
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, self.hold_level(app))
        self.assertLess(app._console.commanded_level, dm7.UNITY)

    async def test_the_ride_shows_where_it_is_going(self):
        app = self.build()
        app._ready_ride_seconds = 0.4
        await app.arm()
        await app.annotate("up-ready")
        self.assertEqual(app.snapshot()["fader"]["target"], self.hold_level(app))
        await app.wait_for_fade()
        self.assertIsNone(app.snapshot()["fader"]["target"])

    async def test_a_trigger_commits_to_target_fast_from_wherever_it_got_to(self):
        app = self.build()
        app._ready_ride_seconds = 5.0
        await app.arm()
        await app.annotate("up-ready")
        await asyncio.sleep(UNDER_WAY)
        self.assertLess(app._console.commanded_level, dm7.UNITY)

        await app.annotate("up-whistle")
        # At unity when the tap returns: the snap is awaited like any other.
        self.assertEqual(app._console.commanded_level, dm7.UNITY)
        self.assertEqual(app.snapshot()["state"], state.State.OPEN.value)
        await asyncio.sleep(SUPERSEDED_WAKES)
        await app.wait_for_fade()

    async def test_a_trigger_commits_even_after_the_ride_has_landed(self):
        app = self.build()
        app._ready_ride_seconds = 0.1
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        self.assertEqual(app.snapshot()["state"], state.State.READY.value)

        await app.annotate("up-drums")
        self.assertEqual(app._console.commanded_level, dm7.UNITY)
        self.assertEqual(app.snapshot()["state"], state.State.OPEN.value)

    async def test_score_reversed_fades_like_an_ordinary_close(self):
        app = self.build(fade=0.1)
        app._ready_ride_seconds = 0.1
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        await app.annotate("score-reversed")
        self.assertIn(app.machine.state, (state.State.RELEASING, state.State.IDLE))
        self.assertIn("score-reversed", self.keys())
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, dm7.MINUS_INF)

    async def test_a_close_during_the_ride_takes_over(self):
        app = self.build(fade=0.2)
        app._ready_ride_seconds = 5.0
        await app.arm()
        await app.annotate("up-ready")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("out")
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.snapshot()["fader"]["target"], dm7.MINUS_INF)
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, dm7.MINUS_INF)

    async def test_a_stand_down_from_ready_fades_rather_than_snapping(self):
        # #6: the box cannot be sure the band has not quietly started under
        # the hold level, so this gets the same caution as a stand-down from
        # OPEN rather than a snap-close.
        app = self.build(fade=0.1)
        app._ready_ride_seconds = 0.1
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        await app.stand_down()
        self.assertEqual(app.machine.state, state.State.RELEASING)
        self.assertGreater(app._console.commanded_level, dm7.MINUS_INF)
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(app._console.commanded_level, dm7.MINUS_INF)

    async def test_a_fader_button_while_standing_down_arms_and_readies(self):
        # #89's rule extended to READY: a forgotten Arm must not cost a
        # heads-up either.
        app = self.build()
        app._ready_ride_seconds = 0.2
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        await app.annotate("up-ready")
        self.assertEqual(app.machine.state, state.State.READY)
        self.assertIn("up-ready", self.keys())
        self.assertIn(tacet_app.ARMED, self.keys())
        self.assertIsNone(app.snapshot()["refusal"])
        await app.wait_for_fade()

    async def test_the_snapshot_tells_the_page_which_buttons_act(self):
        app = self.build()
        buttons = {b["key"]: b["action"] for b in app.snapshot()["buttons"]}
        self.assertEqual(buttons["up-ready"], "ready")
        self.assertEqual(buttons["score-reversed"], "release")


class TestFaderPositionTrust(AppTestCase):
    """#107 (generalising #12): the box does not know where the fader is at
    cold boot, and again after handing the DCA to StageMix. Everything here is
    built without `level_known=True` unless it is about a known level."""

    async def test_a_fresh_app_does_not_know_the_level(self):
        # No `machine=` argument: the production default, which is unknown.
        app = self.build(level_known=False)
        self.assertFalse(app.machine.level_known)
        self.assertFalse(app.snapshot()["fader"]["level_known"])

    async def test_the_production_default_is_unknown(self):
        self.build()
        # No `machine=` at all, the way `serve` constructs it.
        app = tacet_app.App(console=self.console, log=self.log)
        self.assertFalse(app.machine.level_known)
        self.assertFalse(app.snapshot()["fader"]["level_known"])

    async def test_arm_at_cold_boot_is_refused_and_logs_nothing(self):
        app = self.build(level_known=False)
        outcome = await app.arm()
        self.assertEqual(outcome.refusal, state.UNKNOWN_LEVEL_ARM)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertNotIn(tacet_app.ARMED, self.keys())
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_ARM)
        self.assertEqual(self.console_sender.packets, [])

    async def test_the_cold_boot_ritual_is_close_now_then_arm(self):
        app = self.build(level_known=False)
        await app.close_now()
        self.assertEqual(self.console_sender.levels(), [dm7.MINUS_INF])
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertTrue(app.snapshot()["fader"]["level_known"])
        await app.arm()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIsNone(app.snapshot()["refusal"])
        # One packet in all: arming still sends nothing of its own.
        self.assertEqual(self.console_sender.levels(), [dm7.MINUS_INF])
        self.assertEqual(self.keys().count(tacet_app.TOOK_BACK), 1)
        self.assertIn(tacet_app.ARMED, self.keys())

    async def test_a_snap_open_at_cold_boot_arms_opens_and_trusts(self):
        # The #89 pin, at the shell: a forgotten Arm must not cost a downbeat.
        app = self.build(level_known=False)
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertTrue(app.snapshot()["fader"]["level_known"])
        self.assertIn(tacet_app.ARMED, self.keys())
        self.assertIn(tacet_app.TOOK_BACK, self.keys())

    async def test_a_fade_at_cold_boot_is_refused_and_sends_nothing(self):
        app = self.build(level_known=False)
        outcome = await app.release()
        self.assertEqual(outcome.refusal, state.UNKNOWN_LEVEL_MOVE)
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_MOVE)

    async def test_a_slow_open_at_cold_boot_is_refused_but_its_reason_is_still_logged(self):
        # The operator saw what they saw: a log that kept only the accepted
        # taps would misrepresent the night.
        app = self.build(level_known=False)
        await app.annotate("up-slow")
        self.assertIn("up-slow", self.keys())
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_MOVE)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)

    async def test_a_fade_reason_at_cold_boot_is_refused_but_still_logged(self):
        app = self.build(level_known=False)
        await app.annotate("out")
        self.assertIn("out", self.keys())
        self.assertEqual(self.console_sender.packets, [])

    async def test_a_refused_move_is_never_run_later(self):
        # #107 deleted the queue: a tap blocked for want of a known level does
        # not wait to run on a belief nobody has looked at.
        app = self.build(level_known=False)
        await app.annotate("out")
        await app.annotate("up-slow")
        await app.close_now()
        await app.wait_for_fade()
        self.assertEqual(self.console_sender.levels(), [dm7.MINUS_INF])

    async def test_handoff_is_logged_and_sends_nothing(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        sent = len(self.console_sender.packets)
        await app.handoff()
        self.assertFalse(app.machine.level_known)
        self.assertFalse(app.snapshot()["fader"]["level_known"])
        self.assertEqual(len(self.console_sender.packets), sent)
        self.assertIn(tacet_app.HANDED_OFF, self.keys())

    async def test_a_second_handoff_is_logged_again_and_changes_nothing(self):
        # #118: the box cannot tell a double tap from a restart under
        # StageMix, and the operator confirmed both times - same reasoning as
        # `still-mine`. The second tap changes nothing on the box, but it is
        # still the record Phase 1's control-authority labels are made of.
        app = self.build()
        await app.handoff()
        after_first = app.machine
        sent = len(self.console_sender.packets)
        await app.handoff()
        self.assertEqual(self.keys().count(tacet_app.HANDED_OFF), 2)
        self.assertEqual(app.machine, after_first)
        self.assertEqual(len(self.console_sender.packets), sent)

    async def test_handoff_is_legal_while_standing_down(self):
        app = self.build()
        await app.handoff()
        self.assertFalse(app.machine.level_known)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)

    async def test_a_snap_open_after_handoff_makes_the_level_known_again(self):
        app = self.build()
        await app.arm()
        await app.handoff()
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertTrue(app.machine.level_known)
        self.assertIn(tacet_app.TOOK_BACK, self.keys())

    async def test_a_snap_open_writes_even_when_the_box_believes_the_fader_is_already_there(self):
        # The absolute half of the invariant (#107). After a hand-off from OPEN
        # the box still *believes* unity, and `Dm7Client.open` says nothing to
        # a level it thinks it is already at: without a real write the box
        # would mark the level known having put the fader nowhere, while
        # StageMix may have left it anywhere at all.
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.handoff()
        sent = len(self.console_sender.packets)
        await app.trigger()
        self.assertEqual(len(self.console_sender.packets), sent + 1)
        self.assertEqual(self.console_sender.levels()[-1], dm7.UNITY)
        self.assertTrue(app.machine.level_known)
        self.assertTrue(app.snapshot()["fader"]["level_known"])

    async def test_that_holds_after_standing_down_while_unknown_too(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.handoff()
        await app.stand_down()
        sent = len(self.console_sender.packets)
        await app.annotate("up-whistle")
        self.assertEqual(len(self.console_sender.packets), sent + 1)
        self.assertEqual(self.console_sender.levels()[-1], dm7.UNITY)
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def test_an_open_while_the_level_is_known_and_already_there_still_says_nothing(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        sent = len(self.console_sender.packets)
        await app.trigger()
        self.assertEqual(len(self.console_sender.packets), sent)

    async def test_handoff_cancels_a_move_in_flight(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.annotate("out")
        await asyncio.sleep(UNDER_WAY)
        await app.handoff()
        self.assertFalse(app.machine.level_known)
        # Presumed landed rather than left fading forever (#12): nothing will
        # ever fire FADE_COMPLETE for a fade that got cancelled.
        self.assertEqual(app.machine.state, state.State.IDLE)
        level_after_cancel = self.console.commanded_level
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(self.console.commanded_level, level_after_cancel)

    async def test_a_fade_after_handoff_is_refused_not_queued(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        commanded_before = self.console.commanded_level
        count_before = len([e for e in self.entries() if e.event == tacet_app.COMMANDED])
        await app.handoff()
        await app.annotate("out")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, commanded_before)
        self.assertIn("out", self.keys())
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_MOVE)
        count_after = len([e for e in self.entries() if e.event == tacet_app.COMMANDED])
        self.assertEqual(count_after, count_before)

    async def test_close_now_sends_one_packet_to_minus_infinity(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.handoff()
        sent = len(self.console_sender.packets)
        await app.close_now()
        self.assertEqual(len(self.console_sender.packets), sent + 1)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertTrue(app.machine.level_known)
        self.assertIn(tacet_app.TOOK_BACK, self.keys())

    async def test_close_now_works_when_the_level_is_already_known(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.close_now()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)
        # Not the 2 s fade: no background move is left running.
        self.assertFalse(self.console.is_ramping)
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED][-1]
        self.assertEqual(commanded.data["command"], "close-now")
        self.assertEqual(commanded.data["detail"], "close-now")
        self.assertTrue(commanded.data["delivered"])

    async def test_close_now_cancels_a_move_in_flight(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.sleep(UNDER_WAY)
        await app.close_now()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertIsNone(app.snapshot()["fader"]["target"])
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_close_now_from_a_pending_stand_down_lands_standing_down(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.stand_down()
        await app.close_now()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)

    async def test_a_failed_close_now_leaves_the_level_unknown_and_close_now_still_works(self):
        # #116: close now is never gated by state or belief, including a
        # belief the box itself just un-knew.
        sender = FlakySender(fail_after=0)
        app = self.build(console_sender=sender, level_known=False)
        await app.close_now()
        self.assertFalse(app.machine.level_known)
        self.assertTrue(app.machine.stalled)
        sender.heal()
        await app.close_now()
        self.assertTrue(app.machine.level_known)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)

    async def test_a_failed_absolute_move_is_never_logged_as_a_hand_off(self):
        # The #116 x #118 composition hazard: `session_entries` must read the
        # outcome from before `_move_fader` ran, or a failed absolute move
        # would look exactly like a hand-off.
        app = self.build(console_sender=FailingSender(), level_known=True)
        await app.arm()
        await app.trigger()
        self.assertFalse(app.machine.level_known)
        self.assertNotIn(tacet_app.HANDED_OFF, self.keys())
        self.assertIn(tacet_app.MOVE_FAILED, self.keys())

    async def test_a_handoff_after_a_restart_under_stagemix_is_logged(self):
        # #118's headline case: the box restarted while StageMix had the DCA,
        # so the level already reads unknown and the hand-off changes nothing
        # on the box - but the tap is the only record of it.
        app = self.build(level_known=False)
        before = app.machine
        outcome = await app.handoff()
        self.assertIsNone(outcome.refusal)
        self.assertEqual(app.machine, before)
        self.assertEqual(self.console_sender.packets, [])
        self.assertFalse(app.snapshot()["fader"]["level_known"])
        self.assertIn(tacet_app.HANDED_OFF, self.keys())

    async def test_report_ready_sends_no_packet_and_assumes_the_hold_level(self):
        app = self.build(level_known=False)
        outcome = await app.report_ready()
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual(outcome.fader, state.FaderCommand.REPORT_READY)
        self.assertEqual(self.console.commanded_level, app._hold_level())
        self.assertEqual(app.machine.state, state.State.READY)
        self.assertTrue(app.snapshot()["fader"]["level_known"])
        self.assertIn(tacet_app.ARMED, self.keys())
        self.assertIn(tacet_app.TOOK_BACK, self.keys())

    async def test_report_ready_is_logged_as_the_belief_it_is(self):
        app = self.build(level_known=False)
        await app.report_ready()
        commanded = [e for e in self.entries() if e.event == tacet_app.COMMANDED][-1]
        self.assertEqual(commanded.data["command"], "report-ready")
        self.assertEqual(commanded.data["detail"], "report-ready")
        self.assertEqual(commanded.data["level"], app._hold_level())

    async def test_report_ready_is_refused_once_the_level_is_known(self):
        app = self.build()
        await app.arm()
        outcome = await app.report_ready()
        self.assertEqual(outcome.refusal, state.LEVEL_ALREADY_KNOWN)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)

    async def test_a_fade_after_a_ready_report_is_ordinary(self):
        app = self.build(level_known=False, fade=0.05)
        await app.report_ready()
        await app.release()
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_the_belief_answers_are_stale_checked_like_every_fader_tap(self):
        # Parity with stand_down and every other fader tap (#16), kept deliberately.
        stale = late(taps.DEFAULT_STALE_TAP_SECONDS + 1.0)
        for name in ("close_now", "report_ready"):
            with self.subTest(name=name):
                app = self.build(level_known=False)
                outcome = await getattr(app, name)(tap=stale)
                self.assertFalse(outcome.changed)
                self.assertFalse(app.machine.level_known)
                self.assertEqual(self.console_sender.packets, [])
        self.assertIn(tacet_app.STALE_TAP, self.keys())

    async def test_still_mine_is_logged_and_changes_nothing(self):
        app = self.build()
        await app.arm()
        before = app.machine
        await app.confirm_still_mine()
        self.assertEqual(app.machine, before)
        self.assertIn(tacet_app.STILL_MINE, self.keys())

    async def test_the_snapshot_carries_level_known_and_no_handoff_or_queue(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        snap = app.snapshot()
        self.assertTrue(snap["fader"]["level_known"])
        await app.handoff()
        snap = app.snapshot()
        self.assertFalse(snap["fader"]["level_known"])
        # Two facts became one (#107): the top-level flags are gone.
        self.assertNotIn("handoff", snap)
        self.assertNotIn("queued", snap)

    async def test_the_why_line_says_the_level_is_unknown_and_not_why(self):
        app = self.build(level_known=False)
        why = app.snapshot()["why"].lower()
        self.assertIn("does not know where the fader", why)
        self.assertNotIn("stagemix", why)

    async def test_an_absolute_command_whose_packet_failed_un_knows_the_level(self):
        # #116: the open is absolute, so `step` marks the level known the
        # moment it is applied - but a send that came back having delivered
        # nothing never earned that, and the belief goes back with it. What
        # cannot be seen from here is a packet that left the box and was
        # simply lost: the protocol has no acknowledgement (#18).
        app = self.build(console_sender=FailingSender(), level_known=False)
        await app.trigger()
        self.assertFalse(app.machine.level_known)
        self.assertTrue(app.machine.stalled)
        self.assertFalse(app.snapshot()["fader"]["healthy"])
        self.assertFalse(app.snapshot()["fader"]["level_known"])

    async def test_the_snapshot_carries_when_the_last_command_was_sent(self):
        # Built directly rather than through `build()`: the console and the
        # app need to share one fake clock, which `build()`'s `steady` option
        # does not wire up - it gives the console its own separate one. The
        # clock also has to advance on its own `sleep`, like LoopClock in
        # test_dm7.py, or the console's own ramp-scheduling loop never sees
        # time pass and spins forever waiting for a step that is always "due".
        clock = LoopClock()
        self.log = ann.AnnotationLog(self.root / "game.jsonl")
        self.log.open()
        self.addCleanup(self.log.close)
        console = dm7.Dm7Client(
            "192.0.2.1", dca=3, sender=self.console_sender, monotonic=clock.monotonic, sleep=clock.sleep
        )
        app = tacet_app.App(
            console=console,
            log=self.log,
            recorder=None,
            monotonic=clock.monotonic,
            machine=state.Machine(level_known=True),
        )
        self.assertIsNone(app.snapshot()["fader"]["sent_at"])
        await app.arm()
        await app.trigger()
        sent_at = clock.now
        self.assertEqual(app.snapshot()["fader"]["sent_at"], sent_at)
        # Fixed at the send, not an age: nothing was sent in between, so it
        # must not move, or every snapshot differs from the last and the
        # playhead coalescing never holds anything back (#147).
        clock.now += 10.0
        self.assertEqual(app.snapshot()["fader"]["sent_at"], sent_at)


class TestTheStandingTarget(AppTestCase):
    """#9: where an open goes is a setting the operator can change, and
    changing it moves nothing.

    Two things are called a target and they are not the same. `snapshot()
    ["target"]` is the standing setting: the level the next open goes to.
    `snapshot()["fader"]["target"]` is where a move already in flight is
    heading. A ride-in that started before the setting changed keeps going to
    the old level, so the two differ, and the tests below say which is which.
    """

    def sent(self):
        return len(self.console_sender.packets)

    def commanded_entries(self):
        return [e for e in self.entries() if e.event == tacet_app.COMMANDED]

    async def test_the_default_target_is_the_first_preset(self):
        app = self.build()
        target = app.snapshot()["target"]
        self.assertEqual(target["level"], dm7.UNITY)
        self.assertEqual(target["db"], 0.0)
        self.assertEqual(target["default_db"], 0.0)
        self.assertEqual(target["presets_db"], [0.0, -3.0, -6.0])

    async def test_a_site_that_starts_quieter_gets_that_as_its_default(self):
        self.build()
        app = tacet_app.App(
            console=self.console,
            log=self.log,
            target_levels=targets.build((-2.0, -5.0, -8.0), 3.0),
            machine=state.Machine(level_known=True),
        )
        self.assertEqual(app.snapshot()["target"]["db"], -2.0)
        self.assertEqual(app.snapshot()["target"]["default_db"], -2.0)
        await app.arm()
        await app.trigger()
        self.assertEqual(self.console.commanded_level, -200)

    async def test_a_site_whose_default_is_not_the_first_preset_opens_to_it(self):
        self.build()
        app = tacet_app.App(
            console=self.console,
            log=self.log,
            target_levels=targets.build((3.0, 0.0, -3.0), 3.0, 0.0),
            machine=state.Machine(level_known=True),
        )
        target = app.snapshot()["target"]
        self.assertEqual(target["db"], 0.0)
        self.assertEqual(target["default_db"], 0.0)
        self.assertEqual(target["presets_db"], [3.0, 0.0, -3.0])
        await app.arm()
        await app.trigger()
        self.assertEqual(self.console.commanded_level, 0)

    async def test_target_set_records_default_against_the_configured_default_not_the_first_preset(self):
        self.build()
        app = tacet_app.App(
            console=self.console,
            log=self.log,
            target_levels=targets.build((3.0, 0.0, -3.0), 3.0, 0.0),
            machine=state.Machine(level_known=True),
        )
        await app.set_target(3.0)
        entry = [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]
        self.assertFalse(entry.data["default"])
        await app.set_target(0.0)
        entry = [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]
        self.assertTrue(entry.data["default"])

    async def releasing_it(self, app):
        # Set directly: a real fade would send packets of its own in the
        # background, and this test counts them.
        app.machine = state.Machine(state=state.State.RELEASING, level_known=True)

    async def test_setting_the_target_while_closed_or_releasing_sends_no_packet_and_leaves_the_level_alone(self):
        for label, prepare in (
            ("standing down", None),
            ("idle", lambda app: app.arm()),
            ("releasing", self.releasing_it),
        ):
            with self.subTest(state=label):
                app = self.build()
                if prepare is not None:
                    await prepare(app)
                before, level = self.sent(), self.console.commanded_level
                await app.set_target(-3.0)
                self.assertEqual(self.sent(), before)
                self.assertEqual(self.console.commanded_level, level)
                self.assertEqual(app.snapshot()["target"]["db"], -3.0)

    async def open_it(self, app):
        await app.arm()
        await app.trigger()

    async def test_the_next_open_goes_to_the_new_target(self):
        app = self.build()
        await app.arm()
        await app.set_target(-3.0)
        await app.trigger()
        self.assertEqual(self.console.commanded_level, -300)
        self.assertEqual(self.console_sender.levels()[-1], -300)
        opened = self.commanded_entries()[-1]
        self.assertEqual(opened.data["target"], -300)
        self.assertEqual(opened.data["target_db"], -3.0)
        self.assertEqual(app.snapshot()["fader"]["db"], -3.0)

    async def test_a_slow_open_goes_to_the_new_target_too(self):
        app = self.build()
        app._slow_open_seconds = 0.1
        await app.arm()
        await app.set_target(-6.0)
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -600)

    async def test_the_hold_level_follows_the_target(self):
        app = self.build()
        app._ready_ride_seconds = 0.1
        await app.arm()
        await app.set_target(-6.0)
        self.assertEqual(app._hold_level(), dm7.clamp(-600 - round(app._hold_below_db * dm7.UNITS_PER_DB)))
        await app.annotate("up-ready")
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, app._hold_level())
        self.assertEqual(self.console.commanded_level, -2100)

    async def test_a_ready_report_after_the_change_assumes_the_new_hold_level(self):
        app = self.build(level_known=False)
        await app.set_target(-6.0)
        await app.report_ready()
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual(self.console.commanded_level, -2100)
        self.assertEqual(app.machine.state, state.State.READY)

    async def test_fader_target_is_where_a_move_is_going_and_target_level_is_the_standing_setting(self):
        # The naming trap, pinned: while READY rides to its hold level the two
        # disagree, and no target tap is needed for that. (A target tap mid-ride
        # now retargets the ride, #128, so it can no longer make them differ.)
        app = self.build()
        app._ready_ride_seconds = 0.4
        await app.arm()
        await app.annotate("up-ready")
        snap = app.snapshot()
        self.assertNotEqual(snap["fader"]["target"], snap["target"]["level"])
        self.assertEqual(snap["fader"]["target"], -1500)
        self.assertEqual(snap["target"]["level"], 0)
        await app.wait_for_fade()

    async def test_the_standing_setting_is_not_inside_the_fader_block(self):
        snap = self.build().snapshot()
        self.assertIn("presets_db", snap["target"])
        self.assertNotIn("presets_db", snap["fader"])
        # Nothing in flight: `fader.target` is null while `target.level` is set.
        self.assertIsNone(snap["fader"]["target"])
        self.assertIsNotNone(snap["target"]["level"])

    async def test_a_target_change_while_releasing_stores_only_and_the_fade_still_ends_at_minus_infinity(self):
        app = self.build(fade=0.2)
        await self.open_it(app)
        await app.release()
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.machine.state, state.State.RELEASING)
        entries_before = len(self.commanded_entries())

        await app.set_target(-3.0)

        self.assertEqual(app.snapshot()["fader"]["target"], dm7.MINUS_INF)
        self.assertEqual(len(self.commanded_entries()), entries_before)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.snapshot()["target"]["db"], -3.0)

    async def test_a_level_that_is_not_a_preset_is_refused_and_changes_nothing(self):
        for label, level_known in (("known", True), ("unknown", False)):
            for bad in (-2.5, 3.0, 0.5, -2.99):
                with self.subTest(level=label, db=bad):
                    app = self.build(level_known=level_known)
                    before = self.sent()
                    keys = self.keys()
                    await app.set_target(bad)
                    snap = app.snapshot()
                    self.assertIn(f"{bad:g}", snap["refusal"])
                    self.assertIn("-3", snap["refusal"])
                    self.assertEqual(self.sent(), before)
                    self.assertEqual(self.keys(), keys)
                    self.assertNotIn(tacet_app.TARGET_SET, self.keys())
                    self.assertEqual(snap["target"]["db"], 0.0)

    async def test_a_non_finite_level_is_refused_like_any_other_non_preset(self):
        # `set_target` is a public coroutine, so it cannot rely on the route to
        # have kept `inf` out: `round(inf * 100)` raises OverflowError.
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(db=bad):
                app = self.build()
                before, keys = self.sent(), self.keys()
                await app.set_target(bad)
                snap = app.snapshot()
                self.assertTrue(snap["refusal"].startswith("Not a target level:"))
                self.assertEqual(self.sent(), before)
                self.assertEqual(self.keys(), keys)
                self.assertEqual(snap["target"]["db"], 0.0)

    async def test_a_preset_is_matched_in_console_units(self):
        app = self.build()
        await app.set_target(-3.004)
        self.assertEqual(app.snapshot()["target"]["level"], -300)
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_the_target_can_be_changed_in_every_state_and_while_the_level_is_unknown(self):
        app = self.build(level_known=False)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        await app.set_target(-3.0)
        self.assertEqual(app.snapshot()["target"]["db"], -3.0)
        self.assertIsNone(app.snapshot()["refusal"])
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertFalse(app.machine.level_known)
        self.assertEqual(self.console_sender.packets, [])

    async def test_a_late_target_tap_that_only_stores_is_still_applied(self):
        # Not stale-checked while it would only store (#9): there is no late
        # move to guard against, and a silently refused tap is a dead end.
        for label, prepare in (("standing down", None), ("idle", lambda app: app.arm())):
            with self.subTest(state=label):
                app = self.build()
                if prepare is not None:
                    await prepare(app)
                await app.set_target(-3.0, tap=late(taps.DEFAULT_STALE_TAP_SECONDS + 5.0))
                self.assertEqual(app.snapshot()["target"]["db"], -3.0)
                self.assertIsNone(app.snapshot()["stale_tap"])
                self.assertNotIn(tacet_app.STALE_TAP, self.keys())

    async def test_a_change_is_logged_with_what_it_replaced(self):
        app = self.build()
        await app.arm()
        await app.set_target(-3.0)
        entry = [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]
        self.assertEqual(
            {k: v for k, v in entry.data.items() if k != "tap"},
            {
                "level": -300,
                "db": -3.0,
                "previous_level": 0,
                "previous_db": 0.0,
                "default": False,
                "state": state.State.IDLE.value,
                # The log keeps the reason even though the page shows nothing
                # for a tap while the fader is closed (#153).
                "stored_because": "closed",
            },
        )

    async def test_going_back_to_the_default_says_so(self):
        app = self.build()
        await app.set_target(-3.0)
        await app.set_target(0.0)
        entry = [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]
        self.assertTrue(entry.data["default"])
        self.assertEqual(entry.data["previous_db"], -3.0)

    async def test_the_tap_stamp_reaches_the_log_entry(self):
        app = self.build()
        tap = TestEveryEntryATapProducesCarriesItsTiming.TAP
        await app.set_target(-3.0, tap=tap)
        entry = [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]
        self.assertEqual(entry.data["tap"], tap.as_data())

    async def test_target_set_is_a_session_entry_and_never_a_button(self):
        event = ann.lookup(tacet_app.TARGET_SET)
        self.assertEqual(event.category, ann.Category.SESSION)
        self.assertNotIn(tacet_app.TARGET_SET, [b.key for b in ann.BUTTONS])

    async def test_a_change_clears_an_earlier_refusal_and_a_refused_one_sets_it(self):
        app = self.build(level_known=False)
        await app.arm()
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_ARM)
        await app.set_target(-3.0)
        self.assertIsNone(app.snapshot()["refusal"])
        await app.set_target(-2.5)
        self.assertIsNotNone(app.snapshot()["refusal"])
        await app.set_target(-6.0)
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_listeners_are_told_of_a_change_and_of_a_refusal(self):
        app = self.build()
        told = []
        app.on_change(lambda: told.append(app.snapshot()["target"]["db"]))
        await app.set_target(-3.0)
        await app.set_target(-2.5)
        self.assertEqual(told, [-3.0, -3.0])

    async def test_every_packet_of_a_cycle_with_a_target_change_is_a_fader_level_write(self):
        # The app-level sibling of test_dm7's faders-only test: the target has
        # its own path now, and nothing it does may be anything but that write.
        app = self.build(fade=0.05)
        app._slow_open_seconds = 0.05
        app._retarget_ride_seconds = 0.05
        app._ready_ride_seconds = 0.05
        await app.arm()
        await app.trigger()
        await app.set_target(-3.0)  # a retarget ride while open (#128)
        await app.wait_for_fade()
        await app.release()
        await app.wait_for_fade()
        await app.annotate("up-slow")
        await app.set_target(-6.0)  # retargets the ride-in
        await app.wait_for_fade()
        await app.release()
        await app.wait_for_fade()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        await app.set_target(0.0)  # moves the READY hold
        await app.wait_for_fade()
        await app.release()
        await app.wait_for_fade()
        self.assertGreater(self.sent(), 3)
        levels = self.console_sender.levels()
        self.assertIn(-300, levels)
        self.assertIn(-600, levels)
        self.assertIn(-1500, levels)
        for address in self.console_sender.addresses():
            self.assertEqual(address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3")
        for level in levels:
            self.assertIsInstance(level, int)


class TestARetargetRide(AppTestCase):
    """#128: a target tap while the fader is up rides there. It is an operator
    tap on the fader, so it is stale-checked like one, refused (the ride, not
    the store) while the level is unknown, and never a mute."""

    def commanded_entries(self):
        return [e for e in self.entries() if e.event == tacet_app.COMMANDED]

    def target_sets(self):
        return [e for e in self.entries() if e.event == tacet_app.TARGET_SET]

    def build_up(self, **kwargs):
        app = self.build(**kwargs)
        app._retarget_ride_seconds = 0.05
        app._ready_ride_seconds = 0.05
        return app

    async def open_it(self, app):
        await app.arm()
        await app.trigger()

    async def ready_it(self, app):
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.READY)

    async def test_a_target_tap_while_open_rides_to_the_new_level(self):
        app = self.build_up()
        await self.open_it(app)
        before = len(self.console_sender.packets)
        await app.set_target(-3.0)
        move = app.snapshot()["fader"]["move"]
        self.assertEqual(move["kind"], "ride")
        self.assertEqual(move["by"], tacet_app.TARGET_SET)
        self.assertEqual(move["to_db"], -3.0)
        self.assertIsNotNone(move["knee"])
        await app.wait_for_fade()
        self.assertGreater(len(self.console_sender.packets), before)
        self.assertEqual(self.console_sender.levels()[-1], -300)
        self.assertEqual(self.console.commanded_level, -300)
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def test_the_ride_is_commanded_by_the_target_tap_and_lands(self):
        app = self.build_up()
        await self.open_it(app)
        await app.set_target(-3.0)
        await app.wait_for_fade()
        keys = [e.event for e in self.entries()]
        commanded = [e for e in self.commanded_entries() if e.data.get("command") == "retarget"]
        self.assertEqual(len(commanded), 1)
        self.assertEqual(commanded[0].data["detail"], tacet_app.TARGET_SET)
        self.assertEqual(commanded[0].data["target"], -300)
        self.assertIsNone(commanded[0].data["delivered"])
        self.assertIsNone(self.target_sets()[-1].data["stored_because"])
        # The move goes first and the target-set entry after it, then it lands.
        last_commanded = max(i for i, k in enumerate(keys) if k == tacet_app.COMMANDED)
        self.assertLess(last_commanded, keys.index(tacet_app.TARGET_SET))
        last_landed = max(i for i, k in enumerate(keys) if k == tacet_app.MOVE_LANDED)
        self.assertLess(keys.index(tacet_app.TARGET_SET), last_landed)
        landed = [e for e in self.entries() if e.event == tacet_app.MOVE_LANDED]
        self.assertEqual(landed[-1].data["target"], -300)

    async def test_a_target_tap_while_riding_in_retargets_the_ride(self):
        app = self.build()
        app._slow_open_seconds = 0.4
        app._retarget_ride_seconds = 0.4
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.set_target(-6.0)
        self.assertEqual(app.snapshot()["fader"]["move"]["to_db"], -6.0)
        await asyncio.sleep(UNDER_WAY)
        # A fast trigger mid-retarget still snaps, now to the new target.
        await app.trigger()
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -600)

        app = self.build()
        app._slow_open_seconds = 0.4
        app._retarget_ride_seconds = 0.05
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.set_target(-6.0)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -600)

    async def test_a_target_tap_while_ready_moves_the_hold(self):
        app = self.build_up()
        await self.ready_it(app)
        self.assertEqual(self.console.commanded_level, -1500)
        await app.set_target(-6.0)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -2100)
        self.assertEqual(app.machine.state, state.State.READY)
        # The commit still goes to the new target.
        await app.trigger()
        self.assertEqual(self.console.commanded_level, -600)

    async def test_a_target_tap_mid_ready_ride_moves_the_ride_to_the_new_hold(self):
        app = self.build()
        app._ready_ride_seconds = 0.4
        app._retarget_ride_seconds = 0.05
        await app.arm()
        await app.annotate("up-ready")
        await asyncio.sleep(UNDER_WAY)
        await app.set_target(-6.0)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -2100)
        self.assertEqual(app.machine.state, state.State.READY)
        self.assertFalse(app.machine.riding_in)

    async def test_a_target_tap_while_releasing_stores_and_the_fade_still_ends_at_minus_infinity(self):
        app = self.build(fade=0.2)
        await self.open_it(app)
        await app.release()
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.machine.state, state.State.RELEASING)
        entries_before = len(self.commanded_entries())
        await app.set_target(-3.0)
        self.assertEqual(app.snapshot()["target"]["stored"], {"db": -3.0, "because": "releasing"})
        self.assertEqual(len(self.commanded_entries()), entries_before)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)

    async def test_a_target_tap_while_up_and_the_level_unknown_stores_and_refuses_the_ride(self):
        for which in (state.State.OPEN, state.State.READY):
            with self.subTest(state=which.value):
                app = self.build_up()
                app.machine = state.Machine(state=which, level_known=False)
                before = len(self.console_sender.packets)
                commanded = len(self.commanded_entries())
                await app.set_target(-3.0)
                snap = app.snapshot()
                self.assertEqual(len(self.console_sender.packets), before)
                self.assertEqual(snap["target"]["db"], -3.0)
                self.assertEqual(snap["refusal"], state.UNKNOWN_LEVEL_MOVE)
                self.assertEqual(snap["target"]["stored"], {"db": -3.0, "because": "unknown"})
                self.assertEqual(self.target_sets()[-1].data["stored_because"], "unknown")
                self.assertEqual(len(self.commanded_entries()), commanded)

    async def test_a_late_target_tap_that_would_ride_is_refused_and_not_stored(self):
        app = self.build_up()
        await self.open_it(app)
        before = len(self.console_sender.packets)
        await app.set_target(-3.0, tap=late(taps.DEFAULT_STALE_TAP_SECONDS + 5.0))
        snap = app.snapshot()
        self.assertIsNotNone(snap["stale_tap"])
        self.assertEqual(snap["target"]["db"], 0.0)
        self.assertIsNone(snap["fader"]["move"])
        self.assertEqual(len(self.console_sender.packets), before)
        self.assertNotIn(tacet_app.TARGET_SET, self.keys())
        stale = [e for e in self.entries() if e.event == tacet_app.STALE_TAP]
        self.assertEqual(stale[-1].data["command"], "set_target")

    async def test_a_tap_on_the_current_target_while_open_moves_nothing(self):
        app = self.build_up()
        await self.open_it(app)
        before = len(self.console_sender.packets)
        await app.set_target(0.0)
        self.assertEqual(len(self.console_sender.packets), before)
        self.assertIsNone(app.snapshot()["fader"]["move"])
        self.assertEqual(app.snapshot()["target"]["stored"], {"db": 0.0, "because": "unchanged"})

    async def test_after_a_failed_retarget_ride_the_same_segment_retries_it(self):
        sender = FlakySender()
        app = self.build_up(console_sender=sender, steady=True)
        await self.open_it(app)
        sender.fail_after = len(sender.packets) + 2
        await app.set_target(-3.0)
        await app.wait_for_fade()
        self.assertIn(tacet_app.MOVE_FAILED, self.keys())
        self.assertTrue(app.machine.level_known)
        self.assertTrue(app.machine.stalled)
        sender.heal()
        await app.set_target(-3.0)
        await app.wait_for_fade()
        self.assertEqual(self.console.commanded_level, -300)
        self.assertFalse(app.machine.stalled)

    async def test_a_fade_mid_retarget_takes_over(self):
        app = self.build(fade=5.0)
        app._retarget_ride_seconds = 5.0
        await self.open_it(app)
        await app.set_target(-3.0)
        await asyncio.sleep(UNDER_WAY)
        await app.release()
        await asyncio.sleep(SUPERSEDED_WAKES)
        move = app.snapshot()["fader"]["move"]
        self.assertEqual(move["kind"], "fade")
        self.assertIsNone(move["by"])  # a bare release names no button
        app._cancel_move()

    async def test_a_handoff_mid_retarget_cancels_the_ride(self):
        app = self.build()
        app._retarget_ride_seconds = 5.0
        await self.open_it(app)
        await app.set_target(-3.0)
        await asyncio.sleep(UNDER_WAY)
        await app.handoff()
        snap = app.snapshot()
        self.assertIsNone(snap["fader"]["move"])
        self.assertFalse(snap["fader"]["level_known"])

    async def test_the_target_note_is_empty_after_a_ride(self):
        app = self.build_up()
        await self.open_it(app)
        await app.set_target(-3.0)
        await app.wait_for_fade()
        self.assertIsNone(app.snapshot()["target"]["stored"])


class TestATargetTapThatOnlyStoresSaysSo(AppTestCase):
    """#153: a target tap stores and moves nothing, and while the fader is up
    the page is told so, with the reason. A tap while the fader is closed is
    what the operator expects, so the page is told nothing - but the log still
    says why it only stored."""

    FADER_UP = (state.State.OPEN, state.State.READY, state.State.RELEASING)

    def stored(self, app):
        return app.snapshot()["target"]["stored"]

    def target_set(self):
        return [e for e in self.entries() if e.event == tacet_app.TARGET_SET][-1]

    async def open_it(self, app):
        await app.arm()
        await app.annotate("up-whistle")

    async def ready_it(self, app):
        app._ready_ride_seconds = 0.05
        await app.arm()
        await app.annotate("up-ready")
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.READY)

    def test_stored_only_because_names_every_state(self):
        expected = {
            state.State.STANDING_DOWN: tacet_app.StoredOnly.CLOSED,
            state.State.IDLE: tacet_app.StoredOnly.CLOSED,
            state.State.OPEN: tacet_app.StoredOnly.OPEN,
            state.State.READY: tacet_app.StoredOnly.READY,
            state.State.RELEASING: tacet_app.StoredOnly.RELEASING,
        }
        self.assertEqual(set(expected), set(state.State))
        for which, because in expected.items():
            with self.subTest(state=which.value):
                machine = state.Machine(state=which)
                self.assertIs(tacet_app.stored_only_because(machine, None, unchanged=False), because)
                self.assertIs(
                    tacet_app.stored_only_because(machine, None, unchanged=True), tacet_app.StoredOnly.UNCHANGED
                )
        riding = state.Machine(state=state.State.OPEN, riding_in=True)
        self.assertIs(tacet_app.stored_only_because(riding, None, unchanged=False), tacet_app.StoredOnly.OPEN)

    def test_stored_only_because_reads_the_outcome(self):
        opened = state.Machine(state=state.State.OPEN, level_known=True)
        rode = state.step(opened, state.Event(state.Command.RETARGET))
        self.assertIsNone(tacet_app.stored_only_because(opened, rode, unchanged=False))
        unknown = state.Machine(state=state.State.OPEN, level_known=False)
        refused = state.step(unknown, state.Event(state.Command.RETARGET))
        self.assertIs(tacet_app.stored_only_because(unknown, refused, unchanged=False), tacet_app.StoredOnly.UNKNOWN)
        self.assertEqual(refused.refusal, state.UNKNOWN_LEVEL_MOVE)
        for which, because in (
            (state.State.IDLE, tacet_app.StoredOnly.CLOSED),
            (state.State.RELEASING, tacet_app.StoredOnly.RELEASING),
        ):
            with self.subTest(state=which.value):
                machine = state.Machine(state=which, level_known=True)
                outcome = state.step(machine, state.Event(state.Command.RETARGET))
                self.assertIs(tacet_app.stored_only_because(machine, outcome, unchanged=False), because)
        self.assertIs(tacet_app.stored_only_because(opened, None, unchanged=True), tacet_app.StoredOnly.UNCHANGED)

    def test_stored_note_shown_only_while_the_fader_is_up(self):
        for which in state.State:
            with self.subTest(state=which.value):
                self.assertEqual(
                    tacet_app.stored_note_shown(state.Machine(state=which)),
                    which in self.FADER_UP,
                )

    async def test_nothing_is_stored_before_any_target_tap(self):
        app = self.build()
        self.assertIsNone(self.stored(app))

    async def test_a_target_set_while_open_and_riding_leaves_no_note(self):
        # #128: it rode, so there is nothing to say it did not.
        app = self.build()
        app._retarget_ride_seconds = 0.05
        await self.open_it(app)
        await app.set_target(-3.0)
        self.assertIsNone(self.stored(app))
        await app.wait_for_fade()

    async def test_a_target_set_while_up_and_unknown_says_the_box_does_not_know_where_it_is(self):
        for which in (state.State.OPEN, state.State.READY):
            with self.subTest(state=which.value):
                app = self.build()
                app.machine = state.Machine(state=which, level_known=False)
                before = len(self.console_sender.packets)
                await app.set_target(-3.0)
                self.assertEqual(self.stored(app), {"db": -3.0, "because": "unknown"})
                self.assertEqual(len(self.console_sender.packets), before)

    async def test_a_target_set_while_closed_shows_no_note_but_logs_why(self):
        for label, level_known, arm in (
            ("standing down", True, False),
            ("idle", True, True),
            ("standing down, level unknown", False, False),
        ):
            with self.subTest(state=label):
                app = self.build(level_known=level_known)
                if arm:
                    await app.arm()
                await app.set_target(-3.0)
                self.assertIsNone(self.stored(app))
                self.assertEqual(self.target_set().data["stored_because"], "closed")

    async def test_a_target_set_while_releasing_says_the_fade_carries_on(self):
        app = self.build()
        app.machine = state.Machine(state=state.State.RELEASING, level_known=True)
        await app.set_target(-6.0)
        self.assertEqual(self.stored(app), {"db": -6.0, "because": "releasing"})

    async def test_a_tap_on_the_target_already_set_while_open_says_nothing_changed(self):
        app = self.build()
        await self.open_it(app)
        before = len(self.console_sender.packets)
        await app.set_target(0.0)
        self.assertEqual(self.stored(app), {"db": 0.0, "because": "unchanged"})
        self.assertEqual(self.target_set().data["stored_because"], "unchanged")
        self.assertEqual(len(self.console_sender.packets), before)

    async def test_a_tap_on_the_target_already_set_while_closed_shows_no_note(self):
        app = self.build()
        await app.arm()
        await app.set_target(0.0)
        self.assertIsNone(self.stored(app))
        self.assertEqual(self.target_set().data["stored_because"], "unchanged")

    async def test_the_note_clears_when_the_fader_next_moves(self):
        # Open and unknown: the tap stores and refuses the ride, and the
        # instant close, which is absolute, then moves the fader (#107).
        app = self.build()
        app.machine = state.Machine(state=state.State.OPEN, level_known=False)
        await app.set_target(-3.0)
        self.assertIsNotNone(self.stored(app))
        await app.close_now()
        self.assertIsNone(self.stored(app))

        app = self.build()
        app.machine = state.Machine(state=state.State.READY, level_known=False)
        await app.set_target(-6.0)
        self.assertIsNotNone(self.stored(app))
        await app.trigger()
        self.assertIsNone(self.stored(app))

    async def test_the_note_clears_when_the_state_changes(self):
        app = self.build()
        await self.open_it(app)
        await app.release()
        self.assertEqual(app.machine.state, state.State.RELEASING)
        await app.set_target(-3.0)
        self.assertEqual(self.stored(app), {"db": -3.0, "because": "releasing"})
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIsNone(self.stored(app))

    async def test_a_handoff_that_leaves_the_state_alone_keeps_the_note(self):
        app = self.build()
        app.machine = state.Machine(state=state.State.OPEN, level_known=False)
        await app.set_target(-3.0)
        await app.handoff()
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.stored(app), {"db": -3.0, "because": "unknown"})

    async def test_a_refused_level_leaves_the_note_alone(self):
        app = self.build()
        app.machine = state.Machine(state=state.State.RELEASING, level_known=True)
        await app.set_target(-3.0)
        await app.set_target(-2.5)
        self.assertEqual(self.stored(app), {"db": -3.0, "because": "releasing"})

    async def test_a_newer_tap_replaces_the_note(self):
        app = self.build()
        app.machine = state.Machine(state=state.State.RELEASING, level_known=True)
        await app.set_target(-3.0)
        await app.set_target(-6.0)
        self.assertEqual(self.stored(app), {"db": -6.0, "because": "releasing"})

    async def test_target_set_logs_why_it_only_stored(self):
        app = self.build()
        app.machine = state.Machine(state=state.State.RELEASING, level_known=True)
        await app.set_target(-3.0)
        self.assertEqual(self.target_set().data["stored_because"], "releasing")
        await app.set_target(-3.0)
        self.assertEqual(self.target_set().data["stored_because"], "unchanged")


class SwallowingConsole(dm7.Dm7Client):
    """The console client as it was before #34: a fade whose caller was
    cancelled returned as though it had finished. Here so the app's own guard
    is tested on its own rather than only behind the client's fix."""

    async def fade_out(self, seconds=dm7.DEFAULT_FADE_SECONDS):
        with contextlib.suppress(asyncio.CancelledError):
            await super().fade_out(seconds)


class TestAMoveIsDescribedOnce(AppTestCase):
    """#154: a fade or ride is described in `fader.move` when it starts and the
    fader block holds still until it ends, so nothing is pushed per ramp step."""

    async def test_a_fade_is_described_from_where_it_starts(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        started = app._monotonic()
        await app.annotate("out")
        fader = app.snapshot()["fader"]
        move = fader["move"]
        self.assertEqual(move["from_db"], 0.0)
        self.assertIsNone(move["to_db"])
        self.assertEqual(move["seconds"], 5.0)
        self.assertGreaterEqual(move["started_at"], started)
        self.assertEqual(move["by"], "out")
        self.assertEqual(move["kind"], "fade")
        self.assertEqual(move["seq"], 1)
        self.assertTrue(fader["moving"])
        self.assertEqual(fader["target"], dm7.MINUS_INF)
        app._cancel_move()

    async def test_up_slow_and_ready_are_described_as_rides_on_the_taper(self):
        app = self.build()
        app._slow_open_seconds = 1.5
        app._ready_ride_seconds = 4.0
        await app.arm()
        await app.annotate("up-slow")
        move = app.snapshot()["fader"]["move"]
        self.assertEqual(move["kind"], "ride")
        self.assertEqual(move["knee"], {"db": -20.0, "fraction": 0.15})
        self.assertEqual(move["to_db"], 0.0)
        self.assertEqual(move["seconds"], 1.5)
        self.assertEqual(move["by"], "up-slow")
        app._cancel_move()

        app = self.build()
        app._ready_ride_seconds = 4.0
        await app.arm()
        await app.annotate("up-ready")
        move = app.snapshot()["fader"]["move"]
        self.assertEqual(move["kind"], "ride")
        self.assertIsNotNone(move["knee"])
        self.assertEqual(move["to_db"], dm7.to_db(app._hold_level()))
        self.assertEqual(move["seconds"], 4.0)
        app._cancel_move()

    async def test_a_snap_open_and_close_now_are_not_described(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-drums")
        self.assertIsNone(app.snapshot()["fader"]["move"])
        await app.close_now()
        self.assertIsNone(app.snapshot()["fader"]["move"])

    async def test_the_description_goes_when_the_move_lands(self):
        app = self.build(fade=0.05)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.wait_for_fade()
        fader = app.snapshot()["fader"]
        self.assertIsNone(fader["move"])
        self.assertFalse(fader["moving"])
        self.assertEqual(fader["commanded"], dm7.MINUS_INF)
        self.assertEqual(fader["sent_at"], self.console.last_sent_at)

    async def test_a_snap_back_mid_fade_clears_the_description(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.annotate("up-whistle")
        fader = app.snapshot()["fader"]
        self.assertIsNone(fader["move"])
        self.assertEqual(fader["commanded"], dm7.UNITY)

    async def test_each_move_gets_a_new_seq(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        first = app.snapshot()["fader"]["move"]["seq"]
        await app.annotate("up-whistle")
        await app.annotate("out")
        second = app.snapshot()["fader"]["move"]["seq"]
        self.assertEqual((first, second), (1, 2))
        app._cancel_move()

    async def test_a_bare_release_names_no_button(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.release()
        move = app.snapshot()["fader"]["move"]
        self.assertEqual(move["kind"], "fade")
        self.assertIsNone(move["by"])
        app._cancel_move()

    async def test_a_failed_move_clears_the_description_and_shows_the_fault(self):
        sender = FlakySender()
        app = self.build(console_sender=sender, steady=True)
        await app.arm()
        await app.annotate("up-drums")
        sender.fail_after = len(sender.packets) + 2
        await app.annotate("out")
        await app.wait_for_fade()
        fader = app.snapshot()["fader"]
        self.assertIsNone(fader["move"])
        self.assertFalse(fader["healthy"])

    async def test_the_snapshot_does_not_change_while_a_move_runs(self):
        # The console and the app share one clock that is never late, so every
        # ramp step is sent however slowly the runner goes.
        clock = LoopClock()
        self.log = ann.AnnotationLog(self.root / "game.jsonl")
        self.log.open()
        self.addCleanup(self.log.close)
        console = dm7.Dm7Client(
            "192.0.2.1", dca=3, sender=self.console_sender, monotonic=clock.monotonic, sleep=clock.sleep
        )
        app = tacet_app.App(
            console=console,
            log=self.log,
            recorder=None,
            fade_seconds=1.0,
            monotonic=clock.monotonic,
            machine=state.Machine(level_known=True),
        )
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        sent_before = len(self.console_sender.packets)
        seen = []
        while app.snapshot()["fader"]["move"] is not None:
            snapshot = app.snapshot()
            del snapshot["at"]
            seen.append(snapshot)
            await asyncio.sleep(0)
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(snapshot == seen[0] for snapshot in seen))
        self.assertGreater(len(self.console_sender.packets) - sent_before, 10)

    async def test_moving_is_true_from_the_first_snapshot_of_a_move(self):
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        # Straight after the tap, before the ramp task has had a turn.
        self.assertTrue(app.snapshot()["fader"]["moving"])
        app._cancel_move()


class TestASupersededMove(AppTestCase):
    """A move replaced by a newer one must leave the newer one alone.

    Cancelling a move only asks. The old task wakes a loop iteration later,
    after its replacement has started, and its cleanup used to act as though it
    were still the current move: it stopped the replacement's page push,
    cleared its target, and - since the console client swallowed the
    cancellation - a stale fade could go on to complete a fade that was still
    running (#34).
    """

    async def test_a_superseded_fade_does_not_clear_the_ride_in_description(self):
        app = self.build(fade=5.0)
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.trigger()
        await app.annotate("out")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("up-slow")
        await asyncio.sleep(SUPERSEDED_WAKES)
        fader = app.snapshot()["fader"]
        self.assertEqual(fader["target"], dm7.UNITY)
        self.assertEqual(fader["move"]["kind"], "ride")
        self.assertEqual(fader["move"]["to_db"], 0.0)
        self.assertEqual(fader["move"]["seq"], 2)
        app._cancel_move()

    async def test_a_superseded_ride_in_does_not_clear_the_fade_description(self):
        app = self.build(fade=5.0)
        app._slow_open_seconds = 5.0
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        await app.annotate("out")
        await asyncio.sleep(SUPERSEDED_WAKES)
        fader = app.snapshot()["fader"]
        self.assertEqual(fader["target"], dm7.MINUS_INF)
        self.assertEqual(fader["move"]["kind"], "fade")
        self.assertEqual(fader["move"]["seq"], 2)
        app._cancel_move()

    async def test_a_stale_fade_task_cannot_complete_a_newer_fade(self):
        # A snap back to OPEN and a second close, landing in the same loop
        # iteration - two taps arriving together over stalled wifi.
        app = self.build(fade=5.0)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.sleep(UNDER_WAY)
        await asyncio.gather(app.trigger(), app.release())
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.machine.state, state.State.RELEASING)
        self.assertTrue(self.console.is_ramping)
        self.assertEqual(app.snapshot()["fader"]["target"], dm7.MINUS_INF)
        app._cancel_move()

    async def test_a_replaced_fade_that_returns_anyway_does_not_complete(self):
        # RELEASING alone does not say the fade that finished is the one the
        # machine is waiting on.
        app = self.build(fade=5.0, console_class=SwallowingConsole)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.sleep(UNDER_WAY)
        await asyncio.gather(app.trigger(), app.release())
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(app.machine.state, state.State.RELEASING)
        app._cancel_move()

    async def test_the_current_fade_still_completes(self):
        # The guard must not be so keen that nothing ever completes.
        app = self.build(fade=0.05)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.gather(app.trigger(), app.release())
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertIsNone(app.snapshot()["fader"]["target"])


class TestEveryEntryATapProducesCarriesItsTiming(AppTestCase):
    """#11: the box logged receipt, so a tap that arrived seconds late looked
    exactly like a prompt one. Each entry a tap produces now carries when it was
    tapped as well as when it arrived - including the ones a fade writes after
    the request has been answered."""

    # Prompt enough to be executed: a stale fader tap is refused, not stamped
    # and run (#16), and that has tests of its own.
    TAP = taps.TapTiming(received=100.0, tapped=99.5, delay=0.5, uncertainty=0.04)

    def taps_by_event(self):
        return {entry.event: entry.data.get("tap") for entry in self.entries()}

    async def test_an_annotation(self):
        app = self.build()
        await app.annotate("note", data={"text": "thin"}, tap=self.TAP)
        entry = self.entries()[-1]
        self.assertEqual(entry.data["tap"], self.TAP.as_data())
        self.assertEqual(entry.data["text"], "thin")

    async def test_a_fader_button_and_the_move_it_makes(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-drums", tap=self.TAP)
        stamped = self.taps_by_event()
        self.assertEqual(stamped["up-drums"], self.TAP.as_data())
        self.assertEqual(stamped[tacet_app.COMMANDED], self.TAP.as_data())

    async def test_a_fade_that_lands_after_the_request_was_answered(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release(tap=self.TAP)
        await app.wait_for_fade()
        landed = [e for e in self.entries() if e.event == tacet_app.MOVE_LANDED]
        self.assertEqual(landed[-1].data["tap"], self.TAP.as_data())

    async def test_a_stand_down_and_where_it_lands(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.stand_down(tap=self.TAP)
        await app.wait_for_fade()
        stamped = self.taps_by_event()
        self.assertEqual(stamped[tacet_app.STAND_DOWN_REQUESTED], self.TAP.as_data())
        self.assertEqual(stamped[tacet_app.STOOD_DOWN], self.TAP.as_data())

    async def test_spans_and_arming_and_recording(self):
        app = self.build()
        await app.arm(tap=self.TAP)
        span = await app.start_span("last-two-minutes", tap=self.TAP)
        assert span is not None
        await app.end_span(span, tap=self.TAP)
        self.reaper_parked(app)
        await app.start_recording(tap=self.TAP)
        for entry in self.entries():
            with self.subTest(entry.event, phase=entry.phase):
                self.assertEqual(entry.data["tap"], self.TAP.as_data())

    async def test_an_untapped_entry_carries_no_timing(self):
        app = self.build()
        await app.arm()
        await app.annotate("note", data={"text": "no stamp"})
        for entry in self.entries():
            self.assertNotIn(taps.TAP_FIELD, entry.data)

    async def test_one_tap_does_not_stamp_the_next(self):
        app = self.build()
        await app.annotate("note", data={"text": "first"}, tap=self.TAP)
        await app.annotate("note", data={"text": "second"})
        self.assertNotIn(taps.TAP_FIELD, self.entries()[-1].data)

    async def test_operator_data_cannot_claim_the_tap_key(self):
        app = self.build()
        with self.assertRaises(ann.DataError):
            await app.annotate("note", data={"tap": {"delay": 0}})
        self.assertEqual(self.entries(), [])


class TestSnapshotsSayWhenTheyWereTaken(AppTestCase):
    """#11: a delayed POST response rendered an older snapshot over newer pushes.
    The page compares this and keeps the newer one."""

    async def test_a_snapshot_carries_the_box_clock(self):
        clock = [50.0]
        app = self.build(monotonic=lambda: clock[0])
        first = app.snapshot()["at"]
        clock[0] = 51.0
        self.assertEqual((first, app.snapshot()["at"]), (50.0, 51.0))

    async def test_the_box_clock_is_what_taps_are_measured_on(self):
        clock = [42.0]
        app = self.build(monotonic=lambda: clock[0])
        self.assertEqual(app.now(), 42.0)


def late(delay, *, uncertainty=0.05, received=100.0):
    return taps.TapTiming(received=received, tapped=received - delay, delay=delay, uncertainty=uncertainty)


class TestStaleFaderTapsAreNotExecuted(AppTestCase):
    """#16: a late open brings the PA up after the music stopped, and a late
    fade cuts a band mid-phrase. A fader tap that arrived later than the
    threshold is logged and said out loud, and the operator decides again."""

    STALE = late(taps.DEFAULT_STALE_TAP_SECONDS + 1.0)
    PROMPT = late(0.1)

    def events(self):
        return [entry.event for entry in self.entries()]

    async def open_app(self):
        app = self.build()
        await app.arm()
        await app.trigger()
        return app

    def assert_refused_loudly(self, app, command):
        snapshot = app.snapshot()
        self.assertIn("not done", snapshot["refusal"])
        self.assertIn("3.0s", snapshot["refusal"])
        self.assertEqual(snapshot["stale_tap"], {"delay": 3.0, "threshold": taps.DEFAULT_STALE_TAP_SECONDS})
        stale = [e for e in self.entries() if e.event == tacet_app.STALE_TAP]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].data["command"], command)
        self.assertEqual(stale[0].data["tap"], self.STALE.as_data())

    async def test_a_stale_open_does_not_open(self):
        app = self.build()
        await app.arm()
        await app.trigger(tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console_sender.packets, [])
        self.assertNotIn(tacet_app.COMMANDED, self.events())
        self.assert_refused_loudly(app, "trigger")

    async def test_a_stale_fade_does_not_fade(self):
        app = await self.open_app()
        sent = len(self.console_sender.packets)
        await app.release(tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(len(self.console_sender.packets), sent)
        self.assert_refused_loudly(app, "release")

    async def test_a_stale_stand_down_does_not_stand_down(self):
        # It fades an open fader, so it is a fader tap.
        app = await self.open_app()
        await app.stand_down(tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertNotIn(tacet_app.STAND_DOWN_REQUESTED, self.events())
        self.assert_refused_loudly(app, "stand_down")

    async def test_a_stale_fader_button_keeps_its_reason_and_says_it_was_not_done(self):
        app = self.build()
        await app.arm()
        entry = await app.annotate("up-drums", tap=self.STALE)
        assert entry is not None
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console_sender.packets, [])
        self.assertEqual((entry.data["stale"], entry.data["executed"]), (True, False))
        self.assertEqual(entry.data["tap"], self.STALE.as_data())
        self.assertNotIn(tacet_app.STALE_TAP, self.events())
        self.assertIsNotNone(app.snapshot()["stale_tap"])

    async def test_an_annotation_is_never_refused_only_stamped(self):
        app = self.build()
        entry = await app.annotate("touchdown", tap=self.STALE)
        assert entry is not None
        self.assertNotIn("stale", entry.data)
        self.assertEqual(entry.data["tap"], self.STALE.as_data())
        self.assertIsNone(app.snapshot()["stale_tap"])

    async def test_spans_arming_and_recording_are_never_refused(self):
        app = self.build()
        await app.arm(tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIsNotNone(await app.start_span("last-two-minutes", tap=self.STALE))
        self.reaper_parked(app)
        await app.start_recording(tap=self.STALE)
        self.assertIn(tacet_app.RECORDING_REQUESTED, self.events())
        self.assertNotIn(tacet_app.STALE_TAP, self.events())

    async def test_a_prompt_tap_is_executed(self):
        app = self.build()
        await app.arm()
        await app.trigger(tap=self.PROMPT)
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertIsNone(app.snapshot()["stale_tap"])

    async def test_an_untimed_tap_is_executed_however_late_it_might_be(self):
        # It cannot be judged, so it is not refused (#16).
        app = self.build()
        await app.arm()
        await app.trigger(tap=taps.TapTiming(received=100.0))
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def test_the_threshold_is_the_boxs_to_set(self):
        app = self.build(stale_tap_seconds=10.0)
        await app.arm()
        await app.trigger(tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def test_the_next_tap_that_is_done_clears_the_warning(self):
        app = self.build()
        await app.arm()
        await app.trigger(tap=self.STALE)
        await app.trigger(tap=self.PROMPT)
        snapshot = app.snapshot()
        self.assertIsNone(snapshot["stale_tap"])
        self.assertIsNone(snapshot["refusal"])


class PromptTestCase(AppTestCase):
    """#19: the box asks whether to arm or stand down, and only the operator's
    tap on the answer ever changes anything.

    Named for the question, not the tap: `TestStaleFaderTapsAreNotExecuted.PROMPT`
    already means "a tap that was on time".
    """

    STALE = late(taps.DEFAULT_STALE_TAP_SECONDS + 1.0)

    def named(self, key):
        return [entry for entry in self.entries() if entry.event == key]

    def question(self, app):
        return app.snapshot()["prompt"]

    async def open_with_a_stand_down_question(self, app):
        """Armed and open, band-exits-stands tapped: a stand-down prompt, seq 1."""
        await app.arm()
        await app.annotate("up-whistle")
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["kind"], "stand-down")

    async def standing_down_with_an_arm_question(self, app):
        await app.annotate(ann.BAND_ENTERS_STANDS)
        self.assertEqual(self.question(app)["kind"], "arm")


class TestPromptsFromAnnotations(PromptTestCase):
    async def test_a_fresh_box_has_asked_nothing(self):
        app = self.build()
        self.assertIsNone(self.question(app))

    async def test_band_exits_stands_raises_a_stand_down_prompt_and_logs_it(self):
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app), {"seq": 1, "kind": "stand-down", "source": ann.BAND_EXITS_STANDS})
        keys = self.keys()
        # The reason the operator tapped comes first, then what the box asked.
        self.assertLess(keys.index(ann.BAND_EXITS_STANDS), keys.index(prompts.PROMPT_RAISED))
        (raised,) = self.named(prompts.PROMPT_RAISED)
        self.assertEqual(
            raised.data,
            {"prompt": "stand-down", "seq": 1, "source": ann.BAND_EXITS_STANDS, "state": "idle"},
        )

    async def test_band_enters_stands_raises_an_arm_prompt(self):
        app = self.build()
        await app.annotate(ann.BAND_ENTERS_STANDS)
        self.assertEqual(self.question(app), {"seq": 1, "kind": "arm", "source": ann.BAND_ENTERS_STANDS})
        (raised,) = self.named(prompts.PROMPT_RAISED)
        self.assertEqual(raised.data["state"], "standing-down")

    async def test_halftime_exodus_span_start_raises_a_stand_down_prompt(self):
        app = self.build()
        await app.arm()
        await app.start_span(ann.HALFTIME_EXODUS)
        self.assertEqual(self.question(app), {"seq": 1, "kind": "stand-down", "source": ann.HALFTIME_EXODUS})
        keys = self.keys()
        self.assertLess(keys.index(ann.HALFTIME_EXODUS), keys.index(prompts.PROMPT_RAISED))

    async def test_ending_the_halftime_exodus_span_raises_nothing(self):
        # D6: only the start of the span asks.
        app = self.build()
        await app.arm()
        span = await app.start_span(ann.HALFTIME_EXODUS)
        assert span is not None
        await app.dismiss_prompt(1)
        await app.end_span(span)
        self.assertIsNone(self.question(app))
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 1)

    async def test_a_span_that_asks_nothing_asks_nothing(self):
        app = self.build()
        await app.arm()
        await app.start_span("last-two-minutes")
        self.assertIsNone(self.question(app))
        self.assertEqual(self.named(prompts.PROMPT_RAISED), [])

    async def test_an_annotation_with_no_question_asks_nothing(self):
        app = self.build()
        await app.arm()
        for key in ("touchdown", "note", "band-exits-stadium"):
            await app.annotate(key)
        self.assertIsNone(self.question(app))

    async def test_a_prompt_that_already_matches_is_logged_resolved_and_never_raised(self):
        app = self.build()
        await app.annotate(ann.BAND_EXITS_STANDS)  # boot is STANDING DOWN
        self.assertIsNone(self.question(app))
        self.assertEqual(self.named(prompts.PROMPT_RAISED), [])
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual(
            resolved.data,
            {"prompt": "stand-down", "seq": None, "source": ann.BAND_EXITS_STANDS, "state": "standing-down"},
        )

    async def test_an_arm_prompt_that_already_matches_is_logged_resolved(self):
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_ENTERS_STANDS)
        self.assertIsNone(self.question(app))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual(resolved.data["prompt"], "arm")
        self.assertIsNone(resolved.data["seq"])

    async def test_resolving_directly_does_not_burn_a_seq(self):
        app = self.build()
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["seq"], 1)

    async def test_the_same_question_twice_is_one_prompt(self):
        # D4. The annotation itself is still logged twice, as always.
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.start_span(ann.HALFTIME_EXODUS)
        self.assertEqual(self.question(app)["seq"], 1)
        self.assertEqual(self.question(app)["source"], ann.BAND_EXITS_STANDS)
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 1)
        self.assertEqual(self.keys().count(ann.BAND_EXITS_STANDS), 2)

    async def test_a_question_the_operator_contradicts_withdraws_the_open_one(self):
        # A Stand down question is open and the operator says the opposite:
        # Arm is already true, so nothing is raised, and the contradicted
        # question comes down instead of staying on the page and unterminated
        # in the log. Nothing moves.
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        machine = app.machine
        sent = len(self.console_sender.packets)
        await app.annotate(ann.BAND_ENTERS_STANDS)
        self.assertIsNone(self.question(app))
        self.assertEqual(app.machine, machine)
        self.assertEqual(len(self.console_sender.packets), sent)
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        (withdrawn,) = self.named(prompts.PROMPT_WITHDRAWN)
        self.assertEqual((resolved.data["prompt"], resolved.data["seq"]), ("arm", None))
        self.assertEqual(
            withdrawn.data,
            {"prompt": "stand-down", "seq": 1, "source": ann.BAND_EXITS_STANDS, "state": "open"},
        )
        self.assertNotIn("replaced_by", withdrawn.data)
        # What happened to this tap first, then what it did to the question.
        keys = self.keys()
        self.assertLess(keys.index(prompts.PROMPT_RESOLVED), keys.index(prompts.PROMPT_WITHDRAWN))
        # Nothing was raised in its place, and there was no answer.
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 1)
        self.assertEqual(self.named(prompts.PROMPT_ACCEPTED), [])
        self.assertEqual(self.named(prompts.PROMPT_DISMISSED), [])

    async def test_a_mis_tapped_exit_taken_back_closes_the_question_and_leaves_the_fader_alone(self):
        # The hallway case: band-exits-stands mid-drive by mistake, then
        # band-enters-stands to correct it. The band is still playing; nothing
        # about either tap touches the fader or the duty state.
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        sent = self.console_sender.levels()
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["kind"], "stand-down")
        await app.annotate(ann.BAND_ENTERS_STANDS)
        self.assertIsNone(self.question(app))
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertEqual(self.console_sender.levels(), sent)
        self.assertEqual(self.keys().count(tacet_app.COMMANDED), 1)

    async def test_the_other_way_round_an_arm_question_is_withdrawn_by_a_stand_down_tap(self):
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.start_span(ann.HALFTIME_EXODUS)
        self.assertIsNone(self.question(app))
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        (withdrawn,) = self.named(prompts.PROMPT_WITHDRAWN)
        self.assertEqual((withdrawn.data["prompt"], withdrawn.data["seq"]), ("arm", 1))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual((resolved.data["prompt"], resolved.data["source"]), ("stand-down", ann.HALFTIME_EXODUS))

    async def test_a_withdrawn_question_can_be_asked_again_and_takes_a_fresh_seq(self):
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.annotate(ann.BAND_ENTERS_STANDS)
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["seq"], 2)

    async def test_an_answer_to_a_withdrawn_question_does_nothing(self):
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.annotate(ann.BAND_ENTERS_STANDS)
        keys = self.keys()
        await app.accept_prompt(1)
        await app.dismiss_prompt(1)
        self.assertEqual(self.keys(), keys)
        self.assertEqual(app.machine.state, state.State.OPEN)

    async def every_button_that_only_records(self, *, armed):
        """Every recording-only button is tapped; the machine is exactly what
        it was after each, no packet went out, and the log holds no fader move
        or stand-down."""
        recording_only = [event for event in ann.BUTTONS if event.action is None]
        self.assertGreater(len(recording_only), 10)
        app = self.build()
        if armed:
            await app.arm()
        before = app.machine
        sent = len(self.console_sender.packets)
        for event in recording_only:
            if event.kind is ann.Kind.SPAN:
                await app.start_span(event.key)
            else:
                await app.annotate(event.key)
            self.assertEqual(app.machine, before, event.key)
        self.assertEqual(len(self.console_sender.packets), sent)
        for key in (tacet_app.STOOD_DOWN, tacet_app.STAND_DOWN_REQUESTED, tacet_app.COMMANDED):
            self.assertNotIn(key, self.keys(), key)
        # Only the arm this test made itself.
        self.assertEqual(self.keys().count(tacet_app.ARMED), 1 if armed else 0)

    async def test_no_annotation_ever_changes_the_machine_by_itself_from_an_armed_box(self):
        # The constraint (#19 "Out"; CLAUDE.md principle 4): a mis-tapped
        # band-exits-stands mid-drive must not disable anything.
        await self.every_button_that_only_records(armed=True)

    async def test_no_annotation_ever_changes_the_machine_by_itself_from_a_standing_down_box(self):
        await self.every_button_that_only_records(armed=False)

    async def test_a_prompt_is_raised_even_when_the_log_is_refusing_writes(self):
        # The question is state on the box, not something the log holds.
        app = self.build()
        await app.arm()
        with mock.patch.object(self.log, "record", side_effect=ann.WriteError("log refused")):
            await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["kind"], "stand-down")

    async def test_a_span_start_the_log_refused_still_asks(self):
        app = self.build()
        await app.arm()
        with mock.patch.object(self.log, "start_span", side_effect=ann.WriteError("log refused")):
            self.assertIsNone(await app.start_span(ann.HALFTIME_EXODUS))
        self.assertEqual(self.question(app)["source"], ann.HALFTIME_EXODUS)

    async def test_the_entries_a_tap_earns_carry_its_stamp(self):
        app = self.build()
        await app.arm()
        tap = late(0.25)
        await app.annotate(ann.BAND_EXITS_STANDS, tap=tap)
        (raised,) = self.named(prompts.PROMPT_RAISED)
        self.assertEqual(raised.data[taps.TAP_FIELD], tap.as_data())

    async def test_a_stale_annotation_tap_still_asks(self):
        # Annotation-only taps are never refused (#16), and the question is
        # only a question.
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS, tap=self.STALE)
        self.assertEqual(self.question(app)["kind"], "stand-down")
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_a_prompt_moves_no_fader(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        sent = len(self.console_sender.packets)
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(len(self.console_sender.packets), sent)
        self.assertEqual(app.machine.state, state.State.OPEN)


class TestAnsweringAPrompt(PromptTestCase):
    async def test_accepting_stand_down_stands_the_box_down(self):
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertIsNone(self.question(app))
        keys = self.keys()
        self.assertLess(keys.index(tacet_app.STOOD_DOWN), keys.index(prompts.PROMPT_ACCEPTED))
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertEqual(
            accepted.data,
            {
                "prompt": "stand-down",
                "seq": 1,
                "source": ann.BAND_EXITS_STANDS,
                "state": "standing-down",
                "executed": True,
                "refusal": None,
                "stale": False,
            },
        )

    async def test_accepting_writes_no_second_entry_for_the_same_answer(self):
        # The prompt is cleared before the command runs, so the command's own
        # settling does not also log it resolved.
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.accept_prompt(1)
        self.assertEqual(self.named(prompts.PROMPT_RESOLVED), [])
        self.assertEqual(len(self.named(prompts.PROMPT_ACCEPTED)), 1)

    async def test_accepting_stand_down_while_open_fades_rather_than_slamming(self):
        app = self.build(fade=0.05)
        await self.open_with_a_stand_down_question(app)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.RELEASING)
        self.assertTrue(app.machine.pending_stand_down)
        self.assertIsNone(self.question(app))
        self.assertNotIn(tacet_app.STOOD_DOWN, self.keys())
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        keys = self.keys()
        self.assertLess(keys.index(tacet_app.STAND_DOWN_REQUESTED), keys.index(prompts.PROMPT_ACCEPTED))
        self.assertLess(keys.index(prompts.PROMPT_ACCEPTED), keys.index(tacet_app.STOOD_DOWN))
        self.assertEqual(self.console_sender.levels()[-1], dm7.MINUS_INF)
        # Once, at the tap: landing does not log the answer again.
        self.assertEqual(len(self.named(prompts.PROMPT_ACCEPTED)), 1)
        self.assertEqual(self.named(prompts.PROMPT_RESOLVED), [])

    async def test_accepting_stand_down_while_the_level_is_unknown_sends_nothing_and_stands_down(self):
        # #107: STAND_DOWN is never refused, and sends nothing when the box
        # does not know where the fader is.
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.handoff()
        self.assertFalse(app.machine.level_known)
        sent = len(self.console_sender.packets)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(len(self.console_sender.packets), sent)
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertTrue(accepted.data["executed"])
        self.assertIsNone(self.question(app))

    async def test_accepting_arm_arms_and_sends_no_packet(self):
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.console_sender.packets, [])
        self.assertIsNone(self.question(app))
        keys = self.keys()
        self.assertLess(keys.index(tacet_app.ARMED), keys.index(prompts.PROMPT_ACCEPTED))
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertEqual((accepted.data["prompt"], accepted.data["executed"]), ("arm", True))
        self.assertEqual(self.named(prompts.PROMPT_RESOLVED), [])

    async def test_accepting_arm_while_the_level_is_unknown_is_refused_and_the_prompt_stays_open(self):
        # D2. The box's own refusal shows; the question does not vanish, loop
        # or get replaced, and nothing is sent to make the arm "work".
        app = self.build(level_known=False)
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(self.question(app), {"seq": 1, "kind": "arm", "source": ann.BAND_ENTERS_STANDS})
        self.assertEqual(app.snapshot()["refusal"], state.UNKNOWN_LEVEL_ARM)
        self.assertEqual(self.console_sender.packets, [])
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertFalse(accepted.data["executed"])
        self.assertEqual(accepted.data["refusal"], state.UNKNOWN_LEVEL_ARM)
        self.assertEqual(self.named(prompts.PROMPT_RESOLVED), [])
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 1)

    async def test_a_refused_accept_never_shows_the_page_a_prompt_that_is_gone(self):
        # Every push while the box works the refusal out still carries the
        # question: it must not vanish and come back.
        app = self.build(level_known=False)
        await self.standing_down_with_an_arm_question(app)
        seen = []
        app.on_change(lambda: seen.append(app.snapshot()["prompt"]))
        await app.accept_prompt(1)
        self.assertTrue(seen)
        self.assertNotIn(None, seen)

    async def test_a_refused_accept_can_be_answered_again_after_close_now(self):
        app = self.build(level_known=False)
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1)
        await app.close_now()
        self.assertEqual(self.question(app)["seq"], 1)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIsNone(self.question(app))
        accepted = self.named(prompts.PROMPT_ACCEPTED)
        self.assertEqual([entry.data["executed"] for entry in accepted], [False, True])
        self.assertEqual([entry.data["seq"] for entry in accepted], [1, 1])

    async def test_a_stale_accept_of_stand_down_is_not_executed_and_the_prompt_stays_open(self):
        # #16: accepting Stand down is a fader tap, so it is stale-checked.
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.accept_prompt(1, tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.question(app)["seq"], 1)
        (stale,) = self.named(tacet_app.STALE_TAP)
        self.assertEqual(stale.data["command"], "stand_down")
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertTrue(accepted.data["stale"])
        self.assertFalse(accepted.data["executed"])
        self.assertIn("not done", app.snapshot()["refusal"])
        self.assertIsNotNone(app.snapshot()["stale_tap"])

    async def test_a_stale_accept_of_arm_still_arms(self):
        # #16's decision: arming is a mode change, not a fader tap, and moves
        # nothing. Deliberately not stale-checked.
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1, tap=self.STALE)
        self.assertEqual(app.machine.state, state.State.IDLE)
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        self.assertTrue(accepted.data["executed"])

    async def test_an_old_stale_refusal_is_not_blamed_on_an_accept_that_was_not_stale_checked(self):
        # `_stale` lingers until the next fader tap is judged; arming does not
        # clear it. The answer's own entry must describe the answer.
        app = self.build()
        await app.trigger(tap=self.STALE)
        self.assertIsNotNone(app.snapshot()["stale_tap"])
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1)
        (accepted,) = self.named(prompts.PROMPT_ACCEPTED)
        # Never judged, which is not the same as judged on time.
        self.assertIsNone(accepted.data["stale"])
        self.assertTrue(accepted.data["executed"])

    async def test_every_entry_an_answer_earns_carries_its_tap_stamp(self):
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        tap = late(0.3)
        await app.accept_prompt(1, tap=tap)
        for key in (tacet_app.ARMED, prompts.PROMPT_ACCEPTED):
            (entry,) = self.named(key)
            self.assertEqual(entry.data[taps.TAP_FIELD], tap.as_data(), key)

    async def test_dismissing_logs_it_and_closes_the_prompt(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        await app.annotate(ann.BAND_EXITS_STANDS)
        sent = len(self.console_sender.packets)
        await app.dismiss_prompt(1)
        self.assertIsNone(self.question(app))
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(len(self.console_sender.packets), sent)
        (dismissed,) = self.named(prompts.PROMPT_DISMISSED)
        self.assertEqual(
            dismissed.data,
            {"prompt": "stand-down", "seq": 1, "source": ann.BAND_EXITS_STANDS, "state": "open"},
        )
        self.assertEqual(self.named(prompts.PROMPT_ACCEPTED), [])

    async def test_a_stale_dismissal_is_still_taken(self):
        # "Not yet" changes nothing, so lateness cannot make it dangerous.
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.dismiss_prompt(1, tap=self.STALE)
        self.assertIsNone(self.question(app))
        self.assertEqual(len(self.named(prompts.PROMPT_DISMISSED)), 1)

    async def test_a_dismissed_prompt_is_never_raised_again_by_itself(self):
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.dismiss_prompt(1)
        await app.release()
        await app.wait_for_fade()
        await app.trigger()
        self.assertIsNone(self.question(app))
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 1)

    async def test_tapping_the_annotation_again_raises_a_fresh_prompt(self):
        # D5: "Not yet" is per raised prompt, not per kind or session.
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        await app.dismiss_prompt(1)
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["seq"], 2)
        self.assertEqual(len(self.named(prompts.PROMPT_RAISED)), 2)

    async def test_an_answer_for_a_prompt_that_is_no_longer_open_does_nothing(self):
        app = self.build()
        await self.open_with_a_stand_down_question(app)
        for answer in (app.accept_prompt, app.dismiss_prompt):
            before_keys = self.keys()
            before = app.machine
            await answer(99)
            self.assertEqual(self.keys(), before_keys)
            self.assertEqual(app.machine, before)
            self.assertEqual(self.question(app)["seq"], 1)

    async def test_an_answer_when_nothing_is_open_does_nothing(self):
        app = self.build()
        await app.arm()
        before_keys = self.keys()
        await app.accept_prompt(1)
        await app.dismiss_prompt(1)
        self.assertEqual(self.keys(), before_keys)
        self.assertEqual(app.machine.state, state.State.IDLE)

    async def test_a_late_answer_to_a_prompt_that_was_answered_does_nothing(self):
        # Two browsers, or a tap that crossed the reply on stadium wifi.
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.accept_prompt(1)
        await app.stand_down()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertEqual(len(self.named(prompts.PROMPT_ACCEPTED)), 1)

    async def test_an_open_arm_prompt_is_resolved_when_an_open_arms_the_box(self):
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertIsNone(self.question(app))
        keys = self.keys()
        self.assertLess(keys.index(tacet_app.ARMED), keys.index(prompts.PROMPT_RESOLVED))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual(
            resolved.data,
            {"prompt": "arm", "seq": 1, "source": ann.BAND_ENTERS_STANDS, "state": "open"},
        )
        self.assertEqual(self.named(prompts.PROMPT_ACCEPTED), [])

    async def test_an_open_arm_prompt_is_resolved_when_the_operator_arms_directly(self):
        app = self.build()
        await self.standing_down_with_an_arm_question(app)
        await app.arm()
        self.assertIsNone(self.question(app))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual(resolved.data["seq"], 1)

    async def test_an_open_stand_down_prompt_is_resolved_when_the_operator_stands_down_elsewhere(self):
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        await app.stand_down()
        self.assertIsNone(self.question(app))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual((resolved.data["prompt"], resolved.data["seq"]), ("stand-down", 1))
        self.assertEqual(self.named(prompts.PROMPT_ACCEPTED), [])

    async def test_a_stand_down_asked_elsewhere_while_open_resolves_the_prompt_when_requested(self):
        # A pending stand-down already answers the question (D3).
        app = self.build(fade=0.05)
        await self.open_with_a_stand_down_question(app)
        await app.stand_down()
        self.assertTrue(app.machine.pending_stand_down)
        self.assertIsNone(self.question(app))
        (resolved,) = self.named(prompts.PROMPT_RESOLVED)
        self.assertEqual(resolved.data["seq"], 1)
        await app.wait_for_fade()

    async def test_the_hallway_test_the_prompt_does_not_take_the_tap(self):
        # Tap Band exits stands, then Up on whistle blind within a second.
        # The open runs; the question is still just a question.
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(self.question(app)["seq"], 1)
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console.commanded_level, dm7.UNITY)
        self.assertIn(tacet_app.COMMANDED, self.keys())
        self.assertEqual(self.question(app), {"seq": 1, "kind": "stand-down", "source": ann.BAND_EXITS_STANDS})
        self.assertEqual(self.named(prompts.PROMPT_ACCEPTED), [])
        self.assertEqual(self.named(prompts.PROMPT_DISMISSED), [])

    async def test_accepting_is_the_only_path_from_an_annotation_to_a_state_change(self):
        app = self.build()
        await app.arm()
        await app.annotate(ann.BAND_EXITS_STANDS)
        self.assertEqual(app.machine.state, state.State.IDLE)
        await app.accept_prompt(1)
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)

    async def test_a_game_days_worth_of_questions_reads_back_unambiguously(self):
        app = self.build(fade=0.05)
        await app.close_now()
        await app.annotate(ann.BAND_ENTERS_STANDS)  # raised, seq 1
        await app.accept_prompt(1)  # armed
        await app.annotate("up-whistle")
        await app.start_span(ann.HALFTIME_EXODUS)  # raised, seq 2
        await app.accept_prompt(2)  # fades, stands down on landing
        await app.wait_for_fade()
        await app.annotate(ann.BAND_ENTERS_STANDS)  # raised, seq 3
        await app.dismiss_prompt(3)  # "not yet"
        await app.annotate("up-whistle")  # arms by opening
        await app.annotate(ann.BAND_EXITS_STANDS)  # raised, seq 4
        await app.accept_prompt(4)
        await app.wait_for_fade()
        await app.annotate(ann.BAND_EXITS_STANDS)  # already so: resolved directly
        prompt_keys = {
            prompts.PROMPT_RAISED,
            prompts.PROMPT_ACCEPTED,
            prompts.PROMPT_DISMISSED,
            prompts.PROMPT_RESOLVED,
            prompts.PROMPT_WITHDRAWN,
        }
        story = [(e.event, e.data["prompt"], e.data["seq"]) for e in self.entries() if e.event in prompt_keys]
        self.assertEqual(
            story,
            [
                (prompts.PROMPT_RAISED, "arm", 1),
                (prompts.PROMPT_ACCEPTED, "arm", 1),
                (prompts.PROMPT_RAISED, "stand-down", 2),
                (prompts.PROMPT_ACCEPTED, "stand-down", 2),
                (prompts.PROMPT_RAISED, "arm", 3),
                (prompts.PROMPT_DISMISSED, "arm", 3),
                (prompts.PROMPT_RAISED, "stand-down", 4),
                (prompts.PROMPT_ACCEPTED, "stand-down", 4),
                (prompts.PROMPT_RESOLVED, "stand-down", None),
            ],
        )
        self.assertIsNone(self.question(app))

    async def test_a_mis_tapped_exit_that_was_taken_back_reads_back_unambiguously(self):
        app = self.build()
        await app.arm()
        await app.annotate("up-whistle")
        await app.annotate(ann.BAND_EXITS_STANDS)  # raised, seq 1
        await app.annotate(ann.BAND_ENTERS_STANDS)  # resolved directly; seq 1 withdrawn
        prompt_keys = {
            prompts.PROMPT_RAISED,
            prompts.PROMPT_ACCEPTED,
            prompts.PROMPT_DISMISSED,
            prompts.PROMPT_RESOLVED,
            prompts.PROMPT_WITHDRAWN,
        }
        story = [(e.event, e.data["prompt"], e.data["seq"]) for e in self.entries() if e.event in prompt_keys]
        self.assertEqual(
            story,
            [
                (prompts.PROMPT_RAISED, "stand-down", 1),
                (prompts.PROMPT_RESOLVED, "arm", None),
                (prompts.PROMPT_WITHDRAWN, "stand-down", 1),
            ],
        )

    async def test_a_prompt_survives_the_fade_of_an_unrelated_move(self):
        app = self.build(fade=0.05)
        await self.open_with_a_stand_down_question(app)
        await app.release()
        await app.wait_for_fade()
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(self.question(app)["seq"], 1)


class TestTheDutyClock(AppTestCase):
    """#19: the page will say ARMED 10:42 or STOOD DOWN 12:51, so the box
    says when, on its own monotonic clock: the one `snapshot()["at"]` is on.
    Never a wall time, and never invented after a restart."""

    START = 1000.0

    def setUp(self):
        super().setUp()
        self.clock = [self.START]

    def build(self, **options):
        return super().build(monotonic=lambda: self.clock[0], **options)

    async def test_a_fresh_box_has_no_duty_time(self):
        app = self.build()
        self.assertEqual(app.snapshot()["duty"], {"armed": False, "since": None})

    async def test_arming_records_when_on_the_boxs_own_clock(self):
        app = self.build()
        self.clock[0] += 50.0
        await app.arm()
        self.assertEqual(app.snapshot()["duty"], {"armed": True, "since": self.START + 50.0})
        self.assertEqual(app.snapshot()["at"], self.START + 50.0)

    async def test_standing_down_records_when(self):
        app = self.build()
        await app.arm()
        self.clock[0] += 500.0
        await app.stand_down()
        self.assertEqual(app.snapshot()["duty"], {"armed": False, "since": self.START + 500.0})

    async def test_the_time_does_not_move_while_nothing_changes(self):
        app = self.build()
        await app.arm()
        self.clock[0] += 100.0
        await app.trigger()
        self.clock[0] += 100.0
        await app.release()
        await app.wait_for_fade()
        self.clock[0] += 100.0
        self.assertEqual(app.snapshot()["duty"], {"armed": True, "since": self.START})

    async def test_it_moves_only_when_the_duty_entry_is_written(self):
        # An arm that changes nothing, and a stand-down that is refused as
        # stale, write no duty entry and so do not touch the clock.
        app = self.build()
        await app.arm()
        self.clock[0] += 10.0
        await app.arm()
        await app.trigger()
        await app.stand_down(tap=late(taps.DEFAULT_STALE_TAP_SECONDS + 1.0))
        self.assertEqual(app.snapshot()["duty"], {"armed": True, "since": self.START})

    async def test_a_stand_down_while_open_sets_it_when_the_fade_lands(self):
        # Matching `stood-down`, which is written at the landing (#50).
        app = self.build(fade=0.05)
        await app.arm()
        await app.trigger()
        self.clock[0] += 200.0
        await app.stand_down()
        self.assertEqual(app.snapshot()["duty"], {"armed": True, "since": self.START})
        self.clock[0] += 3.0
        await app.wait_for_fade()
        self.assertEqual(app.snapshot()["duty"], {"armed": False, "since": self.START + 203.0})

    async def test_an_open_from_cold_boot_arms_and_says_when(self):
        app = self.build(level_known=False)
        self.clock[0] += 7.0
        await app.annotate("up-whistle")
        self.assertEqual(app.snapshot()["duty"], {"armed": True, "since": self.START + 7.0})

    async def test_a_close_while_standing_down_says_nothing_about_duty(self):
        app = self.build(level_known=False)
        await app.close_now()
        self.assertEqual(app.snapshot()["duty"], {"armed": False, "since": None})

    async def test_a_box_that_restarts_has_no_duty_history_and_does_not_invent_one(self):
        first = self.build()
        await first.arm()
        self.assertIsNotNone(first.snapshot()["duty"]["since"])
        # A restart is a new App: whatever the log says, the box has not
        # watched a stand-down or an arm happen.
        second = tacet_app.App(
            console=self.console,
            log=self.log,
            machine=state.Machine(state=state.State.IDLE, level_known=True),
            monotonic=lambda: self.clock[0],
        )
        self.assertEqual(second.snapshot()["duty"], {"armed": True, "since": None})

    async def test_a_pending_stand_down_still_reads_as_armed(self):
        app = self.build(fade=0.05)
        await app.arm()
        await app.trigger()
        await app.stand_down()
        self.assertTrue(app.snapshot()["duty"]["armed"])
        await app.wait_for_fade()
        self.assertFalse(app.snapshot()["duty"]["armed"])


CODE = prov.Provenance(
    source=prov.Source.CHECKOUT,
    commit="0123456789abcdef0123456789abcdef01234567",
    branch="157-fix",
    detached=False,
    dirty=True,
    untracked=0,
    worktree=True,
    path="/checkout",
    error=None,
)


class TestTheBoxSaysWhatCodeItRuns(AppTestCase):
    """The run's first entry names the code (#157)."""

    async def test_box_started_carries_the_provenance(self):
        app = self.build(provenance=CODE)
        app.log_box_started()
        last = self.entries()[-1]
        self.assertEqual(last.event, ann.BOX_STARTED)
        self.assertEqual(last.data, CODE.as_data())

    async def test_box_started_is_not_stamped_with_a_playhead(self):
        # Reaper has said nothing about this run when the box starts.
        app = self.build(provenance=CODE)
        app.handle_recorder_packet(osc.encode_message("/time", 1234.5))
        app.log_box_started()
        self.assertIsNone(self.entries()[-1].project_seconds)

    async def test_the_snapshot_carries_what_the_page_needs(self):
        app = self.build(provenance=CODE)
        self.assertEqual(app.snapshot()["provenance"], CODE.as_snapshot())

    async def test_a_box_built_without_one_says_null(self):
        app = self.build()
        self.assertIsNone(app.snapshot()["provenance"])

    async def test_a_box_started_that_cannot_be_saved_is_counted_not_raised(self):
        disk = Disk()
        disk.full = True
        app = self.build(provenance=CODE, opener=disk.open)
        app.log_box_started()
        self.log.flush()
        self.assertFalse(app.snapshot()["log"]["healthy"])


def result(kind, trigger=reach.Trigger.KEEPALIVE, *, at=1.0, detail=None):
    return reach.Check(kind, trigger, HOST, at, detail=detail)


class TestTheConsoleCheck(AppTestCase):
    """#73: whether something answers at the console's address. Warned about,
    never gated on, and never a packet to the console."""

    def watch_for(self, app, *, runner=None, clock=None):
        clock = clock or ManualClock()
        self.clock = clock
        return reach.Watch(
            HOST,
            reach.Tools(platform=reach.Platform.MACOS, ping="/sbin/ping", arp="/usr/sbin/arp"),
            runner=runner or FakeRunner(),
            busy=lambda: app.fader_moving,
            on_result=app.console_checked,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            first_due=reach.KEEPALIVE_SECONDS,
        )

    def logged(self):
        return [e for e in self.entries() if e.event == ann.CONSOLE_CHECKED]

    async def test_the_console_check_and_its_keepalive_never_send_a_packet_to_the_console(self):
        # The faders-only assertion for this feature: whatever the check
        # finds, the only thing on the wire is the operator's own fader write,
        # and no check adds one of its own - not even a repeat of the last.
        await self.assert_checks_add_nothing_to_the_cycle()
        self.assertEqual(self.reaper_sender.packets, [])

    async def test_arming_requests_a_console_check(self):
        app = self.build()
        asked = []
        app.on_armed(lambda: asked.append(True))
        await app.arm()
        self.assertEqual(asked, [True])

    async def test_an_open_from_standing_down_requests_a_check_after_its_packet(self):
        app = self.build()
        seen = []
        app.on_armed(lambda: seen.append(len(self.console_sender.packets)))
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.OPEN)
        # Every packet of the open had gone by the time the check was asked for.
        self.assertGreaterEqual(seen[0], 1)
        self.assertEqual(seen, [len(self.console_sender.packets)])

    async def test_accepting_the_arm_prompt_requests_a_check(self):
        app = self.build()
        asked = []
        app.on_armed(lambda: asked.append(True))
        await app.annotate(ann.BAND_ENTERS_STANDS)
        await app.accept_prompt(app.snapshot()["prompt"]["seq"])
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertEqual(asked, [True])

    async def test_standing_down_does_not_request_a_check(self):
        app = self.build()
        await app.arm()
        asked = []
        app.on_armed(lambda: asked.append(True))
        await app.stand_down()
        self.assertEqual(asked, [])

    async def test_a_listener_that_raises_cannot_break_an_arm(self):
        app = self.build()

        def broken():
            raise RuntimeError("check is broken")

        app.on_armed(broken)
        outcome = await app.arm()
        self.assertIsNone(outcome.refusal)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIn("armed", self.keys())

    async def test_a_failing_check_never_delays_or_refuses_the_arm(self):
        gated = GatedRunner()
        app = self.build()
        watch = self.watch_for(app, runner=gated)
        app.on_armed(lambda: watch.request(reach.Trigger.ARM))
        task = asyncio.ensure_future(watch.run())
        await self.clock.settle()
        outcome = await asyncio.wait_for(app.arm(), 1.0)
        self.assertIsNone(outcome.refusal)
        self.assertEqual(app.machine.state, state.State.IDLE)
        await self.clock.settle()
        self.assertTrue(watch.running)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_the_fader_is_moving_while_a_fade_runs_and_not_after(self):
        app = self.build(fade=0.2)
        self.assertFalse(app.fader_moving)
        await app.arm()
        await app.trigger()
        await app.release()
        self.assertTrue(app.fader_moving)
        await app.wait_for_fade()
        self.assertFalse(app.fader_moving)

    async def test_the_startup_check_is_logged_right_after_box_started(self):
        startup = result(reach.Reach.ANSWERED, reach.Trigger.STARTUP)
        app = self.build(provenance=CODE, console_check=startup)
        app.log_box_started()
        app.console_checked(startup)
        keys = self.keys()
        self.assertEqual(keys[-2:], [ann.BOX_STARTED, ann.CONSOLE_CHECKED])
        entry = self.logged()[0]
        self.assertEqual(entry.data["reach"], "answered")
        self.assertEqual(entry.data["trigger"], "startup")
        self.assertEqual(entry.data["host"], HOST)

    async def test_an_arm_check_is_always_logged(self):
        app = self.build()
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP))
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.ARM))
        self.assertEqual([e.data["trigger"] for e in self.logged()], ["startup", "arm"])

    async def test_an_unchanged_keepalive_result_is_not_logged(self):
        app = self.build()
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP))
        app.console_checked(result(reach.Reach.ANSWERED))
        app.console_checked(result(reach.Reach.ANSWERED))
        self.assertEqual(len(self.logged()), 1)

    async def test_a_changed_keepalive_result_is_logged_with_the_previous_one(self):
        app = self.build()
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP))
        app.console_checked(result(reach.Reach.NO_ANSWER))
        entry = self.logged()[-1]
        self.assertEqual(entry.data["reach"], "no-answer")
        self.assertEqual(entry.data["previous"], "answered")

    async def test_a_change_notifies_the_page_and_an_unchanged_result_does_not(self):
        app = self.build()
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP))
        pushes = []
        app.on_change(lambda: pushes.append(True))
        app.console_checked(result(reach.Reach.ANSWERED, at=2.0))
        self.assertEqual(pushes, [])
        app.console_checked(result(reach.Reach.NOTHING_THERE, at=3.0))
        self.assertEqual(pushes, [True])
        app.console_checked(result(reach.Reach.NOTHING_THERE, at=4.0))
        self.assertEqual(pushes, [True])
        app.console_checked(result(reach.Reach.COULD_NOT_CHECK, detail="x", at=5.0))
        self.assertEqual(pushes, [True, True])

    async def test_the_snapshot_carries_the_console_block(self):
        app = self.build()
        self.assertEqual(app.snapshot()["console"]["reach"], "not-checked")
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP, at=9.0))
        block = app.snapshot()["console"]
        self.assertEqual(block, {"reach": "answered", "detail": None, "checked_at": 9.0, "trigger": "startup"})
        # Presence at an address is not confirmation of a fader move.
        self.assertFalse(app.snapshot()["fader"]["confirmed"])

    async def test_a_box_built_with_a_startup_check_says_so_in_its_first_snapshot(self):
        app = self.build(console_check=result(reach.Reach.NOTHING_THERE, reach.Trigger.STARTUP))
        self.assertEqual(app.snapshot()["console"]["reach"], "nothing-there")

    async def test_a_check_that_cannot_be_saved_is_counted_not_raised(self):
        disk = Disk()
        disk.full = True
        app = self.build(opener=disk.open)
        app.console_checked(result(reach.Reach.ANSWERED, reach.Trigger.STARTUP))
        self.log.flush()
        self.assertFalse(app.snapshot()["log"]["healthy"])

    async def test_a_nothing_there_does_not_stop_the_fader(self):
        app = self.build()
        app.console_checked(result(reach.Reach.NOTHING_THERE, reach.Trigger.ARM))
        await app.arm()
        await app.trigger()
        self.assertEqual(app.machine.state, state.State.OPEN)
        self.assertEqual(self.console_sender.levels()[-1], dm7.UNITY)


class TestStopDeadline(unittest.TestCase):
    """How long a stopping box waits for the move in flight (#44). Pure."""

    @staticmethod
    def move(kind: moves.MoveKind, *, started_at: float = 10.0, seconds: float = 2.0) -> moves.MoveDescription:
        return moves.MoveDescription(
            seq=1,
            kind=kind,
            by=None,
            start=dm7.UNITY,
            end=dm7.MINUS_INF,
            seconds=seconds,
            started_at=started_at,
            floor=dm7.DEFAULT_FADE_FLOOR,
            taper=None,
        )

    def test_a_fade_is_given_its_own_schedule_plus_the_margin(self):
        move = self.move(moves.MoveKind.FADE)
        self.assertEqual(move.ends_at, 12.0)
        self.assertEqual(tacet_app.stop_deadline(move, margin=0.5), 12.5)

    def test_a_ride_is_given_no_time_to_finish(self):
        self.assertIsNone(tacet_app.stop_deadline(self.move(moves.MoveKind.RIDE), margin=0.5))

    def test_nothing_moving_needs_no_wait(self):
        self.assertIsNone(tacet_app.stop_deadline(None, margin=0.5))

    def test_the_margin_is_short_next_to_a_fade(self):
        self.assertGreater(tacet_app.STOP_LANDING_MARGIN_SECONDS, 0)
        self.assertLess(tacet_app.STOP_LANDING_MARGIN_SECONDS, dm7.DEFAULT_FADE_SECONDS)


class ClosableFakeSender(FakeSender):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestStoppingTheBox(AppTestCase):
    """The box starts no move on the way out (#44). A fade under way lands, a
    ride stops where it is, and nothing is sent that the move had not planned."""

    def moved(self, key):
        return [e for e in self.entries() if e.event == key]

    async def fade_in_flight(self, app):
        await app.arm()
        await app.trigger()
        await app.release()

    async def test_stopping_during_a_fade_lets_it_land(self):
        app = self.build(fade=1.0)
        await self.fade_in_flight(app)
        await asyncio.sleep(UNDER_WAY)
        landing = app.begin_stop()
        self.assertIsNotNone(landing)
        self.assertEqual(landing.kind, moves.MoveKind.FADE)
        result = await app.finish_stop(abandon=asyncio.Event())
        self.assertEqual(self.console.commanded_level, dm7.MINUS_INF)
        self.assertEqual(app.machine.state, state.State.IDLE)
        self.assertIn(tacet_app.MOVE_LANDED, self.keys())
        self.assertNotIn(tacet_app.MOVE_ABANDONED, self.keys())
        self.assertIsNone(result.abandoned)
        self.assertEqual(result.kind, moves.MoveKind.FADE)

    async def test_a_fade_landing_on_the_way_out_sends_only_the_steps_it_had_planned(self) -> None:
        app = self.build(fade=1.0, steady=True)
        await app.arm()
        await app.trigger()
        start = self.console.commanded_level
        released = len(self.console_sender.packets)
        await app.release()
        for _ in range(5):
            await asyncio.sleep(0)
        stopped_at = len(self.console_sender.packets)
        app.begin_stop()
        await app.finish_stop(abandon=asyncio.Event())
        # The drive sends only the newest of the steps sharing an instant (a
        # close's floor and its -inf, #40), so the plan is the newest level per
        # offset. Counted from before the release: any extra packet anywhere in
        # the stop path, a re-send or a new -inf, breaks the equality.
        by_offset: dict[float, int] = {}
        for offset, level in dm7.ramp_steps(start, dm7.MINUS_INF, 1.0, tick_hz=200.0):
            by_offset[offset] = level
        self.assertEqual(self.console_sender.levels()[released:], list(by_offset.values()))
        self.assertLess(released, stopped_at)
        self.assertLess(stopped_at, len(self.console_sender.packets))

    async def test_every_packet_sent_while_stopping_is_a_fader_level_write(self):
        address = "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3"
        self.assertEqual(dm7.fader_address(3), address)
        for case in ("fade", "ride"):
            with self.subTest(case):
                sender = FakeSender()
                app = self.build(console_sender=sender, fade=0.3, steady=True)
                app._slow_open_seconds = 5.0
                await app.arm()
                if case == "fade":
                    await app.trigger()
                    await app.release()
                else:
                    await app.annotate("up-slow")
                for _ in range(3):
                    await asyncio.sleep(0)
                before = len(sender.packets)
                app.begin_stop()
                await app.finish_stop(abandon=asyncio.Event())
                for packet in sender.packets[before:]:
                    message = osc.decode_packet(packet)
                    assert isinstance(message, osc.Message)
                    self.assertEqual(message.address, address)
                    self.assertEqual(len(message.args), 1)
                    self.assertIsInstance(message.args[0], int)
                self.assertNotIn(b"Fader/On", b"".join(sender.packets))
                app.close_console()

    async def test_no_ride_is_finished_on_the_way_out(self):
        async def up_slow(app):
            await app.arm()
            await app.annotate("up-slow")

        async def up_ready(app):
            await app.arm()
            await app.annotate("up-ready")

        async def retarget(app):
            await app.arm()
            await app.trigger()
            await app.set_target(-6.0)

        for name, begin in (("up-slow", up_slow), ("up-ready", up_ready), ("retarget", retarget)):
            with self.subTest(name):
                self.console_sender.packets.clear()
                app = self.build()
                app._slow_open_seconds = 5.0
                app._ready_ride_seconds = 5.0
                app._retarget_ride_seconds = 5.0
                await begin(app)
                await asyncio.sleep(UNDER_WAY)
                end = app._move.end
                start = app._move.start
                self.assertIsNone(app.begin_stop())
                count = len(self.console_sender.packets)
                await asyncio.sleep(SUPERSEDED_WAKES)
                self.assertEqual(len(self.console_sender.packets), count)
                level = self.console.commanded_level
                self.assertTrue(min(start, end) < level < max(start, end))
                abandoned = self.moved(tacet_app.MOVE_ABANDONED)[-1]
                self.assertEqual(abandoned.data["kind"], "ride")
                self.assertEqual(abandoned.data["because"], "ride")
                self.assertEqual(abandoned.data["level"], level)
                self.assertEqual(abandoned.data["target"], end)
                self.assertNotIn(tacet_app.MOVE_LANDED, self.keys())
                result = await app.finish_stop(abandon=asyncio.Event())
                self.assertEqual(result.abandoned, tacet_app.AbandonedBecause.RIDE)
                self.assertEqual(result.level, level)
                self.assertEqual(len(self.console_sender.packets), count)

    async def test_stopping_with_nothing_moving_sends_nothing(self):
        for name in ("idle", "open"):
            with self.subTest(name):
                self.console_sender.packets.clear()
                app = self.build()
                await app.arm()
                if name == "open":
                    await app.trigger()
                packets = len(self.console_sender.packets)
                entries = len(self.entries())
                self.assertIsNone(app.begin_stop())
                result = await app.finish_stop(abandon=asyncio.Event())
                self.assertEqual(len(self.console_sender.packets), packets)
                self.assertEqual(len(self.entries()), entries)
                self.assertIsNone(result.kind)
                self.assertIsNone(result.abandoned)
                self.assertEqual(result.level, self.console.commanded_level)

    async def test_a_fade_that_misses_its_deadline_is_abandoned(self):
        now = [100.0]
        app = self.build(fade=10.0, monotonic=lambda: now[0])
        await self.fade_in_flight(app)
        await asyncio.sleep(UNDER_WAY)
        app.begin_stop()
        now[0] = app._move.ends_at + tacet_app.STOP_LANDING_MARGIN_SECONDS + 1.0
        result = await asyncio.wait_for(app.finish_stop(abandon=asyncio.Event()), 1.0)
        self.assertEqual(result.abandoned, tacet_app.AbandonedBecause.LATE)
        count = len(self.console_sender.packets)
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(len(self.console_sender.packets), count)
        self.assertNotEqual(self.console.commanded_level, dm7.MINUS_INF)
        abandoned = self.moved(tacet_app.MOVE_ABANDONED)
        self.assertEqual(abandoned[-1].data["because"], "late")
        self.assertEqual(abandoned[-1].data["kind"], "fade")

    async def test_another_stop_signal_leaves_a_landing_fade_where_it_is(self):
        app = self.build(fade=10.0)
        await self.fade_in_flight(app)
        abandon = asyncio.Event()
        app.begin_stop()
        task = asyncio.ensure_future(app.finish_stop(abandon=abandon))
        await asyncio.sleep(UNDER_WAY)
        abandon.set()
        result = await asyncio.wait_for(task, 1.0)
        self.assertEqual(result.abandoned, tacet_app.AbandonedBecause.INTERRUPTED)
        count = len(self.console_sender.packets)
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(len(self.console_sender.packets), count)
        self.assertEqual(self.moved(tacet_app.MOVE_ABANDONED)[-1].data["because"], "interrupted")

    async def test_a_cancelled_stop_still_logs_the_fade_it_abandoned(self):
        app = self.build(fade=10.0)
        await self.fade_in_flight(app)
        app.begin_stop()
        task = asyncio.ensure_future(app.finish_stop(abandon=asyncio.Event()))
        await asyncio.sleep(UNDER_WAY)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        count = len(self.console_sender.packets)
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(len(self.console_sender.packets), count)
        self.assertEqual(self.moved(tacet_app.MOVE_ABANDONED)[-1].data["because"], "interrupted")

    async def test_a_fade_that_fails_while_landing_is_a_failure_not_an_abandonment(self):
        sender = FlakySender()
        app = self.build(console_sender=sender, fade=1.0, steady=True)
        await app.arm()
        await app.trigger()
        await app.release()
        sender.fail_after = len(sender.packets) + 2
        app.begin_stop()
        result = await app.finish_stop(abandon=asyncio.Event())
        self.assertIn(tacet_app.MOVE_FAILED, self.keys())
        self.assertNotIn(tacet_app.MOVE_ABANDONED, self.keys())
        self.assertIsNone(result.abandoned)
        self.assertNotEqual(result.level, result.end)

    async def test_a_landing_fade_still_completes_a_pending_stand_down(self):
        app = self.build(fade=0.3)
        await app.arm()
        await app.trigger()
        await app.stand_down()
        await asyncio.sleep(SUPERSEDED_WAKES)
        app.begin_stop()
        await app.finish_stop(abandon=asyncio.Event())
        self.assertIn(tacet_app.STOOD_DOWN, self.keys())
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)

    async def test_a_ride_started_after_begin_stop_is_abandoned_by_finish_stop(self):
        app = self.build()
        app._slow_open_seconds = 5.0
        await app.arm()
        self.assertIsNone(app.begin_stop())
        await app.annotate("up-slow")
        await asyncio.sleep(UNDER_WAY)
        result = await app.finish_stop(abandon=asyncio.Event())
        self.assertEqual(result.abandoned, tacet_app.AbandonedBecause.RIDE)
        count = len(self.console_sender.packets)
        await asyncio.sleep(SUPERSEDED_WAKES)
        self.assertEqual(len(self.console_sender.packets), count)
        self.assertEqual(self.moved(tacet_app.MOVE_ABANDONED)[-1].data["because"], "ride")

    async def test_a_fade_that_lands_before_finish_stop_is_reported_as_landed(self):
        app = self.build(fade=0.2)
        await self.fade_in_flight(app)
        self.assertIsNotNone(app.begin_stop())
        await app.wait_for_fade()
        result = await app.finish_stop(abandon=asyncio.Event())
        self.assertEqual(result.kind, moves.MoveKind.FADE)
        self.assertEqual(result.level, dm7.MINUS_INF)
        self.assertIsNone(result.abandoned)

    async def test_close_console_closes_the_sender(self):
        sender = ClosableFakeSender()
        app = self.build(console_sender=sender)
        app.close_console()
        self.assertTrue(sender.closed)
        self.assertFalse(self.console.is_ramping)


if __name__ == "__main__":
    unittest.main()
