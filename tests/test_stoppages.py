import unittest

from tacet import rtd, rtd_replay, stoppages
from tacet.rtd_replay import FieldEvent, GapEvent, SourceEvent
from tacet.stoppages import Doubt, Team

from . import rtd_frames as real

START = 1792261493.0
SOURCE = "192.168.0.30"
ORDINARY_HOLD = 14.5


def clock_text(seconds: int | None) -> str:
    return "        " if seconds is None else f"{seconds:>5}   "


def clock(time: float, old: int | None, new: int | None, *, first: bool = False) -> FieldEvent:
    before = None if first else clock_text(old)
    return FieldEvent(START + time, SOURCE, rtd.FieldChange(rtd.PLAY_CLOCK, before, clock_text(new)))


def left(field: rtd.Field, time: float, old: str | None, new: str) -> FieldEvent:
    return FieldEvent(START + time, SOURCE, rtd.FieldChange(field, old, new))


def scan_of(*events: rtd_replay.Event, **kwargs: float) -> stoppages.Scan:
    return stoppages.scan(events, **kwargs)


def countdown(first: float, count: int) -> list[FieldEvent]:
    """A run of ticks from 40, one a second, starting at `first`."""
    return [clock(first + i, 40 - i, 40 - i - 1) for i in range(count)]


class TestPlayClockText(unittest.TestCase):
    def test_the_test_text_matches_a_real_frame(self):
        self.assertEqual(clock_text(39), "   39   ")
        self.assertIn(clock_text(39).encode("ascii"), real.PLAY_CLOCK_39)

    def test_numbers_blanks_and_junk(self):
        self.assertEqual(stoppages.count_of("   39   "), 39)
        self.assertEqual(stoppages.count_of("    0   "), 0)
        self.assertIsNone(stoppages.count_of("        "))
        self.assertIsNone(stoppages.count_of("   --   "))
        self.assertIsNone(stoppages.count_of(None))


class TestTheHoldRule(unittest.TestCase):
    def test_ticks_only_is_no_stoppage(self):
        result = scan_of(*countdown(0, 30))
        self.assertEqual(result.stoppages, ())
        self.assertIsNone(result.open_hold_since)

    def test_set_to_25_held_then_a_tick(self):
        result = scan_of(clock(0, 40, 25), clock(30, 25, 24))
        (hold,) = result.stoppages
        self.assertEqual((hold.start, hold.end), (START, START + 30))
        self.assertTrue(hold.trusted)

    def test_exactly_the_minimum_qualifies_and_just_under_does_not(self):
        at = scan_of(clock(0, 40, 25), clock(stoppages.MIN_HOLD_SECONDS, 25, 24))
        self.assertEqual(len(at.stoppages), 1)
        under = scan_of(clock(0, 40, 25), clock(stoppages.MIN_HOLD_SECONDS - 0.001, 25, 24))
        self.assertEqual(under.stoppages, ())

    def test_a_reset_to_40_held_72_seconds_qualifies(self):
        result = scan_of(clock(0, 3, 40), clock(72, 40, 39))
        (hold,) = result.stoppages
        self.assertEqual(hold.held, 72)

    def test_40_then_25_with_no_tick_between_is_one_stoppage_from_the_40(self):
        result = scan_of(clock(0, 3, 40), clock(60, 40, 25), clock(120, 25, 24))
        (hold,) = result.stoppages
        self.assertEqual((hold.start, hold.end), (START, START + 120))

    def test_a_freeze_by_a_tick_starts_at_that_tick(self):
        result = scan_of(clock(0, 27, 26), clock(1, 26, 25), clock(41, 25, 24))
        (hold,) = result.stoppages
        self.assertEqual((hold.start, hold.end), (START + 1, START + 41))

    def test_a_play_clock_frozen_during_a_play_is_not_counted(self):
        # Ticks to 8, the play runs for 6 s, the board resets to 40 and holds 66 s.
        result = scan_of(clock(0, 9, 8), clock(6, 8, 40), clock(72, 40, 39))
        (hold,) = result.stoppages
        self.assertEqual((hold.start, hold.end), (START + 6, START + 72))

    def test_an_ordinary_reset_and_wait_is_no_stoppage(self):
        result = scan_of(clock(0, 9, 8), clock(8, 8, 40), clock(8 + ORDINARY_HOLD, 40, 39))
        self.assertEqual(result.stoppages, ())

    def test_a_mistaken_countdown_then_a_reset_starts_at_the_second_set(self):
        result = scan_of(clock(0, 40, 25), clock(5, 25, 24), clock(8, 24, 40), clock(48, 40, 39))
        (hold,) = result.stoppages
        self.assertEqual((hold.start, hold.end), (START + 8, START + 48))

    def test_a_blank_or_non_numeric_value_is_not_a_tick(self):
        blank = scan_of(clock(0, 25, None), clock(1, None, 25), clock(30, 25, 24))
        self.assertEqual(len(blank.stoppages), 1)
        junk = FieldEvent(START, SOURCE, rtd.FieldChange(rtd.PLAY_CLOCK, clock_text(25), "   --   "))
        self.assertEqual(len(scan_of(junk, clock(30, 25, 24)).stoppages), 1)

    def test_a_capture_that_ends_mid_hold_reports_when_it_began(self):
        end = left(rtd.TIME_OF_DAY, 100, " 2:00:00", " 2:00:01")
        result = scan_of(clock(0, 40, 25), end)
        self.assertEqual(result.stoppages, ())
        self.assertEqual(result.open_hold_since, START)

    def test_a_short_open_hold_is_not_reported(self):
        end = left(rtd.TIME_OF_DAY, 5, " 2:00:00", " 2:00:01")
        self.assertIsNone(scan_of(clock(0, 40, 25), end).open_hold_since)

    def test_fault_and_unmapped_events_are_ignored(self):
        noise: list[rtd_replay.Event] = [
            rtd_replay.FaultEvent(START + 1, SOURCE, rtd.FrameFault(rtd.Fault.CHECKSUM, b"x")),
            rtd_replay.UnmappedEvent(START + 2, SOURCE, (7,)),
        ]
        result = scan_of(clock(0, 40, 25), *noise, clock(30, 25, 24))
        self.assertEqual(len(result.stoppages), 1)


class TestDoubts(unittest.TestCase):
    def test_the_first_value_from_nothing_is_capture_start(self):
        result = scan_of(clock(0, None, 25, first=True), clock(30, 25, 24))
        (hold,) = result.stoppages
        self.assertEqual(hold.doubts, (Doubt.CAPTURE_START,))
        self.assertFalse(hold.trusted)

    def test_a_hold_with_no_reset_is_never_capture_start(self):
        result = scan_of(clock(0, 27, 26), clock(1, 26, 25), clock(41, 25, 24))
        self.assertEqual(result.stoppages[0].doubts, ())

    def test_a_gap_inside_the_hold_interrupts_it(self):
        gap = GapEvent(START + 20, START + 10)
        (hold,) = scan_of(clock(0, 40, 25), gap, clock(30, 25, 24)).stoppages
        self.assertEqual(hold.doubts, (Doubt.INTERRUPTED,))

    def test_a_gap_ending_exactly_at_the_start_interrupts_it(self):
        gap = GapEvent(START + 10, START)
        (hold,) = scan_of(gap, clock(10, 40, 25), clock(40, 25, 24)).stoppages
        self.assertEqual(hold.doubts, (Doubt.INTERRUPTED,))

    def test_a_gap_entirely_before_the_hold_does_not(self):
        gap = GapEvent(START + 9, START + 1)
        (hold,) = scan_of(gap, clock(10, 40, 25), clock(40, 25, 24)).stoppages
        self.assertEqual(hold.doubts, ())

    def test_a_source_change_inside_the_hold_is_flagged_but_the_first_source_is_not(self):
        first = SourceEvent(START, None, SOURCE)
        moved = SourceEvent(START + 15, SOURCE, "192.168.0.26")
        clean = scan_of(first, clock(0, 40, 25), clock(30, 25, 24))
        self.assertEqual(clean.stoppages[0].doubts, ())
        flagged = scan_of(first, clock(0, 40, 25), moved, clock(30, 25, 24))
        self.assertEqual(flagged.stoppages[0].doubts, (Doubt.SOURCE_CHANGED,))

    def test_a_hold_longer_than_the_limit_is_too_long(self):
        long = stoppages.LONGEST_STOPPAGE_SECONDS + 1
        (hold,) = scan_of(clock(0, 40, 25), clock(long, 25, 24)).stoppages
        self.assertEqual(hold.doubts, (Doubt.TOO_LONG,))

    def test_doubts_are_sorted_and_all_listed(self):
        long = stoppages.LONGEST_STOPPAGE_SECONDS + 1
        events: list[rtd_replay.Event] = [
            clock(0, None, 25, first=True),
            GapEvent(START + 20, START + 10),
            clock(long, 25, 24),
        ]
        (hold,) = scan_of(*events).stoppages
        self.assertEqual(hold.doubts, (Doubt.CAPTURE_START, Doubt.INTERRUPTED, Doubt.TOO_LONG))


class TestTeamTimeouts(unittest.TestCase):
    def test_a_guest_decrease_is_attached_to_the_hold(self):
        mark = left(rtd.GUEST_TIMEOUTS, 10, "3", "2")
        result = scan_of(clock(0, 40, 25), mark, clock(30, 25, 24))
        self.assertEqual(result.stoppages[0].marks, (stoppages.TeamTimeout(START + 10, Team.GUEST),))
        self.assertEqual(result.team_timeouts, result.stoppages[0].marks)

    def test_a_home_decrease_is_home(self):
        mark = left(rtd.HOME_TIMEOUTS, 10, "2", "1")
        result = scan_of(clock(0, 40, 25), mark, clock(30, 25, 24))
        self.assertEqual(result.stoppages[0].marks[0].team, Team.HOME)

    def test_an_increase_the_first_value_and_the_copies_are_ignored(self):
        events = [
            left(rtd.HOME_TIMEOUTS, 1, "2", "3"),
            left(rtd.GUEST_TIMEOUTS, 2, None, "3"),
            left(rtd.HOME_TIMEOUTS_COPY, 3, "3", "2"),
            left(rtd.GUEST_TIMEOUTS_COPY, 4, "3", "2"),
        ]
        self.assertEqual(scan_of(*events).team_timeouts, ())

    def test_a_mark_outside_every_hold_is_listed_but_attached_to_none(self):
        mark = left(rtd.GUEST_TIMEOUTS, 100, "3", "2")
        result = scan_of(clock(0, 40, 25), clock(30, 25, 24), mark)
        self.assertEqual(result.stoppages[0].marks, ())
        self.assertEqual(len(result.team_timeouts), 1)


class TestConstants(unittest.TestCase):
    def test_the_minimum_sits_between_an_ordinary_hold_and_the_shortest_timeout(self):
        self.assertLess(ORDINARY_HOLD, stoppages.MIN_HOLD_SECONDS)
        self.assertLess(stoppages.MIN_HOLD_SECONDS, 40)

    def test_the_limit_sits_between_the_longest_timeout_and_halftime(self):
        self.assertLess(241, stoppages.LONGEST_STOPPAGE_SECONDS)
        self.assertLess(stoppages.LONGEST_STOPPAGE_SECONDS, 1235)

    def test_the_play_clock_counts_down_by_one(self):
        self.assertEqual(stoppages.COUNTDOWN_STEP, 1)


if __name__ == "__main__":
    unittest.main()
