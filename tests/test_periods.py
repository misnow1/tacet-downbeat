import unittest
from datetime import UTC, datetime
from pathlib import Path

from tacet import periods, rtd, rtd_replay, stoppages
from tacet.periods import AT_ZERO, REWRITE_SECONDS, Doubt, Edge, Period
from tacet.rtd_replay import FieldEvent, GapEvent, SourceEvent

from . import rtd_frames as real

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "tacet"
START = 1792261493.0
SOURCE = "192.168.0.30"
BLANK = " "


def utc(hour: int, minute: int, second: float) -> float:
    whole = int(second)
    return datetime(2026, 10, 17, hour, minute, whole, round((second - whole) * 1_000_000), tzinfo=UTC).timestamp()


def at(seconds: float) -> float:
    return START + seconds


def z(time: float, old: str | None, new: str) -> FieldEvent:
    return FieldEvent(time, SOURCE, rtd.FieldChange(rtd.CLOCK_AT_ZERO, old, new))


def rise(time: float) -> FieldEvent:
    return z(time, BLANK, AT_ZERO)


def fall(time: float) -> FieldEvent:
    return z(time, AT_ZERO, BLANK)


def quarter(
    time: float, number: str | None, text: str | None, *, old: tuple[str | None, str | None] = (None, None)
) -> list[FieldEvent]:
    """The quarter field, then its text, in one datagram: two events at one time."""
    qold, told = old
    return [
        FieldEvent(time, SOURCE, rtd.FieldChange(rtd.QUARTER, qold, number or "")),
        FieldEvent(time, SOURCE, rtd.FieldChange(rtd.QUARTER_TEXT, told, text or "")),
    ]


def shown(time: float, which: str) -> list[FieldEvent]:
    """The quarter and its text for one of the Game 4 labels, as one datagram."""
    number, text = SHOWN[which]
    return quarter(time, number, text)


SHOWN = {
    "pre": ("  ", "PRE "),
    "q1": (" 1", "1ST "),
    "q2": (" 2", "2ND "),
    "half": ("  ", "HALF"),
    "q3": (" 3", "3RD "),
    "q4": (" 4", "4TH "),
    "ot": (" 5", "OT  "),
    "final": ("  ", "FINL"),
}


def gap(since: float, resume: float) -> GapEvent:
    return GapEvent(resume, since)


def source(time: float, old: str | None, new: str) -> SourceEvent:
    return SourceEvent(time, old, new)


def board_of(*items: object) -> periods.Board:
    events: list[rtd_replay.Event] = []
    for item in items:
        if isinstance(item, list):
            events += item
        else:
            events.append(item)  # type: ignore[arg-type]
    return periods.scan(events)


def by_period(board: periods.Board) -> dict[Period, periods.PeriodSpan]:
    return {s.period: s for s in board.spans}


# Section 8's rows, transcribed: UTC wall times from the Game 4 capture. The tests never read the pcap.
PREGAME_ZERO = utc(19, 35, 0.347213)
GAME_4_RISES = (
    PREGAME_ZERO,
    utc(20, 7, 58.290233),
    utc(20, 53, 33.292567),
    utc(21, 13, 45.098357),
    utc(21, 54, 44.941560),
    utc(22, 40, 49.078274),
)


def game_4_events() -> list[rtd_replay.Event]:
    pre, q1, q2, half, q3, end = GAME_4_RISES
    events: list[rtd_replay.Event] = [
        source(utc(18, 24, 53.523224), None, SOURCE),
        z(utc(18, 24, 53.523224), None, BLANK),
        *shown(utc(18, 24, 53.523224), "pre"),
        rise(pre),
        fall(utc(19, 35, 7.179347)),
        *shown(utc(19, 35, 9.687834), "q1"),
        rise(q1),
        fall(utc(20, 8, 8.148117)),
        *shown(utc(20, 8, 14.576001), "q2"),
        rise(q2),
        fall(utc(20, 53, 42.845283)),
        *shown(utc(20, 53, 46.061900), "half"),
        rise(half),
        fall(utc(21, 13, 52.133881)),
        *shown(utc(21, 13, 54.544695), "q3"),
        rise(q3),
        fall(utc(21, 54, 53.589006)),
        *shown(utc(21, 54, 56.701260), "q4"),
        rise(end),
    ]
    outages: list[rtd_replay.Event] = [
        gap(utc(22, 5, 41.079215), utc(22, 6, 34.549093)),
        source(utc(22, 6, 34.549093), SOURCE, "192.168.0.26"),
        gap(utc(22, 19, 28.036600), utc(22, 24, 40.528728)),
        source(utc(22, 24, 40.528728), "192.168.0.26", "192.168.0.31"),
        gap(utc(22, 24, 51.198160), utc(22, 26, 13.986841)),
    ]
    return events + outages


class TestGame4(unittest.TestCase):
    def test_game_4_gives_the_five_spans_in_the_issue(self):
        board = periods.scan(game_4_events())
        spans = by_period(board)
        self.assertEqual(
            [s.period for s in board.spans],
            [Period.PREGAME, Period.Q1, Period.Q2, Period.HALFTIME, Period.Q3, Period.Q4],
        )
        self.assertEqual(board.zeros, GAME_4_RISES)
        self.assertIsNone(spans[Period.PREGAME].start)
        order = [Period.Q1, Period.Q2, Period.HALFTIME, Period.Q3, Period.Q4]
        for number, period in enumerate(order):
            span = spans[period]
            self.assertTrue(span.trusted, period)
            assert span.start is not None and span.end is not None
            self.assertAlmostEqual(span.start.time, GAME_4_RISES[number], places=5)
            self.assertAlmostEqual(span.end.time, GAME_4_RISES[number + 1], places=5)
            self.assertEqual((span.start.edge, span.end.edge), (Edge.CLOCK_AT_ZERO, Edge.CLOCK_AT_ZERO))

    def test_game_4s_outages_cast_no_doubt(self):
        for span in periods.scan(game_4_events()).spans:
            self.assertEqual(span.doubts, (), span.period)

    def test_the_board_text_is_what_the_board_showed(self):
        spans = by_period(periods.scan(game_4_events()))
        self.assertEqual(spans[Period.PREGAME].shown, "  PRE ")
        self.assertEqual(spans[Period.Q1].shown, " 11ST ")
        self.assertEqual(spans[Period.HALFTIME].shown, "  HALF")
        self.assertAlmostEqual(spans[Period.Q1].shown_from, utc(19, 35, 9.687834), places=5)
        self.assertAlmostEqual(spans[Period.Q1].shown_to or 0, utc(20, 8, 14.576001), places=5)
        self.assertIsNone(spans[Period.Q4].shown_to)


class TestWhatIsRead(unittest.TestCase):
    def test_it_reads_only_the_flag_and_the_quarter_fields(self):
        self.assertEqual(periods.WATCHED, (rtd.CLOCK_AT_ZERO, rtd.QUARTER, rtd.QUARTER_TEXT))

    def test_the_main_clock_is_never_read(self):
        slip = [
            FieldEvent(at(5), SOURCE, rtd.FieldChange(rtd.MAIN_CLOCK, "20:00", " 0:00")),
            FieldEvent(at(6), SOURCE, rtd.FieldChange(rtd.MAIN_CLOCK, " 0:00", "20:00")),
        ]
        plain = [*shown(at(0), "q1"), rise(at(100)), *shown(at(110), "q2")]
        self.assertEqual(board_of(plain, slip), board_of(plain))
        for name in ("periods.py", "quarters.py"):
            self.assertNotIn("MAIN_CLOCK", (PACKAGE_ROOT / name).read_text(encoding="utf-8"), name)

    def test_real_frames_read_as_expected(self):
        display = rtd.Display()
        for raw in (real.PREGAME_FULL, real.QUARTER_1, real.CLOCK_AT_ZERO):
            for parsed in rtd.parse_datagram(raw):
                assert isinstance(parsed, rtd.Frame)
                display.apply(parsed)
        self.assertEqual(display.value(rtd.CLOCK_AT_ZERO), AT_ZERO)
        self.assertIs(periods.period_of(display.value(rtd.QUARTER), display.value(rtd.QUARTER_TEXT)), Period.Q1)
        pre = rtd.Display()
        for parsed in rtd.parse_datagram(real.PREGAME_FULL):
            assert isinstance(parsed, rtd.Frame)
            pre.apply(parsed)
        self.assertIs(periods.period_of(pre.value(rtd.QUARTER), pre.value(rtd.QUARTER_TEXT)), Period.PREGAME)


class TestPeriodOf(unittest.TestCase):
    def test_quarters_one_to_four(self):
        for number, expected in ((1, Period.Q1), (2, Period.Q2), (3, Period.Q3), (4, Period.Q4)):
            self.assertIs(periods.period_of(f" {number}", "x"), expected)

    def test_any_other_number_is_other(self):
        self.assertIs(periods.period_of(" 5", "OT  "), Period.OTHER)

    def test_a_blank_quarter_reads_its_text(self):
        self.assertIs(periods.period_of("  ", "PRE "), Period.PREGAME)
        self.assertIs(periods.period_of("  ", "HALF"), Period.HALFTIME)
        self.assertIs(periods.period_of("  ", "FINL"), Period.OTHER)

    def test_junk_in_the_quarter_is_other(self):
        self.assertIs(periods.period_of(" X", "PRE "), Period.OTHER)

    def test_unwritten_is_none(self):
        self.assertIsNone(periods.period_of(None, "PRE "))
        self.assertIsNone(periods.period_of(" 1", None))
        self.assertIsNone(periods.period_of(None, None))


class TestRises(unittest.TestCase):
    def test_a_rise_from_never_written_is_not_a_rise(self):
        self.assertFalse(periods.is_rise(None, AT_ZERO))
        self.assertTrue(periods.is_rise(BLANK, AT_ZERO))
        self.assertFalse(periods.is_rise(AT_ZERO, AT_ZERO))
        self.assertFalse(periods.is_rise(AT_ZERO, BLANK))
        board = board_of(*shown(at(0), "q1"), z(at(0), None, AT_ZERO), *shown(at(50), "q2"))
        self.assertEqual(board.zeros, ())
        self.assertEqual(board.spans[0].end, periods.Boundary(at(50), Edge.QUARTER_FIELD))


class TestBoundaries(unittest.TestCase):
    def test_changes_in_one_datagram_settle_together(self):
        # Quarter first, text second, one datagram: no showing of a blank quarter with "2ND".
        board = board_of(*shown(at(0), "q2"), rise(at(100)), *shown(at(110), "half"))
        self.assertEqual([s.period for s in board.spans], [Period.Q2, Period.HALFTIME])
        self.assertNotIn(Period.OTHER, [s.period for s in board.spans])

    def test_a_rise_in_the_same_datagram_as_the_quarter_change_ends_the_old_period(self):
        board = board_of(*shown(at(0), "q1"), rise(at(110)), *shown(at(110), "q2"))
        q1, q2 = board.spans
        self.assertEqual(q1.zeros, (at(110),))
        self.assertEqual(q2.zeros, ())
        assert q1.end is not None
        self.assertEqual((q1.end.time, q1.end.edge), (at(110), Edge.CLOCK_AT_ZERO))

    def test_a_flicker_uses_the_last_rise_and_counts_both(self):
        board = board_of(*shown(at(0), "q1"), rise(at(100)), fall(at(101)), rise(at(105)), *shown(at(110), "q2"))
        q1 = board.spans[0]
        assert q1.end is not None
        self.assertEqual(q1.end.time, at(105))
        self.assertEqual(q1.zeros, (at(100), at(105)))

    def test_without_a_rise_the_quarter_field_is_the_boundary(self):
        board = board_of(*shown(at(0), "q1"), *shown(at(110), "q2"))
        q1, q2 = board.spans
        assert q1.end is not None and q2.start is not None
        self.assertEqual((q1.end.time, q1.end.edge), (at(110), Edge.QUARTER_FIELD))
        self.assertEqual(q2.start, q1.end)

    def test_the_capture_starting_mid_game_gives_no_start(self):
        board = board_of(*shown(at(0), "q2"), rise(at(100)), *shown(at(110), "half"))
        self.assertIsNone(board.spans[0].start)
        self.assertFalse(board.spans[0].trusted)

    def test_the_last_period_without_a_rise_has_no_end(self):
        board = board_of(*shown(at(0), "q3"), rise(at(100)), *shown(at(110), "q4"))
        self.assertIsNone(board.spans[-1].end)
        self.assertFalse(board.spans[-1].trusted)

    def test_a_period_is_not_split_by_a_rewrite_of_the_same_values(self):
        board = board_of(*shown(at(0), "q1"), *shown(at(20), "q1"), rise(at(100)), *shown(at(110), "q2"))
        self.assertEqual(len(board.spans), 2)


class TestDoubts(unittest.TestCase):
    def two(self, *extra: object) -> periods.PeriodSpan:
        board = board_of(*shown(at(0), "q1"), rise(at(1000)), *shown(at(1010), "q2"), *extra)
        return board.spans[0]

    def test_a_gap_just_before_a_rise_is_interrupted_and_one_a_rewrite_earlier_is_not(self):
        near = self.two(gap(at(980), at(1000 - 1)))
        self.assertEqual(near.doubts, (Doubt.INTERRUPTED,))
        before = 1000 - REWRITE_SECONDS - 1
        far = self.two(gap(at(before - 30), at(before)))
        self.assertEqual(far.doubts, ())

    def test_a_source_change_just_before_a_rise_is_source_changed_and_the_first_source_is_not(self):
        self.assertEqual(self.two(source(at(995), SOURCE, "192.168.0.26")).doubts, (Doubt.SOURCE_CHANGED,))
        self.assertEqual(self.two(source(at(995), None, SOURCE)).doubts, ())

    def test_a_gap_between_the_rise_and_the_quarter_change_is_interrupted(self):
        self.assertEqual(self.two(gap(at(1002), at(1008))).doubts, (Doubt.INTERRUPTED,))

    def test_a_gap_after_the_last_rise_of_the_capture_does_not_matter(self):
        board = board_of(*shown(at(0), "q4"), rise(at(1000)), gap(at(1100), at(5000)))
        self.assertEqual(board.spans[0].end, periods.Boundary(at(1000), Edge.CLOCK_AT_ZERO))

    def test_a_gap_just_before_the_last_rise_still_counts(self):
        board = board_of(*shown(at(0), "q4"), rise(at(1000)), gap(at(900), at(995)))
        self.assertEqual(board.spans[0].doubts, (Doubt.INTERRUPTED,))

    def test_without_a_rise_a_gap_anywhere_in_the_period_counts(self):
        board = board_of(*shown(at(0), "q1"), *shown(at(1000), "q2"), gap(at(300), at(400)))
        self.assertEqual(board.spans[0].doubts, (Doubt.INTERRUPTED,))

    def test_a_period_shown_twice_is_repeated(self):
        board = board_of(*shown(at(0), "q1"), rise(at(100)), *shown(at(110), "q2"), *shown(at(120), "q1"))
        q1s = [s for s in board.spans if s.period is Period.Q1]
        self.assertEqual(len(q1s), 2)
        for span in q1s:
            self.assertIn(Doubt.REPEATED, span.doubts)

    def test_other_showings_are_never_repeated(self):
        board = board_of(*shown(at(0), "ot"), *shown(at(10), "q4"), *shown(at(20), "final"))
        others = [s for s in board.spans if s.period is Period.OTHER]
        self.assertEqual(len(others), 2)
        for span in others:
            self.assertNotIn(Doubt.REPEATED, span.doubts)

    def test_a_skipped_period_is_out_of_order(self):
        board = board_of(*shown(at(0), "q1"), rise(at(100)), *shown(at(110), "q3"))
        q1, q3 = board.spans
        self.assertIn(Doubt.OUT_OF_ORDER, q1.doubts)
        self.assertIn(Doubt.OUT_OF_ORDER, q3.doubts)

    def test_overtime_is_other_and_q4_still_ends_at_its_rise(self):
        board = board_of(*shown(at(0), "q4"), rise(at(1000)), *shown(at(1010), "ot"))
        q4, other = board.spans
        self.assertIs(other.period, Period.OTHER)
        self.assertEqual(q4.doubts, ())
        assert q4.end is not None
        self.assertEqual(q4.end.time, at(1000))


class TestPinned(unittest.TestCase):
    def test_the_constants_are_pinned(self):
        self.assertEqual(AT_ZERO, "z")
        self.assertEqual(REWRITE_SECONDS, 11.0)
        self.assertEqual([p.value for p in Period], ["pregame", "q1", "q2", "halftime", "q3", "q4", "other"])
        self.assertEqual(periods.ORDER, tuple(p for p in Period if p is not Period.OTHER))

    def test_the_shared_doubts_read_as_stoppages_do(self):
        self.assertEqual(Doubt.INTERRUPTED.value, stoppages.Doubt.INTERRUPTED.value)
        self.assertEqual(Doubt.SOURCE_CHANGED.value, stoppages.Doubt.SOURCE_CHANGED.value)


if __name__ == "__main__":
    unittest.main()
