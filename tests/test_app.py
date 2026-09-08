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
