import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from tacet import app, dm7, markers, pilot, state, wav
from tacet.annotations import Entry

from .pilot_wav import Canvas, commanded, paint_envelope, pcm24, plain, tone, wav_bytes

CLOSED = dm7.MINUS_INF
#: Pilot dBFS minus DCA dB, as Game 3 measured it.
OFFSET = -12.0
T = 10.0
RATE = 8000


def wall(minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 10, 2, 22, minute, second, tzinfo=UTC).isoformat()


def p(dca: float) -> float:
    """Pilot dBFS for a DCA level in dB."""
    return dca + OFFSET


def units(db: float) -> int:
    return round(db * dm7.UNITS_PER_DB)


CALIBRATION = pilot.Calibration(offset_db=OFFSET, used=((1, OFFSET),), silent_opens=(), uncovered=0)
LAYOUT = wav.WavLayout(
    sample_rate=RATE, channels=1, bits=24, data_offset=690, data_bytes=0, time_reference=0, truncated=False
)


def take_of(entries: list[Entry]) -> pilot.Take:
    anchor = pilot.Anchor(wall=datetime(2026, 10, 2, tzinfo=UTC), entry=None, description="test anchor")
    return pilot.Take(anchor=anchor, entries=tuple(entries), ended_by=None)


def fade(seq: int, ps: float | None, level: int = 0, target: int = CLOSED, detail: str = "out") -> Entry:
    return commanded(seq, wall(), ps, "fade", detail, level, target)


def snap_open(seq: int, ps: float | None, target: int = 0) -> Entry:
    return commanded(seq, wall(), ps, "open", "up-drums", CLOSED, target)


def up_slow(seq: int, ps: float | None) -> Entry:
    return commanded(seq, wall(), ps, "open", "up-slow", CLOSED, 0)


def check_one(canvas: Canvas, entry: Entry) -> pilot.RampCheck:
    return pilot.check_ramp(canvas.envelope(), pilot.ramp_of(entry), CALIBRATION)


class TestConstants(unittest.TestCase):
    def test_the_windows_and_tolerances_game_3_used(self):
        self.assertEqual(pilot.WINDOW_SECONDS, 0.01)
        self.assertEqual(pilot.RAMP_BEFORE_WINDOW, (-0.5, 0.03))
        self.assertEqual(pilot.RAMP_AFTER_SECONDS, 0.4)
        self.assertEqual(pilot.CALIBRATION_SILENCE_WINDOW, (-1.0, -0.3))
        self.assertEqual(pilot.CALIBRATION_SETTLED_WINDOW, (0.5, 1.5))
        self.assertEqual(pilot.JUMP_TOLERANCE_DB, 1.5)
        self.assertEqual(pilot.MISMATCH_TOLERANCE_DB, 1.5)
        self.assertEqual(pilot.SINE_PEAK_POWER_RATIO, 2.0)
        self.assertEqual(pilot.SILENCE_FLOOR_DBFS, -200.0)
        self.assertEqual(pilot.CLOSED_BELOW_DBFS, -100.0)
        self.assertEqual(pilot.ENVELOPE_BLOCK_SECONDS, 60.0)
        self.assertEqual(pilot.FULL_SCALE_24, 8_388_608)

    def test_the_ramp_windows_allow_for_game_3_alignment(self):
        self.assertLess(pilot.RAMP_BEFORE_WINDOW[1], pilot.GAME_3_LAG_RANGE[0])
        self.assertGreater(pilot.RAMP_BEFORE_WINDOW[1] + pilot.RAMP_AFTER_SECONDS, pilot.GAME_3_LAG_RANGE[1])

    def test_commanded_event_is_the_one_the_box_writes(self):
        self.assertEqual(pilot.COMMANDED_EVENT, app.COMMANDED)

    def test_the_data_keys_are_the_ones_the_box_writes(self):
        data = commanded(1, wall(), 1.0, "fade", "out", 0, CLOSED).data
        for key in (
            pilot.DATA_COMMAND,
            pilot.DATA_DETAIL,
            pilot.DATA_LEVEL,
            pilot.DATA_TARGET,
            pilot.DATA_TARGET_DB,
            pilot.DATA_DELIVERED,
        ):
            self.assertIn(key, data)

    def test_ramp_commands_are_fader_commands(self):
        commands = {c.value for c in state.FaderCommand}
        self.assertLessEqual(pilot.RAMP_COMMANDS, commands)
        self.assertEqual(pilot.RAMP_COMMANDS, {"fade", "ready", "retarget"})


class TestEnvelope(unittest.TestCase):
    def test_decodes_24_bit_signed_little_endian(self):
        top, bottom = (1 << 23) - 1, -(1 << 23)
        raw = np.frombuffer(pcm24([0, 1, -1, top, bottom]), dtype=np.uint8)
        full = pilot.FULL_SCALE_24
        np.testing.assert_allclose(pilot.decode_pcm24(raw), [0, 1 / full, -1 / full, top / full, -1.0])

    def test_full_scale_sine_reads_0_dbfs(self):
        t = np.arange(RATE) / RATE
        env = pilot.envelope_dbfs(np.sin(2 * np.pi * 1000 * t), 80)
        np.testing.assert_allclose(env, 0.0, atol=1e-4)

    def test_sine_at_minus_12_reads_minus_12(self):
        raw = np.frombuffer(tone([(0.1, -12.0)], rate=48_000), dtype=np.uint8)
        env = pilot.envelope_of(raw, sample_rate=48_000, start_seconds=0.0)
        self.assertEqual(len(env.dbfs), 10)
        np.testing.assert_allclose(env.dbfs, -12.0, atol=0.01)

    def test_silence_reads_the_floor(self):
        raw = np.zeros(3 * 160, dtype=np.uint8)
        env = pilot.envelope_of(raw, sample_rate=RATE, start_seconds=0.0)
        np.testing.assert_array_equal(env.dbfs, np.float32(pilot.SILENCE_FLOOR_DBFS))

    def test_partial_trailing_window_is_dropped(self):
        raw = np.frombuffer(tone([(105 / RATE, -6.0)], rate=RATE), dtype=np.uint8)
        self.assertEqual(len(pilot.envelope_of(raw, sample_rate=RATE, start_seconds=0.0).dbfs), 1)

    def test_blocks_join_seamlessly(self):
        raw = np.frombuffer(tone([(0.3, -6.0), (0.3, -20.0)], rate=RATE), dtype=np.uint8)
        whole = pilot.envelope_of(raw, sample_rate=RATE, start_seconds=0.0)
        pieces = pilot.envelope_of(raw, sample_rate=RATE, start_seconds=0.0, block_seconds=0.03)
        np.testing.assert_array_equal(whole.dbfs, pieces.dbfs)

    def test_window_spacing_comes_from_the_sample_rate(self):
        rate = 44_117
        raw = np.zeros(3 * rate, dtype=np.uint8)
        env = pilot.envelope_of(raw, sample_rate=rate, start_seconds=2.0)
        self.assertEqual(env.window_seconds, round(rate * pilot.WINDOW_SECONDS) / rate)
        self.assertEqual(env.start_seconds, 2.0)

    def test_window_is_none_unless_wholly_covered(self):
        env = paint_envelope([(1.0, -10.0)])
        window = env.window(0.2, 0.5)
        assert window is not None
        self.assertEqual(len(window), 30)
        self.assertIsNone(env.window(-0.1, 0.5))
        self.assertIsNone(env.window(0.5, 1.2))
        self.assertAlmostEqual(env.end_seconds, 1.0)

    def test_read_envelope_from_a_file_places_it_by_time_reference(self):
        data = wav_bytes(tone([(0.5, -12.0)], rate=RATE), rate=RATE, time_reference=5 * RATE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.wav"
            path.write_bytes(data)
            layout, env = pilot.read_envelope(path)
        self.assertEqual(layout.start_seconds, 5.0)
        self.assertEqual(env.start_seconds, 5.0)
        np.testing.assert_allclose(env.dbfs, -12.0, atol=0.01)


class TestClassification(unittest.TestCase):
    def test_fade_ready_retarget_and_up_slow_are_ramps(self):
        entries = [
            fade(1, 1.0),
            commanded(2, wall(), 1.0, "ready", "up-ready", CLOSED, -600),
            commanded(3, wall(), 1.0, "retarget", "target-set", 0, -300),
            up_slow(4, 1.0),
        ]
        self.assertTrue(all(pilot.is_ramp(e) for e in entries))
        self.assertEqual([pilot.kind_of(e) for e in entries], ["fade", "ready", "retarget", "up-slow ride"])

    def test_snap_open_close_now_and_report_ready_are_not(self):
        entries = [
            snap_open(1, 1.0),
            commanded(2, wall(), 1.0, "close-now", "x", 0, CLOSED),
            commanded(3, wall(), 1.0, "report-ready", "x", CLOSED, -600),
            plain(4, wall(), "note"),
        ]
        self.assertFalse(any(pilot.is_ramp(e) for e in entries))
        self.assertTrue(pilot.is_snap_open(entries[0]))
        self.assertFalse(any(pilot.is_snap_open(e) for e in entries[1:]))

    def test_up_slow_is_a_ride_even_when_delivered_is_true(self):
        entry = commanded(1, wall(), 1.0, "open", "up-slow", CLOSED, 0, delivered=True)
        self.assertTrue(pilot.is_ramp(entry))
        self.assertFalse(pilot.is_snap_open(entry))


class TestTake(unittest.TestCase):
    def test_stamped_entries_before_a_logged_anchor_are_excluded(self):
        entries = [
            fade(1, 6.7),
            plain(2, wall(1), "recording-started"),
            fade(3, 3.0),
        ]
        anchor = pilot.resolve_anchor(entries, None)
        self.assertIsNotNone(anchor.entry)
        self.assertEqual([e.seq for e in pilot.take_of(entries, anchor).entries], [3])

    def test_take_start_selects_by_wall_time(self):
        entries = [fade(1, 6.7, detail="a"), fade(2, 7.0), fade(3, 8.0)]
        entries = [
            Entry(**{**e.as_dict(), "wall": wall(minute)}) for e, minute in zip(entries, (10, 50, 55), strict=True)
        ]
        start = datetime(2026, 10, 2, 22, 30, tzinfo=UTC)
        anchor = pilot.resolve_anchor(entries, start)
        self.assertIsNone(anchor.entry)
        self.assertIn("--take-start", anchor.description)
        self.assertEqual([e.seq for e in pilot.take_of(entries, anchor).entries], [2, 3])

    def test_take_ends_at_a_second_recording(self):
        entries = [
            Entry(**{**e.as_dict(), "wall": wall(minute)})
            for e, minute in [(fade(1, 1.0), 40), (plain(2, "", "recording-started"), 50), (fade(3, 1.0), 55)]
        ]
        start = datetime(2026, 10, 2, 22, 30, tzinfo=UTC)
        take = pilot.take_of(entries, pilot.resolve_anchor(entries[:1] + entries[2:], start))
        self.assertEqual([e.seq for e in take.entries], [1])
        assert take.ended_by is not None
        self.assertEqual(take.ended_by.seq, 2)

    def test_a_consistent_recording_found_does_not_end_the_take(self):
        started = plain(1, wall(1), "recording-started")
        found = plain(3, wall(2), "recording-found", ps=2.0)
        entries = [started, fade(2, 1.0), found, fade(4, 3.0)]
        take = pilot.take_of(entries, pilot.resolve_anchor(entries, None))
        self.assertIsNone(take.ended_by)
        self.assertEqual([e.seq for e in take.entries if pilot.is_ramp(e)], [2, 4])

    def test_a_second_recording_ends_a_logged_take(self):
        entries = [
            plain(1, wall(1), "recording-started"),
            fade(2, 1.0),
            plain(3, wall(2), "recording-started"),
            fade(4, 1.0),
        ]
        take = pilot.take_of(entries, pilot.resolve_anchor(entries, None))
        self.assertEqual([e.seq for e in take.entries], [2])
        assert take.ended_by is not None
        self.assertEqual(take.ended_by.seq, 3)

    def test_take_start_with_a_logged_anchor_is_refused(self):
        entries = [plain(1, wall(1), "recording-started")]
        with self.assertRaises(pilot.AnchorConflictError):
            pilot.resolve_anchor(entries, datetime(2026, 10, 2, tzinfo=UTC))

    def test_no_anchor_is_refused_and_names_the_flag(self):
        with self.assertRaises(markers.NoAnchorError) as caught:
            pilot.resolve_anchor([fade(1, 1.0)], None)
        self.assertIn("--take-start", str(caught.exception))


def open_canvas(opens: list[tuple[float, float]], seconds: float = 60.0) -> Canvas:
    """Pilot silent except for a hold after each (stamp, pilot dBFS) open, at the 90 ms Game 3 median lag."""
    canvas = Canvas(seconds)
    for stamp, level in opens:
        canvas.hold(stamp + 0.09, stamp + 8.0, level)
    return canvas


class TestCalibration(unittest.TestCase):
    def test_offset_is_the_median_of_snap_opens_from_silence(self):
        canvas = open_canvas([(10, -12.3), (20, -12.0), (30, -11.8)])
        take = take_of([snap_open(1, 10.0), snap_open(2, 20.0), snap_open(3, 30.0)])
        cal = pilot.calibrate(canvas.envelope(), take)
        self.assertAlmostEqual(cal.offset_db, -12.0, places=2)
        self.assertEqual(cal.count, 3)
        self.assertEqual(cal.lowest[0], 1)
        self.assertAlmostEqual(cal.lowest[1], -12.3, places=2)
        self.assertEqual(cal.highest[0], 3)
        self.assertAlmostEqual(cal.highest[1], -11.8, places=2)

    def test_rides_are_not_calibration_opens(self):
        canvas = open_canvas([(10, -12.0), (30, -20.0)])
        take = take_of([snap_open(1, 10.0), up_slow(2, 30.0)])
        cal = pilot.calibrate(canvas.envelope(), take)
        self.assertEqual(cal.count, 1)
        self.assertAlmostEqual(cal.offset_db, -12.0, places=2)

    def test_opens_not_from_silence_are_not_used(self):
        canvas = open_canvas([(10, -12.0), (30, -20.0)])
        canvas.hold(28.0, 30.0, -40.0)
        take = take_of([snap_open(1, 10.0), snap_open(2, 30.0)])
        self.assertEqual(pilot.calibrate(canvas.envelope(), take).count, 1)

    def test_an_open_followed_by_another_command_before_it_settles_is_not_used(self):
        canvas = open_canvas([(10, -12.0), (30, -20.0)])
        take = take_of([snap_open(1, 10.0), snap_open(2, 30.0), fade(3, 31.0)])
        cal = pilot.calibrate(canvas.envelope(), take)
        self.assertEqual([seq for seq, _ in cal.used], [1])

    def test_an_open_the_pilot_never_shows_is_reported(self):
        canvas = open_canvas([(10, -12.0)])
        take = take_of([snap_open(1, 10.0), snap_open(2, 30.0)])
        cal = pilot.calibrate(canvas.envelope(), take)
        self.assertEqual(cal.silent_opens, (2,))
        self.assertEqual(cal.count, 1)

    def test_an_open_outside_the_pilot_is_counted(self):
        canvas = open_canvas([(10, -12.0)], seconds=40.0)
        take = take_of([snap_open(1, 10.0), snap_open(2, 100.0)])
        self.assertEqual(pilot.calibrate(canvas.envelope(), take).uncovered, 1)

    def test_no_usable_open_raises(self):
        with self.assertRaises(pilot.NoCalibrationError):
            pilot.calibrate(Canvas(60.0).envelope(), take_of([snap_open(1, 10.0), fade(2, 20.0)]))


class TestCheckRamp(unittest.TestCase):
    def test_seq_33_blast_is_flagged(self):
        canvas = Canvas(30.0)
        canvas.hold(T + 0.07, T + 0.2, p(-0.8))
        canvas.ramp(T + 0.2, T + 2.0, p(-0.8), -80.0)
        result = check_one(canvas, fade(33, T, level=0))
        self.assertIn(pilot.Finding.BLAST, result.findings)
        self.assertIn(pilot.Finding.MISMATCH, result.findings)
        self.assertEqual(result.before_db, float("-inf"))
        self.assertAlmostEqual(result.after_db or 0.0, -0.8, places=1)
        self.assertTrue(result.flagged)

    def test_seq_59_step_down_is_flagged(self):
        canvas = Canvas(30.0)
        canvas.hold(T - 2.0, T + 0.09, p(-2.5))
        canvas.ramp(T + 0.09, T + 2.0, p(-2.5), -80.0)
        result = check_one(canvas, fade(59, T, level=units(-6)))
        self.assertEqual(result.findings, (pilot.Finding.MISMATCH,))
        self.assertAlmostEqual(result.before_db or 0.0, -2.5, places=1)

    def test_clean_fade_is_not_flagged_at_game_3_lags(self):
        for lag in (0.04, 0.09, 0.23):
            with self.subTest(lag=lag):
                canvas = Canvas(30.0)
                canvas.hold(T - 2.0, T + lag, p(0.0))
                canvas.ramp(T + lag, T + lag + 2.0, p(0.0), -80.0)
                result = check_one(canvas, fade(1, T, level=0))
                self.assertEqual(result.findings, ())

    def test_ready_ride_from_closed_is_not_flagged(self):
        canvas = Canvas(30.0)
        canvas.hold(T + 0.09, T + 0.1, None)
        canvas.ramp(T + 0.09, T + 1.6, -80.0, p(-6.0))
        entry = commanded(1, wall(), T, "ready", "up-ready", CLOSED, units(-6))
        self.assertEqual(check_one(canvas, entry).findings, ())

    def test_up_slow_from_closed_is_not_flagged(self):
        canvas = Canvas(30.0)
        canvas.ramp(T + 0.09, T + 3.0, -80.0, p(0.0))
        self.assertEqual(check_one(canvas, up_slow(1, T)).findings, ())

    def test_rising_ride_that_drops_is_flagged(self):
        canvas = Canvas(30.0)
        canvas.hold(T - 2.0, T + 0.1, p(-6.0))
        canvas.hold(T + 0.1, T + 0.3, p(-20.0))
        canvas.ramp(T + 0.3, T + 2.0, p(-20.0), p(0.0))
        entry = commanded(1, wall(), T, "ready", "up-ready", units(-6), 0)
        self.assertEqual(check_one(canvas, entry).findings, (pilot.Finding.DROPOUT,))

    def test_downward_retarget_ride_is_not_a_dropout(self):
        canvas = Canvas(30.0)
        canvas.hold(T - 2.0, T + 0.09, p(0.0))
        canvas.ramp(T + 0.09, T + 1.0, p(0.0), p(-3.0))
        canvas.hold(T + 1.0, T + 3.0, p(-3.0))
        entry = commanded(1, wall(), T, "retarget", "target-set", 0, units(-3))
        result = check_one(canvas, entry)
        self.assertEqual(result.ramp.direction, pilot.Direction.FALLING)
        self.assertEqual(result.findings, ())

    def test_upward_retarget_ride_is_checked_for_dropout(self):
        canvas = Canvas(30.0)
        canvas.hold(T - 2.0, T + 0.1, p(-3.0))
        canvas.hold(T + 0.1, T + 0.3, p(-12.0))
        canvas.hold(T + 0.3, T + 3.0, p(0.0))
        entry = commanded(1, wall(), T, "retarget", "target-set", units(-3), 0)
        result = check_one(canvas, entry)
        self.assertEqual(result.ramp.direction, pilot.Direction.RISING)
        self.assertIn(pilot.Finding.DROPOUT, result.findings)

    def test_fade_from_closed_while_closed_matches(self):
        result = check_one(Canvas(30.0), fade(1, T, level=CLOSED))
        self.assertEqual(result.ramp.direction, pilot.Direction.LEVEL)
        self.assertEqual(result.findings, ())

    def test_fade_from_closed_while_the_pilot_is_up_is_flagged(self):
        canvas = Canvas(30.0)
        canvas.hold(T - 2.0, T + 3.0, p(-6.0))
        result = check_one(canvas, fade(1, T, level=CLOSED))
        self.assertIn(pilot.Finding.MISMATCH, result.findings)

    def test_mismatch_inside_tolerance_passes_and_outside_flags(self):
        for gap, flagged in ((1.4, False), (1.6, True)):
            with self.subTest(gap=gap):
                canvas = Canvas(30.0)
                canvas.hold(T - 2.0, T + 3.0, p(-gap))
                result = check_one(canvas, fade(1, T, level=0))
                self.assertEqual(result.flagged, flagged)

    def test_ramp_outside_the_pilot_is_unchecked(self):
        result = check_one(Canvas(5.0), fade(1, T))
        self.assertEqual(result.findings, (pilot.Finding.UNCOVERED,))
        self.assertTrue(result.unchecked)
        self.assertFalse(result.flagged)

    def test_unstamped_ramp_is_unchecked(self):
        result = check_one(Canvas(30.0), fade(1, None))
        self.assertEqual(result.findings, (pilot.Finding.UNSTAMPED,))
        self.assertTrue(result.unchecked)


class Game3(unittest.TestCase):
    """The shape of Game 3's findings, on a painted envelope."""

    def setUp(self):
        self.canvas = Canvas(600.0)
        self.entries: list[Entry] = []
        self.seq = 0
        self.clock = 5.0

    def slot(self) -> float:
        self.clock += 12.0
        self.seq += 1
        return self.clock

    def calibration_open(self, jitter: float) -> None:
        t = self.slot()
        self.entries.append(snap_open(self.seq, t))
        self.canvas.hold(t + 0.09, t + 4.0, p(jitter))
        self.canvas.ramp(t + 4.09, t + 6.0, p(jitter), -80.0)
        # The fade that closes it, stamped as it starts, from the believed level.
        self.entries.append(fade(1000 + self.seq, t + 4.0, level=0))

    def clean_fade(self) -> None:
        t = self.slot()
        self.canvas.hold(t - 2.0, t + 0.12, p(0.0))
        self.canvas.ramp(t + 0.12, t + 2.1, p(0.0), -80.0)
        self.entries.append(fade(self.seq, t, level=0))

    def blast(self, seq: int) -> None:
        t = self.slot()
        self.canvas.hold(t + 0.07, t + 0.2, p(-0.8))
        self.canvas.ramp(t + 0.2, t + 2.1, p(-0.8), -80.0)
        self.entries.append(fade(seq, t, level=0))

    def step_down(self, seq: int) -> None:
        t = self.slot()
        self.canvas.hold(t - 2.0, t + 0.09, p(-2.5))
        self.canvas.ramp(t + 0.09, t + 2.1, p(-2.5), -80.0)
        self.entries.append(fade(seq, t, level=units(-6)))

    def ready_from_closed(self) -> None:
        t = self.slot()
        self.canvas.ramp(t + 0.1, t + 1.6, -80.0, p(-6.0))
        self.canvas.hold(t + 1.6, t + 4.0, p(-6.0))
        self.entries.append(commanded(self.seq, wall(), t, "ready", "up-ready", CLOSED, units(-6)))

    def up_slow_from_closed(self) -> None:
        t = self.slot()
        self.canvas.ramp(t + 0.1, t + 3.0, -80.0, p(0.0))
        self.canvas.hold(t + 3.0, t + 4.0, p(0.0))
        self.entries.append(commanded(self.seq, wall(), t, "open", "up-slow", CLOSED, 0))


class TestGame3Verdict(Game3):
    def test_the_two_real_findings_and_nothing_else(self):
        for jitter in (-0.3, 0.0, 0.2, 0.0):
            self.calibration_open(jitter)
        for _ in range(3):
            self.clean_fade()
        self.blast(33)
        for _ in range(6):
            self.ready_from_closed()
        self.step_down(59)
        for _ in range(15):
            self.up_slow_from_closed()
        for _ in range(3):
            self.clean_fade()
        report = pilot.check(self.canvas.envelope(), take_of(self.entries), LAYOUT)
        self.assertEqual([c.ramp.entry.seq for c in report.flagged], [33, 59])
        self.assertEqual(report.unchecked, ())
        self.assertAlmostEqual(report.calibration.offset_db, -12.0, delta=0.05)
        spread = report.calibration.highest[1] - report.calibration.lowest[1]
        self.assertLessEqual(spread, 0.6)
        rides = [c for c in report.checks if pilot.kind_of(c.ramp.entry) in ("ready", "up-slow ride")]
        self.assertEqual(len(rides), 21)
        self.assertFalse(any(c.flagged for c in rides))
        counts = report.counts_by_kind
        self.assertEqual((counts["ready"], counts["up-slow ride"], counts["retarget"]), (6, 15, 0))
        self.assertEqual(counts["fade"], 4 + 3 + 1 + 1 + 3)


class TestRender(Game3):
    EASTERN = timezone(timedelta(hours=-4))

    def report(self, *, unchecked: bool = False, flagged: bool = True) -> pilot.Report:
        self.setUp()
        self.calibration_open(0.0)
        self.clean_fade()
        if flagged:
            t = self.slot()
            self.canvas.hold(t + 0.07, t + 0.2, p(-0.8))
            self.canvas.ramp(t + 0.2, t + 2.1, p(-0.8), -80.0)
            blast = commanded(33, wall(15), t, "fade", "out", 0, CLOSED)
            self.entries.append(Entry(**{**blast.as_dict(), "wall": "2026-10-02T22:15:00+00:00"}))
        if unchecked:
            self.entries.append(fade(77, 1000.0))
        return pilot.check(self.canvas.envelope(), take_of(self.entries), LAYOUT)

    def test_flagged_row_shows_local_and_utc(self):
        text = pilot.render(self.report(), tz=self.EASTERN)
        row = next(line for line in text.splitlines() if line.startswith("33 "))
        self.assertIn("18:15:00", row)
        self.assertIn("22:15:00", row)

    def test_minus_infinity_renders_as_text(self):
        text = pilot.render(self.report(), tz=self.EASTERN)
        row = next(line for line in text.splitlines() if line.startswith("33 "))
        self.assertIn("-inf", row)

    def test_verdict_clean_flagged_unchecked(self):
        clean = pilot.render(self.report(flagged=False), tz=UTC)
        self.assertIn("none flagged", clean.splitlines()[-1])
        self.assertIn("flagged (seq 33)", pilot.render(self.report(), tz=UTC).splitlines()[-1])
        unchecked = pilot.render(self.report(flagged=False, unchecked=True), tz=UTC)
        self.assertIn("1 could not be checked", unchecked.splitlines()[-1])
        self.assertIn("77", unchecked)

    def test_report_is_ascii(self):
        text = pilot.render(self.report(unchecked=True), tz=self.EASTERN, source="pilot.wav")
        text.encode("ascii")
        self.assertIn("pilot file: pilot.wav", text)
        self.assertIn("pilot = DCA -12.0 dB", text)


if __name__ == "__main__":
    unittest.main()
