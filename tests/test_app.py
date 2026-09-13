import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tacet import annotations as ann
from tacet import app as tacet_app
from tacet import dm7, osc, reaper, state


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


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.console_sender = FakeSender()
        self.reaper_sender = FakeSender()

    def build(self, *, console_sender=None, fade=0.05, monotonic=None):
        self.log = ann.AnnotationLog(self.root / "game.jsonl")
        self.log.open()
        self.addCleanup(self.log.close)
        self.console = dm7.Dm7Client(
            "192.0.2.1",
            dca=3,
            sender=console_sender or self.console_sender,
            tick_hz=200.0,
        )
        clock = monotonic if monotonic is not None else time.monotonic
        self.reaper = reaper.ReaperClient(sender=self.reaper_sender, monotonic=clock)
        return tacet_app.App(
            console=self.console,
            log=self.log,
            recorder=self.reaper,
            fade_seconds=fade,
            monotonic=clock,
        )

    def entries(self):
        return list(ann.read_entries(self.root / "game.jsonl"))

    def keys(self):
        return [e.event for e in self.entries()]


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
        # Faders only, never mutes - asserted at the level the operator drives.
        app = self.build()
        await app.arm()
        await app.trigger()
        await app.release()
        await app.wait_for_fade()
        for address in self.console_sender.addresses():
            self.assertEqual(address, dm7.fader_address(3))


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
        await app.stand_down()
        self.assertIn("stood-down", self.keys())


class TestAFailedMoveCanBeRetried(AppTestCase):
    """#27: a send error mid-move used to leave the machine stuck.

    A fade that died partway stayed RELEASING, so FADE OUT sent nothing and the
    only way out was OPEN - slamming a half-closed band to unity. A failed open
    stayed OPEN at -inf, and OPEN sent nothing either.
    """

    async def fail_a_fade_partway(self, sender):
        app = self.build(console_sender=sender)
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
        app = self.build(console_sender=sender)
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
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        # One command reached Reaper, not two. The second would have stopped it.
        self.assertEqual(self.record_packets().count("/record"), 1)

    async def test_the_second_tap_says_why_it_did_nothing(self):
        app = self.build()
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertIn("already recording", app.snapshot()["refusal"])

    async def test_a_refused_tap_does_not_log_a_second_start(self):
        app = self.build()
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertEqual(self.keys().count(tacet_app.RECORDING_STARTED), 1)

    async def test_a_silent_reaper_can_still_be_started(self):
        # The ordinary pre-game case: Reaper parked, and silent because it is
        # parked. Refusing here would make the button useless.
        app = self.build()
        await app.start_recording()
        self.assertIn("/record", self.record_packets())
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_a_reaper_that_reported_a_stop_can_be_started(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        await app.start_recording()
        self.assertIn("/record", self.record_packets())

    async def test_a_moving_transport_of_unknown_record_state_is_refused(self):
        """A box restarted mid-game hears /time and no transport change, so it
        cannot tell a safe send from one that ends the recording."""
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 12.0))
        await app.start_recording()
        self.assertNotIn("/record", self.record_packets())
        self.assertIn("not said whether", app.snapshot()["refusal"])

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
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        self.assertIsNotNone(app.snapshot()["refusal"])
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_a_successful_start_clears_an_earlier_refusal(self):
        app = self.build()
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        await app.start_recording()
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        await app.start_recording()
        self.assertIsNone(app.snapshot()["refusal"])

    async def test_the_snapshot_tells_the_page_when_to_disable_the_button(self):
        app = self.build()
        self.assertTrue(app.snapshot()["recording"]["can_start"])
        app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        self.assertFalse(app.snapshot()["recording"]["can_start"])

    async def test_there_is_still_no_way_to_ask_reaper_to_stop(self):
        app = self.build()
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

    async def test_a_refused_move_still_records_the_reason(self):
        """Standing down, a trigger is refused. The operator still heard the
        whistle, and a log that kept only the accepted taps would misrepresent
        the night."""
        app = self.build()
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        await app.annotate("up-whistle")
        self.assertEqual(app.machine.state, state.State.STANDING_DOWN)
        self.assertIn("up-whistle", self.keys())
        self.assertTrue(app.snapshot()["refusal"])

    async def test_a_refused_move_does_not_move_the_fader(self):
        app = self.build()
        await app.annotate("up-whistle")
        self.assertEqual(app.snapshot()["fader"]["commanded"], dm7.MINUS_INF)

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
        self.assertIn("touchdown-sequence", keys)
        self.assertNotIn("commanded", keys)
        self.assertNotIn("armed", keys)


class TestAnnotation(AppTestCase):
    async def test_an_annotation_is_logged(self):
        app = self.build()
        await app.annotate("touchdown-sequence")
        self.assertIn("touchdown-sequence", self.keys())

    async def test_a_note_carries_its_text(self):
        app = self.build()
        await app.annotate("note", data={"text": "band sounds thin"})
        note = [e for e in self.entries() if e.event == "note"][-1]
        self.assertEqual(note.data["text"], "band sounds thin")

    async def test_an_unknown_event_is_refused_without_crashing(self):
        app = self.build()
        with self.assertRaises(ann.UnknownEventError):
            await app.annotate("no-such-event")

    async def test_spans_open_and_close(self):
        app = self.build()
        span = await app.start_span("q1")
        self.assertIn(span, app.snapshot()["open_spans"])
        await app.end_span(span)
        self.assertEqual(app.snapshot()["open_spans"], [])


class TestRecorder(AppTestCase):
    async def test_start_recording_commands_reaper_and_logs_the_anchor(self):
        app = self.build()
        await app.start_recording()
        self.assertEqual(self.reaper_sender.addresses(), [reaper.DEFAULT_ADDRESSES.record])
        self.assertIn("recording-started", self.keys())

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

    async def test_spans_are_stamped_at_both_ends(self):
        app = self.build()
        app.handle_recorder_packet(osc.encode_message("/time", 10.0))
        span = await app.start_span("q1")
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

    async def test_the_anchor_itself_is_not_stamped(self):
        # The command has just gone out; Reaper has not begun rolling, so its
        # last position is where the transport was parked.
        app = self.build()
        # Reaper says stopped, then streams a position: the record button is
        # allowed and there is a fresh playhead available to stamp.
        app.handle_recorder_packet(osc.encode_message("/record", 0.0))
        app.handle_recorder_packet(osc.encode_message("/time", 77.0))
        await app.start_recording()
        anchor = [e for e in self.entries() if e.event == ann.ANCHOR_EVENT][-1]
        self.assertIsNone(anchor.project_seconds)

    async def test_no_recorder_means_no_stamp(self):
        self.build()  # sets up the console and the log
        app = tacet_app.App(console=self.console, log=self.log, recorder=None)
        await app.annotate("band-enters-stands")
        self.assertIsNone(self.last().project_seconds)


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

    async def test_the_page_is_pushed_to_while_the_fade_runs(self):
        # Without this the number holds its pre-fade value for the whole close
        # and then jumps, which reads as a fader that never moved.
        pushes = []
        app = self.build(fade=0.4)
        app.on_change(lambda: pushes.append(app.snapshot()["fader"]["db"]))
        await app.arm()
        await app.annotate("up-drums")
        before = len(pushes)
        await app.annotate("out")
        await app.wait_for_fade()
        self.assertGreater(len(pushes) - before, 2)

    async def test_no_pusher_survives_a_finished_fade(self):
        app = self.build(fade=0.05)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.wait_for_fade()
        await asyncio.sleep(0)
        self.assertIsNone(app._move_push)

    async def test_snapping_back_to_open_stops_the_pusher(self):
        # A trigger during the close cancels the fade; nothing should still be
        # describing a move that is no longer happening.
        app = self.build(fade=5.0)
        await app.arm()
        await app.annotate("up-drums")
        await app.annotate("out")
        await app.annotate("up-whistle")
        self.assertIsNone(app._move_push)
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
        self.assertIsNone(app._move_target)

    async def test_up_slow_does_not_arrive_immediately(self):
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        await app.annotate("up-slow")
        # Still on its way up: a snap would already be at unity.
        self.assertLess(app._console.commanded_level, dm7.UNITY)
        await app.wait_for_fade()

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

    async def test_the_page_is_pushed_to_during_the_ride_in(self):
        pushes = []
        app = self.build()
        app._slow_open_seconds = 0.4
        await app.arm()
        app.on_change(lambda: pushes.append(app.snapshot()["fader"]["db"]))
        await app.annotate("up-slow")
        await app.wait_for_fade()
        self.assertGreater(len(pushes), 2)

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
        await app.annotate("out")
        self.assertEqual(app.snapshot()["fader"]["target"], dm7.MINUS_INF)
        await app.wait_for_fade()
        self.assertEqual(app._console.commanded_level, dm7.MINUS_INF)

    async def test_up_slow_is_still_an_open_to_the_machine(self):
        app = self.build()
        app._slow_open_seconds = 0.2
        await app.arm()
        await app.annotate("up-slow")
        self.assertEqual(app.snapshot()["state"], state.State.OPEN.value)
        await app.wait_for_fade()
