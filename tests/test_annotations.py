import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tacet import annotations as ann


class FakeClock:
    """Deterministic clock. Each reading advances by one second."""

    def __init__(self, start: float = 1000.0) -> None:
        self._monotonic = start
        self._wall = datetime(2026, 9, 13, 19, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        value = self._monotonic
        self._monotonic += 1.0
        self._wall = self._wall.replace(second=int(self._monotonic) % 60)
        return value


class TestVocabulary(unittest.TestCase):
    def test_keys_are_unique(self):
        keys = [e.key for e in ann.VOCABULARY]
        self.assertEqual(len(keys), len(set(keys)))

    def test_keys_are_machine_safe(self):
        # They end up in marker names and in filenames of derived exports.
        for event in ann.VOCABULARY:
            self.assertRegex(event.key, r"^[a-z0-9-]+$")
            self.assertNotIn(ann.MARKER_DELIMITER, event.key)

    def test_covers_the_documented_event_vocabulary(self):
        # design.md 5.6, plus the game timing that stands in for RTD.
        for key in (
            "band-enters-stands",
            "band-exits-stands",
            "drumline-cadence",
            "touchdown-sequence",
            "false-open",
            "missed-entrance",
            "note",
        ):
            self.assertIn(key, ann.EVENTS, f"{key} missing from the vocabulary")

    def test_spans_exist_for_the_intervals_the_design_calls_regions(self):
        for key in ("q1", "halftime-exodus", "last-two-minutes"):
            self.assertEqual(ann.EVENTS[key].kind, ann.Kind.SPAN, key)

    def test_lookup_rejects_an_unknown_key(self):
        with self.assertRaises(ann.UnknownEventError):
            ann.lookup("no-such-event")


class TestMarkerNames(unittest.TestCase):
    def test_name_is_category_delimiter_key(self):
        entry = ann.Entry.build(1, ann.lookup("note"), FakeClock())
        self.assertEqual(ann.marker_name(entry), "NOTE|note")

    def test_names_split_cleanly_on_the_delimiter(self):
        # The point of the convention: the later read is a split, not a regex.
        for event in ann.VOCABULARY:
            entry = ann.Entry.build(1, event, FakeClock())
            category, key = ann.marker_name(entry).split(ann.MARKER_DELIMITER)
            self.assertEqual(category, event.category.value)
            self.assertEqual(key, event.key)


class TestEntry(unittest.TestCase):
    def test_json_round_trip(self):
        entry = ann.Entry.build(7, ann.lookup("note"), FakeClock(), data={"text": "band sounds thin"})
        restored = ann.Entry.from_json(entry.to_json())
        self.assertEqual(restored, entry)

    def test_json_is_one_line(self):
        entry = ann.Entry.build(1, ann.lookup("note"), FakeClock())
        self.assertNotIn("\n", entry.to_json())

    def test_entry_carries_a_schema_version(self):
        entry = ann.Entry.build(1, ann.lookup("note"), FakeClock())
        self.assertEqual(json.loads(entry.to_json())["v"], ann.SCHEMA_VERSION)

    def test_wall_clock_is_utc_iso8601(self):
        entry = ann.Entry.build(1, ann.lookup("note"), FakeClock())
        parsed = datetime.fromisoformat(entry.wall)
        self.assertIsNotNone(parsed.tzinfo)


class TestProjectTime(unittest.TestCase):
    def test_offset_is_measured_from_the_recording_anchor(self):
        clock = FakeClock(start=500.0)
        anchor = ann.Entry.build(1, ann.lookup("recording-started"), clock)
        later = ann.Entry.build(2, ann.lookup("note"), clock)
        self.assertEqual(ann.project_seconds(later, anchor), 1.0)

    def test_offset_uses_monotonic_so_a_clock_step_cannot_corrupt_it(self):
        clock = FakeClock()
        anchor = ann.Entry.build(1, ann.lookup("recording-started"), clock)
        later = ann.Entry.build(2, ann.lookup("note"), clock)
        # Wall clock jumps backwards, as an NTP correction mid-game would.
        shifted = ann.Entry(**{**later.as_dict(), "wall": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(ann.project_seconds(shifted, anchor), 1.0)


class LogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "game.jsonl"
        self.clock = FakeClock()
        self.addCleanup(self._tmp.cleanup)

    def log(self):
        return ann.AnnotationLog(self.path, clock=self.clock)


class TestAnnotationLog(LogTestCase):
    def test_records_are_durable_immediately(self):
        # The log is the source of truth; an entry not on disk is an entry lost.
        with self.log() as log:
            log.record("note", data={"text": "hello"})
            self.assertEqual(len(list(ann.read_entries(self.path))), 1)

    def test_sequence_numbers_increment_from_one(self):
        with self.log() as log:
            entries = [log.record("note"), log.record("false-open")]
        self.assertEqual([e.seq for e in entries], [1, 2])

    def test_appends_rather_than_truncating_on_reopen(self):
        with self.log() as log:
            log.record("note")
        with self.log() as log:
            log.record("note")
        self.assertEqual(len(list(ann.read_entries(self.path))), 2)

    def test_sequence_continues_across_a_reopen(self):
        with self.log() as log:
            log.record("note")
        with self.log() as log:
            self.assertEqual(log.record("note").seq, 2)

    def test_recording_an_unknown_event_raises(self):
        with self.log() as log, self.assertRaises(ann.UnknownEventError):
            log.record("no-such-event")

    def test_an_instant_event_has_no_span_fields(self):
        with self.log() as log:
            entry = log.record("note")
        self.assertIsNone(entry.phase)
        self.assertIsNone(entry.span_id)

    def test_recording_a_span_event_as_an_instant_is_refused(self):
        with self.log() as log, self.assertRaises(ann.EventKindError):
            log.record("q1")


class TestSpans(LogTestCase):
    def test_start_and_end_share_a_span_id(self):
        with self.log() as log:
            span = log.start_span("q1")
            end = log.end_span(span)
        entries = list(ann.read_entries(self.path))
        self.assertEqual(entries[0].phase, "start")
        self.assertEqual(entries[1].phase, "end")
        self.assertEqual(entries[0].span_id, end.span_id)

    def test_span_ids_are_deterministic_not_random(self):
        # Replaying a log must reproduce the same regions every time.
        with self.log() as log:
            first = log.start_span("q1")
        self.clock = FakeClock()
        self.path.unlink()
        with self.log() as log:
            second = log.start_span("q1")
        self.assertEqual(first, second)

    def test_starting_a_span_on_an_instant_event_is_refused(self):
        with self.log() as log, self.assertRaises(ann.EventKindError):
            log.start_span("note")

    def test_ending_an_unknown_span_is_refused(self):
        with self.log() as log, self.assertRaises(ann.UnknownSpanError):
            log.end_span("q1-999")

    def test_ending_a_span_twice_is_refused(self):
        with self.log() as log:
            span = log.start_span("q1")
            log.end_span(span)
            with self.assertRaises(ann.UnknownSpanError):
                log.end_span(span)

    def test_open_spans_are_visible(self):
        with self.log() as log:
            span = log.start_span("q1")
            self.assertEqual(log.open_spans(), (span,))
            log.end_span(span)
            self.assertEqual(log.open_spans(), ())


class TestReading(LogTestCase):
    def test_a_truncated_final_line_is_tolerated(self):
        # The power-loss case: a torn write costs the last entry, not the log.
        with self.log() as log:
            log.record("note")
            log.record("false-open")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 3, "event": "no')
        self.assertEqual(len(list(ann.read_entries(self.path))), 2)

    def test_a_corrupt_line_in_the_middle_is_raised_not_skipped(self):
        # Silent degradation is worse than an obvious stop.
        with self.log() as log:
            log.record("note")
            log.record("false-open")
        text = self.path.read_text(encoding="utf-8").splitlines()
        text.insert(1, "{ this is not json")
        self.path.write_text("\n".join(text) + "\n", encoding="utf-8")
        with self.assertRaises(ann.CorruptLogError):
            list(ann.read_entries(self.path))

    def test_reading_a_missing_log_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(ann.read_entries(self.path / "nope.jsonl"))

    def test_blank_lines_are_ignored(self):
        with self.log() as log:
            log.record("note")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        self.assertEqual(len(list(ann.read_entries(self.path))), 1)

    def test_round_trips_every_event_in_the_vocabulary(self):
        with self.log() as log:
            for event in ann.VOCABULARY:
                if event.kind is ann.Kind.SPAN:
                    log.end_span(log.start_span(event.key))
                else:
                    log.record(event.key)
        entries = list(ann.read_entries(self.path))
        self.assertEqual(len(entries), len(ann.VOCABULARY) + sum(1 for e in ann.VOCABULARY if e.kind is ann.Kind.SPAN))


if __name__ == "__main__":
    unittest.main()
