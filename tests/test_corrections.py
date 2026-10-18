import json
import tempfile
import unittest
from pathlib import Path

from tacet import annotations as ann
from tacet import corrections
from tacet.corrections import Add, CorrectionsError, Supersede

WALL = "2026-10-17T22:02:56.995609+00:00"
OTHER_WALL = "2026-10-17T22:09:02.595268+00:00"


def supersede(**changes: object) -> Supersede:
    base: dict[str, object] = {"seq": 637, "source": "rtd", "reason": "the clock counted down", "wall": WALL}
    return Supersede(**{**base, **changes})  # type: ignore[arg-type]


def add_end(**changes: object) -> Add:
    base: dict[str, object] = {
        "event": "timeout",
        "wall": WALL,
        "source": "rtd",
        "reason": "the span was never ended",
        "phase": "end",
        "span_id": "timeout-590",
    }
    return Add(**{**base, **changes})  # type: ignore[arg-type]


def line(**fields: object) -> str:
    raw: dict[str, object] = {"v": 1, "action": "supersede", "seq": 7, "source": "rtd", "reason": "r", "wall": WALL}
    raw.update(fields)
    return json.dumps({k: v for k, v in raw.items() if v is not ...})


class TestSidecarPath(unittest.TestCase):
    def test_it_sits_beside_the_log_and_is_named_from_its_stem(self):
        path = corrections.sidecar_path(Path("/games/2026-10-17-gt.jsonl"))
        self.assertEqual(path, Path("/games/2026-10-17-gt.corrections.jsonl"))

    def test_the_suffix_is_pinned(self):
        self.assertEqual(corrections.SIDECAR_SUFFIX, ".corrections.jsonl")
        self.assertEqual(corrections.CORRECTIONS_SCHEMA_VERSION, 1)


class TestRoundTrip(unittest.TestCase):
    def test_supersede_round_trips(self):
        item = supersede(evidence={"capture": "g.pcap", "superseded_wall": OTHER_WALL})
        self.assertEqual(Supersede.from_json(item.to_json()), item)

    def test_add_round_trips_with_every_optional_field(self):
        item = add_end(project_seconds=12.5, data={"note": "x"}, evidence={"capture": "g.pcap"})
        self.assertEqual(Add.from_json(item.to_json()), item)

    def test_an_event_only_supersede_round_trips(self):
        item = supersede(wall=None, event="timeout-away")
        self.assertEqual(Supersede.from_json(item.to_json()), item)

    def test_the_line_is_compact_sorted_and_ascii(self):
        text = supersede(evidence={"b": 1, "a": 2}).to_json()
        self.assertNotIn(", ", text)
        self.assertNotIn(": ", text)
        self.assertTrue(text.isascii())
        keys = list(json.loads(text))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(list(json.loads(text)["evidence"]), ["a", "b"])

    def test_absent_optional_fields_are_left_out(self):
        self.assertEqual(
            set(json.loads(supersede().to_json())),
            {"v", "action", "seq", "source", "reason", "wall"},
        )

    def test_the_dispatcher_reads_either_action(self):
        self.assertEqual(corrections.correction_from_json(supersede().to_json()), supersede())
        self.assertEqual(corrections.correction_from_json(add_end().to_json()), add_end())

    def test_nan_cannot_be_written(self):
        with self.assertRaises(ValueError):
            add_end(project_seconds=float("nan")).to_json()


class TestRejected(unittest.TestCase):
    def rejects(self, text: str) -> str:
        with self.assertRaises(CorrectionsError) as caught:
            corrections.correction_from_json(text)
        return str(caught.exception)

    def test_a_correction_error_is_an_annotation_error(self):
        self.assertTrue(issubclass(CorrectionsError, ann.AnnotationError))

    def test_a_missing_or_unknown_version(self):
        self.assertIn("'v'", self.rejects(line(v=...)))
        self.assertIn("v2", self.rejects(line(v=2)))
        self.assertIn("whole number", self.rejects(line(v="1")))

    def test_an_unknown_action(self):
        self.assertIn("action", self.rejects(line(action="delete")))
        self.assertIn("action", self.rejects(line(action=...)))

    def test_an_unknown_key(self):
        self.assertIn("surprise", self.rejects(line(surprise=1)))
        self.assertIn("phase", self.rejects(line(phase="end")))

    def test_not_json_and_not_an_object(self):
        self.assertIn("not JSON", self.rejects("{nope"))
        self.assertIn("not an object", self.rejects("[1]"))

    def test_a_supersede_needs_a_wall_or_an_event(self):
        self.assertIn("wall or an event", self.rejects(line(wall=...)))

    def test_seq_must_be_a_whole_number_and_not_a_bool(self):
        self.assertIn("seq", self.rejects(line(seq="7")))
        self.assertIn("seq", self.rejects(line(seq=True)))
        self.assertIn("seq", self.rejects(line(seq=...)))

    def test_a_wall_must_be_an_aware_iso_time(self):
        self.assertIn("wall", self.rejects(line(wall="2026-10-17T22:02:56")))
        self.assertIn("wall", self.rejects(line(wall="yesterday")))
        self.assertIn("wall", self.rejects(line(wall=5)))

    def test_an_event_must_be_in_the_vocabulary(self):
        self.assertIn("timeout-lunch", self.rejects(line(wall=..., event="timeout-lunch")))

    def test_source_and_reason_must_be_present_and_not_empty(self):
        for name in ("source", "reason"):
            self.assertIn(name, self.rejects(line(**{name: ""})))
            self.assertIn(name, self.rejects(line(**{name: ...})))

    def test_evidence_must_be_an_object(self):
        self.assertIn("evidence", self.rejects(line(evidence=[1])))

    def add_line(self, **fields: object) -> str:
        raw: dict[str, object] = {
            "v": 1,
            "action": "add",
            "event": "timeout",
            "wall": WALL,
            "phase": "end",
            "span_id": "timeout-9",
            "source": "rtd",
            "reason": "r",
        }
        raw.update(fields)
        return json.dumps({k: v for k, v in raw.items() if v is not ...})

    def test_a_span_add_needs_a_phase_and_a_span_id(self):
        self.assertIn("phase", self.rejects(self.add_line(phase=...)))
        self.assertIn("span_id", self.rejects(self.add_line(span_id=...)))
        self.assertIn("phase", self.rejects(self.add_line(phase="middle")))

    def test_an_instant_add_forbids_a_phase_and_a_span_id(self):
        instant = {"event": "touchdown", "phase": ..., "span_id": ...}
        self.assertEqual(corrections.correction_from_json(self.add_line(**instant)).event, "touchdown")
        self.assertIn("phase", self.rejects(self.add_line(event="touchdown", span_id=...)))
        self.assertIn("span_id", self.rejects(self.add_line(event="touchdown", phase=...)))

    def test_an_add_needs_an_event_and_a_wall(self):
        self.assertIn("event", self.rejects(self.add_line(event=...)))
        self.assertIn("wall", self.rejects(self.add_line(wall=...)))

    def test_project_seconds_and_data_are_checked(self):
        self.assertIn("project_seconds", self.rejects(self.add_line(project_seconds="3")))
        self.assertIn("project_seconds", self.rejects(self.add_line().replace('"r"', '"r", "project_seconds": NaN')))
        self.assertIn("data", self.rejects(self.add_line(data=[])))

    def test_each_class_refuses_the_other_action(self):
        with self.assertRaises(CorrectionsError):
            Supersede.from_json(add_end().to_json())
        with self.assertRaises(CorrectionsError):
            Add.from_json(supersede().to_json())


class TestReadAndAppend(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "game.corrections.jsonl"

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(corrections.read_corrections(self.path), [])

    def test_blank_lines_are_skipped(self):
        self.path.write_text(f"\n{supersede().to_json()}\n\n{add_end().to_json()}\n", encoding="utf-8")
        self.assertEqual(corrections.read_corrections(self.path), [supersede(), add_end()])

    def test_a_bad_line_is_named_by_its_number(self):
        self.path.write_text(f"{supersede().to_json()}\n\n{{nope\n", encoding="utf-8")
        with self.assertRaises(CorrectionsError) as caught:
            corrections.read_corrections(self.path)
        self.assertIn("line 3", str(caught.exception))

    def test_append_creates_the_file_and_then_appends(self):
        corrections.append_corrections(self.path, [supersede()])
        corrections.append_corrections(self.path, [add_end()])
        self.assertEqual(corrections.read_corrections(self.path), [supersede(), add_end()])
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_append_repairs_a_missing_final_newline(self):
        self.path.write_text(supersede().to_json(), encoding="utf-8")
        corrections.append_corrections(self.path, [add_end()])
        self.assertEqual(corrections.read_corrections(self.path), [supersede(), add_end()])

    def test_appending_nothing_creates_no_file(self):
        corrections.append_corrections(self.path, [])
        self.assertFalse(self.path.exists())


class TestTargetsAndUnrecorded(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(corrections.targets(supersede()), (("supersede", 637, "wall"),))
        by_event = supersede(wall=None, event="timeout-away")
        self.assertEqual(corrections.targets(by_event), (("supersede", 637, "event"),))
        both = supersede(event="timeout-away")
        self.assertEqual(corrections.targets(both), (("supersede", 637, "wall"), ("supersede", 637, "event")))
        self.assertEqual(corrections.targets(add_end()), (("add", "timeout-590", "end"),))

    def test_an_existing_wall_supersede_of_the_same_seq_suppresses_a_proposal(self):
        proposal = supersede()
        by_hand = supersede(source="post-game review", wall=OTHER_WALL)
        fresh, already = corrections.unrecorded([proposal], [by_hand])
        self.assertEqual((fresh, already), ([], [proposal]))

    def test_an_event_only_supersede_does_not_suppress_a_wall_proposal(self):
        proposal = supersede()
        fresh, already = corrections.unrecorded([proposal], [supersede(wall=None, event="timeout-away")])
        self.assertEqual((fresh, already), ([proposal], []))

    def test_a_different_seq_is_not_suppressed(self):
        fresh, already = corrections.unrecorded([supersede(seq=1)], [supersede(seq=2)])
        self.assertEqual((len(fresh), len(already)), (1, 0))

    def test_an_existing_add_of_the_same_span_end_suppresses(self):
        fresh, already = corrections.unrecorded([add_end()], [add_end(source="post-game review")])
        self.assertEqual((fresh, already), ([], [add_end()]))

    def test_writing_twice_adds_nothing(self):
        proposals: list[corrections.Correction] = [supersede(), add_end()]
        fresh, _ = corrections.unrecorded(proposals, [])
        again, already = corrections.unrecorded(proposals, fresh)
        self.assertEqual((again, already), ([], proposals))


if __name__ == "__main__":
    unittest.main()
