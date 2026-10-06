"""The console check (#73): what a ping can and cannot say, and the watch that
repeats it.

No test here sends ICMP. The runner is a fake everywhere except the tests
that start a child which is not ping, to prove a hung or cancelled child is
killed and reaped.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

from tacet import reach
from tests.reach_fixtures import (
    ANSWER_TEXT,
    ANSWERED,
    ARP,
    BSD_INCOMPLETE,
    BSD_NO_ENTRY,
    BSD_RESOLVED,
    HOST,
    LINUX_NO_REPLY,
    LINUX_TOOLS,
    MAC_NO_REPLY,
    MAC_TOOLS,
    PING,
    FakeRunner,
    GatedRunner,
    ManualClock,
    out,
)

PROC_HEADER = "IP address       HW type     Flags       HW address            Mask     Device\n"
PROC_RESOLVED = "192.0.2.1       0x1         0x2         ac:44:f2:c9:02:84     *        eth0\n"
PROC_UNRESOLVED = "192.0.2.1       0x1         0x0         00:00:00:00:00:00     *        eth0\n"
PROC_LONGER = "192.0.2.12      0x1         0x2         ac:44:f2:c9:02:99     *        eth0\n"


def run(coroutine):
    return asyncio.run(coroutine)


def one_check(runner, tools=MAC_TOOLS, *, proc="", trigger=reach.Trigger.ARM):
    return run(
        reach.check(
            HOST,
            tools=tools,
            trigger=trigger,
            runner=runner,
            monotonic=lambda: 5.0,
            read_proc_arp=lambda: proc,
        )
    )


class TestPingFlags(unittest.TestCase):
    def test_macos_ping_uses_t_for_the_deadline(self):
        self.assertEqual(reach.ping_command(MAC_TOOLS, HOST), [PING, "-c", "1", "-n", "-t", "2", HOST])

    def test_linux_ping_uses_w_for_the_deadline(self):
        self.assertEqual(reach.ping_command(LINUX_TOOLS, HOST), ["/bin/ping", "-c", "1", "-n", "-w", "2", HOST])

    def test_neither_platform_uses_capital_w(self):
        # Per-reply, and milliseconds on macOS but seconds on Linux: a trap.
        for tools in (MAC_TOOLS, LINUX_TOOLS):
            self.assertNotIn("-W", reach.ping_command(tools, HOST) or [])

    def test_every_ping_is_one_packet_without_reverse_dns(self):
        for tools in (MAC_TOOLS, LINUX_TOOLS):
            argv = reach.ping_command(tools, HOST) or []
            self.assertEqual(argv[argv.index("-c") + 1], "1")
            self.assertIn("-n", argv)

    def test_an_unknown_platform_has_no_ping_command(self):
        self.assertIsNone(reach.ping_command(reach.Tools(platform=None, ping=PING, arp=None), HOST))
        self.assertIsNone(reach.platform_of("win32"))
        self.assertEqual(reach.platform_of("darwin"), reach.Platform.MACOS)
        self.assertEqual(reach.platform_of("linux"), reach.Platform.LINUX)

    def test_no_ping_without_an_executable(self):
        self.assertIsNone(reach.ping_command(reach.Tools(platform=reach.Platform.MACOS, ping=None, arp=ARP), HOST))

    def test_arp_is_only_run_on_macos(self):
        self.assertEqual(reach.arp_command(MAC_TOOLS, HOST), [ARP, "-n", HOST])
        self.assertIsNone(reach.arp_command(LINUX_TOOLS, HOST))

    def test_tools_are_found_in_system_dirs_when_path_lacks_them(self):
        def which(name, path=None):
            return f"/sbin/{name}" if path and "/sbin" in path.split(os.pathsep) else None

        tools = reach.find_tools(reach.Platform.MACOS, which=which, path="/usr/bin")
        self.assertEqual(tools.ping, "/sbin/ping")
        self.assertEqual(tools.arp, "/sbin/arp")

    def test_arp_is_not_looked_up_on_linux(self):
        tools = reach.find_tools(reach.Platform.LINUX, which=lambda name, path=None: f"/bin/{name}", path="")
        self.assertEqual(tools.ping, "/bin/ping")
        self.assertIsNone(tools.arp)

    def test_a_missing_ping_is_could_not_check_before_any_child_is_started(self):
        runner = FakeRunner()
        result = one_check(runner, reach.Tools(platform=reach.Platform.MACOS, ping=None, arp=ARP))
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(result.detail, reach.PING_NOT_FOUND)
        self.assertEqual(runner.calls, [])

    def test_an_unsupported_platform_says_which(self):
        runner = FakeRunner()
        result = one_check(runner, reach.Tools(platform=None, ping=PING, arp=None))
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertIn("no ping flags known", result.detail or "")
        self.assertEqual(runner.calls, [])


class TestNeighbourParsing(unittest.TestCase):
    def test_bsd_incomplete_is_unresolved(self):
        self.assertEqual(reach.parse_bsd_arp(0, BSD_INCOMPLETE, HOST), (reach.Neighbour.UNRESOLVED, None))

    def test_bsd_mac_is_resolved_with_its_mac(self):
        self.assertEqual(reach.parse_bsd_arp(0, BSD_RESOLVED, HOST), (reach.Neighbour.RESOLVED, "ce:71:fc:ab:d6:7a"))

    def test_bsd_no_entry_is_absent(self):
        self.assertEqual(reach.parse_bsd_arp(1, BSD_NO_ENTRY, HOST), (reach.Neighbour.ABSENT, None))

    def test_bsd_garbage_is_unreadable(self):
        self.assertEqual(reach.parse_bsd_arp(0, "what\n", HOST), (reach.Neighbour.UNREADABLE, None))
        self.assertEqual(reach.parse_bsd_arp(1, "", HOST), (reach.Neighbour.UNREADABLE, None))

    def test_bsd_does_not_match_a_longer_address(self):
        text = "? (192.0.2.12) at (incomplete) on en0 ifscope [ethernet]\n"
        self.assertEqual(reach.parse_bsd_arp(0, text, HOST), (reach.Neighbour.UNREADABLE, None))

    def test_proc_flags_0x2_is_resolved(self):
        text = PROC_HEADER + PROC_RESOLVED
        self.assertEqual(reach.parse_proc_net_arp(text, HOST), (reach.Neighbour.RESOLVED, "ac:44:f2:c9:02:84"))

    def test_proc_flags_0x0_is_unresolved(self):
        text = PROC_HEADER + PROC_UNRESOLVED
        self.assertEqual(reach.parse_proc_net_arp(text, HOST), (reach.Neighbour.UNRESOLVED, None))

    def test_proc_missing_row_is_absent(self):
        text = PROC_HEADER + PROC_LONGER
        self.assertEqual(reach.parse_proc_net_arp(text, HOST), (reach.Neighbour.ABSENT, None))

    def test_proc_resolved_on_any_device_wins(self):
        text = PROC_HEADER + PROC_UNRESOLVED + PROC_RESOLVED.replace("eth0", "eth1")
        self.assertEqual(reach.parse_proc_net_arp(text, HOST), (reach.Neighbour.RESOLVED, "ac:44:f2:c9:02:84"))

    def test_proc_malformed_is_unreadable(self):
        self.assertEqual(reach.parse_proc_net_arp("", HOST), (reach.Neighbour.UNREADABLE, None))
        text = PROC_HEADER + "192.0.2.1 garbage\n"
        self.assertEqual(reach.parse_proc_net_arp(text, HOST), (reach.Neighbour.UNREADABLE, None))


class TestClassification(unittest.TestCase):
    def classify(self, output, neighbour=None, platform=reach.Platform.MACOS):
        return reach.classify(platform, output, neighbour)

    def test_exit_0_is_answered(self):
        self.assertEqual(self.classify(ANSWERED)[0], reach.Reach.ANSWERED)

    def test_host_is_down_is_nothing_there(self):
        output = reach.ProcessOutput(68, b"", b"ping: sendto: Host is down\n")
        result = self.classify(output)
        self.assertEqual(result[0], reach.Reach.NOTHING_THERE)
        self.assertIn("Host is down", result[1] or "")

    def test_no_route_to_host_is_nothing_there(self):
        output = reach.ProcessOutput(68, b"", b"ping: sendto: No route to host\n")
        self.assertEqual(self.classify(output)[0], reach.Reach.NOTHING_THERE)

    def test_destination_host_unreachable_on_stdout_is_nothing_there(self):
        output = reach.ProcessOutput(1, b"From 192.0.2.9 icmp_seq=1 Destination Host Unreachable\n", b"")
        self.assertEqual(self.classify(output, platform=reach.Platform.LINUX)[0], reach.Reach.NOTHING_THERE)

    def test_no_reply_with_an_unresolved_neighbour_is_nothing_there(self):
        result = self.classify(MAC_NO_REPLY, (reach.Neighbour.UNRESOLVED, None))
        self.assertEqual(result[0], reach.Reach.NOTHING_THERE)

    def test_no_reply_with_a_resolved_neighbour_is_only_no_answer(self):
        # A stale MAC is never read as "something is there": expired entries
        # keep theirs. Only the lack of a reply is known.
        found, _, mac = self.classify(MAC_NO_REPLY, (reach.Neighbour.RESOLVED, "ce:71:fc:ab:d6:7a"))
        self.assertEqual(found, reach.Reach.NO_ANSWER)
        self.assertEqual(mac, "ce:71:fc:ab:d6:7a")

    def test_no_reply_with_no_neighbour_row_is_no_answer_off_link(self):
        found, detail, _ = self.classify(MAC_NO_REPLY, (reach.Neighbour.ABSENT, None))
        self.assertEqual(found, reach.Reach.NO_ANSWER)
        self.assertEqual(detail, reach.OFF_LINK)

    def test_no_reply_with_an_unreadable_neighbour_is_no_answer(self):
        for neighbour in ((reach.Neighbour.UNREADABLE, None), None):
            self.assertEqual(self.classify(MAC_NO_REPLY, neighbour)[0], reach.Reach.NO_ANSWER)

    def test_an_unexpected_exit_is_could_not_check_with_stderr(self):
        output = reach.ProcessOutput(64, b"", b"\nping: bad flag\n")
        found, detail, _ = self.classify(output)
        self.assertEqual(found, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(detail, reach.PING_FAILED.format(code=64, detail="ping: bad flag"))

    def test_the_no_reply_exit_code_is_per_platform(self):
        two = reach.ProcessOutput(2, b"", b"")
        self.assertEqual(self.classify(two)[0], reach.Reach.NO_ANSWER)
        self.assertEqual(self.classify(two, platform=reach.Platform.LINUX)[0], reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(self.classify(LINUX_NO_REPLY, platform=reach.Platform.LINUX)[0], reach.Reach.NO_ANSWER)

    def test_rtt_is_parsed_from_either_platforms_output(self):
        self.assertEqual(reach.parse_rtt(ANSWER_TEXT), 1.234)
        self.assertEqual(reach.parse_rtt("64 bytes from x: icmp_seq=1 ttl=64 time=0.05 ms"), 0.05)
        self.assertIsNone(reach.parse_rtt("nothing"))


class TestTheShell(unittest.TestCase):
    def test_the_neighbour_table_is_read_only_after_a_ping_with_no_reply(self):
        runner = FakeRunner(ping=ANSWERED)
        one_check(runner)
        self.assertEqual(len(runner.calls), 1)
        runner = FakeRunner(ping=MAC_NO_REPLY, arp=out(BSD_INCOMPLETE))
        result = one_check(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result.reach, reach.Reach.NOTHING_THERE)

    def test_a_marker_in_pings_own_output_skips_the_neighbour_read(self):
        runner = FakeRunner(ping=reach.ProcessOutput(68, b"", b"ping: sendto: Host is down\n"))
        self.assertEqual(one_check(runner).reach, reach.Reach.NOTHING_THERE)
        self.assertEqual(len(runner.calls), 1)

    def test_the_result_carries_the_trigger_the_host_and_the_rtt(self):
        result = one_check(FakeRunner(), trigger=reach.Trigger.KEEPALIVE)
        self.assertEqual(result.reach, reach.Reach.ANSWERED)
        self.assertEqual(result.trigger, reach.Trigger.KEEPALIVE)
        self.assertEqual(result.host, HOST)
        self.assertEqual(result.rtt_ms, 1.234)
        self.assertEqual(result.at, 5.0)

    def test_linux_reads_proc_not_a_child(self):
        runner = FakeRunner(ping=LINUX_NO_REPLY)
        result = one_check(runner, LINUX_TOOLS, proc=PROC_HEADER + PROC_UNRESOLVED)
        self.assertEqual(result.reach, reach.Reach.NOTHING_THERE)
        self.assertEqual(len(runner.calls), 1)

    def test_a_failed_neighbour_read_degrades_to_no_answer_and_never_raises(self):
        for arp in (reach.CheckUnavailableError("arp did not finish"), RuntimeError("boom")):
            with self.subTest(arp=arp):
                result = one_check(FakeRunner(ping=MAC_NO_REPLY, arp=arp))
                self.assertEqual(result.reach, reach.Reach.NO_ANSWER)

        def unreadable() -> str:
            raise OSError("no /proc")

        result = run(
            reach.check(
                HOST,
                tools=LINUX_TOOLS,
                trigger=reach.Trigger.ARM,
                runner=FakeRunner(ping=LINUX_NO_REPLY),
                monotonic=lambda: 0.0,
                read_proc_arp=unreadable,
            )
        )
        self.assertEqual(result.reach, reach.Reach.NO_ANSWER)

    def test_a_check_never_raises(self):
        result = one_check(FakeRunner(ping=RuntimeError("boom")))
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(result.detail, reach.CHECK_FAILED.format(error="RuntimeError: boom"))

    def test_an_unavailable_child_is_could_not_check_with_its_detail(self):
        result = one_check(FakeRunner(ping=reach.CheckUnavailableError("ping did not finish within 3s")))
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(result.detail, "ping did not finish within 3s")

    def test_the_check_runs_nothing_but_ping_and_arp_against_the_console_host_only(self):
        outcomes: list[reach.ProcessOutput | Exception] = [
            ANSWERED,
            MAC_NO_REPLY,
            reach.ProcessOutput(68, b"", b"Host is down"),
            reach.ProcessOutput(64, b"", b"bad"),
            RuntimeError("boom"),
        ]
        for outcome in outcomes:
            runner = FakeRunner(ping=outcome)
            one_check(runner)
            for argv in runner.calls:
                self.assertIn(argv[0], (MAC_TOOLS.ping, MAC_TOOLS.arp))
                self.assertEqual(argv[-1], HOST)

    def test_probe_never_raises_and_is_tagged_startup(self):
        result = reach.probe(HOST, MAC_TOOLS, runner=FakeRunner(ping=RuntimeError("boom")))
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(result.trigger, reach.Trigger.STARTUP)
        self.assertEqual(reach.probe(HOST, MAC_TOOLS, runner=FakeRunner()).reach, reach.Reach.ANSWERED)
        with mock.patch("tacet.reach.asyncio.run", side_effect=RuntimeError("no loop")):
            result = reach.probe(HOST, MAC_TOOLS, runner=FakeRunner())
        self.assertEqual(result.reach, reach.Reach.COULD_NOT_CHECK)
        self.assertEqual(result.trigger, reach.Trigger.STARTUP)

    def test_the_outer_timeout_is_longer_than_pings_own_deadline(self):
        self.assertGreater(reach.PING_TIMEOUT_SECONDS, reach.PING_DEADLINE_SECONDS)


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TestTheRealRunner(unittest.TestCase):
    def test_a_hung_ping_is_killed_and_reported(self):
        runner = reach.AsyncioRunner()
        with self.assertRaises(reach.CheckUnavailableError) as caught:
            run(runner.run(SLEEPER, timeout=0.2))
        self.assertIn("did not finish", str(caught.exception))
        assert runner.last_pid is not None
        self.assertTrue(gone(runner.last_pid))

    def test_cancelling_the_check_kills_the_child(self):
        runner = reach.AsyncioRunner()

        async def scenario():
            task = asyncio.ensure_future(runner.run(SLEEPER, timeout=30))
            while runner.last_pid is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(scenario())
        assert runner.last_pid is not None
        self.assertTrue(gone(runner.last_pid))

    def test_a_missing_executable_is_unavailable_not_an_exception(self):
        with self.assertRaises(reach.CheckUnavailableError):
            run(reach.AsyncioRunner().run(["/nonexistent/ping-xyz"], timeout=1))

    def test_the_child_runs_with_lc_all_c_and_no_stdin(self):
        runner = reach.AsyncioRunner()
        seen = {}

        class Child:
            returncode = 0
            pid = 1

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

        async def create(*argv, **kwargs):
            seen.update(kwargs)
            return Child()

        with mock.patch("tacet.reach.asyncio.create_subprocess_exec", create):
            run(runner.run(["ping"], timeout=1))
        self.assertEqual(seen["env"]["LC_ALL"], "C")
        self.assertEqual(seen["stdin"], asyncio.subprocess.DEVNULL)

    def test_the_spawn_is_timed(self):
        runner = reach.AsyncioRunner()
        run(runner.run([sys.executable, "-c", "pass"], timeout=10))
        self.assertIsNotNone(runner.last_spawn_ms)


class TestTheWords(unittest.TestCase):
    def test_the_summary_says_only_what_the_ping_proved(self):
        cases = {
            reach.Reach.ANSWERED: "answered ping (1.2 ms)",
            reach.Reach.NO_ANSWER: "did not answer ping",
            reach.Reach.NOTHING_THERE: "nothing at this address (no ARP reply)",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(reach.Check(kind, None, HOST, 0.0, rtt_ms=1.234).summary(), text)
        self.assertEqual(
            reach.Check(reach.Reach.COULD_NOT_CHECK, None, HOST, 0.0, detail="ping is not installed").summary(),
            "could not check: ping is not installed",
        )
        self.assertEqual(reach.Check.not_checked(HOST).summary(), "not checked yet")

    def test_the_words_are_ascii(self):
        for word in (reach.ANSWERED, reach.NO_ANSWER, reach.NOTHING_THERE, reach.COULD_NOT_CHECK, reach.NOT_CHECKED):
            word.encode("ascii")

    def test_the_log_data_is_flat_and_the_snapshot_is_small(self):
        check = reach.Check(reach.Reach.NO_ANSWER, reach.Trigger.ARM, HOST, 7.0, detail="d", mac="aa", spawn_ms=1.0)
        data = check.as_data()
        self.assertEqual(data["reach"], "no-answer")
        self.assertEqual(data["trigger"], "arm")
        self.assertEqual(data["host"], HOST)
        self.assertEqual(
            check.as_snapshot(), {"reach": "no-answer", "detail": "d", "checked_at": 7.0, "trigger": "arm"}
        )
        self.assertEqual(reach.Check.not_checked(HOST).as_snapshot()["reach"], "not-checked")


class TestTheIntervals(unittest.TestCase):
    def test_the_keepalive_interval_is_well_inside_the_macos_arp_lifetime(self):
        self.assertEqual(reach.MACOS_ARP_MAX_AGE_SECONDS, 1200)
        self.assertEqual(reach.KEEPALIVE_SECONDS * reach.KEEPALIVES_PER_ARP_LIFETIME, reach.MACOS_ARP_MAX_AGE_SECONDS)
        self.assertEqual(reach.KEEPALIVE_SECONDS, 240)
        # A check can never still be running when the next is due.
        self.assertGreater(reach.KEEPALIVE_SECONDS, reach.PING_TIMEOUT_SECONDS + reach.ARP_TIMEOUT_SECONDS)


class WatchCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.runner = FakeRunner()
        self.results: list[reach.Check] = []
        self.moving = False

    def watch(self, runner=None, on_result=None, first_due=None):
        return reach.Watch(
            HOST,
            MAC_TOOLS,
            runner=runner or self.runner,
            busy=lambda: self.moving,
            on_result=on_result or self.results.append,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            first_due=first_due,
        )

    async def started(self, watch):
        task = asyncio.ensure_future(watch.run())
        self.addAsyncCleanup(self.stop, task)
        await self.clock.settle()
        return task

    async def stop(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def pings(self):
        return [c for c in self.runner.calls if c[0] == PING]


class TestTheWatch(WatchCase):
    async def test_a_request_runs_a_check_now(self):
        watch = self.watch(first_due=reach.KEEPALIVE_SECONDS)
        await self.started(watch)
        self.assertEqual(self.pings(), [])
        watch.request(reach.Trigger.ARM)
        await self.clock.settle()
        self.assertEqual(len(self.pings()), 1)
        self.assertEqual([r.trigger for r in self.results], [reach.Trigger.ARM])

    async def test_checks_repeat_every_keepalive_interval(self):
        watch = self.watch(first_due=0.0)
        await self.started(watch)
        self.assertEqual(len(self.pings()), 1)
        await self.clock.advance(reach.KEEPALIVE_SECONDS - 1)
        self.assertEqual(len(self.pings()), 1)
        await self.clock.advance(1)
        self.assertEqual(len(self.pings()), 2)
        await self.clock.advance(reach.KEEPALIVE_SECONDS)
        self.assertEqual(len(self.pings()), 3)
        self.assertEqual({r.trigger for r in self.results}, {reach.Trigger.KEEPALIVE})

    async def test_the_first_keepalive_waits_one_interval_after_startup(self):
        watch = self.watch(first_due=self.clock.now + reach.KEEPALIVE_SECONDS)
        await self.started(watch)
        self.assertEqual(self.pings(), [])
        await self.clock.advance(reach.KEEPALIVE_SECONDS - 1)
        self.assertEqual(self.pings(), [])
        await self.clock.advance(1)
        self.assertEqual(len(self.pings()), 1)

    async def test_a_keepalive_tick_during_a_fader_move_is_skipped(self):
        self.moving = True
        watch = self.watch(first_due=0.0)
        await self.started(watch)
        await self.clock.advance(reach.BUSY_RETRY_SECONDS * 3)
        self.assertEqual(self.pings(), [])
        self.assertEqual(self.results, [])
        self.moving = False
        await self.clock.advance(reach.BUSY_RETRY_SECONDS)
        self.assertEqual(len(self.pings()), 1)

    async def test_an_arm_check_during_a_move_runs_as_soon_as_the_move_ends(self):
        watch = self.watch(first_due=reach.KEEPALIVE_SECONDS)
        await self.started(watch)
        self.moving = True
        watch.request(reach.Trigger.ARM)
        await self.clock.advance(reach.BUSY_RETRY_SECONDS * 2)
        self.assertEqual(self.pings(), [])
        self.moving = False
        await self.clock.advance(reach.BUSY_RETRY_SECONDS)
        self.assertEqual([r.trigger for r in self.results], [reach.Trigger.ARM])

    async def test_a_ping_is_not_started_while_one_is_running(self):
        gated = GatedRunner()
        watch = self.watch(runner=gated, first_due=reach.KEEPALIVE_SECONDS)
        await self.started(watch)
        watch.request(reach.Trigger.ARM)
        await self.clock.settle()
        self.assertTrue(watch.running)
        watch.request(reach.Trigger.ARM)
        await self.clock.settle()
        self.assertEqual(gated.started, 1)
        gated.gate.set()
        await self.clock.settle()
        self.assertEqual(gated.started, 1)
        self.assertEqual(len(self.results), 1)
        self.assertFalse(watch.running)

    async def test_cancelling_the_watch_cancels_the_check_in_flight(self):
        cancelled = []

        class Hangs(FakeRunner):
            async def run(self, argv, *, timeout):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
                raise AssertionError("unreachable")

        watch = self.watch(runner=Hangs(), first_due=0.0)
        task = asyncio.ensure_future(watch.run())
        await self.clock.settle()
        self.assertTrue(watch.running)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancelled, [True])

    async def test_the_watch_survives_a_check_that_raises(self):
        watch = self.watch(runner=FakeRunner(ping=RuntimeError("boom")), first_due=0.0)
        task = await self.started(watch)
        self.assertEqual(self.results[0].reach, reach.Reach.COULD_NOT_CHECK)
        await self.clock.advance(reach.KEEPALIVE_SECONDS)
        self.assertEqual(len(self.results), 2)
        self.assertFalse(task.done())

    async def test_the_watch_survives_a_callback_that_raises(self):
        seen = []

        def callback(check):
            seen.append(check)
            raise RuntimeError("page is gone")

        watch = self.watch(on_result=callback, first_due=0.0)
        task = await self.started(watch)
        await self.clock.advance(reach.KEEPALIVE_SECONDS)
        self.assertEqual(len(seen), 2)
        self.assertFalse(task.done())

    async def test_an_arm_request_before_the_loop_has_started_is_not_lost(self):
        watch = self.watch(first_due=reach.KEEPALIVE_SECONDS)
        watch.request(reach.Trigger.ARM)
        await self.started(watch)
        self.assertEqual([r.trigger for r in self.results], [reach.Trigger.ARM])


if __name__ == "__main__":
    unittest.main()
