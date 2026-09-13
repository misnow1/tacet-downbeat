import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tacet import annotations as ann
from tacet import mirror


class StepClock:
    def __init__(self):
        self._m = 0.0

    def now(self):
        return datetime(2026, 9, 13, 19, 0, 0, tzinfo=UTC)

    def monotonic(self):
        self._m += 1.0
        return self._m


def entry(key, **kwargs):
    return ann.Entry.build(1, ann.lookup(key), StepClock(), **kwargs)


class TestQueueLine(unittest.TestCase):
    def test_an_instant_uses_the_absent_sentinel_for_span_fields(self):
        line = mirror.queue_line(entry("note"))
        self.assertEqual(line, f"NOTE|note{mirror.DELIMITER}-{mirror.DELIMITER}-")

    def test_a_span_start_carries_its_phase_and_id(self):
        line = mirror.queue_line(entry("q1", phase="start", span_id="q1-2"))
        name, phase, span_id = line.split(mirror.DELIMITER)
        self.assertEqual((name, phase, span_id), ("GAME|q1", "start", "q1-2"))

    def test_no_field_may_contain_the_delimiter(self):
        # The Lua side splits on tabs; a tab in a note would desynchronise it.
        bad = entry("note", data={"text": "x"})
        bad = ann.Entry(**{**bad.as_dict(), "event": "no\tte"})
        with self.assertRaises(mirror.QueueFormatError):
            mirror.queue_line(bad)

    def test_no_field_may_contain_a_newline(self):
        bad = entry("note")
        bad = ann.Entry(**{**bad.as_dict(), "event": "no\nte"})
        with self.assertRaises(mirror.QueueFormatError):
            mirror.queue_line(bad)

    def test_round_trips(self):
        original = entry("q1", phase="start", span_id="q1-2")
        item = mirror.parse_queue_line(mirror.queue_line(original))
        self.assertEqual(item.name, "GAME|q1")
        self.assertEqual(item.phase, "start")
        self.assertEqual(item.span_id, "q1-2")

    def test_absent_fields_come_back_as_none(self):
        item = mirror.parse_queue_line(mirror.queue_line(entry("note")))
        self.assertIsNone(item.phase)
        self.assertIsNone(item.span_id)

    def test_a_short_line_is_refused(self):
        with self.assertRaises(mirror.QueueFormatError):
            mirror.parse_queue_line("only-one-field")


class TestRebuild(unittest.TestCase):
    def test_regenerates_the_whole_queue_from_a_log(self):
        clock = StepClock()
        entries = [
            ann.Entry.build(1, ann.lookup("recording-started"), clock),
            ann.Entry.build(2, ann.lookup("note"), clock),
        ]
        text = mirror.rebuild_queue(entries)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertTrue(text.endswith("\n"))


class TestMirrorQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "queue.tsv"

    def test_appends_one_line_per_entry(self):
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("note"))
            queue.append(entry("false-open"))
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_lines_are_durable_immediately(self):
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("note"))
            self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_every_line_ends_with_a_newline(self):
        # The Lua tail only consumes complete lines; a missing terminator would
        # strand the last event until the next one arrived.
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("note"))
        self.assertTrue(self.path.read_text().endswith("\n"))

    def test_reopening_after_a_torn_line_starts_on_a_fresh_line(self):
        # Appending after a fragment would glue the next line onto it; the Lua
        # tail would then read one bogus line with too many fields.
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("note"))
        with self.path.open("a") as fh:
            fh.write("FDR|up-wh")
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("false-open"))
        lines = self.path.read_text().splitlines()
        self.assertEqual([mirror.parse_queue_line(line).name for line in lines], ["NOTE|note", "DET|false-open"])

    def test_a_torn_queue_line_is_set_aside_even_when_it_looks_complete(self):
        # The queue is regenerable, and three fields can still be a truncated
        # span id, so nothing unterminated is trusted.
        with self.path.open("w") as fh:
            fh.write("GAME|q1\tstart\tq1-")
        with mirror.MirrorQueue(self.path) as queue:
            repair = queue.repair
        assert repair is not None
        self.assertEqual(self.path.read_text(), "")
        self.assertEqual(repair.set_aside, self.path.with_name(self.path.name + ann.TORN_SUFFIX))

    def test_an_intact_queue_is_left_alone(self):
        with mirror.MirrorQueue(self.path) as queue:
            queue.append(entry("note"))
        with mirror.MirrorQueue(self.path) as queue:
            self.assertIsNone(queue.repair)


class TestLoggerIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_log_with_a_mirror_writes_both(self):
        log_path = self.root / "game.jsonl"
        queue_path = self.root / "queue.tsv"
        with (
            mirror.MirrorQueue(queue_path) as queue,
            ann.AnnotationLog(log_path, clock=StepClock(), mirror=queue) as log,
        ):
            log.record("note")
            span = log.start_span("q1")
            log.end_span(span)
        self.assertEqual(len(list(ann.read_entries(log_path))), 3)
        self.assertEqual(len(queue_path.read_text().splitlines()), 3)

    def test_a_log_without_a_mirror_still_works(self):
        log_path = self.root / "game.jsonl"
        with ann.AnnotationLog(log_path, clock=StepClock()) as log:
            log.record("note")
        self.assertEqual(len(list(ann.read_entries(log_path))), 1)

    def test_the_queue_can_be_rebuilt_to_match_what_was_written_live(self):
        # The mirror is a derived view; regenerating it must reproduce it.
        log_path = self.root / "game.jsonl"
        queue_path = self.root / "queue.tsv"
        with (
            mirror.MirrorQueue(queue_path) as queue,
            ann.AnnotationLog(log_path, clock=StepClock(), mirror=queue) as log,
        ):
            log.record("recording-started")
            log.end_span(log.start_span("q2"))
            log.record("touchdown-sequence")
        rebuilt = mirror.rebuild_queue(ann.read_entries(log_path))
        self.assertEqual(rebuilt, queue_path.read_text())


if __name__ == "__main__":
    unittest.main()
