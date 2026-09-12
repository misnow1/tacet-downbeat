import unittest
from datetime import UTC, datetime

from tacet import annotations as ann
from tacet import markers


class StepClock:
    """One second per reading, so offsets are exact."""

    def __init__(self, start: float = 100.0) -> None:
        self._m = start

    def now(self) -> datetime:
        return datetime(2026, 9, 13, 19, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        value = self._m
        self._m += 1.0
        return value


def build(clock, seq, key, **kwargs):
    return ann.Entry.build(seq, ann.lookup(key), clock, **kwargs)


class TestFindAnchor(unittest.TestCase):
    def test_finds_the_recording_started_entry(self):
        clock = StepClock()
        entries = [build(clock, 1, "note"), build(clock, 2, "recording-started")]
        self.assertEqual(markers.find_anchor(entries).seq, 2)

    def test_uses_the_first_anchor_when_recording_restarted(self):
        clock = StepClock()
        entries = [
            build(clock, 1, "recording-started"),
            build(clock, 2, "recording-started"),
        ]
        self.assertEqual(markers.find_anchor(entries).seq, 1)

    def test_a_log_without_an_anchor_is_an_error(self):
        clock = StepClock()
        with self.assertRaises(markers.NoAnchorError):
            markers.find_anchor([build(clock, 1, "note")])


class TestInstants(unittest.TestCase):
    def test_position_is_measured_from_the_anchor(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        note = build(clock, 2, "note")
        result = markers.derive([anchor, note], anchor)
        placed = [m for m in result.markers if m.name == "NOTE|note"]
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0].start, 1.0)
        self.assertFalse(placed[0].is_region)

    def test_marker_names_use_the_documented_convention(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        result = markers.derive([anchor, build(clock, 2, "false-open")], anchor)
        self.assertIn("DET|false-open", [m.name for m in result.markers])

    def test_the_anchor_itself_is_placed_at_zero(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        result = markers.derive([anchor], anchor)
        self.assertAlmostEqual(result.markers[0].start, 0.0)

    def test_a_recorded_project_time_is_preferred_over_the_computed_one(self):
        # Reaper feedback knows the real playhead; trust it over clock arithmetic.
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        note = build(clock, 2, "note", project_seconds=42.5)
        result = markers.derive([anchor, note], anchor)
        placed = next(m for m in result.markers if m.name == "NOTE|note")
        self.assertAlmostEqual(placed.start, 42.5)


class TestRegions(unittest.TestCase):
    def spanned(self, gap=3):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        start = build(clock, 2, "q1", phase="start", span_id="q1-2")
        for _ in range(gap):
            clock.monotonic()
        end = build(clock, 3, "q1", phase="end", span_id="q1-2")
        return anchor, [anchor, start, end]

    def test_a_span_pair_becomes_one_region(self):
        anchor, entries = self.spanned()
        result = markers.derive(entries, anchor)
        regions = [m for m in result.markers if m.is_region]
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].name, "GAME|q1")

    def test_region_runs_from_start_to_end(self):
        anchor, entries = self.spanned(gap=3)
        region = next(m for m in markers.derive(entries, anchor).markers if m.is_region)
        assert region.end is not None
        self.assertAlmostEqual(region.start, 1.0)
        self.assertAlmostEqual(region.end, 5.0)

    def test_a_span_produces_no_separate_instant_markers(self):
        anchor, entries = self.spanned()
        result = markers.derive(entries, anchor)
        self.assertEqual(sum(1 for m in result.markers if not m.is_region), 1)

    def test_an_unclosed_span_runs_to_the_timeline_end_and_is_reported(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        start = build(clock, 2, "q4", phase="start", span_id="q4-2")
        result = markers.derive([anchor, start], anchor, timeline_end=99.0)
        region = next(m for m in result.markers if m.is_region)
        assert region.end is not None
        self.assertAlmostEqual(region.end, 99.0)
        self.assertEqual(result.unclosed_spans, ("q4-2",))

    def test_an_unclosed_span_defaults_to_the_last_entry(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        start = build(clock, 2, "q4", phase="start", span_id="q4-2")
        note = build(clock, 3, "note")
        derived = markers.derive([anchor, start, note], anchor)
        region = next(m for m in derived.markers if m.is_region)
        assert region.end is not None
        self.assertAlmostEqual(region.end, 2.0)

    def test_an_end_without_a_start_is_reported_not_dropped(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        orphan = build(clock, 2, "q1", phase="end", span_id="q1-999")
        result = markers.derive([anchor, orphan], anchor)
        self.assertEqual(result.orphan_ends, ("q1-999",))


class TestBeforeTheAnchor(unittest.TestCase):
    def test_entries_before_recording_started_are_skipped_and_reported(self):
        # Reaper has no negative timeline. Dropping them silently would be the
        # kind of quiet data loss CLAUDE.md forbids, so they are reported.
        clock = StepClock()
        early = build(clock, 1, "band-enters-stadium")
        anchor = build(clock, 2, "recording-started")
        result = markers.derive([early, anchor], anchor)
        self.assertEqual([e.seq for e in result.skipped_before_anchor], [1])
        self.assertNotIn("BAND|band-enters-stadium", [m.name for m in result.markers])


class TestOrdering(unittest.TestCase):
    def test_markers_come_out_in_timeline_order(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        entries = [anchor] + [build(clock, n, "note") for n in range(2, 6)]
        starts = [m.start for m in markers.derive(entries, anchor).markers]
        self.assertEqual(starts, sorted(starts))


class TestCsv(unittest.TestCase):
    def test_round_trips_through_csv(self):
        clock = StepClock()
        anchor = build(clock, 1, "recording-started")
        entries = [
            anchor,
            build(clock, 2, "q1", phase="start", span_id="q1-2"),
            build(clock, 3, "note"),
            build(clock, 4, "q1", phase="end", span_id="q1-2"),
        ]
        derived = markers.derive(entries, anchor)
        restored = markers.from_csv(markers.to_csv(derived.markers))
        self.assertEqual(restored, list(derived.markers))

    def test_csv_has_a_header(self):
        text = markers.to_csv([markers.Marker(name="X|y", start=1.0)])
        self.assertTrue(text.splitlines()[0].startswith("name,"))


if __name__ == "__main__":
    unittest.main()


class TestARestartedRecordingIsReported(unittest.TestCase):
    """The silent one. Everything after a restart is placed the length of the
    stop away from the audio it describes, with a positive, plausible position
    that no other field here would flag."""

    def entries(self, clock):
        return [
            build(clock, 1, "recording-started"),
            build(clock, 2, "band-enters-stands"),
            build(clock, 3, "recording-started"),
            build(clock, 4, "drumline-cadence"),
        ]

    def test_the_later_recording_is_reported(self):
        clock = StepClock()
        entries = self.entries(clock)
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertEqual([entry.seq for entry in result.extra_anchors], [3])

    def test_that_makes_the_result_unclean(self):
        clock = StepClock()
        entries = self.entries(clock)
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertFalse(result.is_clean)

    def test_nothing_is_dropped_and_no_position_goes_negative(self):
        # Precisely why it needed its own field: the existing reports stay
        # empty, so the old is_clean would have called this fine.
        clock = StepClock()
        entries = self.entries(clock)
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertEqual(result.skipped_before_anchor, ())
        self.assertEqual(result.orphan_ends, ())
        self.assertEqual(result.unclosed_spans, ())

    def test_one_recording_reports_nothing_and_stays_clean(self):
        clock = StepClock()
        entries = [build(clock, 1, "recording-started"), build(clock, 2, "band-enters-stands")]
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertEqual(result.extra_anchors, ())
        self.assertTrue(result.is_clean)


class TestAStampedPlayheadBeatsTheArithmetic(unittest.TestCase):
    """The reason the box stamps Reaper's position onto every entry.

    Without it, positions are our clock's arithmetic from the anchor: they
    accumulate drift over a three-hour game, and after a stop and restart they
    are out by the whole length of the gap while still looking plausible.
    """

    def test_a_stamped_entry_ignores_the_anchor(self):
        clock = StepClock()
        entries = [
            build(clock, 1, "recording-started"),
            build(clock, 2, "band-enters-stands", project_seconds=1234.5),
        ]
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertEqual(result.markers[-1].start, 1234.5)

    def test_a_restarted_recording_is_placed_correctly_when_stamped(self):
        # The whole point. The anchor is still the first recording, so this is
        # reported as untidy - but the position is Reaper's own, so the marker
        # lands on the audio it describes instead of the length of the gap away.
        clock = StepClock()
        entries = [
            build(clock, 1, "recording-started"),
            build(clock, 2, "recording-started"),
            build(clock, 3, "drumline-cadence", project_seconds=12.0),
        ]
        result = markers.derive(entries, markers.find_anchor(entries))
        placed = {marker.name: marker.start for marker in result.markers}
        self.assertEqual(placed[ann.marker_name(entries[2])], 12.0)
        self.assertEqual([entry.seq for entry in result.extra_anchors], [2])
        self.assertFalse(result.is_clean)

    def test_an_unstamped_entry_still_uses_the_arithmetic(self):
        # The fallback has to keep working: Reaper is silent while parked, so
        # entries made then carry no position at all.
        clock = StepClock()
        entries = [build(clock, 1, "recording-started"), build(clock, 2, "band-enters-stands")]
        result = markers.derive(entries, markers.find_anchor(entries))
        self.assertEqual(result.markers[-1].start, 1.0)
