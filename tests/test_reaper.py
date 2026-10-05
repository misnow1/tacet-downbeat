import unittest

from tacet import osc, reaper
from tacet.net import TransportError

#: A meter address from the bench capture: Reaper streams these whenever its
#: audio device runs, parked or rolling (#163).
METER = "/master/vu"


class FakeSender:
    def __init__(self):
        self.packets = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)

    def addresses(self) -> list[str]:
        out = []
        for packet in self.packets:
            decoded = osc.decode_packet(packet)
            assert isinstance(decoded, osc.Message)
            out.append(decoded.address)
        return out


class FailingSender:
    def send(self, packet: bytes) -> None:
        raise TransportError("reaper is not listening")


def client(**kwargs):
    sender = kwargs.pop("sender", None) or FakeSender()
    return reaper.ReaperClient(sender=sender, **kwargs), sender


def feed(state, address, *args, now=0.0):
    return reaper.apply_feedback(osc.encode_message(address, *args), state, now=now)


class TestTransportState(unittest.TestCase):
    def test_everything_starts_unknown_not_stopped(self):
        # "Not recording" and "we have not heard" are different, and the UI must
        # be able to tell them apart. See CLAUDE.md on failing visibly.
        state = reaper.TransportState()
        self.assertIsNone(state.recording)
        self.assertIsNone(state.playing)
        self.assertIsNone(state.position)
        self.assertIsNone(state.last_packet)

    def test_never_heard_from_is_never_fresh(self):
        self.assertFalse(reaper.TransportState().is_fresh(now=0.0))

    def test_freshness_expires(self):
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        self.assertTrue(state.is_fresh(now=100.5, timeout=2.0))
        self.assertFalse(state.is_fresh(now=103.0, timeout=2.0))


class TestLiveness(unittest.TestCase):
    """Reaper only talks while the transport moves.

    Measured 2026-09-08: about 11 Hz of `/time` while rolling, one burst per
    transport change, and complete silence when parked. So silence only carries
    information when we were expecting a stream.
    """

    def test_never_heard_from_is_unknown(self):
        got = reaper.TransportState().liveness(now=0.0)
        self.assertIs(got, reaper.Liveness.UNKNOWN)

    def test_recent_traffic_is_live(self):
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        self.assertIs(state.liveness(now=100.5, timeout=2.0), reaper.Liveness.LIVE)

    def test_silence_while_rolling_is_lost(self):
        # Reaper was streaming /time and stopped mid-sentence. That is a fault
        # and the operator has to see it.
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        self.assertIs(state.liveness(now=103.0, timeout=2.0), reaper.Liveness.LOST)

    def test_silence_while_stopped_is_quiet(self):
        # The old code called this a lost link. It is just Reaper sitting there.
        state = feed(reaper.TransportState(), "/record", 0.0, now=100.0)
        state = feed(state, "/play", 0.0, now=100.0)
        self.assertIs(state.liveness(now=103.0, timeout=2.0), reaper.Liveness.QUIET)

    def test_silence_after_traffic_that_never_said_what_it_was_doing(self):
        # A position with no transport state leaves us unable to say whether
        # silence is expected. Unknown, not a fault, and not reassurance.
        state = feed(reaper.TransportState(), "/time", 4.0, now=100.0)
        self.assertIs(state.liveness(now=103.0, timeout=2.0), reaper.Liveness.UNKNOWN)

    def test_quiet_can_never_assert_that_reaper_is_rolling(self):
        """The safety property that makes trusting a stale reading acceptable.

        QUIET is only reachable when the last thing Reaper said was that it had
        stopped, so a believed-but-stale reading can only ever under-claim. If
        Reaper dies while parked we keep showing "stopped", which stays true;
        there is no path on which we show ROLLING at a dead recorder.
        """
        for recording in (True, None):
            for playing in (True, None):
                state = reaper.TransportState(recording=recording, playing=playing, last_packet=100.0)
                if state.liveness(now=103.0, timeout=2.0) is reaper.Liveness.QUIET:
                    self.assertNotEqual(state.recording, True)

    def test_a_recording_report_with_meters_but_no_clock_is_lost(self):
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        for now in (101.0, 102.0, 103.0):
            state = feed(state, METER, 0.0, now=now)
        self.assertIs(state.liveness(103.0, timeout=2.0), reaper.Liveness.LOST)

    def test_a_relaunched_reaper_is_not_shown_rolling(self):
        # Quit mid-take, relaunched parked: meters flow again but the last
        # transport word is a stale "recording" (#163).
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        state = feed(state, "/play", 1.0, now=100.0)
        now = 100.0
        while now <= 110.0:
            state = feed(state, "/time", now - 100.0, now=now)
            now += 0.1
        for at in (130.0, 131.0):
            state = feed(state, METER, 0.0, now=at)
        self.assertIs(state.liveness(131.0, timeout=2.0), reaper.Liveness.LOST)
        self.assertEqual(reaper.record_refusal(state, 131.0), reaper.RECORD_REFUSED_LOST)

    def test_the_start_burst_is_live_before_the_first_clock(self):
        state = reaper.TransportState()
        for address, value in (("/record", 1.0), ("/stop", 0.0), ("/play", 1.0)):
            state = feed(state, address, value, now=100.0)
        self.assertIs(state.liveness(100.05, timeout=2.0), reaper.Liveness.LIVE)
        self.assertIs(state.liveness(102.0, timeout=2.0), reaper.Liveness.LIVE)
        self.assertIs(state.liveness(102.5, timeout=2.0), reaper.Liveness.LOST)

    def test_a_clock_that_keeps_coming_keeps_a_recording_live(self):
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        now = 100.0
        while now <= 110.0:
            state = feed(state, "/time", now - 100.0, now=now)
            state = feed(state, METER, 0.0, now=now)
            now += 0.1
        self.assertIs(state.liveness(110.0, timeout=2.0), reaper.Liveness.LIVE)

    def test_a_stale_clock_from_an_earlier_take_does_not_hold_a_new_one_live(self):
        state = feed(reaper.TransportState(), "/time", 3.0, now=50.0)
        state = feed(state, "/record", 1.0, now=100.0)
        self.assertIs(state.liveness(102.5, timeout=2.0), reaper.Liveness.LOST)

    def test_is_fresh_still_means_live(self):
        state = feed(reaper.TransportState(), "/record", 1.0, now=100.0)
        for now in (100.5, 103.0):
            self.assertEqual(
                state.is_fresh(now=now, timeout=2.0),
                state.liveness(now=now, timeout=2.0) is reaper.Liveness.LIVE,
            )


class TestFeedback(unittest.TestCase):
    def test_record_on_and_off(self):
        state = feed(reaper.TransportState(), "/record", 1.0)
        self.assertTrue(state.recording)
        self.assertFalse(feed(state, "/record", 0.0).recording)

    def test_play_on_and_off(self):
        state = feed(reaper.TransportState(), "/play", 1.0)
        self.assertTrue(state.playing)
        self.assertFalse(feed(state, "/play", 0.0).playing)

    def test_position(self):
        state = feed(reaper.TransportState(), "/time", 12.5)
        assert state.position is not None
        self.assertAlmostEqual(state.position, 12.5)

    def test_a_position_json_cannot_carry_is_ignored(self):
        # It would be stamped onto log entries as `project_seconds` (#36).
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                state = feed(reaper.TransportState(), "/time", value)
                self.assertIsNone(state.position)

    def test_integer_arguments_are_accepted(self):
        # Not every controller sends floats.
        self.assertTrue(feed(reaper.TransportState(), "/record", 1).recording)

    def test_transport_reports_stamp_transport_at(self):
        for address in ("/play", "/record"):
            with self.subTest(address=address):
                state = feed(reaper.TransportState(), address, 0.0, now=7.0)
                self.assertEqual(state.transport_at, 7.0)

    def test_meters_do_not_stamp_transport_at(self):
        state = feed(reaper.TransportState(), METER, 0.5, now=7.0)
        self.assertIsNone(state.transport_at)
        state = feed(state, "/time", 1.0, now=8.0)
        self.assertIsNone(state.transport_at)

    def test_a_packet_stamps_liveness_even_when_unrecognised(self):
        # Any valid packet proves the link is up, which is what freshness means.
        state = feed(reaper.TransportState(), "/something/unmapped", 1.0, now=5.0)
        self.assertEqual(state.last_packet, 5.0)

    def test_an_unrecognised_address_changes_nothing_else(self):
        state = feed(reaper.TransportState(), "/record", 1.0)
        after = feed(state, "/track/1/volume", 0.5)
        self.assertTrue(after.recording)
        self.assertIsNone(after.position)

    def test_a_bundle_applies_every_message(self):
        packet = osc.encode_bundle(
            [
                osc.encode_message("/record", 1.0),
                osc.encode_message("/time", 3.25),
            ]
        )
        state = reaper.apply_feedback(packet, reaper.TransportState(), now=1.0)
        self.assertTrue(state.recording)
        assert state.position is not None
        self.assertAlmostEqual(state.position, 3.25)

    def test_a_malformed_packet_leaves_the_state_alone(self):
        # Reaper is not the enemy, but a half-received datagram must not crash a
        # box that is mid-game.
        state = feed(reaper.TransportState(), "/record", 1.0, now=1.0)
        after = reaper.apply_feedback(b"\x01\x02not-osc", state, now=9.0)
        self.assertTrue(after.recording)
        self.assertEqual(after.last_packet, 1.0)

    def test_an_argumentless_message_is_ignored_safely(self):
        state = reaper.apply_feedback(osc.encode_message("/record"), reaper.TransportState(), now=1.0)
        self.assertIsNone(state.recording)

    def test_addresses_are_configurable(self):
        custom = reaper.AddressMap(recording="/rec_state")
        state = reaper.apply_feedback(
            osc.encode_message("/rec_state", 1.0),
            reaper.TransportState(),
            now=0.0,
            addresses=custom,
        )
        self.assertTrue(state.recording)


class TestCurrentPosition(unittest.TestCase):
    """Where the playhead is now, as distinct from where it was last reported.

    Any packet proves the link, but only `/time` says where the playhead is.
    This rig streams meter data continuously while parked, so a link that
    reads live says nothing about whether the last position is current (#35).
    """

    # 4.5 rather than a round-looking 4.8: OSC carries float32, which holds
    # 4.5 exactly.

    def test_a_position_that_keeps_arriving_is_current(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        self.assertEqual(state.current_position(100.5, timeout=2.0), 4.5)

    def test_a_position_that_stopped_arriving_is_not(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        self.assertIsNone(state.current_position(103.0, timeout=2.0))

    def test_other_feedback_does_not_keep_a_position_current(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        for now in (101.0, 102.0, 103.0):
            state = feed(state, "/track/1/vu", 0.5, now=now)
        self.assertEqual(state.liveness(103.0, timeout=2.0), reaper.Liveness.LIVE)
        self.assertIsNone(state.current_position(103.0, timeout=2.0))

    def test_nor_does_a_transport_report(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        state = feed(state, "/play", 0.0, now=102.5)
        self.assertIsNone(state.current_position(103.0, timeout=2.0))

    def test_nothing_reported_is_not_current(self):
        state = feed(reaper.TransportState(), "/track/1/vu", 0.5, now=100.0)
        self.assertIsNone(state.current_position(100.0))

    def test_an_unreadable_position_does_not_refresh_the_last_one(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        state = feed(state, "/time", "not a number", now=102.5)
        self.assertIsNone(state.current_position(103.0, timeout=2.0))

    def test_the_default_timeout_is_the_feedback_timeout(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        self.assertEqual(state.current_position(100.0 + reaper.DEFAULT_FEEDBACK_TIMEOUT), 4.5)


class TestRecordReports(unittest.TestCase):
    def test_every_record_report_is_counted(self):
        state = feed(reaper.TransportState(), "/record", 1.0)
        state = feed(state, "/record", 0.0)
        self.assertEqual(state.record_reports, 2)

    def test_other_traffic_is_not_a_record_report(self):
        state = feed(reaper.TransportState(), "/time", 3.0)
        state = feed(state, "/play", 1.0)
        self.assertEqual(state.record_reports, 0)


class TestRecordLatch(unittest.TestCase):
    """#28: a start the box sent and Reaper has not yet answered.

    `/record` is a toggle. Two taps that both go out before Reaper's first
    `/record 1` comes back are a start and a stop.
    """

    def pending(self, state, sent_at=10.0):
        return reaper.RecordRequest(sent_at=sent_at, reports_before=state.record_reports)

    def test_an_unanswered_start_refuses_another_on_a_silent_reaper(self):
        state = reaper.TransportState()
        refusal = reaper.record_refusal(state, 10.1, request=self.pending(state))
        self.assertIsNotNone(refusal)

    def test_an_unanswered_start_refuses_another_on_a_live_reaper_that_is_not_recording(self):
        # This rig after the greyed-button workaround: never silent, and it last
        # said it was not recording. That reading alone would allow a send.
        state = feed(reaper.TransportState(), "/record", 0.0, now=10.0)
        self.assertIsNone(reaper.record_refusal(state, 10.0))
        refusal = reaper.record_refusal(state, 10.1, request=self.pending(state))
        self.assertIsNotNone(refusal)

    def test_the_answer_releases_the_latch(self):
        state = reaper.TransportState()
        request = self.pending(state)
        state = feed(state, "/record", 1.0, now=10.2)
        refusal = reaper.record_refusal(state, 10.3, request=request)
        self.assertIn("already recording", refusal or "")

    def test_a_report_that_says_it_is_not_recording_also_answers(self):
        # An explicit answer either way means the state is known again.
        state = reaper.TransportState()
        request = self.pending(state)
        state = feed(state, "/record", 0.0, now=10.2)
        self.assertIsNone(reaper.record_refusal(state, 10.3, request=request))

    def test_a_report_from_before_the_send_does_not_answer_it(self):
        state = feed(reaper.TransportState(), "/record", 0.0, now=9.0)
        request = self.pending(state)
        self.assertIsNotNone(reaper.record_refusal(state, 9.5, request=request))

    def test_the_latch_never_times_out(self):
        # A timed release is what would let a lost confirmation turn the next
        # tap into a stop. Starting by hand in Reaper is the recoverable cost.
        state = reaper.TransportState()
        refusal = reaper.record_refusal(state, 10.0 + 3600.0, request=self.pending(state))
        self.assertIsNotNone(refusal)

    def test_the_unanswered_refusal_says_what_to_do(self):
        state = reaper.TransportState()
        refusal = reaper.record_refusal(state, 52.0, request=self.pending(state, sent_at=10.0))
        self.assertEqual(refusal, reaper.RECORD_REFUSED_UNANSWERED)
        self.assertIn("Check Reaper", refusal or "")

    def test_the_unanswered_refusal_does_not_change_while_it_waits(self):
        # No age in the text: it would churn the snapshot every second (#147).
        state = reaper.TransportState()
        request = self.pending(state)
        self.assertEqual(
            reaper.record_refusal(state, 10.1, request=request),
            reaper.record_refusal(state, 3610.0, request=request),
        )

    def test_the_latch_is_checked_before_presence(self):
        state = reaper.TransportState()
        refusal = reaper.record_refusal(state, 10.1, request=self.pending(state))
        self.assertEqual(refusal, reaper.RECORD_REFUSED_UNANSWERED)

    def test_no_request_means_no_latch(self):
        state = feed(reaper.TransportState(), METER, 0.0, now=10.0)
        self.assertIsNone(reaper.record_refusal(state, 10.0, request=None))


def meter_stream(state, start, stop, *, step=0.1):
    now = start
    while now <= stop + 1e-9:
        state = feed(state, METER, 0.0, now=now)
        now += step
    return state


class TestMotionIsTheClock(unittest.TestCase):
    """Motion is judged on `/time` alone; meters flow parked or rolling (#163)."""

    def test_meters_alone_with_record_state_unknown_allow_a_start(self):
        state = feed(reaper.TransportState(), METER, 0.0, now=100.0)
        self.assertIsNone(reaper.record_refusal(state, 100.5))

    def test_a_streaming_clock_with_record_state_unknown_refuses(self):
        state = feed(reaper.TransportState(), "/time", 3.0, now=100.0)
        state = feed(state, METER, 0.0, now=100.4)
        self.assertEqual(reaper.record_refusal(state, 100.5), reaper.RECORD_REFUSED_MOVING)

    def test_a_clock_that_stopped_while_meters_flow_allows_a_start(self):
        state = feed(reaper.TransportState(), "/time", 3.0, now=100.0)
        state = meter_stream(state, 101.0, 103.0, step=1.0)
        self.assertIsNone(reaper.record_refusal(state, 103.0))

    def test_clock_running_ignores_meters_and_transport_reports(self):
        state = feed(reaper.TransportState(), METER, 0.0, now=100.0)
        state = feed(state, "/play", 1.0, now=100.0)
        self.assertFalse(state.clock_running(100.5))
        state = feed(state, "/time", 1.0, now=100.4)
        self.assertTrue(state.clock_running(100.4 + reaper.DEFAULT_FEEDBACK_TIMEOUT))
        self.assertFalse(state.clock_running(100.4 + reaper.DEFAULT_FEEDBACK_TIMEOUT + 0.5))

    def test_current_position_and_clock_running_agree(self):
        state = feed(reaper.TransportState(), "/time", 4.5, now=100.0)
        for now in (100.0, 101.0, 102.0, 102.5, 200.0):
            self.assertEqual(
                state.clock_running(now),
                state.current_position(now) is not None,
            )


class TestPresence(unittest.TestCase):
    """Silence never permits a send: a `/record` into nothing is a false anchor."""

    def test_never_heard_from_refuses_as_silent(self):
        self.assertEqual(reaper.record_refusal(reaper.TransportState(), 100.0), reaper.RECORD_REFUSED_SILENT)

    def test_a_reaper_that_went_quiet_after_a_stop_refuses_as_silent(self):
        state = feed(reaper.TransportState(), "/record", 0.0, now=100.0)
        state = feed(state, "/play", 0.0, now=100.0)
        self.assertIs(state.liveness(103.0), reaper.Liveness.QUIET)
        self.assertEqual(reaper.record_refusal(state, 103.0), reaper.RECORD_REFUSED_SILENT)

    def test_a_reaper_that_quit_is_not_sent_a_start(self):
        state = feed(reaper.TransportState(), METER, 0.0, now=100.0)
        self.assertIsNone(reaper.record_refusal(state, 100.5))
        self.assertIsNotNone(reaper.record_refusal(state, 103.0))


#: The packets of a rolling Reaper on the bench, one tick's worth (#163).
ROLLING_MIX = (METER, "/track/1/vu", "/time", "/time/str", "/beat/str", "/samples", "/frames/str")
TICKS_PER_SECOND = 12
MIX_SECONDS = 5


def rolling_tick(address: str, tick: int) -> bytes:
    value: float | str = "0:00.0" if address.endswith("/str") else float(tick)
    return osc.encode_message(address, value)


class TestNeverSentBlind(unittest.TestCase):
    """What stands between a tap and stopping the game's take."""

    def test_record_is_never_permitted_while_reaper_may_be_rolling(self):
        """`/record` is a toggle: sent at a rolling Reaper it stops the take.

        Sweeps every combination of what Reaper may have said, building each
        state through `apply_feedback`, and asserts that whenever the box would
        send, it had positive evidence that it can only start.
        """
        end = 105.0
        checked = 0
        for record in (None, 1.0, 0.0):
            for play in (None, 1.0, 0.0):
                for clock in ("fresh", "never", "stale"):
                    for meters in (True, False):
                        for prior in (False, True):
                            for outstanding in (False, True):
                                state = reaper.TransportState()
                                if record is not None:
                                    state = feed(state, "/record", record, now=100.0)
                                if play is not None:
                                    state = feed(state, "/play", play, now=100.0)
                                if clock == "fresh":
                                    state = feed(state, "/time", 5.0, now=104.5)
                                elif clock == "stale":
                                    state = feed(state, "/time", 5.0, now=100.0)
                                if meters:
                                    state = meter_stream(state, 104.0, end, step=0.5)
                                request = (
                                    reaper.RecordRequest(sent_at=99.0, reports_before=state.record_reports)
                                    if outstanding
                                    else None
                                )
                                refusal = reaper.record_refusal(state, end, request=request, prior_recording=prior)
                                checked += 1
                                if refusal is not None:
                                    continue
                                label = (record, play, clock, meters, prior, outstanding)
                                self.assertIsNot(state.recording, True, label)
                                if state.recording is None:
                                    self.assertFalse(state.clock_running(end), label)
                                    self.assertFalse(prior, label)
                                self.assertIs(state.liveness(end), reaper.Liveness.LIVE, label)
                                self.assertIsNone(request, label)
        self.assertEqual(checked, 3 * 3 * 3 * 2 * 2 * 2)

    def test_a_box_started_mid_take_never_sends_record(self):
        # A fresh box, a Reaper already rolling: no `/record` or `/play` report
        # was ever announced to it, only the steady stream. One bundle per tick,
        # so the clock arrives with the meters it is sent beside.
        state = reaper.TransportState()
        for tick in range(TICKS_PER_SECOND * MIX_SECONDS):
            now = 100.0 + tick / TICKS_PER_SECOND
            packet = osc.encode_bundle([rolling_tick(address, tick) for address in ROLLING_MIX])
            state = reaper.apply_feedback(packet, state, now=now)
            self.assertIsNotNone(reaper.record_refusal(state, now), tick)

    def test_a_box_started_mid_take_with_no_clock_pattern_still_refuses_against_a_prior_recording(self):
        state = reaper.TransportState()
        mix = [address for address in ROLLING_MIX if address != "/time"]
        for tick in range(TICKS_PER_SECOND * MIX_SECONDS):
            now = 100.0 + tick / TICKS_PER_SECOND
            packet = osc.encode_bundle([rolling_tick(address, tick) for address in mix])
            state = reaper.apply_feedback(packet, state, now=now)
            self.assertEqual(
                reaper.record_refusal(state, now, prior_recording=True),
                reaper.RECORD_REFUSED_PRIOR,
                tick,
            )


class TestCommands(unittest.TestCase):
    def test_start_recording_sends_the_record_address(self):
        c, sender = client()
        c.start_recording()
        self.assertEqual(sender.addresses(), [reaper.DEFAULT_ADDRESSES.record])

    def test_play_sends_the_play_address(self):
        c, sender = client()
        c.play()
        self.assertEqual(sender.addresses(), [reaper.DEFAULT_ADDRESSES.play])

    def test_run_action_addresses_the_command_id(self):
        c, sender = client()
        c.run_action(40157)
        self.assertEqual(sender.addresses(), ["/action/40157"])

    def test_there_is_no_stop(self):
        # design.md 5.9: each home game is a single irreplaceable sample, and a
        # stop button does not belong on a screen tapped by someone watching a
        # field. Stopping is done deliberately, in Reaper.
        c, _ = client()
        for forbidden in ("stop", "stop_recording", "abort", "cancel_recording"):
            self.assertFalse(hasattr(c, forbidden), f"{forbidden} must not exist")

    def test_no_command_ever_emits_the_stop_address(self):
        c, sender = client()
        c.start_recording()
        c.play()
        c.pause()
        c.run_action(40157)
        self.assertNotIn(reaper.DEFAULT_ADDRESSES.stop, sender.addresses())

    def test_a_send_failure_is_raised_and_recorded(self):
        c, _ = client(sender=FailingSender())
        with self.assertRaises(TransportError):
            c.start_recording()
        self.assertFalse(c.healthy)
        self.assertIsNotNone(c.last_error)

    def test_starting_remembers_the_unanswered_request(self):
        c, _ = client(monotonic=lambda: 7.0)
        self.assertIsNone(c.record_request)
        c.start_recording()
        self.assertEqual(c.record_request, reaper.RecordRequest(sent_at=7.0, reports_before=0))

    def test_a_start_that_failed_to_send_is_not_awaiting_an_answer(self):
        # Nothing reached Reaper, so there is nothing a second tap could undo.
        c, _ = client(sender=FailingSender())
        with self.assertRaises(TransportError):
            c.start_recording()
        self.assertIsNone(c.record_request)

    def test_handling_feedback_updates_the_clients_state(self):
        c, _ = client(monotonic=lambda: 42.0)
        c.handle_packet(osc.encode_message("/record", 1.0))
        self.assertTrue(c.state.recording)
        self.assertEqual(c.state.last_packet, 42.0)


if __name__ == "__main__":
    unittest.main()
