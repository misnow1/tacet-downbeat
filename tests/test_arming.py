"""Whether every track but Pilot is armed to record (#167).

Pure core: parse the track lines of the mirror's report, judge them, and say
nothing until the report is believed (that rule is mirror_report's).
"""

import unittest
from pathlib import Path

from tacet import arming
from tacet import mirror_report as mr

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_TRACKS = FIXTURES / "report-v1-tracks.report"
GOLDEN_OLD = FIXTURES / "report-v1-checksum.report"

HEAD: tuple[tuple[str, str], ...] = (("version", "1"), ("seq", "1"))


def fields(count: int | str, *lines: str) -> tuple[tuple[str, str], ...]:
    return (*HEAD, ("tracks", str(count)), *(("track", line) for line in lines))


def track(number: int, armed: bool, name: str) -> arming.Track:
    return arming.Track(number, armed, name)


def build(*specs: tuple[bool, str]) -> tuple[arming.Track, ...]:
    return tuple(track(index + 1, armed, name) for index, (armed, name) in enumerate(specs))


def report(seq: int, *pairs: tuple[str, str]) -> mr.Report:
    body = tuple(pairs) if pairs else fields(0)
    return mr.Report(version=1, seq=seq, project="/p.RPP", record_path="/m", fields=body)


def witnessed(first: mr.Report, second: mr.Report) -> mr.Observed:
    return mr.observe(mr.observe(None, first, 0.0), second, 1.0)


def golden(path: Path) -> mr.Report:
    return mr.parse_report(path.read_text(encoding="utf-8"))


def believed(*pairs: tuple[str, str]) -> mr.Observed:
    return witnessed(report(1, *pairs), report(2, *pairs))


class TestParsing(unittest.TestCase):
    def test_the_golden_file_parses_to_four_tracks(self):
        got = arming.parse_tracks(golden(GOLDEN_TRACKS).fields)
        self.assertEqual(
            got,
            (
                track(1, True, "MV Conductor"),
                track(2, False, "MV Spare"),
                track(3, True, "Pilot Reference"),
                track(4, False, "Pilot"),
            ),
        )

    def test_a_script_from_before_this_field_gives_none(self):
        self.assertIsNone(arming.parse_tracks(golden(GOLDEN_OLD).fields))

    def test_zero_tracks_is_an_empty_tuple(self):
        self.assertEqual(arming.parse_tracks(fields(0)), ())

    def test_each_of_these_is_unreadable(self):
        cases = {
            "more lines than the count": fields(1, "1\t1\tA", "2\t1\tB"),
            "fewer lines than the count": fields(2, "1\t1\tA"),
            "out of order": fields(2, "2\t1\tB", "1\t1\tA"),
            "duplicated": fields(2, "1\t1\tA", "1\t1\tB"),
            "starts at zero": fields(2, "0\t1\tA", "1\t1\tB"),
            "number not numeric": fields(1, "x\t1\tA"),
            "flag is a float": fields(1, "1\t1.0\tA"),
            "flag is 2": fields(1, "1\t2\tA"),
            "flag is empty": fields(1, "1\t\tA"),
            "too few parts": fields(1, "1\t1"),
            "no parts": fields(1, ""),
            "negative count": fields(-1),
            "count not numeric": fields("abc"),
        }
        for label, pairs in cases.items():
            with self.subTest(label):
                self.assertIsInstance(arming.parse_tracks(pairs), mr.Unreadable)

    def test_a_tab_in_a_name_is_kept_and_an_empty_name_is_empty(self):
        got = arming.parse_tracks(fields(2, "1\t1\tHorns\t4", "2\t0\t"))
        self.assertEqual(got, (track(1, True, "Horns\t4"), track(2, False, "")))

    def test_a_garbled_tracks_block_leaves_the_project_saved(self):
        bad = fields(2, "1\t1\tA")
        got = mr.assess(witnessed(report(1, *bad), report(2, *bad)), 1.0, "")
        self.assertEqual(got.status, mr.Status.SAVED)


class TestJudging(unittest.TestCase):
    def test_all_armed_but_pilot_is_armed(self):
        got = arming.judge(build((True, "A"), (True, "B"), (False, "Pilot")), None)
        self.assertEqual((got.status, got.armed, got.tracks), (arming.ArmStatus.ARMED, 2, 3))
        self.assertEqual(got.unarmed, ())

    def test_one_unarmed_is_listed(self):
        got = arming.judge(build((True, "A"), (False, "B"), (False, "Pilot")), None)
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)
        self.assertEqual(got.unarmed, (track(2, False, "B"),))

    def test_several_are_listed_in_order(self):
        got = arming.judge(build((False, "A"), (True, "B"), (False, "C"), (False, "Pilot")), None)
        self.assertEqual([t.number for t in got.unarmed], [1, 3])

    def test_an_armed_pilot_is_armed_and_counted(self):
        got = arming.judge(build((True, "A"), (True, "Pilot")), None)
        self.assertEqual((got.status, got.armed), (arming.ArmStatus.ARMED, 2))

    def test_pilot_reference_is_not_exempt(self):
        got = arming.judge(build((True, "A"), (False, "Pilot Reference"), (False, "Pilot")), None)
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)
        self.assertEqual([t.name for t in got.unarmed], ["Pilot Reference"])

    def test_pilot_is_matched_exactly_after_strip(self):
        self.assertTrue(arming.is_pilot(" Pilot "))
        self.assertFalse(arming.is_pilot("pilot"))
        self.assertFalse(arming.is_pilot("PILOT"))
        got = arming.judge(build((True, "A"), (False, " Pilot ")), None)
        self.assertEqual(got.status, arming.ArmStatus.ARMED)
        got = arming.judge(build((True, "A"), (False, "pilot")), None)
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)

    def test_no_pilot_and_all_armed_is_armed_and_not_ambiguous(self):
        got = arming.judge(build((True, "A"), (True, "B")), None)
        self.assertEqual(got.status, arming.ArmStatus.ARMED)
        self.assertFalse(got.pilot_ambiguous)
        self.assertEqual(got.pilots, ())

    def test_two_unarmed_pilots_are_both_listed_and_ambiguous(self):
        got = arming.judge(build((True, "A"), (False, "Pilot"), (False, "Pilot")), None)
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)
        self.assertEqual([t.number for t in got.unarmed], [2, 3])
        self.assertTrue(got.pilot_ambiguous)

    def test_two_pilots_one_armed_lists_the_unarmed_one(self):
        got = arming.judge(build((True, "A"), (True, "Pilot"), (False, "Pilot")), None)
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)
        self.assertEqual([t.number for t in got.unarmed], [3])
        self.assertTrue(got.pilot_ambiguous)

    def test_zero_tracks_is_no_tracks(self):
        got = arming.judge((), None)
        self.assertEqual((got.status, got.armed, got.tracks), (arming.ArmStatus.NO_TRACKS, 0, 0))

    def test_an_unnamed_unarmed_track_is_listed_with_an_empty_name(self):
        got = arming.judge(build((True, "A"), (False, "")), None)
        self.assertEqual(got.unarmed, (track(2, False, ""),))

    def test_no_count_or_position_is_assumed(self):
        cases = {
            "pilot last of 40": [(True, f"T{i}") for i in range(39)] + [(False, "Pilot")],
            "pilot first": [(False, "Pilot")] + [(True, f"T{i}") for i in range(30)],
            "pilot last of 31": [(True, f"T{i}") for i in range(30)] + [(False, "Pilot")],
        }
        for label, specs in cases.items():
            with self.subTest(label):
                got = arming.judge(build(*specs), None)
                self.assertEqual(got.status, arming.ArmStatus.ARMED)
                self.assertEqual(got.armed, len(specs) - 1)


class TestChannels(unittest.TestCase):
    def armed(self, channels: int | None, pilot_armed: bool = False) -> arming.ArmCheck:
        specs = [(True, f"T{i}") for i in range(3)] + [(pilot_armed, "Pilot")]
        return arming.judge(build(*specs), channels)

    def test_none_and_equal_do_not_differ(self):
        self.assertFalse(self.armed(None).channels_differ)
        self.assertFalse(self.armed(3).channels_differ)

    def test_a_different_count_differs(self):
        got = self.armed(5)
        self.assertTrue(got.channels_differ)
        self.assertEqual(got.channels, 5)

    def test_an_armed_pilot_is_counted(self):
        self.assertTrue(self.armed(3, pilot_armed=True).channels_differ)
        self.assertFalse(self.armed(4, pilot_armed=True).channels_differ)

    def test_unarmed_and_no_tracks_never_differ(self):
        self.assertFalse(arming.judge(build((False, "A")), 9).channels_differ)
        self.assertFalse(arming.judge((), 9).channels_differ)


class TestTheReasonsStayInStep(unittest.TestCase):
    def test_every_freshness_reason_is_an_arm_reason(self):
        # assess() converts one to the other by value; a new Reason must not raise there.
        self.assertLessEqual({r.value for r in mr.Reason}, {a.value for a in arming.ArmReason})


class TestAssessing(unittest.TestCase):
    def check(self, observed: mr.Observed, now: float = 1.0) -> arming.ArmCheck:
        return arming.assess(observed, now, channels=None)

    def test_it_says_unknown_for_every_freshness_reason(self):
        unknown = arming.ArmStatus.UNKNOWN
        got = self.check(mr.Observed.empty(), 0.0)
        self.assertEqual((got.status, got.reason), (unknown, arming.ArmReason.NO_REPORT))
        baseline = mr.observe(None, report(1), 0.0)
        self.assertEqual(self.check(baseline, 0.0).reason, arming.ArmReason.NO_REPORT)
        both = witnessed(report(1), report(2))
        self.assertEqual(self.check(both, 1.0 + mr.STALE_SECONDS).reason, arming.ArmReason.STOPPED)
        bad = mr.observe(None, mr.Unreadable("x"), 0.0)
        self.assertEqual(self.check(bad, 0.0).reason, arming.ArmReason.UNREADABLE)
        self.assertEqual(self.check(both, 1.0 + mr.STALE_SECONDS - 0.001).status, arming.ArmStatus.NO_TRACKS)

    def test_a_fresh_report_from_an_old_script_is_not_reported(self):
        old = golden(GOLDEN_OLD)
        got = self.check(witnessed(old, mr.Report(1, old.seq + 1, old.project, old.record_path, old.fields)))
        self.assertEqual((got.status, got.reason), (arming.ArmStatus.UNKNOWN, arming.ArmReason.NOT_REPORTED))

    def test_bad_track_lines_are_tracks_unreadable(self):
        got = self.check(believed(*fields(2, "1\t1\tA")))
        self.assertEqual((got.status, got.reason), (arming.ArmStatus.UNKNOWN, arming.ArmReason.TRACKS_UNREADABLE))

    def test_a_believed_report_is_judged(self):
        got = self.check(believed(*fields(2, "1\t1\tA", "2\t0\tB")))
        self.assertEqual(got.status, arming.ArmStatus.UNARMED)

    def test_the_snapshot_is_exact_and_has_no_seq(self):
        got = arming.assess(believed(*fields(3, "1\t1\tA", "2\t0\t", "3\t1\tPilot")), 1.0, channels=2)
        self.assertEqual(
            got.as_snapshot(),
            {
                "status": "unarmed",
                "reason": None,
                "armed": 2,
                "tracks": 3,
                "unarmed": [{"number": 2, "name": ""}],
                "pilot_ambiguous": False,
                "channels": 2,
                "channels_differ": False,
            },
        )

    def test_the_same_tracks_across_two_seqs_are_equal(self):
        pairs = fields(2, "1\t1\tA", "2\t0\tB")
        first = self.check(witnessed(report(1, *pairs), report(2, *pairs)))
        later = self.check(witnessed(report(7, *pairs), report(8, *pairs)))
        self.assertEqual(first, later)

    def test_unknown_snapshot_has_nulls(self):
        got = self.check(mr.Observed.empty(), 0.0).as_snapshot()
        self.assertEqual(
            got,
            {
                "status": "unknown",
                "reason": "no-report",
                "armed": None,
                "tracks": None,
                "unarmed": [],
                "pilot_ambiguous": False,
                "channels": None,
                "channels_differ": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
