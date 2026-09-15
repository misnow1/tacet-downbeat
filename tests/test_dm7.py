import asyncio
import unittest
from typing import Any, cast

from tacet import dm7, osc
from tacet.net import TransportError


class FakeSender:
    """Records packets instead of opening a socket."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)

    def messages(self) -> list[osc.Message]:
        decoded = [osc.decode_packet(p) for p in self.packets]
        assert all(isinstance(m, osc.Message) for m in decoded)
        return cast("list[osc.Message]", decoded)

    def levels(self) -> list[int]:
        return [cast("int", m.args[0]) for m in self.messages()]


class FailingSender:
    def send(self, packet: bytes) -> None:
        raise TransportError("network unreachable")


class NotAnOsErrorSender:
    """Fails the way `sendto` does for an out-of-range port."""

    def send(self, packet: bytes) -> None:
        raise OverflowError("sendto(): port must be 0-65535.")


def client(**kwargs: Any) -> tuple[dm7.Dm7Client, FakeSender]:
    sender: FakeSender = kwargs.setdefault("sender", FakeSender())
    kwargs.setdefault("tick_hz", 100.0)
    return dm7.Dm7Client(UNREACHABLE_HOST, dca=3, **kwargs), sender


#: Asserted as a literal on purpose. These strings are the wire format; if a
#: refactor of the address constants changes them, that is a breaking change and
#: these tests are the alarm.
DCA_3_LEVEL_ADDRESS = "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3"
UNREACHABLE_HOST = "192.0.2.1"  # TEST-NET-1, guaranteed not to route


class TestLevelHelpers(unittest.TestCase):
    def test_clamp_holds_the_documented_range(self):
        self.assertEqual(dm7.clamp(-99999), dm7.LEVEL_MIN)
        self.assertEqual(dm7.clamp(99999), dm7.LEVEL_MAX)
        self.assertEqual(dm7.clamp(-2000), -2000)

    def test_to_db_uses_hundredths(self):
        self.assertEqual(dm7.to_db(0), 0.0)
        self.assertEqual(dm7.to_db(-2000), -20.0)
        self.assertEqual(dm7.to_db(1000), 10.0)
        self.assertEqual(dm7.to_db(dm7.MINUS_INF), float("-inf"))

    def test_derived_constants_match_the_spec_values(self):
        # These are computed from UNITS_PER_DB now, so pin the numbers the spec
        # actually prints: min -32768, max 1000, scaling 100.
        self.assertEqual(dm7.UNITS_PER_DB, 100)
        self.assertEqual(dm7.LEVEL_MIN, -32768)
        self.assertEqual(dm7.LEVEL_MAX, 1000)
        self.assertEqual(dm7.DEFAULT_FADE_FLOOR, -6000)
        self.assertEqual(dm7.MINUS_INF, -32768)
        self.assertEqual(dm7.UNITY, 0)

    def test_table_1_is_the_43_values_from_the_spec(self):
        self.assertEqual(len(dm7.TABLE_1), 43)
        self.assertEqual(dm7.TABLE_1[0], dm7.MINUS_INF)
        self.assertEqual(dm7.TABLE_1[-1], 1000)
        self.assertEqual(sorted(dm7.TABLE_1), list(dm7.TABLE_1))
        self.assertIn(0, dm7.TABLE_1)

    def test_quantize_snaps_to_the_nearest_table_1_value(self):
        # The open question in design.md §7: -1550 either lands or snaps to -16.
        self.assertEqual(dm7.quantize(-1550), -1600)
        self.assertEqual(dm7.quantize(-1520), -1600)
        self.assertEqual(dm7.quantize(-1480), -1400)
        self.assertEqual(dm7.quantize(0), 0)

    def test_fader_address_targets_level_and_never_on(self):
        address = dm7.fader_address(7)
        self.assertEqual(address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/7")
        self.assertNotIn("/On", address)


class TestRampSteps(unittest.TestCase):
    def steps(self, *args: Any, **kwargs: Any) -> list[tuple[float, int]]:
        return list(dm7.ramp_steps(*args, **kwargs))

    def test_reaches_the_target(self):
        steps = self.steps(dm7.MINUS_INF, 0, 0.1, tick_hz=100.0)
        self.assertEqual(steps[-1][1], 0)

    def test_offsets_are_monotonic_and_bounded_by_the_duration(self):
        steps = self.steps(0, -3000, 2.0, tick_hz=50.0)
        offsets = [t for t, _ in steps]
        self.assertEqual(offsets, sorted(offsets))
        self.assertLessEqual(offsets[-1], 2.0 + 1e-9)
        self.assertGreater(offsets[0], 0.0)

    def test_a_close_ends_at_minus_infinity(self):
        steps = self.steps(0, dm7.MINUS_INF, 2.0, tick_hz=50.0)
        self.assertEqual(steps[-1][1], dm7.MINUS_INF)

    def test_a_close_ramps_through_the_floor_rather_than_jumping(self):
        # -32768 is not a dB value; interpolating toward it would collapse the
        # whole fade into the first tick.
        levels = [level for _, level in self.steps(0, dm7.MINUS_INF, 2.0, tick_hz=50.0)]
        self.assertGreater(len(levels), 10)
        self.assertIn(dm7.DEFAULT_FADE_FLOOR, levels)
        descending = levels[:-1]
        self.assertEqual(descending, sorted(descending, reverse=True))

    def test_a_close_from_below_the_floor_goes_straight_to_silence(self):
        steps = self.steps(-9000, dm7.MINUS_INF, 2.0, tick_hz=50.0)
        self.assertEqual([level for _, level in steps], [dm7.MINUS_INF])

    def test_repeated_levels_are_dropped(self):
        # A 50 Hz ramp across 1 dB would otherwise send the same value many times.
        levels = [level for _, level in self.steps(0, -100, 2.0, tick_hz=50.0)]
        self.assertEqual(len(levels), len(set(levels)))

    def test_quantized_ramps_only_emit_table_1_values(self):
        steps = self.steps(0, dm7.MINUS_INF, 2.0, tick_hz=50.0, quantized=True)
        for _, level in steps:
            self.assertIn(level, dm7.TABLE_1)

    def test_a_zero_length_move_still_reaches_the_target(self):
        self.assertEqual(self.steps(0, -2000, 0.0)[-1][1], -2000)

    def test_targets_outside_the_range_are_clamped(self):
        self.assertEqual(self.steps(0, 99999, 0.1)[-1][1], dm7.LEVEL_MAX)


def crossing(steps, level):
    """When a rising ramp first reaches `level`."""
    return next(offset for offset, emitted in steps if emitted >= level)


class TestTheRideInTaper(unittest.TestCase):
    """#8: fast through the bottom, slow through the top.

    Linear in dB spent half a ride-in below -30 dB, inaudible under a crowd,
    and rushed the audible top. The shape is pinned here, not the levels: the
    knee's constants are a guess to be refitted against game 3's post-DCA
    reference channel, and refitting them must not rewrite these tests.
    """

    TAPER = dm7.RIDE_IN_TAPER
    DURATION = 1.5
    HZ = 50.0

    def ride(self, start=dm7.MINUS_INF, target=dm7.UNITY, **kwargs):
        kwargs.setdefault("tick_hz", self.HZ)
        return list(dm7.ramp_steps(start, target, self.DURATION, taper=self.TAPER, **kwargs))

    def linear(self, start=dm7.MINUS_INF, target=dm7.UNITY):
        return list(dm7.ramp_steps(start, target, self.DURATION, tick_hz=self.HZ))

    def test_the_knee_is_below_the_open_level_and_early_in_the_ride(self):
        self.assertLess(dm7.DEFAULT_FADE_FLOOR, self.TAPER.knee_level)
        self.assertLess(self.TAPER.knee_level, dm7.UNITY)
        self.assertGreater(self.TAPER.knee_fraction, 0.0)
        self.assertLess(self.TAPER.knee_fraction, 0.5)

    def test_it_rises_steadily_to_the_target_within_the_duration(self):
        steps = self.ride()
        offsets = [offset for offset, _ in steps]
        levels = [level for _, level in steps]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(len(levels), len(set(levels)))
        self.assertEqual(levels[-1], dm7.UNITY)
        self.assertLessEqual(offsets[-1], self.DURATION + 1e-9)

    def test_it_reaches_the_knee_by_the_knee_fraction(self):
        tick = 1 / self.HZ
        knee_at = crossing(self.ride(), self.TAPER.knee_level)
        self.assertLessEqual(knee_at, self.TAPER.knee_fraction * self.DURATION + tick)

    def test_most_of_the_ride_is_spent_above_the_knee(self):
        knee_at = crossing(self.ride(), self.TAPER.knee_level)
        self.assertLess(knee_at, self.DURATION - knee_at)

    def test_it_gets_through_the_bottom_sooner_than_a_linear_ride(self):
        self.assertLess(
            crossing(self.ride(), self.TAPER.knee_level),
            crossing(self.linear(), self.TAPER.knee_level),
        )

    def test_it_moves_faster_below_the_knee_than_above_it(self):
        steps = self.ride()
        knee_at = crossing(steps, self.TAPER.knee_level)
        first_level = steps[0][1]
        below = (self.TAPER.knee_level - first_level) / knee_at
        above = (dm7.UNITY - self.TAPER.knee_level) / (self.DURATION - knee_at)
        self.assertGreater(below, above)

    def test_a_ride_that_starts_above_the_knee_is_linear(self):
        # A retry after a ride-in failed near the top has no bottom to rush.
        start = self.TAPER.knee_level + 500
        self.assertEqual(self.ride(start=start), self.linear(start=start))

    def test_a_ride_that_ends_below_the_knee_is_linear(self):
        target = self.TAPER.knee_level - 500
        self.assertEqual(self.ride(target=target), self.linear(target=target))

    def test_a_taper_never_shapes_a_close(self):
        # Closes are unchanged (#8): the 2 s fade keeps its shape.
        down = list(dm7.ramp_steps(dm7.UNITY, dm7.MINUS_INF, 2.0, tick_hz=self.HZ, taper=self.TAPER))
        self.assertEqual(down, list(dm7.ramp_steps(dm7.UNITY, dm7.MINUS_INF, 2.0, tick_hz=self.HZ)))

    def test_a_quantized_ride_in_only_emits_table_1_values(self):
        for _, level in self.ride(quantized=True):
            self.assertIn(level, dm7.TABLE_1)

    def test_a_knee_outside_the_ride_is_refused(self):
        for fraction in (0.0, 1.0, -0.1, 1.5):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                dm7.Taper(knee_level=self.TAPER.knee_level, knee_fraction=fraction)


class TestSending(unittest.TestCase):
    def test_send_level_emits_one_int_argument_at_the_fader_address(self):
        c, sender = client()
        c.send_level(-2000)
        message = sender.messages()[0]
        self.assertEqual(message.address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3")
        self.assertEqual(message.args, (-2000,))
        self.assertIn(b",i\x00\x00", sender.packets[0])

    def test_commanded_level_tracks_what_was_sent(self):
        c, _ = client()
        self.assertEqual(c.commanded_level, dm7.MINUS_INF)
        c.send_level(-500)
        self.assertEqual(c.commanded_level, -500)
        self.assertEqual(c.commanded_db, -5.0)

    def test_out_of_range_values_are_clamped_before_sending(self):
        c, sender = client()
        self.assertEqual(c.send_level(50000), dm7.LEVEL_MAX)
        self.assertEqual(sender.levels(), [dm7.LEVEL_MAX])

    def test_a_send_failure_is_raised_and_recorded(self):
        c = dm7.Dm7Client(UNREACHABLE_HOST, sender=FailingSender())
        with self.assertRaises(TransportError):
            c.send_level(0)
        self.assertFalse(c.healthy)
        assert c.last_error is not None
        self.assertIn("network unreachable", c.last_error)

    def test_any_sender_error_is_raised_as_a_transport_error(self):
        # The Sender contract says TransportError, but a fade runs in a task
        # that catches only that. Anything else would kill it silently (#72).
        c = dm7.Dm7Client(UNREACHABLE_HOST, sender=NotAnOsErrorSender())
        with self.assertRaises(TransportError) as caught:
            c.send_level(0)
        self.assertIsInstance(caught.exception.__cause__, OverflowError)
        self.assertFalse(c.healthy)
        assert c.last_error is not None
        self.assertIn("port must be 0-65535", c.last_error)
        self.assertEqual(c.sent_count, 0)
        self.assertEqual(c.commanded_level, dm7.MINUS_INF)

    def test_recovery_clears_the_fault(self):
        c, _ = client()
        c.last_error = "stale"
        c.send_level(0)
        self.assertTrue(c.healthy)


class TestMoves(unittest.IsolatedAsyncioTestCase):
    async def test_open_ends_at_unity(self):
        c, sender = client()
        await c.open(seconds=0.01)
        self.assertEqual(c.commanded_level, dm7.UNITY)
        self.assertEqual(sender.levels()[-1], dm7.UNITY)

    async def test_fade_out_ends_at_minus_infinity(self):
        c, sender = client(initial_level=dm7.UNITY)
        await c.fade_out(seconds=0.05)
        self.assertEqual(c.commanded_level, dm7.MINUS_INF)
        self.assertEqual(sender.levels()[-1], dm7.MINUS_INF)

    async def test_a_fade_descends_rather_than_stepping_once(self):
        c, sender = client(initial_level=dm7.UNITY)
        await c.fade_out(seconds=0.1)
        self.assertGreater(len(sender.levels()), 5)

    async def test_open_during_a_fade_snaps_back(self):
        # design.md §6.3: any qualifying trigger returns to OPEN mid-release.
        c, sender = client(initial_level=dm7.UNITY)
        fade = asyncio.ensure_future(c.fade_out(seconds=5.0))
        await asyncio.sleep(0.05)
        self.assertTrue(c.is_ramping)
        await c.open(seconds=0.01)
        await fade
        self.assertEqual(c.commanded_level, dm7.UNITY)
        self.assertGreater(sender.levels()[-1], sender.levels()[1])
        self.assertNotIn(dm7.MINUS_INF, sender.levels()[1:])

    async def test_a_ride_in_sends_the_tapered_curve(self):
        c, sender = client()
        await c.ride_in(seconds=0.2)
        expected = dm7.ramp_steps(dm7.MINUS_INF, dm7.UNITY, 0.2, tick_hz=100.0, taper=dm7.RIDE_IN_TAPER)
        self.assertEqual(sender.levels(), [level for _, level in expected])
        self.assertEqual(c.commanded_level, dm7.UNITY)

    async def test_an_open_over_the_same_time_is_not_tapered(self):
        c, sender = client()
        await c.open(seconds=0.2)
        expected = dm7.ramp_steps(dm7.MINUS_INF, dm7.UNITY, 0.2, tick_hz=100.0)
        self.assertEqual(sender.levels(), [level for _, level in expected])

    async def test_every_packet_of_a_full_cycle_is_a_fader_level_write(self):
        # The hard constraint, asserted directly: faders only, never mutes.
        c, sender = client(initial_level=dm7.MINUS_INF)
        await c.open(seconds=0.01)
        await c.fade_out(seconds=0.05)
        await c.ride_in(seconds=0.05)
        await c.fade_out(seconds=0.05)
        self.assertGreater(len(sender.packets), 3)
        for message in sender.messages():
            self.assertEqual(message.address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3")
            self.assertEqual(len(message.args), 1)
            self.assertIsInstance(message.args[0], int)

    async def test_a_move_superseded_by_another_returns_quietly(self):
        c, _ = client(initial_level=dm7.UNITY)
        fade = asyncio.ensure_future(c.fade_out(seconds=5.0))
        await asyncio.sleep(0.05)
        await c.open(seconds=0.01)
        await fade  # does not raise

    async def test_cancelling_the_caller_is_not_swallowed(self):
        # Only the ramp's own cancellation is a newer move superseding it. A
        # cancel aimed at whoever awaited the move has to reach them, or a
        # cancelled fade carries on as though it had finished (#34).
        c, _ = client(initial_level=dm7.UNITY)
        fade = asyncio.ensure_future(c.fade_out(seconds=5.0))
        await asyncio.sleep(0.05)
        fade.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await fade
        self.assertFalse(c.is_ramping)

    async def test_cancel_ramp_leaves_the_last_commanded_value_in_place(self):
        c, _ = client(initial_level=dm7.UNITY)
        fade = asyncio.ensure_future(c.fade_out(seconds=5.0))
        await asyncio.sleep(0.05)
        c.cancel_ramp()
        await fade
        self.assertFalse(c.is_ramping)
        self.assertLess(c.commanded_level, dm7.UNITY)
        self.assertGreater(c.commanded_level, dm7.MINUS_INF)


if __name__ == "__main__":
    unittest.main()
