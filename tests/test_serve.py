import argparse
import asyncio
import dataclasses
import io
import os
import signal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tacet import annotations, disk, dm7, moves, provenance, reach, serve, state, targets
from tacet import app as tacet_app
from tests.test_app import ClosableFakeSender

COMMIT = "0123456789abcdef0123456789abcdef01234567"
CLEAN_CODE = provenance.Provenance(
    source=provenance.Source.CHECKOUT,
    commit=COMMIT,
    branch="main",
    detached=False,
    dirty=False,
    untracked=0,
    worktree=False,
    path="/checkout",
    error=None,
)
DIRTY_CODE = dataclasses.replace(CLEAN_CODE, branch="157-fix", dirty=True, worktree=True)
UNKNOWN_CODE = provenance.Provenance.unknown("git did not answer within 5s", path=Path("/checkout"), worktree=False)
NOT_A_CHECKOUT_CODE = provenance.Provenance.not_a_checkout()
CONSOLE = "10.0.0.5"


def console_check(kind=reach.Reach.ANSWERED, *, detail=None, rtt_ms=1.234):
    """What the startup ping found. Never a real ping: no test here sends one."""
    return reach.Check(kind, reach.Trigger.STARTUP, CONSOLE, 1.0, detail=detail, rtt_ms=rtt_ms)


ANSWERED_CHECK = console_check()


class TestStopConfirmation(unittest.TestCase):
    """Ctrl-C is two presses, deliberately.

    It used to be two by accident: the interrupt arrived inside
    `runner.cleanup()`, which waits on websocket handlers, so any connected
    browser made that the common case. It took the rest of the shutdown with it
    - neither the log nor the queue was closed - and printed a page of traceback
    at whoever was standing there. Nothing was lost only because both fsync per
    line, which is an earlier decision covering for this one.
    """

    def test_the_first_press_does_not_stop(self):
        self.assertFalse(serve.confirms_stop(100.0, None))

    def test_a_second_press_inside_the_window_does(self):
        self.assertTrue(serve.confirms_stop(102.0, 100.0))

    def test_the_boundary_confirms(self):
        self.assertTrue(serve.confirms_stop(100.0 + serve.STOP_CONFIRM_SECONDS, 100.0))

    def test_a_second_press_after_the_window_does_not(self):
        self.assertFalse(serve.confirms_stop(100.1 + serve.STOP_CONFIRM_SECONDS, 100.0))

    def test_two_presses_a_game_apart_are_not_a_pair(self):
        # The reason the window exists at all. A confirmation that never expires
        # would let a stray Ctrl-C in the first quarter combine with an
        # unrelated one in the fourth and end the capture.
        self.assertFalse(serve.confirms_stop(100.0 + 3 * 3600, 100.0))


class TestStopWarning(unittest.TestCase):
    def test_it_says_the_recording_keeps_going(self):
        # The expensive assumption. Each home game is a single irreplaceable
        # sample, and stopping the box looks like stopping everything.
        self.assertIn("does not stop the recording", serve.STOP_WARNING)

    def test_it_says_the_box_starts_no_move_on_the_way_out(self):
        # The other one. The box starts nothing on the way out: a fade already
        # under way is let land, a ride stops where it is, and the operator has
        # the iPad (#44).
        self.assertIn("starts no fader move", serve.STOP_WARNING)
        self.assertIn("let land", serve.STOP_WARNING)
        self.assertIn("stops where it is", serve.STOP_WARNING)

    def test_it_names_the_window_it_is_describing(self):
        self.assertIn(f"{serve.STOP_CONFIRM_SECONDS:.0f}s", serve.STOP_WARNING)


class TestStopSignals(unittest.TestCase):
    """What a SIGINT or SIGTERM means, given what came before (#44). Pure."""

    def test_the_first_ctrl_c_warns(self):
        self.assertIs(serve.StopSignals().interrupt(100.0), serve.SignalAction.WARN)

    def test_a_confirming_ctrl_c_stops(self):
        signals = serve.StopSignals()
        signals.interrupt(100.0)
        self.assertIs(signals.interrupt(101.0), serve.SignalAction.STOP)

    def test_sigterm_stops_at_once_without_confirmation(self):
        self.assertIs(serve.StopSignals().terminate(), serve.SignalAction.STOP)

    def test_ctrl_c_while_stopping_abandons_rather_than_warns(self):
        signals = serve.StopSignals()
        signals.interrupt(100.0)
        signals.interrupt(101.0)
        self.assertIs(signals.interrupt(102.0), serve.SignalAction.ABANDON)

    def test_sigterm_while_stopping_abandons(self):
        signals = serve.StopSignals()
        signals.terminate()
        self.assertIs(signals.terminate(), serve.SignalAction.ABANDON)
        self.assertIs(signals.interrupt(1.0), serve.SignalAction.ABANDON)

    def test_a_warn_after_the_window_does_not_pair_with_a_stale_one(self):
        signals = serve.StopSignals()
        signals.interrupt(100.0)
        late = 100.0 + serve.STOP_CONFIRM_SECONDS + 1.0
        self.assertIs(signals.interrupt(late), serve.SignalAction.WARN)
        self.assertIs(signals.interrupt(late + 1.0), serve.SignalAction.STOP)


class TestStopLines(unittest.TestCase):
    """What the terminal says about the fader on the way out (#44)."""

    MARGIN = 0.5

    @staticmethod
    def move(kind=moves.MoveKind.FADE, *, end=dm7.MINUS_INF):
        return moves.MoveDescription(
            seq=1,
            kind=kind,
            by=None,
            start=dm7.UNITY,
            end=end,
            seconds=2.0,
            started_at=10.0,
            floor=dm7.DEFAULT_FADE_FLOOR,
            taper=None,
        )

    def lines(self):
        fade = tacet_app.Stopped(kind=moves.MoveKind.FADE, end=dm7.MINUS_INF, level=dm7.MINUS_INF, abandoned=None)
        ride = tacet_app.Stopped(
            kind=moves.MoveKind.RIDE,
            end=dm7.UNITY,
            level=-1500,
            abandoned=tacet_app.AbandonedBecause.RIDE,
        )
        late = tacet_app.Stopped(
            kind=moves.MoveKind.FADE,
            end=dm7.MINUS_INF,
            level=-1500,
            abandoned=tacet_app.AbandonedBecause.LATE,
        )
        failed = tacet_app.Stopped(kind=moves.MoveKind.FADE, end=dm7.MINUS_INF, level=-1500, abandoned=None)
        nothing = tacet_app.Stopped(kind=None, end=None, level=dm7.UNITY, abandoned=None)
        return [
            serve.stopping_line(self.move(), now=10.5, margin=self.MARGIN),
            serve.stopping_line(None, now=10.5, margin=self.MARGIN),
            *(serve.stopped_line(s) for s in (fade, ride, late, failed, nothing)),
            serve.STOPPING,
            serve.ABANDONING,
        ]

    def test_stopping_with_a_fade_landing_says_how_long_and_how_to_leave_it(self):
        line = serve.stopping_line(self.move(), now=10.5, margin=self.MARGIN)
        self.assertIn("letting the fade land", line)
        self.assertIn("2.0s at most", line)
        self.assertIn("Ctrl-C again to leave the fader where it is", line)

    def test_a_fade_past_its_deadline_says_zero_not_negative(self):
        line = serve.stopping_line(self.move(), now=99.0, margin=self.MARGIN)
        self.assertIn("0.0s at most", line)

    def test_stopping_with_nothing_landing_just_says_stopping(self):
        self.assertEqual(serve.stopping_line(None, now=10.5, margin=self.MARGIN), serve.STOPPING)

    def test_a_landed_fade_says_where(self):
        stopped = tacet_app.Stopped(kind=moves.MoveKind.FADE, end=dm7.MINUS_INF, level=dm7.MINUS_INF, abandoned=None)
        self.assertEqual(serve.stopped_line(stopped), "fader: the fade landed at -inf")

    def test_an_abandoned_ride_says_where_it_stopped_and_where_it_was_going(self):
        stopped = tacet_app.Stopped(
            kind=moves.MoveKind.RIDE, end=dm7.UNITY, level=-1500, abandoned=tacet_app.AbandonedBecause.RIDE
        )
        line = serve.stopped_line(stopped)
        self.assertIn("the ride was stopped where it was, at -15.0 dB, short of 0.0 dB", line)
        self.assertIn("The next start reads the level as unknown.", line)

    def test_an_abandoned_fade_says_it_did_not_close(self):
        stopped = tacet_app.Stopped(
            kind=moves.MoveKind.FADE,
            end=dm7.MINUS_INF,
            level=-1500,
            abandoned=tacet_app.AbandonedBecause.LATE,
        )
        line = serve.stopped_line(stopped)
        self.assertIn("the fade was stopped where it was, at -15.0 dB, short of -inf", line)

    def test_a_failed_fade_points_at_the_log(self):
        stopped = tacet_app.Stopped(kind=moves.MoveKind.FADE, end=dm7.MINUS_INF, level=-1500, abandoned=None)
        line = serve.stopped_line(stopped)
        self.assertIn("the fade did not land", line)
        self.assertIn("move-failed", line)

    def test_nothing_moving_names_the_last_commanded_level(self):
        stopped = tacet_app.Stopped(kind=None, end=None, level=dm7.UNITY, abandoned=None)
        self.assertEqual(serve.stopped_line(stopped), "fader: nothing was moving; last commanded 0.0 dB")

    def test_the_lines_are_ascii(self):
        for line in self.lines():
            self.assertTrue(line.isascii(), line)
        self.assertTrue(serve.STOP_WARNING.isascii())


class TestRunStopsCleanly(unittest.IsolatedAsyncioTestCase):
    """`_run` with a real signal (#44). Every `os.kill` here is guarded by a
    check that the handler is installed: unhandled, SIGTERM ends the process,
    and pytest with it."""

    CONSOLE_HOST = "192.0.2.1"
    #: An unsupported platform, so every console check is could-not-check and
    #: no ping is ever spawned (#73).
    NO_TOOLS = reach.Tools(platform=None, ping=None, arp=None)

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sender = ClosableFakeSender()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGINT, signal.default_int_handler)
        # The terminal lines, for the whole run.
        self.out = io.StringIO()
        patcher = mock.patch("sys.stdout", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self):
        return argparse.Namespace(
            console_host=self.CONSOLE_HOST,
            reaper_host=None,
            listen="127.0.0.1",
            http_port=0,
            reaper_feedback_port=0,
        )

    def build_app(self, *, fade, slow_open=5.0):
        log = annotations.AnnotationLog(self.root / "game.jsonl").open()
        console = dm7.Dm7Client(self.CONSOLE_HOST, dca=3, sender=self.sender, tick_hz=200.0)
        app = tacet_app.App(
            console=console,
            log=log,
            fade_seconds=fade,
            slow_open_seconds=slow_open,
            machine=state.Machine(level_known=True),
        )
        self.console = console
        self.log = log
        closer = mock.patch.object(log, "close", wraps=log.close)
        self.log_close = closer.start()
        self.addCleanup(closer.stop)
        return app, log

    async def running(self, app, log, signum):
        """Start `_run`, and return its task once `signum` is handled. Never
        returns, and never lets the caller send, when it is not."""
        with mock.patch("tacet.serve.build", return_value=(app, log, None)):
            before = signal.getsignal(signum)
            task = asyncio.ensure_future(serve._run(self.args(), CLEAN_CODE, tools=self.NO_TOOLS))
            for _ in range(500):
                if signal.getsignal(signum) is not before:
                    return task
                await asyncio.sleep(0.01)
        task.cancel()
        raise AssertionError("the signal handler was never installed; nothing was sent")

    def events(self):
        return [e.event for e in annotations.read_entries(self.root / "game.jsonl")]

    async def test_sigterm_mid_fade_lets_the_fade_land_and_closes_everything(self):
        app, log = self.build_app(fade=1.0)
        task = await self.running(app, log, signal.SIGTERM)
        await app.arm()
        await app.trigger()
        await app.release()
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.sender.levels()[-1], dm7.MINUS_INF)
        events = self.events()
        self.assertIn(tacet_app.MOVE_LANDED, events)
        self.assertNotIn(tacet_app.MOVE_ABANDONED, events)
        self.assertTrue(self.sender.closed)
        self.log_close.assert_called_once()
        self.assertIn("fader: the fade landed at -inf", self.out.getvalue())
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    async def test_two_ctrl_c_mid_ride_leave_the_ride_where_it_was(self):
        app, log = self.build_app(fade=0.3)
        task = await self.running(app, log, signal.SIGINT)
        await app.arm()
        await app.annotate("up-slow")
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, 5)
        level = self.sender.levels()[-1]
        self.assertTrue(dm7.MINUS_INF < level < dm7.UNITY)
        events = self.events()
        self.assertIn(tacet_app.MOVE_ABANDONED, events)
        self.assertNotIn(tacet_app.MOVE_LANDED, events)
        self.assertTrue(self.sender.closed)
        self.log_close.assert_called_once()
        self.assertIn("the ride was stopped where it was", self.out.getvalue())


FULL = [
    "--console-host", "10.0.0.5",
    "--dca", "3",
    "--log", "/games/game.jsonl",
    "--queue", "/queue/tacet.tsv",
    "--reaper-host", "127.0.0.1",
]  # fmt: skip


def banner(*extra, argv=None, code=None, check=None):
    """The banner for a set of flags, as one string. No config file, no sockets."""
    args = serve.parser().parse_args((FULL if argv is None else argv) + list(extra))
    return "\n".join(serve.startup_lines(args, None, code=code, console_check=check))


class TestStartupBannerSaysWhatItWasTold(unittest.TestCase):
    """The values are no longer on the command line, so the box says them.

    A box configured from a file has its settings invisible at the moment they
    matter most - "why is it driving DCA 3" has to have an answer on the day.
    """

    def test_it_names_the_console_and_the_dca(self):
        text = banner()
        self.assertIn("10.0.0.5", text)
        self.assertIn("DCA 3", text)

    def test_it_names_the_console_port(self):
        self.assertIn(f"10.0.0.5:{dm7.DEFAULT_PORT}", banner())

    def test_it_never_claims_the_console_is_confirmed(self):
        # The whole protocol is write-only. A banner that read like a
        # handshake would be claiming something no packet can support.
        self.assertIn("commanded, never confirmed", banner())

    def test_it_shows_the_config_file_it_used(self):
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, Path("/etc/tacet.toml")))
        self.assertIn("/etc/tacet.toml", text)

    def test_it_says_so_when_there_is_no_config_file(self):
        self.assertIn(serve.NO_CONFIG, banner())

    def test_it_names_the_log_and_the_queue(self):
        text = banner()
        self.assertIn("/games/game.jsonl", text)
        self.assertIn("/queue/tacet.tsv", text)

    def test_it_reports_the_fade(self):
        self.assertIn("2.0s close", banner())

    def test_it_says_how_late_a_fader_tap_may_be(self):
        # #16: the threshold is a site value, so it is said out loud.
        self.assertIn("over 2.0s late is not executed", banner())
        self.assertIn("over 4.5s late", banner("--stale-tap", "4.5"))

    def test_it_says_the_target_level_the_presets_and_the_cap(self):
        # #9: the standing target is a site value, so it is said out loud, and
        # the minus is ASCII because a level here gets copy-pasted.
        text = banner()
        self.assertIn("target", text)
        self.assertIn("0.0 dB   presets 0.0 / -3.0 / -6.0   cap 0.0 dB", text)

    def test_the_target_row_follows_the_flags(self):
        text = banner("--presets=-2,-5,-8", "--max-target", "3")
        self.assertIn("-2.0 dB   presets -2.0 / -5.0 / -8.0   cap 3.0 dB", text)

    def test_the_target_row_says_the_configured_default_first(self):
        # #139: the default need not be the first preset any more, and the
        # banner has to say the one actually configured.
        text = banner("--presets=3,0,-3", "--max-target", "3", "--default-target", "0")
        self.assertIn("0.0 dB   presets 3.0 / 0.0 / -3.0   cap 3.0 dB", text)

    def test_quantized_is_mentioned_only_when_it_is_on(self):
        self.assertNotIn("Table 1", banner())
        self.assertIn("Table 1", banner("--quantized"))


class TestStartupBannerFlagsWhatIsMissing(unittest.TestCase):
    """Fail visible, before anything has failed. Each of these is a live game
    running in a way the operator probably did not intend."""

    def test_no_reaper_is_called_out_with_its_consequence(self):
        text = banner(argv=["--console-host", "10.0.0.5", "--dca", "3", "--log", "/g.jsonl"])
        self.assertIn("not configured", text)
        self.assertIn("recording state stays unknown", text)

    def test_no_queue_says_the_markers_will_not_arrive(self):
        text = banner(argv=["--console-host", "10.0.0.5", "--dca", "3", "--log", "/g.jsonl"])
        self.assertIn("not set", text)
        self.assertIn("no markers reach Reaper", text)

    def test_loopback_says_the_ipad_cannot_reach_it(self):
        # Reads as a firewall problem and is not one (docs/box.md).
        self.assertIn("the iPad cannot reach it", banner("--listen", "127.0.0.1"))

    def test_the_wildcard_address_is_not_offered_as_a_link(self):
        # 0.0.0.0 is a bind wildcard, not somewhere to point a browser.
        text = banner("--listen", serve.ALL_INTERFACES)
        self.assertNotIn(f"http://{serve.ALL_INTERFACES}", text)
        self.assertIn("<this box>", text)

    def test_an_ordinary_address_is_printed_as_a_plain_url(self):
        self.assertIn("http://10.1.2.3:8080", banner("--listen", "10.1.2.3"))


class TestStartupBannerChecklist(unittest.TestCase):
    def test_it_reminds_about_reaper_and_the_script(self):
        text = banner()
        self.assertIn("Reaper up", text)
        self.assertIn("tacet_mirror.lua", text)

    def test_it_repeats_the_queue_path_under_the_script_step(self):
        # The single most common way to have everything look healthy and mirror
        # nothing is a queue path that does not match the script's.
        self.assertEqual(banner().count("/queue/tacet.tsv"), 2)

    def test_it_uses_the_ports_actually_configured(self):
        text = banner("--reaper-port", "9100", "--reaper-feedback-port", "9200")
        self.assertIn("listen 9100", text)
        self.assertIn("device 9200", text)

    def test_it_drops_the_reaper_step_when_there_is_no_reaper(self):
        text = banner(argv=["--console-host", "10.0.0.5", "--dca", "3", "--log", "/g.jsonl"])
        self.assertNotIn("Reaper up", text)

    def test_it_does_not_ask_for_a_recording_it_cannot_start(self):
        # Without Reaper there is no transport control, so "tap Start
        # recording" would be an instruction that cannot be carried out.
        text = banner(argv=["--console-host", "10.0.0.5", "--dca", "3", "--log", "/g.jsonl"])
        self.assertNotIn("Start recording", text)

    def test_arming_is_always_the_last_step(self):
        self.assertTrue(serve.startup_lines(serve.parser().parse_args(FULL), None)[-2].endswith("in the stands"))


class TestStartupBannerShape(unittest.TestCase):
    def test_the_rules_are_the_declared_width(self):
        lines = serve.startup_lines(serve.parser().parse_args(FULL), None)
        self.assertEqual(len(lines[0]), serve.BANNER_WIDTH)
        self.assertEqual(lines[0], lines[-1])

    def test_it_is_ascii(self):
        # Same trap as the source: a typographic character in a level or a path
        # is invisible here and broken when it is copy-pasted.
        banner().encode("ascii")

    def test_no_line_is_blank(self):
        # A blank line in a banner reads as the end of it.
        for line in serve.startup_lines(serve.parser().parse_args(FULL), None):
            self.assertTrue(line.strip(), "banner has an empty line")


class TestStartupBannerWarnsAboutATornFile(unittest.TestCase):
    """A crash mid-write last run. Repaired on open, and said out loud."""

    TORN = annotations.TornTail(offset=120, tail=b'{"seq": 7, "ev')

    def banner_with(self, **torn):
        args = serve.parser().parse_args(FULL)
        return "\n".join(serve.startup_lines(args, None, None, **torn))

    def test_an_intact_log_gets_no_warning(self):
        self.assertNotIn("torn", banner())

    def test_a_torn_log_is_called_out(self):
        text = self.banner_with(torn_log=self.TORN)
        self.assertIn("WARNING", text)
        self.assertIn("log ends in a torn write", text)

    def test_a_torn_queue_is_called_out(self):
        text = self.banner_with(torn_queue=self.TORN)
        self.assertIn("queue ends in a torn write", text)

    def test_it_says_where_the_bytes_go(self):
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, None, None, torn_log=self.TORN))
        self.assertIn(f"{args.log}{annotations.TORN_SUFFIX}", text)

    def test_it_does_not_refuse_to_start(self):
        text = self.banner_with(torn_log=self.TORN)
        self.assertIn("arm when the band is in the stands", text)


class TestStartupBannerWarnsAboutAReusedLog(unittest.TestCase):
    """Yesterday's log, or a log the pre-flight test already wrote to.

    Both produce a timeline anchored to a recording that is not today's, and
    both look completely healthy at every other point in the system.
    """

    WALL = "2026-09-12T19:00:00+00:00"

    def prior(self):
        event = annotations.lookup(annotations.ANCHOR_EVENT)
        return annotations.Entry(
            seq=1,
            event=event.key,
            category=str(event.category),
            kind=str(event.kind),
            label=event.label,
            wall=self.WALL,
            monotonic=0.0,
        )

    def test_a_clean_log_gets_no_warning(self):
        self.assertNotIn("WARNING", banner())

    def test_a_log_with_a_recording_in_it_is_called_out(self):
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, None, self.prior()))
        self.assertIn("WARNING", text)
        self.assertIn("already contains a recording", text)

    def test_a_log_holding_a_found_recording_gets_the_warning(self):
        event = annotations.lookup(annotations.FOUND_EVENT)
        found = annotations.Entry(
            seq=1,
            event=event.key,
            category=str(event.category),
            kind=str(event.kind),
            label=event.label,
            wall=self.WALL,
            monotonic=0.0,
            project_seconds=42.0,
        )
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, None, found))
        self.assertIn("WARNING", text)

    def test_it_says_when_the_earlier_recording_was(self):
        args = serve.parser().parse_args(FULL)
        prior = self.prior()
        text = "\n".join(serve.startup_lines(args, None, prior))
        self.assertIn(prior.wall, text)

    def test_it_says_what_to_do(self):
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, None, self.prior()))
        self.assertIn("--log", text)

    def test_it_does_not_refuse_to_start(self):
        # A box that will not start 20 minutes before kickoff is worse than a
        # log that needs splitting afterwards. The operator is the supervisor.
        args = serve.parser().parse_args(FULL)
        lines = serve.startup_lines(args, None, self.prior())
        self.assertIn("arm when the band is in the stands", "\n".join(lines))


class TestStartupBannerWarnsAboutAClockReset(unittest.TestCase):
    """The log was written before this machine last rebooted."""

    def reset(self):
        event = annotations.lookup("note")
        return annotations.Entry(
            seq=160,
            event=event.key,
            category=str(event.category),
            kind=str(event.kind),
            label=event.label,
            wall="2026-09-12T15:40:00+00:00",
            monotonic=186_000.0,
        )

    def test_a_clean_log_gets_no_warning(self):
        self.assertNotIn("clock", banner())

    def test_a_reset_is_called_out_with_its_consequence(self):
        args = serve.parser().parse_args(FULL)
        text = "\n".join(serve.startup_lines(args, None, clock_reset=self.reset()))
        self.assertIn("WARNING", text)
        self.assertIn("clock has restarted", text)
        self.assertIn("2026-09-12T15:40:00+00:00", text)


class TestStartupBannerWarnsAboutCarriedOverSpans(unittest.TestCase):
    """Spans a previous run left open in this log (#20).

    They are resumed, so their buttons read "(end)" - last game's `q4` still
    running on the page of this one.
    """

    WALL = "2026-09-12T15:31:00+00:00"

    def span(self, key, seq):
        event = annotations.lookup(key)
        return annotations.Entry(
            seq=seq,
            event=event.key,
            category=str(event.category),
            kind=str(event.kind),
            label=event.label,
            wall=self.WALL,
            monotonic=0.0,
            span_id=annotations.span_id_for(key, seq),
            phase=annotations.PHASE_START,
        )

    def banner_with(self, *spans):
        args = serve.parser().parse_args(FULL)
        return "\n".join(serve.startup_lines(args, None, open_spans=list(spans)))

    def test_a_log_with_nothing_open_gets_no_warning(self):
        self.assertNotIn("still open", self.banner_with())

    def test_an_open_span_is_called_out_by_name_and_start(self):
        q4 = self.span("q4", 140)
        text = self.banner_with(q4)
        self.assertIn("WARNING", text)
        self.assertIn("still open", text)
        self.assertIn(q4.label, text)
        self.assertIn(self.WALL, text)

    def test_every_open_span_is_listed(self):
        text = self.banner_with(self.span("q4", 140), self.span("halftime", 90))
        self.assertIn(annotations.lookup("q4").label, text)
        self.assertIn(annotations.lookup("halftime").label, text)

    def test_it_says_what_the_page_will_show(self):
        self.assertIn("(end)", self.banner_with(self.span("q4", 140)))

    def test_a_retired_span_says_where_it_is_ended(self):
        # #155: no button starts a quarter any more, so a q4 an older log left
        # open can only be ended from the page's OPEN FROM AN EARLIER RUN.
        self.assertIn("OPEN FROM AN EARLIER RUN", self.banner_with(self.span("q4", 140)))
        self.assertNotIn("OPEN FROM AN EARLIER RUN", self.banner_with(self.span("timeout-home", 140)))

    def test_the_retired_span_note_fits_the_banner(self):
        # 74 columns less the 12-column label and the 2-column indent.
        self.assertLessEqual(len(serve.RETIRED_SPAN_NOTE), serve.BANNER_WIDTH - serve.BANNER_LABEL_WIDTH - 2)

    def test_it_does_not_refuse_to_start(self):
        # A box restarted mid-quarter resumes its own open quarter, correctly.
        self.assertIn("arm when the band is in the stands", self.banner_with(self.span("q4", 140)))


class _RunMain(unittest.TestCase):
    """`serve.main` up to the point it would bind, with its output captured."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "tacet.toml"
        self.config.write_text("", encoding="utf-8")
        self.log = self.root / "game.jsonl"

    def run_main(self, argv):
        stderr = io.StringIO()
        with (
            mock.patch("sys.stderr", stderr),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("tacet.serve.asyncio.run") as run,
            mock.patch("tacet.serve.provenance.probe", return_value=CLEAN_CODE),
            mock.patch("tacet.serve.reach.probe", return_value=ANSWERED_CHECK),
            self.assertRaises(SystemExit) as caught,
        ):
            serve.main(["--config", str(self.config), *argv])
        run.assert_not_called()
        return caught.exception.code, stderr.getvalue()


class TestTheLogIsRequiredOnTheCommandLine(_RunMain):
    """`--log` is the one value that changes every game, so it is typed every
    game (#20). Game 2's log carried the next day's date because the example was
    copied into the config file."""

    CONSOLE = ("--console-host", "192.0.2.1", "--dca", "3")

    def test_no_log_refuses_and_names_the_flag(self):
        code, stderr = self.run_main([*self.CONSOLE])
        self.assertEqual(code, 2)
        self.assertIn(serve.LOG_REQUIRED, stderr)
        self.assertIn("--log", serve.LOG_REQUIRED)

    def test_a_complete_config_does_not_stand_in_for_it(self):
        self.config.write_text("[console]\nhost = '192.0.2.1'\ndca = 3\n", encoding="utf-8")
        code, stderr = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn(serve.LOG_REQUIRED, stderr)

    def test_a_config_still_carrying_capture_log_refuses(self):
        self.config.write_text("[capture]\nlog = '~/games/2026-09-12.jsonl'\n", encoding="utf-8")
        code, stderr = self.run_main([*self.CONSOLE, "--log", str(self.log)])
        self.assertEqual(code, 2)
        self.assertIn("capture.log", stderr)
        self.assertNotIn("Traceback", stderr)


class TestAPresetAboveTheCapStopsTheBoxInWords(_RunMain):
    """#9: the cap is enforced when it arrives as a flag, not only in the file."""

    ARGV = ("--console-host", "192.0.2.1", "--dca", "3")

    def test_a_flag_above_the_cap_is_a_command_line_error_not_a_traceback(self):
        code, stderr = self.run_main([*self.ARGV, "--log", str(self.log), "--presets", "3,0,-3"])
        self.assertEqual(code, 2)
        self.assertIn("3.0", stderr)
        self.assertIn("--presets", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_a_raised_cap_flag_admits_it(self):
        args = serve.parser().parse_args([*self.ARGV, "--presets", "3,0,-3", "--max-target", "3"])
        self.assertEqual(targets.build(args.presets, args.max_target).default, 300)

    def test_build_enforces_the_cap_before_it_opens_anything(self):
        args = serve.parser().parse_args([*self.ARGV, "--log", str(self.log), "--presets", "3,0"])
        with self.assertRaises(targets.TargetError):
            serve.build(args, CLEAN_CODE)
        self.assertFalse(self.log.exists())

    def test_a_leading_minus_needs_the_equals_form(self):
        # argparse reads `-3,-6` as a flag. The help says so; pinned so the
        # spelling the runbook gives is the one that works.
        args = serve.parser().parse_args([*self.ARGV, "--presets=-3,-6"])
        self.assertEqual(args.presets, (-3.0, -6.0))

    def test_a_config_above_its_own_cap_refuses_at_load(self):
        self.config.write_text("[fader]\npresets = [1.0]\nmax_target_db = 0.0\n", encoding="utf-8")
        code, stderr = self.run_main([*self.ARGV, "--log", str(self.log)])
        self.assertEqual(code, 2)
        self.assertIn("fader.presets", stderr)


class TestTheDefaultTargetMustBeAPreset(_RunMain):
    """#139: a default outside the presets would boot the box to a level the
    operator cannot select again, since the page's control offers presets
    only."""

    ARGV = ("--console-host", "192.0.2.1", "--dca", "3")

    def test_target_refusal_names_every_flag_and_key(self):
        # Pinned directly, like LOG_REQUIRED: a refusal that silently dropped
        # one of the three ways a mismatched target can arrive - the file,
        # either flag - would leave someone staring at a message that does
        # not mention the thing they actually set.
        for flag in ("--presets", "--max-target", "--default-target"):
            self.assertIn(flag, serve.TARGET_REFUSAL)
        for key in ("fader.presets", "fader.max_target_db", "fader.default_target_db"):
            self.assertIn(key, serve.TARGET_REFUSAL)

    def test_a_default_flag_outside_the_presets_is_a_command_line_error_not_a_traceback(self):
        code, stderr = self.run_main([*self.ARGV, "--log", str(self.log), "--default-target", "-1.5"])
        self.assertEqual(code, 2)
        self.assertIn("-1.5", stderr)
        self.assertIn("--default-target", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_a_config_default_outside_its_own_presets_refuses(self):
        self.config.write_text("[fader]\npresets = [0.0, -3.0]\ndefault_target_db = -6.0\n", encoding="utf-8")
        code, stderr = self.run_main([*self.ARGV, "--log", str(self.log)])
        self.assertEqual(code, 2)
        self.assertIn("fader.default_target_db", stderr)
        self.assertIn("-6.0", stderr)

    def test_a_file_default_outside_a_cli_preset_list_refuses(self):
        # Only serve.main's post-resolve targets.build can catch this: the
        # file's default_target_db validates fine on its own (-6.0 is one of
        # the built-in presets, asserted below so this premise cannot rot
        # silently), and the clash only exists once the flag has overridden
        # the preset list. The second assertion discriminates the layer: a
        # refusal at config load reads "<path>: fader.default_target_db: ...",
        # a refusal here reads serve.TARGET_REFUSAL, which names --presets.
        # Without it, a change that made config.load catch this instead would
        # still pass - same exit code, same value in stderr - while silently
        # losing the behaviour this test exists to cover.
        self.assertIn(-6.0, targets.DEFAULT_PRESETS_DB)
        self.config.write_text("[fader]\ndefault_target_db = -6.0\n", encoding="utf-8")
        code, stderr = self.run_main([*self.ARGV, "--log", str(self.log), "--presets", "0,-3"])
        self.assertEqual(code, 2)
        self.assertIn("-6.0", stderr)
        self.assertIn("--presets", stderr)

    def test_a_named_default_in_the_middle_of_the_list_is_admitted(self):
        args = serve.parser().parse_args(
            [*self.ARGV, "--log", str(self.log), "--presets", "3,0,-3", "--max-target", "3", "--default-target", "0"]
        )
        self.assertEqual(targets.build(args.presets, args.max_target, args.default_target).default, 0)

    def test_an_unset_default_still_takes_the_first_preset(self):
        args = serve.parser().parse_args(
            [*self.ARGV, "--log", str(self.log), "--presets", "3,0,-3", "--max-target", "3"]
        )
        self.assertEqual(targets.build(args.presets, args.max_target, args.default_target).default, 300)


class TestAnUnreadableLogStopsTheBoxInWords(_RunMain):
    """Refused before anything binds, as a command-line error rather than a
    page of traceback at whoever is standing there."""

    def run_with_the_log(self):
        return self.run_main(["--console-host", "192.0.2.1", "--dca", "3", "--log", str(self.log)])

    def test_a_newer_schema(self):
        self.log.write_text('{"v": 2, "seq": 1}\n', encoding="utf-8")
        code, stderr = self.run_with_the_log()
        self.assertEqual(code, 2)
        self.assertIn(str(self.log), stderr)
        self.assertIn("v2", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_a_corrupt_line(self):
        self.log.write_text("{ not json\n" + '{"v": 1}\n', encoding="utf-8")
        code, stderr = self.run_with_the_log()
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", stderr)


#: Room on a fake disk for any game the tests describe.
PLENTY = 10**15


def _usage(free):
    """What `shutil.disk_usage` returns, as far as `tacet.disk` reads it."""
    return SimpleNamespace(total=free, used=0, free=free)


class TestCheckAnswersWithoutStarting(unittest.TestCase):
    """#79: `--check` is a real start that stops after the banner.

    Every assertion compares against an actual start of the same command, so
    `--check` cannot grow wording of its own: the troubleshooting table has to
    describe what the operator sees on the day.
    """

    CONSOLE = ("--console-host", "192.0.2.1", "--dca", "3")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "tacet.toml"
        self.config.write_text("", encoding="utf-8")
        self.log = self.root / "game.jsonl"
        self.queue = self.root / "queue.tsv"
        self.free = PLENTY
        self.code = CLEAN_CODE
        self.probe = mock.Mock(side_effect=lambda: self.code)
        self.found = ANSWERED_CHECK
        self.pinged = mock.Mock(side_effect=lambda host, tools: self.found)

    def start(self, argv, *, config=True):
        """(exit code, stdout, stderr) for `serve.main`, which never binds:
        `asyncio.run` is replaced, and reports whether it was reached."""
        stdout, stderr = io.StringIO(), io.StringIO()
        prefix = ["--config", str(self.config)] if config else []
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
            # Closed, not run: a real start's coroutine is made and never awaited.
            mock.patch("tacet.serve.asyncio.run", side_effect=lambda coroutine: coroutine.close()) as run,
            # Fixed, so a start and a check of the same command print the same
            # banner however the real disk moves between them.
            mock.patch("tacet.disk.shutil.disk_usage", return_value=_usage(self.free)),
            # Never git against the checkout the tests run in (#157).
            mock.patch("tacet.serve.provenance.probe", self.probe),
            # Never a real ping either (#73).
            mock.patch("tacet.serve.reach.probe", self.pinged),
        ):
            code: int | str | None
            try:
                code = serve.main([*prefix, *argv])
            except SystemExit as exit_:
                code = exit_.code
        self.ran = run.called
        return code, stdout.getvalue(), stderr.getvalue()

    def good(self):
        return [*self.CONSOLE, "--log", str(self.log), "--queue", str(self.queue), *self.recording()]

    def recording(self):
        return ["--audio-path", str(self.root), "--channels", "22"]

    def test_a_good_config_prints_the_banner_a_start_prints_and_exits_zero(self):
        _, started, _ = self.start(self.good())
        self.assertTrue(self.ran)
        code, checked, stderr = self.start([*self.good(), "--check"])
        self.assertEqual(code, 0)
        self.assertFalse(self.ran)
        self.assertEqual(checked, started)
        self.assertIn("console", checked)
        self.assertEqual(stderr, "")

    def test_every_refusal_is_the_one_a_start_gives(self):
        log = ("--log", str(self.log))
        unreadable = self.root / "unreadable.jsonl"
        unreadable.write_text("{ not json\n" + '{"v": 1}\n', encoding="utf-8")
        cases = {
            "unknown key": ("[console]\nnope = 1\n", [*self.CONSOLE, *log]),
            "bad value": ("[console]\ndca = 'three'\n", [*self.CONSOLE[:2], *log]),
            "bad port": ("", [*self.CONSOLE, *log, "--http-port", "70000"]),
            "no console host": ("", ["--dca", "3", *log]),
            "no log": ("", [*self.CONSOLE]),
            "missing --config": (None, ["--config", str(self.root / "absent.toml"), *self.CONSOLE, *log]),
            "unreadable log": ("", [*self.CONSOLE, "--log", str(unreadable)]),
        }
        for name, (text, argv) in cases.items():
            with self.subTest(name):
                if text is not None:
                    self.config.write_text(text, encoding="utf-8")
                started = self.start(argv, config=text is not None)
                checked = self.start([*argv, "--check"], config=text is not None)
                self.assertNotEqual(checked[0], 0)
                self.assertEqual(checked[0], started[0])
                self.assertEqual(checked[2], started[2])
                self.assertTrue(checked[2])
                self.assertFalse(self.ran)

    def test_a_warning_is_not_a_refusal(self):
        self.log.write_text('{"event": "recording-started"', encoding="utf-8")
        code, stdout, _ = self.start([*self.good(), "--check"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", stdout)

    def test_nothing_is_created(self):
        self.start([*self.good(), "--check"])
        self.assertFalse(self.log.exists())
        self.assertFalse(self.queue.exists())

    def test_a_torn_tail_is_reported_and_left_exactly_as_it_was(self):
        with annotations.AnnotationLog(self.log) as log:
            log.record("note")
        torn_log = self.log.read_bytes() + b'{"v":1,"se'
        torn_queue = b"NOTE|note\t-\t-\nGAME|q1\tsta"
        self.log.write_bytes(torn_log)
        self.queue.write_bytes(torn_queue)
        _, stdout, _ = self.start([*self.good(), "--check"])
        self.assertIn("torn write", stdout)
        self.assertEqual(self.log.read_bytes(), torn_log)
        self.assertEqual(self.queue.read_bytes(), torn_queue)
        self.assertFalse(annotations.set_aside_path(self.log).exists())
        self.assertFalse(annotations.set_aside_path(self.queue).exists())


class TestTheBoxRefusesWithoutRoomForTheGame(TestCheckAnswersWithoutStarting):
    """#53: running out of disk mid-game loses the recording. Checked before
    the banner, so `--check` the night before refuses exactly as the day would."""

    def test_the_banner_says_how_much_room_there_is(self):
        code, stdout, _ = self.start(self.good())
        self.assertEqual(code, 0)
        self.assertTrue(self.ran)
        self.assertIn(f"free on {self.root}, need ~", stdout)
        self.assertIn("22 ch x 5.0 h", stdout)

    def test_insufficient_space_refuses_and_names_the_override(self):
        self.free = disk.required_bytes(22, disk.DEFAULT_GAME_HOURS) - 1
        for argv in (self.good(), [*self.good(), "--check"]):
            with self.subTest(argv[-1]):
                code, stdout, stderr = self.start(argv)
                self.assertEqual(code, 2)
                self.assertFalse(self.ran)
                self.assertIn(disk.OVERRIDE_FLAG, stderr)
                self.assertEqual(stdout, "")

    def test_an_unset_audio_path_refuses(self):
        code, _, stderr = self.start([*self.CONSOLE, "--log", str(self.log)])
        self.assertEqual(code, 2)
        self.assertIn(disk.NOT_SET, stderr)

    def test_an_unmounted_audio_path_is_refused(self):
        absent = self.root / "not-mounted"
        code, _, stderr = self.start(
            [*self.CONSOLE, "--log", str(self.log), "--audio-path", str(absent), "--channels", "22"]
        )
        self.assertEqual(code, 2)
        self.assertIn(str(absent), stderr)
        self.assertFalse(absent.exists())

    def test_the_config_file_supplies_the_recording(self):
        self.config.write_text(f"[capture]\naudio_path = '{self.root}'\nchannels = 16\n", encoding="utf-8")
        code, stdout, _ = self.start([*self.CONSOLE, "--log", str(self.log)])
        self.assertEqual(code, 0)
        self.assertIn("16 ch x 5.0 h", stdout)

    def test_the_override_flag_starts_anyway_and_says_so(self):
        self.free = 1
        code, stdout, _ = self.start([*self.good(), disk.OVERRIDE_FLAG])
        self.assertEqual(code, 0)
        self.assertTrue(self.ran)
        self.assertIn(f"not enforced: {disk.OVERRIDE_FLAG}", stdout)

    def test_the_override_with_no_audio_path_says_nothing_was_checked(self):
        code, stdout, _ = self.start([*self.CONSOLE, "--log", str(self.log), disk.OVERRIDE_FLAG])
        self.assertEqual(code, 0)
        self.assertIn(f"not checked ({disk.OVERRIDE_FLAG})", stdout)


class TestStartupBannerSaysWhatCodeItRuns(unittest.TestCase):
    """The one row the box checked rather than was told (#157)."""

    def lines(self, code):
        return serve.startup_lines(serve.parser().parse_args(FULL), None, code=code)

    def test_the_code_row_comes_straight_after_the_tacet_row(self):
        lines = self.lines(CLEAN_CODE)
        self.assertTrue(lines[1].startswith("  tacet"))
        self.assertTrue(lines[2].startswith("  code"))
        self.assertTrue(lines[3].startswith("  config"))

    def test_the_code_row_names_the_branch_and_short_commit(self):
        self.assertIn("  code        main @ 0123456", banner(code=CLEAN_CODE))

    def test_a_worktree_says_so(self):
        text = banner(code=dataclasses.replace(CLEAN_CODE, branch="157-fix", worktree=True))
        self.assertIn("157-fix @ 0123456 (worktree)", text)

    def test_a_detached_head_says_so(self):
        text = banner(code=dataclasses.replace(CLEAN_CODE, branch=None, detached=True))
        self.assertIn("detached HEAD @ 0123456", text)

    def test_not_a_checkout_says_so_in_those_words(self):
        self.assertIn("  code        not a git checkout", banner(code=NOT_A_CHECKOUT_CODE))

    def test_a_clean_checkout_gets_no_warning(self):
        self.assertNotIn("WARNING", banner(code=CLEAN_CODE))
        self.assertNotIn("WARNING", banner(code=NOT_A_CHECKOUT_CODE))

    def test_a_dirty_tree_is_a_warning_that_names_the_page_chip(self):
        lines = self.lines(DIRTY_CODE)
        self.assertIn(f"  WARNING     {serve.DIRTY_WARNING}", lines)
        self.assertTrue(any(f'"{serve.PAGE_DIRTY_CHIP}"' in line for line in lines))
        self.assertIn("157-fix @ 0123456 (worktree), uncommitted changes", banner(code=DIRTY_CODE))

    def test_an_unknown_is_a_warning_with_its_reason(self):
        text = banner(code=UNKNOWN_CODE)
        self.assertIn(f"  WARNING     {serve.UNKNOWN_CODE_WARNING}", text)
        self.assertIn("unknown - git did not answer within 5s", text)

    def test_no_code_row_without_a_provenance(self):
        text = banner()
        self.assertNotIn("  code ", text)
        self.assertNotIn("WARNING", text)

    def test_the_code_lines_are_ascii_and_never_blank(self):
        for code in (CLEAN_CODE, DIRTY_CODE, UNKNOWN_CODE, NOT_A_CHECKOUT_CODE):
            for line in self.lines(code):
                line.encode("ascii")
                self.assertTrue(line.strip())


class TestGitIsAskedOnceBeforeTheLoop(TestCheckAnswersWithoutStarting):
    def test_a_start_asks_git_exactly_once_before_the_loop_runs(self):
        asked: list[int] = []

        def enter(coroutine):
            asked.append(self.probe.call_count)
            coroutine.close()

        with (
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
            mock.patch("tacet.serve.asyncio.run", side_effect=enter),
            mock.patch("tacet.disk.shutil.disk_usage", return_value=_usage(PLENTY)),
            mock.patch("tacet.serve.provenance.probe", self.probe),
            mock.patch("tacet.serve.reach.probe", self.pinged),
        ):
            serve.main(["--config", str(self.config), *self.good()])
        self.assertEqual(asked, [1])
        self.assertEqual(self.probe.call_count, 1)

    def test_git_is_never_asked_on_the_event_loop(self):
        seen: list[str] = []

        def probe():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen.append("no loop")
            else:
                seen.append("on the loop")
            return CLEAN_CODE

        self.probe = mock.Mock(side_effect=probe)
        self.start(self.good())
        self.assertEqual(seen, ["no loop"])

    def test_a_refused_start_never_asks_git(self):
        code, _, _ = self.start([*self.CONSOLE])  # no --log
        self.assertEqual(code, 2)
        unreadable = self.root / "unreadable.jsonl"
        unreadable.write_text("{ not json\n" + '{"v": 1}\n', encoding="utf-8")
        code, _, _ = self.start([*self.CONSOLE, "--log", str(unreadable)])
        self.assertNotEqual(code, 0)
        self.probe.assert_not_called()

    def test_check_prints_the_code_row_a_start_prints(self):
        _, started, _ = self.start(self.good())
        _, checked, _ = self.start([*self.good(), "--check"])
        self.assertIn("code        main @ 0123456", started)
        self.assertEqual(checked, started)

    def test_a_dirty_tree_is_a_warning_not_a_refusal(self):
        self.code = DIRTY_CODE
        code, stdout, _ = self.start([*self.good(), "--check"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", stdout)
        code, _, _ = self.start(self.good())
        self.assertEqual(code, 0)
        self.assertTrue(self.ran)

    def test_the_coroutine_is_given_the_probed_code(self):
        async def nothing():
            pass

        coroutine = nothing()
        self.addCleanup(coroutine.close)
        with mock.patch("tacet.serve._run", return_value=coroutine) as run:
            self.start(self.good())
        self.assertEqual(run.call_args.args[1], CLEAN_CODE)


class TestStartupBannerSaysWhatThePingProved(unittest.TestCase):
    """#73: presence at an address, never the DM7, the port or a delivered move."""

    def rows(self, check):
        return banner(check=check).splitlines()

    def test_the_banner_says_what_the_ping_proved(self):
        cases = {
            reach.Reach.ANSWERED: "  ping        answered ping (1.2 ms)",
            reach.Reach.NO_ANSWER: "  ping        did not answer ping",
            reach.Reach.NOTHING_THERE: "  ping        nothing at this address (no ARP reply)",
        }
        for kind, row in cases.items():
            with self.subTest(kind=kind):
                self.assertIn(row, self.rows(console_check(kind)))
        self.assertIn(
            "  ping        could not check: ping is not installed or not on PATH",
            self.rows(console_check(reach.Reach.COULD_NOT_CHECK, detail=reach.PING_NOT_FOUND)),
        )

    def test_the_ping_row_sits_under_the_console_row(self):
        lines = self.rows(ANSWERED_CHECK)
        console = next(i for i, line in enumerate(lines) if line.startswith("  console"))
        self.assertTrue(lines[console + 1].strip().startswith("commanded, never confirmed"))
        self.assertTrue(any(line.startswith("  ping") for line in lines[console : console + 4]))

    def test_an_answer_does_not_claim_it_is_the_console(self):
        text = banner(check=ANSWERED_CHECK)
        self.assertIn("not proof it is the DM7", text)
        self.assertIn("not proof the port is right", text)

    def test_nothing_there_is_a_warning(self):
        lines = self.rows(console_check(reach.Reach.NOTHING_THERE))
        self.assertIn(f"  WARNING     {serve.NOTHING_THERE_WARNING}", lines)
        self.assertTrue(any(CONSOLE in line and "wrong address" in line for line in lines))

    def test_the_other_results_are_not_warnings(self):
        for kind in (reach.Reach.ANSWERED, reach.Reach.NO_ANSWER, reach.Reach.COULD_NOT_CHECK):
            with self.subTest(kind=kind):
                self.assertNotIn("WARNING", banner(check=console_check(kind, detail="x")))

    def test_no_ping_row_without_a_check(self):
        self.assertNotIn("  ping ", banner())

    def test_every_ping_line_is_ascii_within_the_width_and_never_blank(self):
        for kind in reach.Reach:
            check = console_check(kind, detail=reach.PING_FAILED.format(code=64, detail="ping: bad flag"))
            for line in self.rows(check):
                line.encode("ascii")
                self.assertTrue(line.strip())
                self.assertLessEqual(len(line), serve.BANNER_WIDTH)


class TestThePingAtStartup(TestCheckAnswersWithoutStarting):
    def test_nothing_there_is_a_warning_and_not_a_refusal(self):
        self.found = console_check(reach.Reach.NOTHING_THERE)
        code, stdout, _ = self.start(self.good())
        self.assertEqual(code, 0)
        self.assertTrue(self.ran)
        self.assertIn(serve.NOTHING_THERE_WARNING, stdout)

    def test_a_refused_start_never_pings(self):
        code, _, _ = self.start([*self.CONSOLE])  # no --log
        self.assertEqual(code, 2)
        unreadable = self.root / "unreadable.jsonl"
        unreadable.write_text("{ not json\n" + '{"v": 1}\n', encoding="utf-8")
        self.start([*self.CONSOLE, "--log", str(unreadable)])
        self.pinged.assert_not_called()

    def test_a_start_pings_the_console_host_once(self):
        self.start(self.good())
        self.assertEqual(self.pinged.call_count, 1)
        self.assertEqual(self.pinged.call_args.args[0], "192.0.2.1")

    def test_check_pings_too(self):
        _, started, _ = self.start(self.good())
        code, checked, _ = self.start([*self.good(), "--check"])
        self.assertEqual(code, 0)
        self.assertEqual(self.pinged.call_count, 2)
        self.assertEqual(checked, started)
        self.assertIn("  ping ", checked)

    def test_the_ping_is_never_made_on_the_event_loop(self):
        seen: list[str] = []

        def probe(host, tools):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen.append("no loop")
            else:
                seen.append("on the loop")
            return self.found

        self.pinged = mock.Mock(side_effect=probe)
        self.start(self.good())
        self.assertEqual(seen, ["no loop"])

    def test_the_coroutine_is_given_the_console_check_and_the_tools(self):
        async def nothing():
            pass

        coroutine = nothing()
        self.addCleanup(coroutine.close)
        self.found = console_check(reach.Reach.NO_ANSWER)
        with mock.patch("tacet.serve._run", return_value=coroutine) as run:
            self.start(self.good())
        self.assertEqual(run.call_args.args[2], self.found)
        self.assertIsInstance(run.call_args.args[3], reach.Tools)


class TestBuildWritesBoxStartedFirst(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "game.jsonl"
        self.args = serve.parser().parse_args(["--console-host", "192.0.2.1", "--dca", "3", "--log", str(self.log)])

    def test_the_first_entry_of_a_run_is_box_started_with_its_code(self):
        app, log, queue = serve.build(self.args, DIRTY_CODE)
        self.assertIsNone(queue)
        self.assertEqual(app.snapshot()["provenance"], DIRTY_CODE.as_snapshot())
        log.close()
        first = next(iter(annotations.read_entries(self.log)))
        self.assertEqual(first.event, annotations.BOX_STARTED)
        self.assertEqual(first.data, DIRTY_CODE.as_data())

    def test_a_box_built_after_a_stop_mid_move_reads_the_level_unknown(self):
        # The boot-side recovery #44 relies on (#107): whatever the last run
        # left on the fader, a new box does not believe it knows the level.
        _, log, _ = serve.build(self.args, CLEAN_CODE)
        log.record(tacet_app.MOVE_ABANDONED, data={"level": -150, "target": 0, "kind": "ride", "because": "ride"})
        log.close()
        app, log, _ = serve.build(self.args, CLEAN_CODE)
        log.close()
        self.assertIs(app.snapshot()["fader"]["level_known"], False)

    def test_a_restart_on_the_same_log_writes_another(self):
        for _ in range(2):
            _, log, _ = serve.build(self.args, CLEAN_CODE)
            log.close()
        events = [e.event for e in annotations.read_entries(self.log)]
        self.assertEqual(events, [annotations.BOX_STARTED, annotations.BOX_STARTED])


class TestBuildLogsTheConsoleCheck(unittest.TestCase):
    def test_the_check_is_logged_straight_after_box_started_and_shown_on_the_page(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "game.jsonl"
            args = serve.parser().parse_args(["--console-host", CONSOLE, "--dca", "3", "--log", str(log_path)])
            app, log, _ = serve.build(args, CLEAN_CODE, console_check=ANSWERED_CHECK)
            self.assertEqual(app.snapshot()["console"]["reach"], "answered")
            log.close()
            events = [e.event for e in annotations.read_entries(log_path)]
        self.assertEqual(events, [annotations.BOX_STARTED, annotations.CONSOLE_CHECKED])

    def test_the_startup_entry_names_no_previous_result(self):
        # Nothing was checked before it, so it must not claim something was.
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "game.jsonl"
            args = serve.parser().parse_args(["--console-host", CONSOLE, "--dca", "3", "--log", str(log_path)])
            _, log, _ = serve.build(args, CLEAN_CODE, console_check=ANSWERED_CHECK)
            log.close()
            entry = next(e for e in annotations.read_entries(log_path) if e.event == annotations.CONSOLE_CHECKED)
        self.assertIsNone(entry.data["previous"])

    def test_a_box_built_without_one_logs_none(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "game.jsonl"
            args = serve.parser().parse_args(["--console-host", CONSOLE, "--dca", "3", "--log", str(log_path)])
            _, log, _ = serve.build(args, CLEAN_CODE)
            log.close()
            events = [e.event for e in annotations.read_entries(log_path)]
        self.assertEqual(events, [annotations.BOX_STARTED])


class TestADeadWatchIsSaidOutLoud(unittest.IsolatedAsyncioTestCase):
    """#41's rule: a background task that dies is a fault of its own."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        log_path = Path(self._tmp.name) / "game.jsonl"
        args = serve.parser().parse_args(["--console-host", CONSOLE, "--dca", "3", "--log", str(log_path)])
        self.app, self.log, _ = serve.build(args, CLEAN_CODE, console_check=ANSWERED_CHECK)
        self.addCleanup(self.log.close)

    async def test_a_task_that_died_reports_could_not_check_with_the_reason(self):
        died = asyncio.get_running_loop().create_future()
        died.set_exception(RuntimeError("boom"))
        serve._watch_stopped(self.app, CONSOLE, died)
        block = self.app.snapshot()["console"]
        self.assertEqual(block["reach"], "could-not-check")
        self.assertEqual(block["detail"], reach.CHECK_STOPPED.format(error="RuntimeError: boom"))

    async def test_a_cancelled_task_changes_nothing(self):
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        serve._watch_stopped(self.app, CONSOLE, cancelled)
        self.assertEqual(self.app.snapshot()["console"]["reach"], "answered")

    async def test_a_task_that_finished_cleanly_changes_nothing(self):
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        serve._watch_stopped(self.app, CONSOLE, done)
        self.assertEqual(self.app.snapshot()["console"]["reach"], "answered")


class TestBuildPassesTheFlagsThrough(unittest.TestCase):
    def test_the_retarget_ride_flag_reaches_the_app(self):
        # #128: a flag that build() dropped would leave the default in force,
        # silently.
        with TemporaryDirectory() as tmp:
            argv = ["--console-host", "10.0.0.5", "--log", str(Path(tmp) / "game.jsonl"), "--retarget-ride", "0.25"]
            app, log, _ = serve.build(serve.parser().parse_args(argv), CLEAN_CODE)
            log.close()
        self.assertEqual(app._retarget_ride_seconds, 0.25)
