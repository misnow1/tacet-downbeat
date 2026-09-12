import unittest
from pathlib import Path

from tacet import annotations, dm7, serve


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
    "--log", "/games/2026-09-13.jsonl",
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
        self.assertIn("/games/2026-09-13.jsonl", text)
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
        # Reads as a firewall problem and is not one (docs/gameday.md).
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
