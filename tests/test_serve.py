import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tacet import annotations, disk, dm7, serve


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

    def test_it_says_the_fader_does_not_move(self):
        # The other one. Nothing fades on the way out; the console keeps its
        # last commanded level and the operator has the iPad.
        self.assertIn("does not move the fader", serve.STOP_WARNING)

    def test_it_names_the_window_it_is_describing(self):
        self.assertIn(f"{serve.STOP_CONFIRM_SECONDS:.0f}s", serve.STOP_WARNING)


FULL = [
    "--console-host", "10.0.0.5",
    "--dca", "3",
    "--log", "/games/game.jsonl",
    "--queue", "/queue/tacet.tsv",
    "--reaper-host", "127.0.0.1",
]  # fmt: skip


def banner(*extra, argv=None):
    """The banner for a set of flags, as one string. No config file, no sockets."""
    args = serve.parser().parse_args((FULL if argv is None else argv) + list(extra))
    return "\n".join(serve.startup_lines(args, None))


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
