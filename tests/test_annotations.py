import json
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tacet import annotations as ann
from tests.disk import Disk, no_space


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
            "touchdown",
            "field-goal",
            "false-open",
            "missed-entrance",
            "note",
        ):
            self.assertIn(key, ann.EVENTS, f"{key} missing from the vocabulary")

    def test_what_the_band_plays_for_is_in_the_vocabulary(self):
        # The operator's rule (#6): the band almost always plays when something
        # good happens for the home team. Each of those is a reason a ride-in
        # was started, tapped afterwards rather than before the fader moves.
        for key in ("touchdown", "field-goal", "first-down", "defensive-stop"):
            with self.subTest(key):
                self.assertIn(key, ann.EVENTS)
                self.assertTrue(ann.EVENTS[key].button)
                self.assertIsNone(ann.EVENTS[key].action, "a reason records; it does not move the fader")

    def test_the_cannon_is_not_a_button(self):
        # It fires only on a touchdown or a field goal, so it is derivable from
        # one of those offline, and a button for it would compete for a thumb
        # in the busiest ten seconds of the night (#14).
        self.assertNotIn("cannon", ann.EVENTS)

    def test_the_trigger_keys_the_prompts_ask_about_have_names(self):
        # #19: named once, like ANCHOR_EVENT, so the vocabulary and the prompts
        # that key off three of its events cannot drift apart.
        self.assertEqual(ann.BAND_ENTERS_STANDS, "band-enters-stands")
        self.assertEqual(ann.BAND_EXITS_STANDS, "band-exits-stands")
        self.assertEqual(ann.HALFTIME_EXODUS, "halftime-exodus")
        for key in (ann.BAND_ENTERS_STANDS, ann.BAND_EXITS_STANDS, ann.HALFTIME_EXODUS):
            self.assertIn(key, ann.EVENTS)
            self.assertTrue(ann.EVENTS[key].button)
        self.assertIs(ann.EVENTS[ann.HALFTIME_EXODUS].kind, ann.Kind.SPAN)

    def test_the_prompt_keys_are_written_by_the_box_not_tapped(self):
        # #19: five keys because each instant becomes a Reaper marker named
        # after its key, and five identically named markers are unreadable.
        keys = ("prompt-raised", "prompt-accepted", "prompt-dismissed", "prompt-resolved", "prompt-withdrawn")
        for key in keys:
            with self.subTest(key=key):
                event = ann.lookup(key)
                self.assertFalse(event.button)
                self.assertIsNone(event.action)
                self.assertIs(event.category, ann.Category.SESSION)
                self.assertTrue(event.label)
                self.assertNotIn(key, {b.key for b in ann.BUTTONS})
                with self.assertRaises(ann.NotAButtonError):
                    ann.operator_event(key)

    def test_retired_keys_still_read_but_are_no_longer_offered(self):
        # A key is never deleted: `end_span` looks up the event of a span an
        # older log left open, and old logs must go on deriving markers (#14).
        offered = {event.key for event in ann.BUTTONS}
        for key in ("touchdown-sequence", "band-returns-to-stands", "band-in-stands"):
            with self.subTest(key):
                self.assertIn(key, ann.EVENTS)
                self.assertNotIn(key, offered)
                self.assertFalse(ann.EVENTS[key].button)

    def test_the_two_touchdowns_are_different_events(self):
        # One is the band's song starting (game 2), the other the score itself.
        self.assertNotEqual(ann.EVENTS["touchdown"].category, ann.EVENTS["touchdown-sequence"].category)
        self.assertEqual(ann.EVENTS["touchdown"].category, ann.Category.GAME)

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

    def test_this_build_reads_what_it_writes(self):
        self.assertIn(ann.SCHEMA_VERSION, ann.READABLE_SCHEMAS)

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
    def test_records_are_on_disk_once_flushed(self):
        # The log is the source of truth; an entry not on disk is an entry lost.
        with self.log() as log:
            log.record("note", data={"text": "hello"})
            self.assertTrue(log.flush())
            self.assertEqual(len(list(ann.read_entries(self.path))), 1)

    def test_closing_writes_everything_accepted(self):
        with self.log() as log:
            for _ in range(20):
                log.record("note")
        self.assertEqual(len(list(ann.read_entries(self.path))), 20)

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
            self.assertEqual(log.open_spans(), {span: "q1"})
            log.end_span(span)
            self.assertEqual(log.open_spans(), {})

    def test_open_spans_name_their_event_rather_than_leaving_it_in_the_id(self):
        # Keys contain hyphens and ids are `<key>-<seq>`, so an id cannot say
        # whether "timeout-home-12" belongs to "timeout" or "timeout-home".
        with self.log() as log:
            home = log.start_span("timeout-home")
            unspecified = log.start_span("timeout")
            self.assertEqual(log.open_spans(), {home: "timeout-home", unspecified: "timeout"})

    def test_resumed_open_spans_keep_their_event(self):
        with self.log() as log:
            span = log.start_span("halftime-exodus")
        with self.log() as log:
            self.assertEqual(log.open_spans(), {span: "halftime-exodus"})


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


def line(**changes):
    """A valid v1 line, with some fields changed or removed (a value of ...)."""
    raw = json.loads(ann.Entry.build(7, ann.lookup("note"), FakeClock(), data={"text": "x"}).to_json())
    for key, value in changes.items():
        if value is ...:
            del raw[key]
        else:
            raw[key] = value
    return json.dumps(raw)


class TestReadingASchema(unittest.TestCase):
    """What a line must be to be read as an entry (#37).

    `from_json` used to be `cls(**raw)`: no look at `v`, an unknown key fatal,
    and a wrong type let through to crash something later with a bare
    TypeError. A log that cannot be read has to say why, in words.
    """

    def test_a_valid_line_reads(self):
        self.assertEqual(ann.Entry.from_json(line()).seq, 7)

    def test_an_unknown_schema_is_refused_by_name(self):
        with self.assertRaises(ann.SchemaVersionError) as caught:
            ann.Entry.from_json(line(v=2))
        self.assertIn("v2", str(caught.exception))

    def test_an_unknown_schema_is_still_an_unreadable_log(self):
        # So everything that already stops on a corrupt log stops on this too.
        self.assertTrue(issubclass(ann.SchemaVersionError, ann.CorruptLogError))

    def test_a_line_with_no_schema_is_refused(self):
        with self.assertRaises(ann.CorruptLogError):
            ann.Entry.from_json(line(v=...))

    def test_a_schema_that_is_not_a_number_is_refused(self):
        for v in ("1", True, 1.0, None):
            with self.subTest(v=v), self.assertRaises(ann.CorruptLogError):
                ann.Entry.from_json(line(v=v))

    def test_a_field_of_the_wrong_type_is_refused(self):
        wrong: dict[str, list[object]] = {
            "seq": ["7", True, 7.0, None],
            "event": [3, None],
            "category": [3],
            "kind": [None],
            "label": [[]],
            "wall": [0],
            "monotonic": ["12.5", None, True],
            "phase": [3],
            "span_id": [3],
            "project_seconds": ["12.5", True],
            "data": [[], "x", None],
        }
        for field, values in wrong.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ann.CorruptLogError):
                    ann.Entry.from_json(line(**{field: value}))

    def test_a_number_json_cannot_carry_is_refused(self):
        # Python's reader takes a bare NaN; nothing that wrote a v1 log did.
        text = line(monotonic=123456.25).replace('"monotonic": 123456.25', '"monotonic": NaN', 1)
        self.assertIn("NaN", text)
        with self.assertRaises(ann.CorruptLogError):
            ann.Entry.from_json(text)

    def test_an_unknown_field_is_refused_and_named(self):
        # v1 is closed. A new fact per entry goes under `data`, or it is v2.
        with self.assertRaises(ann.CorruptLogError) as caught:
            ann.Entry.from_json(line(tap_wall="2026-09-12T19:00:00+00:00"))
        self.assertIn("tap_wall", str(caught.exception))

    def test_a_required_field_that_is_missing_is_refused_and_named(self):
        for field in ("seq", "event", "category", "kind", "label", "wall", "monotonic"):
            with self.subTest(field=field), self.assertRaises(ann.CorruptLogError) as caught:
                ann.Entry.from_json(line(**{field: ...}))
            self.assertIn(field, str(caught.exception))

    def test_fields_the_writer_always_wrote_but_that_have_defaults_may_be_absent(self):
        entry = ann.Entry.from_json(line(phase=..., span_id=..., project_seconds=..., data=...))
        self.assertEqual((entry.phase, entry.span_id, entry.project_seconds, entry.data), (None, None, None, {}))

    def test_an_integer_position_is_a_position(self):
        self.assertEqual(ann.Entry.from_json(line(project_seconds=12)).project_seconds, 12)


class TestResumingAnUnreadableLog(LogTestCase):
    def test_a_wrong_type_is_a_corrupt_log_not_a_crash(self):
        # `"seq": "7"` used to pass the reader and blow up in `max()`.
        self.path.write_text(line(seq="7") + "\n", encoding="utf-8")
        with self.assertRaises(ann.CorruptLogError):
            self.log().open()

    def test_a_newer_schema_is_refused_on_open(self):
        self.path.write_text(line(v=2) + "\n", encoding="utf-8")
        with self.assertRaises(ann.SchemaVersionError):
            self.log().open()

    def test_a_newer_schema_on_the_last_line_is_not_taken_for_a_torn_write(self):
        # A whole line of a schema this build cannot read is not a fragment,
        # and dropping it as one would be dropping an entry quietly.
        self.path.write_text(line() + "\n" + line(v=2), encoding="utf-8")
        with self.assertRaises(ann.SchemaVersionError):
            list(ann.read_entries(self.path))

    def test_opening_does_not_set_aside_a_whole_line_of_a_newer_schema(self):
        # The repair on open judges an unterminated last line. A whole entry
        # this build cannot read is not a fragment: moving it to `.torn` would
        # take it out of the log and let the box start on what remained.
        before = line() + "\n" + line(v=2)
        self.path.write_text(before, encoding="utf-8")
        with self.assertRaises(ann.SchemaVersionError):
            self.log().open()
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(ann.set_aside_path(self.path).exists())


class TestAClockThatWentBackwards(LogTestCase):
    """`monotonic` restarts from near zero when the machine reboots.

    Offsets are computed from it, so a log that carries on across a reboot has
    entries measured against a clock that no longer exists. Nothing noticed
    (#37). Entries stamped with Reaper's playhead are unaffected; the arithmetic
    fallback is not.
    """

    def write_log_ending_at(self, monotonic):
        with ann.AnnotationLog(self.path, clock=FakeClock(start=monotonic - 1.0)) as log:
            log.record("note")
            log.record("note")

    def test_resume_warns_when_monotonic_regresses(self):
        self.write_log_ending_at(186_000.0)
        with ann.AnnotationLog(self.path, clock=FakeClock(start=40.0)) as log:
            reset = log.clock_reset
        assert reset is not None
        self.assertEqual(reset.monotonic, 186_000.0)

    def test_a_clock_that_kept_counting_is_not_a_reset(self):
        self.write_log_ending_at(100.0)
        with ann.AnnotationLog(self.path, clock=FakeClock(start=500.0)) as log:
            self.assertIsNone(log.clock_reset)

    def test_a_new_log_has_no_clock_to_compare(self):
        with self.log() as log:
            self.assertIsNone(log.clock_reset)

    def test_it_can_be_found_before_the_log_is_opened(self):
        # For the startup banner, which reports the log as the last run left it.
        self.write_log_ending_at(186_000.0)
        reset = ann.find_clock_reset(self.path, now=40.0)
        assert reset is not None
        self.assertEqual(reset.seq, 2)
        self.assertIsNone(ann.find_clock_reset(self.path, now=200_000.0))
        self.assertIsNone(ann.find_clock_reset(self.path.with_name("absent.jsonl"), now=40.0))


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


class TestOperatorInput(unittest.TestCase):
    """What an operator - or anything posting to the page's routes - may ask
    the log to record. Checked before anything moves (#36)."""

    def test_every_box_only_event_is_refused(self):
        # A posted `recording-started` becomes the anchor `tacet.markers`
        # measures the whole timeline from.
        box_only = [event.key for event in ann.VOCABULARY if not event.button]
        self.assertIn(ann.ANCHOR_EVENT, box_only)
        for key in box_only:
            with self.subTest(key=key), self.assertRaises(ann.NotAButtonError):
                ann.operator_event(key)

    def test_every_button_is_accepted(self):
        for event in ann.BUTTONS:
            with self.subTest(key=event.key):
                self.assertEqual(ann.operator_event(event.key), event)

    def test_an_unknown_event_is_still_unknown(self):
        with self.assertRaises(ann.UnknownEventError):
            ann.operator_event("no-such-event")

    def test_no_data_is_empty_data(self):
        self.assertEqual(ann.operator_data(None), {})

    def test_a_note_is_accepted(self):
        self.assertEqual(ann.operator_data({"text": "band sounds thin"}), {"text": "band sounds thin"})

    def test_scalars_are_accepted(self):
        data = {"n": 3, "x": 1.5, "ok": True, "none": None}
        self.assertEqual(ann.operator_data(data), data)

    def test_data_that_is_not_an_object_is_refused(self):
        # A string used to raise inside the log, after a fader button had
        # already moved; a list of pairs was quietly stored as an object.
        for data in ("hello", ["ab"], 3, True):
            with self.subTest(data=data), self.assertRaises(ann.DataError):
                ann.operator_data(data)

    def test_nested_values_are_refused(self):
        for value in ({"a": 1}, [1, 2]):
            with self.subTest(value=value), self.assertRaises(ann.DataError):
                ann.operator_data({"text": value})

    def test_keys_must_be_strings(self):
        with self.assertRaises(ann.DataError):
            ann.operator_data({1: "a"})

    def test_numbers_json_cannot_carry_are_refused(self):
        # Python's JSON reader accepts NaN and Infinity; the log's writer must not.
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ann.DataError):
                ann.operator_data({"x": value})

    def test_text_is_bounded(self):
        ann.operator_data({"text": "x" * ann.MAX_DATA_TEXT})
        with self.assertRaises(ann.DataError):
            ann.operator_data({"text": "x" * (ann.MAX_DATA_TEXT + 1)})

    def test_what_is_returned_is_a_copy(self):
        data = {"text": "a"}
        checked = ann.operator_data(data)
        data["text"] = "b"
        self.assertEqual(checked, {"text": "a"})


class TestEntriesAreJson(LogTestCase):
    def test_an_entry_json_cannot_carry_is_not_written(self):
        # `NaN` is not JSON, and a line holding it is one other readers of the
        # log - Lua, jq, a browser - refuse.
        with self.log() as log:
            log.record("note", data={"text": "before"})
            with self.assertRaises(ann.WriteError):
                log.record("note", project_seconds=float("nan"))
            self.assertFalse(log.health.healthy)
            self.assertIn("note", log.health.error)
            log.record("note", data={"text": "after"})
            log.flush()
            self.assertTrue(log.health.healthy)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line, parse_constant=lambda name: self.fail(f"{name} in the log"))


class TestAFailingDisk(LogTestCase):
    """A disk that fills in the third quarter. The write fails; the box must
    say so, must not glue the next entry onto whatever part of the line landed,
    and must start saving again once there is room.

    Writes happen on the log's own thread (#41), so a tap is accepted before
    the disk has been tried. Each test flushes before it looks, and before it
    changes the disk under a write that has not happened yet.
    """

    def setUp(self):
        super().setUp()
        self.disk = Disk()

    def log(self, mirror=None):
        return ann.AnnotationLog(self.path, clock=self.clock, opener=self.disk.open, mirror=mirror)

    def test_a_failed_write_is_accepted_then_reported(self):
        with self.log() as log:
            self.disk.full = True
            entry = log.record("note", data={"text": "lost"})
            self.assertEqual(entry.seq, 1)
            log.flush()
            self.assertFalse(log.health.healthy)
            self.assertIn("No space left on device", log.health.error)

    def test_a_failed_write_is_on_the_log_health_and_stays_counted(self):
        with self.log() as log:
            self.assertTrue(log.health.healthy)
            self.disk.full = True
            log.record("note")
            log.flush()
            self.assertFalse(log.health.healthy)
            self.disk.full = False
            log.record("note")
            log.flush()
            # Saving again, and says so; the entry it cost is still counted.
            self.assertTrue(log.health.healthy)
            self.assertEqual(log.health.failures, 1)

    def test_the_next_entry_is_not_glued_onto_a_torn_write(self):
        # The in-run version of #26: the handle stays open after a failure, so
        # the next line would land straight after the fragment.
        with self.log() as log:
            log.record("note", data={"text": "kept"})
            log.flush()
            self.disk.full = self.disk.tears = True
            log.record("note", data={"text": "torn"})
            log.flush()
            self.disk.full = False
            log.record("note", data={"text": "after"})
        texts = [entry.data["text"] for entry in ann.read_entries(self.path)]
        self.assertEqual(texts, ["kept", "after"])
        # Set aside, never deleted.
        self.assertIn("torn", ann.set_aside_path(self.path).read_text())

    def test_a_log_file_that_went_away_is_not_recreated(self):
        # A fresh empty log where the game's log was would take the rest of the
        # night's entries somewhere nobody would look, and look healthy doing it.
        with self.log() as log:
            log.record("note")
            self.disk.full = True
            log.record("note")
            log.flush()
            self.disk.full = False
            self.path.unlink()
            log.record("note")
            log.flush()
            self.assertFalse(self.path.exists())
            self.assertFalse(log.health.healthy)

    def test_a_span_whose_start_was_not_saved_is_closed_again(self):
        with self.log() as log:
            self.disk.full = True
            span = log.start_span("q3")
            self.assertIn(span, log.open_spans())
            log.flush()
            self.assertEqual(len(log.open_spans()), 0)

    def test_a_span_whose_end_was_not_saved_is_open_again(self):
        # So the button reads (end) again, and tapping it again saves the end.
        with self.log() as log:
            span = log.start_span("q3")
            log.flush()
            self.disk.full = True
            log.end_span(span)
            self.assertNotIn(span, log.open_spans())
            log.flush()
            self.assertIn(span, log.open_spans())
            self.disk.full = False
            log.end_span(span)
            log.flush()
            self.assertNotIn(span, log.open_spans())
        phases = [entry.phase for entry in ann.read_entries(self.path)]
        self.assertEqual(phases, [ann.PHASE_START, ann.PHASE_END])

    def test_a_span_that_saved_stays_closed(self):
        with self.log() as log:
            span = log.end_span(log.start_span("q3")).span_id
            log.flush()
            self.assertNotIn(span, log.open_spans())

    def test_a_failing_mirror_does_not_cost_the_log_entry(self):
        # The queue is a regenerable view of the log. Its failure must neither
        # stop the entry being written nor fail the write that did land.
        class BrokenMirror:
            def write(self, entry):
                raise no_space()

            def sync(self):
                pass

        with self.log(mirror=BrokenMirror()) as log:
            entry = log.record("note", data={"text": "kept"})
            log.flush()
            self.assertTrue(log.health.healthy)
            self.assertFalse(log.mirror_health.healthy)
            self.assertIn("No space left on device", log.mirror_health.error)
        self.assertEqual([e.seq for e in ann.read_entries(self.path)], [entry.seq])

    def test_a_mirror_is_not_written_an_entry_the_log_failed_to_save(self):
        written = []

        class Mirror:
            def write(self, entry):
                written.append(entry.event)

            def sync(self):
                pass

        with self.log(mirror=Mirror()) as log:
            self.disk.full = True
            log.record("note")
            log.flush()
        self.assertEqual(written, [])


class Gate:
    """A sync that holds the writer thread until the test lets it go."""

    #: Long enough never to be the reason a test passes, short enough that a
    #: test that forgets to open the gate fails rather than hangs.
    HOLD_SECONDS = 10.0

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()
        self.calls = 0

    def __call__(self, fd: int) -> None:
        self.calls += 1
        self.entered.set()
        self.opened.wait(self.HOLD_SECONDS)

    def open(self) -> None:
        self.opened.set()


class Killed(BaseException):
    """What nothing in the writer is allowed to catch: it ends the thread."""


class TestTheWriterThread(LogTestCase):
    """#41: the disk is touched on one thread, never on the caller's."""

    def log(self, **options):
        return ann.AnnotationLog(self.path, clock=self.clock, **options)

    def test_a_slow_sync_does_not_hold_up_the_caller(self):
        gate = Gate()
        with self.log(sync=gate) as log:
            self.addCleanup(gate.open)
            log.record("note")
            self.assertTrue(gate.entered.wait(Gate.HOLD_SECONDS))
            # The thread is stuck in a sync, and these still return at once.
            entries = [log.record("note") for _ in range(5)]
            self.assertEqual([entry.seq for entry in entries], [2, 3, 4, 5, 6])
            gate.open()
        self.assertEqual(len(list(ann.read_entries(self.path))), 6)

    def test_one_sync_covers_every_line_queued_behind_it(self):
        gate = Gate()
        with self.log(sync=gate) as log:
            self.addCleanup(gate.open)
            log.record("note")
            self.assertTrue(gate.entered.wait(Gate.HOLD_SECONDS))
            for _ in range(9):
                log.record("note")
            gate.open()
            log.flush()
        self.assertEqual(gate.calls, 2)

    def test_entries_land_in_the_order_they_were_accepted(self):
        with self.log() as log:
            accepted = [log.record("note").seq for _ in range(50)]
        self.assertEqual([entry.seq for entry in ann.read_entries(self.path)], accepted)

    def test_the_mirror_is_written_only_after_the_log_is_synced(self):
        # So the queue never gets ahead of the log.
        happened = []

        class Mirror:
            def write(self, entry):
                happened.append(("mirror", entry.seq))

            def sync(self):
                pass

        def sync(fd):
            happened.append(("log synced", None))

        with self.log(sync=sync, mirror=Mirror()) as log:
            log.record("note")
            log.flush()
            log.record("note")
        self.assertEqual(happened, [("log synced", None), ("mirror", 1), ("log synced", None), ("mirror", 2)])

    def test_the_writer_survives_an_unexpected_exception(self):
        calls = []

        def sync(fd):
            calls.append(fd)
            if len(calls) == 1:
                raise RuntimeError("not an OSError")

        with self.log(sync=sync) as log:
            log.record("note", data={"text": "unlucky"})
            log.flush()
            self.assertFalse(log.health.healthy)
            self.assertIn("RuntimeError", log.health.error)
            log.record("note", data={"text": "saved"})
            self.assertTrue(log.flush())
            self.assertTrue(log.health.healthy)
            self.assertEqual(log.health.failures, 1)
        self.assertIn("saved", [entry.data["text"] for entry in ann.read_entries(self.path)])

    def test_a_failing_wake_up_does_not_stop_the_writer(self):
        def wake():
            raise RuntimeError("listener is broken")

        with self.log() as log:
            log.when_written(wake)
            log.record("note")
            log.flush()
            log.record("note")
            self.assertTrue(log.flush())
            self.assertTrue(log.health.healthy)

    def test_a_dead_writer_is_reported_and_refuses_further_entries(self):
        def sync(fd):
            raise Killed

        woken = threading.Event()
        # The thread's death is the point; its traceback is not.
        with mock.patch.object(threading, "excepthook", lambda _: None), self.log(sync=sync) as log:
            log.when_written(woken.set)
            log.record("note")
            self.assertFalse(log.flush())
            self.assertTrue(woken.is_set())
            self.assertEqual(log.health.error, ann.WRITER_STOPPED)
            # The entry it died holding, and nothing for the stop itself.
            self.assertEqual(log.health.failures, 1)
            with self.assertRaises(ann.WriteError):
                log.record("note")
            self.assertEqual(log.health.failures, 2)

    def test_a_writer_stopped_by_closing_is_not_reported_dead(self):
        log = self.log().open()
        log.record("note")
        log.close()
        self.assertTrue(log.health.healthy)


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


class TestCarriedOverSpansAreNoticed(LogTestCase):
    """Spans a previous run left open, found before the log is opened (#20).

    Resuming them is right - a restarted box mid-quarter must still be able to
    end the quarter - but on a log that was meant to be fresh it means last
    game's `q4` reads "(end)" on the page, so the box says so at startup.
    """

    def test_a_missing_log_has_none(self):
        self.assertEqual(ann.find_open_spans(self.path), [])

    def test_a_closed_span_is_not_reported(self):
        with self.log() as log:
            log.end_span(log.start_span("q3"))
        self.assertEqual(ann.find_open_spans(self.path), [])

    def test_an_open_span_is_reported_by_its_start(self):
        with self.log() as log:
            log.end_span(log.start_span("q3"))
            span = log.start_span("q4")
        found = ann.find_open_spans(self.path)
        self.assertEqual([entry.span_id for entry in found], [span])
        self.assertEqual(found[0].event, "q4")

    def test_they_come_back_in_the_order_they_started(self):
        with self.log() as log:
            first = log.start_span("halftime")
            second = log.start_span("timeout-home")
        self.assertEqual([entry.span_id for entry in ann.find_open_spans(self.path)], [first, second])

    def test_it_agrees_with_what_opening_the_log_resumes(self):
        with self.log() as log:
            log.start_span("q4")
            log.end_span(log.start_span("timeout"))
        with self.log() as reopened:
            resumed = reopened.open_spans()
        self.assertEqual({entry.span_id: entry.event for entry in ann.find_open_spans(self.path)}, resumed)
