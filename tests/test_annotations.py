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

    def test_every_timeout_is_a_game_span(self):
        timeouts = [e for e in ann.VOCABULARY if e.key.startswith("timeout")]
        self.assertGreater(len(timeouts), 1)
        for event in timeouts:
            self.assertEqual(event.kind, ann.Kind.SPAN, event.key)
            self.assertEqual(event.category, ann.Category.GAME, event.key)

    def test_an_injury_timeout_is_distinguishable_from_the_rest(self):
        # design.md 2: the band plays through timeouts *excluding* injury
        # timeouts. A log that cannot tell them apart cannot answer whether the
        # band was meant to be playing.
        self.assertIn("timeout-injury", ann.EVENTS)

    def test_the_original_timeout_key_still_resolves(self):
        # Keys are machine-facing and stable; logs from before the split still
        # have to read back.
        self.assertEqual(ann.lookup("timeout").kind, ann.Kind.SPAN)

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

    def test_reading_still_tolerates_a_torn_final_line_without_opening(self):
        # tacet.markers reads logs it never opens for writing; the repair on
        # open must not be the only thing standing between it and a torn tail.
        with self.log() as log:
            log.record("note")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 2')
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


class TestTornTail(unittest.TestCase):
    """Finding a torn final write. Pure: bytes in, split out."""

    def test_an_intact_file_has_no_torn_tail(self):
        self.assertIsNone(ann.torn_tail(b'{"a":1}\n{"b":2}\n'))

    def test_an_empty_file_has_no_torn_tail(self):
        self.assertIsNone(ann.torn_tail(b""))

    def test_the_tail_is_everything_after_the_last_terminator(self):
        self.assertEqual(ann.torn_tail(b'{"a":1}\n{"b"'), ann.TornTail(offset=8, tail=b'{"b"'))

    def test_a_file_with_no_terminator_at_all_is_all_tail(self):
        self.assertEqual(ann.torn_tail(b'{"a"'), ann.TornTail(offset=0, tail=b'{"a"'))

    def test_a_character_cut_in_half_is_still_found(self):
        # Bytes, not text: a write torn inside a multi-byte character must not
        # make the tail undecodable before it can be set aside.
        # The first two of U+2212's three UTF-8 bytes.
        data = b'{"a":1}\n{"t":"\xe2\x88'
        torn = ann.torn_tail(data)
        assert torn is not None
        self.assertEqual(torn.offset, 8)

    def test_a_missing_file_has_no_torn_tail(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(ann.find_torn_tail(Path(tmp) / "nope.jsonl"))


class TestReopeningATornLog(LogTestCase):
    """A crash mid-write leaves an unterminated final line.

    Reading tolerates that, but appending straight after it glues the next entry
    onto the fragment, and the torn line is then in the middle of the file where
    it is corrupt. The restart after that would not start at all.
    """

    TORN = '{"seq": 99, "event": "no'

    def tear(self):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(self.TORN)

    def test_reopening_after_a_torn_write_leaves_a_readable_log(self):
        for _ in range(3):
            with self.log() as log:
                log.record("note")
            self.tear()
        with self.log() as log:
            log.record("note")
        self.assertEqual(len(list(ann.read_entries(self.path))), 4)

    def test_the_prior_anchor_check_survives_a_torn_log_reopened_twice(self):
        # The exact startup path that used to refuse: serve reads the prior
        # anchor before anything else, and scans the whole file when the log has
        # no recording in it - straight through wherever a glued line would be.
        with self.log() as log:
            log.record("band-enters-stadium")
        self.tear()
        with self.log() as log:
            log.record("note")
        self.tear()
        self.assertIsNone(ann.find_prior_anchor(self.path))

    def test_the_torn_bytes_are_set_aside_not_discarded(self):
        with self.log() as log:
            log.record("note")
        self.tear()
        with self.log():
            pass
        aside = self.path.with_name(self.path.name + ann.TORN_SUFFIX)
        self.assertEqual(aside.read_text(encoding="utf-8"), self.TORN + "\n")

    def test_set_aside_tails_accumulate_rather_than_overwrite(self):
        for _ in range(2):
            with self.log() as log:
                log.record("note")
            self.tear()
        with self.log():
            pass
        aside = self.path.with_name(self.path.name + ann.TORN_SUFFIX)
        self.assertEqual(aside.read_text(encoding="utf-8").count(self.TORN), 2)

    def test_a_complete_final_entry_missing_only_its_newline_is_kept(self):
        # A write torn exactly before its terminator lost nothing but the byte.
        with self.log() as log:
            log.record("note")
            log.record("false-open")
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        with self.log() as log:
            self.assertEqual(log.record("note").seq, 3)
            repair = log.repair
        self.assertEqual([e.event for e in ann.read_entries(self.path)], ["note", "false-open", "note"])
        assert repair is not None
        self.assertIsNone(repair.set_aside)

    def test_opening_reports_what_it_repaired(self):
        with self.log() as log:
            log.record("note")
        self.tear()
        with self.log() as log:
            repair = log.repair
        assert repair is not None
        self.assertEqual(repair.tail, self.TORN.encode())
        self.assertEqual(repair.set_aside, self.path.with_name(self.path.name + ann.TORN_SUFFIX))

    def test_an_intact_log_is_left_alone(self):
        with self.log() as log:
            log.record("note")
        before = self.path.read_bytes()
        with self.log() as log:
            self.assertIsNone(log.repair)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name(self.path.name + ann.TORN_SUFFIX).exists())


class TestPriorAnchorIsNoticed(LogTestCase):
    """A log that already holds a recording anchors today's entries to that one.

    Appending is legitimate and nothing is lost, so this is a thing to say, not
    a thing to refuse. What it must never be is quiet: the positions that come
    out are positive and plausible.
    """

    def test_a_missing_log_has_no_prior_anchor(self):
        # The ordinary case: a fresh game, a filename nothing has written yet.
        self.assertIsNone(ann.find_prior_anchor(self.path))

    def test_a_log_without_a_recording_has_no_prior_anchor(self):
        with self.log() as log:
            log.record("band-enters-stadium")
        self.assertIsNone(ann.find_prior_anchor(self.path))

    def test_a_log_with_a_recording_reports_it(self):
        with self.log() as log:
            log.record(ann.ANCHOR_EVENT)
        found = ann.find_prior_anchor(self.path)
        assert found is not None
        self.assertEqual(found.event, ann.ANCHOR_EVENT)

    def test_the_first_recording_is_the_one_reported(self):
        # Matching markers.find_anchor, which is what the warning is about.
        with self.log() as log:
            log.record(ann.ANCHOR_EVENT)
            log.record("band-enters-stands")
            log.record(ann.ANCHOR_EVENT)
        found = ann.find_prior_anchor(self.path)
        assert found is not None
        self.assertEqual(found.seq, 1)

    def test_a_fresh_log_opens_with_no_prior_anchor(self):
        with self.log() as log:
            log.record(ann.ANCHOR_EVENT)
            self.assertIsNone(log.prior_anchor)

    def test_reopening_a_log_notices_the_recording_already_in_it(self):
        with self.log() as log:
            log.record(ann.ANCHOR_EVENT)
        with self.log() as reopened:
            self.assertIsNotNone(reopened.prior_anchor)

    def test_the_anchor_event_is_in_the_vocabulary(self):
        # The warning, the box's own entry and markers.find_anchor all key on
        # this string. A copy that drifted would silence the warning.
        self.assertIn(ann.ANCHOR_EVENT, ann.EVENTS)
