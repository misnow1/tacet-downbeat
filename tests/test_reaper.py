import unittest

from tacet import osc, reaper
from tacet.net import TransportError


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

    def test_integer_arguments_are_accepted(self):
        # Not every controller sends floats.
        self.assertTrue(feed(reaper.TransportState(), "/record", 1).recording)

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

    def test_handling_feedback_updates_the_clients_state(self):
        c, _ = client(monotonic=lambda: 42.0)
        c.handle_packet(osc.encode_message("/record", 1.0))
        self.assertTrue(c.state.recording)
        self.assertEqual(c.state.last_packet, 42.0)


if __name__ == "__main__":
    unittest.main()
