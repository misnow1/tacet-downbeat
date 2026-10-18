import io
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import UTC
from pathlib import Path
from unittest import mock

from tacet import annotations as ann
from tacet import corrections, rtd, rtd_replay, timeouts
from tacet.corrections import Add, Supersede
from tacet.stoppages import Doubt, Scan, Stoppage, Team, TeamTimeout
from tacet.timeouts import Verdict

from . import pcap_capture as cap
from . import rtd_frames as real

REPO_ROOT = Path(__file__).resolve().parent.parent
START = 1792261493.0
CAPTURE_START = START - 1000
CAPTURE_END = START + 5000
LOG_NAME = "game.jsonl"
CAPTURE_NAME = "game.pcap"


def at(seconds: float) -> float:
    return START + seconds


def entry(seq: int, key: str, seconds: float, phase: str, span_id: str) -> ann.Entry:
    event = ann.EVENTS[key]
    return ann.Entry(
        seq=seq,
        event=key,
        category=event.category.value,
        kind=event.kind.value,
        label=event.label,
        wall=rtd_replay.iso(at(seconds)),
        monotonic=float(seq),
        phase=phase,
        span_id=span_id,
    )


def span(start_seq: int, end_seq: int | None, key: str, start: float, end: float | None) -> list[ann.Entry]:
    span_id = f"{key}-{start_seq}"
    found = [entry(start_seq, key, start, ann.PHASE_START, span_id)]
    if end_seq is not None and end is not None:
        found.append(entry(end_seq, key, end, ann.PHASE_END, span_id))
    return found


def hold(start: float, end: float, *doubts: Doubt, marks: tuple[TeamTimeout, ...] = ()) -> Stoppage:
    return Stoppage(at(start), at(end), tuple(sorted(doubts)), marks)


def mark(seconds: float, team: Team) -> TeamTimeout:
    return TeamTimeout(at(seconds), team)


def compared(entries: list[ann.Entry], *holds: Stoppage, strays: tuple[TeamTimeout, ...] = ()) -> timeouts.Comparison:
    scan = Scan(holds, tuple(m for h in holds for m in h.marks) + strays, None)
    return timeouts.compare(timeouts.tapped(entries), scan, capture_start=CAPTURE_START, capture_end=CAPTURE_END)


def only(comparison: timeouts.Comparison) -> timeouts.Match:
    (match,) = comparison.matches
    return match


class TestTapped(unittest.TestCase):
    def test_pairs_starts_and_ends_by_span_id_in_start_order(self):
        entries = span(10, 12, "timeout", 0, 60) + span(11, 13, "timeout-media", 5, 80)
        entries.sort(key=lambda e: e.seq)
        found = timeouts.tapped(entries)
        pairs = [(t.key, t.start.seq, t.end and t.end.seq) for t in found.timeouts]
        self.assertEqual(pairs, [("timeout", 10, 12), ("timeout-media", 11, 13)])
        self.assertEqual(found.orphan_ends, ())

    def test_other_spans_are_ignored(self):
        entries = span(1, 2, "last-two-minutes", 0, 60)
        found = timeouts.tapped(entries)
        self.assertEqual((found.timeouts, found.orphan_ends), ((), ()))

    def test_an_open_span_has_no_end(self):
        (found,) = timeouts.tapped(span(1, None, "timeout", 0, None)).timeouts
        self.assertIsNone(found.end)
        self.assertIsNone(found.end_time)

    def test_an_end_with_no_start_is_an_orphan(self):
        orphan = entry(5, "timeout", 9, ann.PHASE_END, "timeout-1")
        found = timeouts.tapped([orphan])
        self.assertEqual((found.timeouts, found.orphan_ends), ((), (orphan,)))

    def test_times_come_from_the_wall_clock(self):
        (found,) = timeouts.tapped(span(1, 2, "timeout", 3, 9)).timeouts
        self.assertAlmostEqual(found.start_time, at(3), places=5)
        self.assertAlmostEqual(found.end_time or 0, at(9), places=5)


class TestKeys(unittest.TestCase):
    def test_timeout_keys_are_every_timeout_in_the_vocabulary_and_all_spans(self):
        in_vocabulary = {key for key in ann.EVENTS if key.startswith("timeout")}
        self.assertEqual(set(timeouts.TIMEOUT_KEYS), in_vocabulary)
        for key in timeouts.TIMEOUT_KEYS:
            self.assertIs(ann.EVENTS[key].kind, ann.Kind.SPAN)

    def test_every_team_key_is_a_timeout_key_and_the_guest_is_away(self):
        self.assertTrue(set(timeouts.TEAM_KEYS.values()) <= set(timeouts.TIMEOUT_KEYS))
        self.assertEqual(timeouts.TEAM_KEYS[Team.GUEST], "timeout-away")
        self.assertEqual(timeouts.TEAM_KEYS[Team.HOME], "timeout-home")

    def test_the_constants_are_pinned(self):
        self.assertEqual(timeouts.TAP_BEFORE_HOLD_SECONDS, 5.0)
        self.assertEqual(timeouts.END_AGREEMENT_SECONDS, 1.0)
        self.assertEqual(timeouts.SOURCE, "rtd")


class TestMatching(unittest.TestCase):
    def test_a_tap_inside_the_hold_matches_it(self):
        match = only(compared(span(1, 2, "timeout", 20, 90), hold(10, 80)))
        self.assertEqual(match.stoppage, hold(10, 80))

    def test_a_tap_just_before_the_hold_matches_and_further_before_does_not(self):
        early = timeouts.TAP_BEFORE_HOLD_SECONDS
        just_early = only(compared(span(1, 2, "timeout", 100 - early, 190), hold(100, 180)))
        self.assertEqual(just_early.stoppage, hold(100, 180))
        beyond = only(compared(span(1, 2, "timeout", 100 - early - 0.5, 190), hold(100, 180)))
        self.assertEqual(beyond.verdict, Verdict.NO_STOPPAGE)

    def test_a_tap_during_a_running_clock_is_no_stoppage(self):
        match = only(compared(span(1, 2, "timeout", 300, 400), hold(10, 80)))
        self.assertEqual((match.verdict, match.stoppage), (Verdict.NO_STOPPAGE, None))

    def test_a_tap_outside_the_capture_is_outside_capture(self):
        before = only(compared(span(1, 2, "timeout", -2000, -1900), hold(10, 80)))
        after = only(compared(span(1, 2, "timeout", 9000, 9100), hold(10, 80)))
        self.assertEqual((before.verdict, after.verdict), (Verdict.OUTSIDE_CAPTURE,) * 2)

    def test_the_hold_containing_the_tap_beats_a_nearer_start(self):
        first, second = hold(0, 100), hold(103, 200)
        self.assertEqual(only(compared(span(1, 2, "timeout", 99, 150), first, second)).stoppage, first)


class TestEnds(unittest.TestCase):
    def test_a_late_end_is_corrected_to_ready_for_play(self):
        match = only(compared(span(1, 2, "timeout", 12, 200), hold(10, 80)))
        self.assertEqual(match.verdict, Verdict.END_CORRECTED)
        self.assertAlmostEqual(match.shift or 0, -120, places=3)
        self.assertAlmostEqual(match.lead or 0, 2, places=3)

    def test_an_early_end_on_the_last_tap_is_corrected_too(self):
        match = only(compared(span(1, 2, "timeout", 12, 40), hold(10, 80)))
        self.assertEqual(match.verdict, Verdict.END_CORRECTED)
        self.assertAlmostEqual(match.shift or 0, 40, places=3)

    def test_an_end_within_a_second_agrees(self):
        for end in (79.5, 80, 81):
            self.assertEqual(only(compared(span(1, 2, "timeout", 12, end), hold(10, 80))).verdict, Verdict.AGREES)

    def test_overlapping_taps_correct_the_last_and_nest_the_inner(self):
        entries = span(1, 4, "timeout-injury", 12, 50) + span(2, 3, "timeout", 20, 78)
        entries.sort(key=lambda e: e.seq)
        outer, inner = compared(entries, hold(10, 80)).matches
        self.assertEqual((outer.tap.start.seq, outer.verdict), (1, Verdict.NESTED))
        self.assertEqual((inner.tap.start.seq, inner.verdict), (2, Verdict.END_CORRECTED))

    def test_an_inner_tap_that_overruns_the_hold_is_corrected_as_well(self):
        entries = span(1, 3, "timeout-injury", 12, 300) + span(2, 4, "timeout", 20, 400)
        entries.sort(key=lambda e: e.seq)
        first, second = compared(entries, hold(10, 80)).matches
        self.assertEqual((first.verdict, second.verdict), (Verdict.END_CORRECTED, Verdict.END_CORRECTED))

    def test_a_long_tap_over_three_holds_matches_the_first_and_covers_the_rest(self):
        entries = span(1, 9, "timeout", 12, 500) + span(2, 3, "timeout", 205, 215) + span(4, 5, "timeout", 405, 415)
        entries.sort(key=lambda e: e.seq)
        holds = (hold(10, 100), hold(200, 260), hold(400, 450))
        long, middle, last = compared(entries, *holds).matches
        self.assertEqual(long.stoppage, holds[0])
        self.assertEqual(long.covered, holds[1:])
        self.assertEqual((middle.stoppage, last.stoppage), (holds[1], holds[2]))
        self.assertEqual(middle.covered, ())

    def test_a_span_ended_by_starting_the_next_is_corrected_to_its_own_hold(self):
        entries = span(1, 3, "timeout-away", 12, 210) + span(4, 5, "timeout-away", 210, 300)
        entries.sort(key=lambda e: e.seq)
        holds = (
            hold(10, 100, marks=(mark(11, Team.GUEST),)),
            hold(200, 260, marks=(mark(201, Team.GUEST),)),
        )
        first, second = compared(entries, *holds).matches
        self.assertEqual((first.verdict, second.verdict), (Verdict.END_CORRECTED, Verdict.END_CORRECTED))
        self.assertEqual((first.stoppage, second.stoppage), holds)

    def test_an_open_span_gets_an_end_added(self):
        match = only(compared(span(1, None, "timeout", 12, None), hold(10, 80)))
        self.assertEqual(match.verdict, Verdict.END_ADDED)


class TestDoubtsBlockCorrections(unittest.TestCase):
    def test_every_doubt_makes_the_tap_untrusted_and_proposes_nothing(self):
        for doubt in Doubt:
            comparison = compared(span(1, 2, "timeout", 12, 200), hold(10, 80, doubt))
            self.assertEqual(only(comparison).verdict, Verdict.UNTRUSTED, doubt)
            self.assertEqual(timeouts.propose(comparison, capture=CAPTURE_NAME), (), doubt)

    def test_an_untrusted_group_marks_every_tap_in_it(self):
        entries = span(1, 3, "timeout", 12, 50) + span(2, 4, "timeout", 20, 60)
        entries.sort(key=lambda e: e.seq)
        verdicts = [m.verdict for m in compared(entries, hold(10, 80, Doubt.INTERRUPTED)).matches]
        self.assertEqual(verdicts, [Verdict.UNTRUSTED] * 2)


class TestKind(unittest.TestCase):
    def guest(self) -> Stoppage:
        return hold(10, 80, marks=(mark(11, Team.GUEST),))

    def test_a_generic_timeout_becomes_the_teams(self):
        match = only(compared(span(1, 2, "timeout", 12, 80), self.guest()))
        self.assertEqual(match.reclassify_to, "timeout-away")
        self.assertIsNone(match.conflict)

    def test_the_right_kind_needs_nothing(self):
        match = only(compared(span(1, 2, "timeout-away", 12, 80), self.guest()))
        self.assertEqual((match.reclassify_to, match.conflict), (None, None))

    def test_another_kind_is_a_conflict_not_a_correction(self):
        for key in ("timeout-home", "timeout-injury", "timeout-media", "timeout-official"):
            comparison = compared(span(1, 2, key, 12, 80), self.guest())
            match = only(comparison)
            self.assertEqual((match.reclassify_to, match.conflict), (None, Team.GUEST), key)
            self.assertEqual(timeouts.propose(comparison, capture=CAPTURE_NAME), (), key)

    def test_marks_from_both_teams_are_a_note_only(self):
        both = hold(10, 80, marks=(mark(11, Team.GUEST), mark(12, Team.HOME)))
        match = only(compared(span(1, 2, "timeout", 12, 80), both))
        self.assertEqual((match.reclassify_to, match.conflict), (None, None))
        self.assertTrue(match.notes)

    def test_a_team_tap_with_no_rtd_mark_is_a_note_only(self):
        match = only(compared(span(1, 2, "timeout-home", 12, 80), hold(10, 80)))
        self.assertEqual((match.reclassify_to, match.conflict), (None, None))
        self.assertTrue(match.notes)

    def test_an_untrusted_hold_is_not_used_for_the_kind(self):
        untrusted = hold(10, 80, Doubt.INTERRUPTED, marks=(mark(11, Team.GUEST),))
        match = only(compared(span(1, 2, "timeout", 12, 80), untrusted))
        self.assertEqual((match.reclassify_to, match.conflict), (None, None))


class TestComparison(unittest.TestCase):
    def test_untapped_holds_and_stray_marks_are_listed_in_order(self):
        stray = (mark(500, Team.HOME), mark(700, Team.GUEST))
        comparison = compared(span(1, 2, "timeout", 12, 80), hold(10, 80), hold(200, 260), hold(300, 360), strays=stray)
        self.assertEqual(comparison.untapped, (hold(200, 260), hold(300, 360)))
        self.assertEqual(comparison.stray_marks, stray)

    def test_a_mark_inside_a_hold_is_not_stray(self):
        held = hold(10, 80, marks=(mark(11, Team.GUEST),))
        self.assertEqual(compared(span(1, 2, "timeout-away", 12, 80), held).stray_marks, ())

    def test_orphan_ends_are_carried_through(self):
        orphan = entry(5, "timeout", 9, ann.PHASE_END, "timeout-1")
        self.assertEqual(compared([orphan]).orphan_ends, (orphan,))


class TestProposals(unittest.TestCase):
    def test_a_late_end_proposes_a_wall_supersede_with_its_evidence(self):
        entries = span(59, 67, "timeout", 12, 100)
        comparison = compared(entries, hold(10, 70))
        (proposal,) = timeouts.propose(comparison, capture=CAPTURE_NAME)
        assert isinstance(proposal, Supersede)
        self.assertEqual(proposal.seq, 67)
        self.assertEqual(proposal.wall, rtd_replay.iso(at(70)))
        self.assertEqual(proposal.source, "rtd")
        self.assertIsNone(proposal.event)
        self.assertEqual(proposal.reason, "play clock counted down after a 60 s hold; the tapped end was 30 s late")
        self.assertEqual(
            proposal.evidence,
            {
                "capture": CAPTURE_NAME,
                "hold_start": rtd_replay.iso(at(10)),
                "hold_end": rtd_replay.iso(at(70)),
                "superseded_wall": entries[1].wall,
            },
        )

    def test_an_early_end_says_early(self):
        comparison = compared(span(1, 2, "timeout", 12, 40), hold(10, 80))
        (proposal,) = timeouts.propose(comparison, capture=CAPTURE_NAME)
        self.assertTrue(proposal.reason.endswith("the tapped end was 40 s early"))

    def test_an_open_span_proposes_an_add_of_its_end(self):
        entries = span(7, None, "timeout-media", 12, None)
        (proposal,) = timeouts.propose(compared(entries, hold(10, 80)), capture=CAPTURE_NAME)
        assert isinstance(proposal, Add)
        self.assertEqual(
            (proposal.event, proposal.phase, proposal.span_id, proposal.wall),
            ("timeout-media", "end", "timeout-media-7", rtd_replay.iso(at(80))),
        )

    def test_a_reclassification_supersedes_the_start_entry(self):
        held = hold(10, 80, marks=(mark(11, Team.GUEST),))
        proposals = timeouts.propose(compared(span(3, 4, "timeout", 12, 80), held), capture=CAPTURE_NAME)
        (reclassify,) = proposals
        assert isinstance(reclassify, Supersede)
        self.assertEqual((reclassify.seq, reclassify.event, reclassify.wall), (3, "timeout-away", None))
        self.assertEqual(
            reclassify.evidence,
            {"capture": CAPTURE_NAME, "team": "guest", "team_timeout": rtd_replay.iso(at(11))},
        )

    def test_proposals_are_ordered_by_the_seq_they_target(self):
        entries = span(1, 9, "timeout", 12, 200) + span(2, 5, "timeout", 215, 400)
        entries.sort(key=lambda e: e.seq)
        proposals = timeouts.propose(compared(entries, hold(10, 100), hold(210, 300)), capture=CAPTURE_NAME)
        self.assertEqual([p.seq for p in proposals if isinstance(p, Supersede)], [5, 9])

    def test_an_agreeing_or_nested_tap_proposes_nothing(self):
        self.assertEqual(timeouts.propose(compared(span(1, 2, "timeout", 12, 80), hold(10, 80)), capture="c"), ())

    def test_every_proposal_survives_the_sidecar_round_trip(self):
        held = hold(10, 80, marks=(mark(11, Team.GUEST),))
        proposals = timeouts.propose(compared(span(3, 4, "timeout", 12, 200), held), capture=CAPTURE_NAME)
        self.assertEqual(len(proposals), 2)
        for proposal in proposals:
            self.assertEqual(corrections.correction_from_json(proposal.to_json()), proposal)


def build(
    entries: list[ann.Entry], *holds: Stoppage, existing: tuple[corrections.Correction, ...] = ()
) -> timeouts.Report:
    scan = Scan(holds, tuple(m for h in holds for m in h.marks), None)
    summary = rtd_replay.Summary(
        rtd_datagrams=10, first_time=CAPTURE_START, last_time=CAPTURE_END, sources=[(CAPTURE_START, "192.168.0.30")]
    )
    return timeouts.build_report(
        entries,
        scan,
        summary,
        existing=list(existing),
        log=Path(LOG_NAME),
        capture=Path(CAPTURE_NAME),
    )


class TestRender(unittest.TestCase):
    def text(self, report: timeouts.Report) -> str:
        return timeouts.render(report, tz=UTC)

    def test_it_is_ascii_and_ends_with_a_verdict(self):
        report = build(span(1, 2, "timeout", 12, 200), hold(10, 80))
        text = self.text(report)
        self.assertTrue(text.isascii())
        self.assertTrue(text.endswith("\n"))
        self.assertIn("VERDICT: 1 tapped; 1 end corrected", text)

    def test_the_header_counts_spans_and_names_the_files(self):
        entries = span(1, 2, "timeout", 12, 200) + span(3, None, "timeout", 300, None)
        entries.append(entry(9, "timeout", 5, ann.PHASE_END, "timeout-77"))
        text = self.text(build(entries, hold(10, 80)))
        self.assertIn(LOG_NAME, text)
        self.assertIn(CAPTURE_NAME, text)
        self.assertIn("2 timeout spans, 1 open, 1 orphan end", text)
        self.assertIn("0 faults, 0 gaps, 1 source", text)

    def test_a_row_shows_the_tap_the_hold_the_lead_the_shift_and_the_verdict(self):
        text = self.text(build(span(59, 67, "timeout", 12, 200), hold(10, 80)))
        row = next(line for line in text.splitlines() if line.startswith("59-67"))
        clock = lambda seconds: rtd_replay.iso(at(seconds))[11:19]  # noqa: E731
        self.assertIn(f"{clock(12)}-{clock(200)}", row)
        self.assertIn(f"{clock(10)}-{clock(80)}", row)
        self.assertIn("+2", row)
        self.assertIn("-120", row)
        self.assertIn("end-corrected", row)

    def test_a_covering_tap_names_what_it_covers(self):
        entries = span(1, 9, "timeout", 12, 500)
        text = self.text(build(entries, hold(10, 100), hold(200, 260)))
        self.assertIn("covers", text)

    def test_an_untrusted_row_names_its_doubts(self):
        text = self.text(build(span(1, 2, "timeout", 12, 80), hold(10, 80, Doubt.INTERRUPTED, Doubt.SOURCE_CHANGED)))
        self.assertIn("untrusted (interrupted, source-changed)", text)
        self.assertIn("1 not checked (seq 1)", text)

    def test_holds_with_no_tap_and_stray_marks_have_their_own_sections(self):
        report = build(span(1, 2, "timeout", 12, 80), hold(10, 80), hold(200, 260, Doubt.TOO_LONG))
        text = self.text(report)
        self.assertIn("holds with no tap", text)
        self.assertIn("too-long", text)

    def test_the_proposals_and_the_write_hint(self):
        report = build(span(1, 2, "timeout", 12, 200), hold(10, 80))
        text = self.text(report)
        self.assertIn("1 correction proposed", text)
        self.assertIn("--write", text)
        self.assertIn("game.corrections.jsonl", text)

    def test_what_the_sidecar_already_holds_is_not_proposed_again(self):
        report = build(span(1, 2, "timeout", 12, 200), hold(10, 80))
        by_hand = corrections.Supersede(seq=2, source="post-game review", reason="seen", wall=rtd_replay.iso(at(79)))
        again = build(span(1, 2, "timeout", 12, 200), hold(10, 80), existing=(by_hand,))
        self.assertEqual(len(report.fresh), 1)
        self.assertEqual((len(again.fresh), len(again.already)), (0, 1))
        self.assertIn("1 already in", self.text(again))
        self.assertNotIn("--write", self.text(again))

    def test_the_header_names_the_zone_it_prints_in(self):
        report = build(span(1, 2, "timeout", 12, 200), hold(10, 80))
        self.assertIn("(UTC)", self.text(report))

    def test_times_are_in_the_zone_given(self):
        from datetime import timedelta, timezone

        report = build(span(1, 2, "timeout", 12, 200), hold(10, 80))
        shifted = timeouts.render(report, tz=timezone(timedelta(hours=-4)))
        self.assertNotEqual(shifted, self.text(report))


class TestExitCode(unittest.TestCase):
    def test_a_clean_report_is_0(self):
        self.assertEqual(timeouts.exit_code(build(span(1, 2, "timeout", 12, 200), hold(10, 80))), timeouts.EXIT_CLEAN)

    def test_untrusted_no_stoppage_and_outside_capture_are_1(self):
        cases = {
            Verdict.UNTRUSTED: (span(1, 2, "timeout", 12, 80), hold(10, 80, Doubt.INTERRUPTED)),
            Verdict.NO_STOPPAGE: (span(1, 2, "timeout", 500, 580), hold(10, 80)),
            Verdict.OUTSIDE_CAPTURE: (span(1, 2, "timeout", 9000, 9100), hold(10, 80)),
        }
        for verdict, (entries, held) in cases.items():
            report = build(entries, held)
            self.assertEqual(report.comparison.matches[0].verdict, verdict)
            self.assertEqual(timeouts.exit_code(report), timeouts.EXIT_ATTENTION, verdict)

    def test_a_kind_conflict_is_1(self):
        held = hold(10, 80, marks=(mark(11, Team.GUEST),))
        self.assertEqual(timeouts.exit_code(build(span(1, 2, "timeout-home", 12, 80), held)), timeouts.EXIT_ATTENTION)

    def test_capture_faults_and_a_cut_off_file_are_1(self):
        report = build(span(1, 2, "timeout", 12, 80), hold(10, 80))
        report.summary.faults[rtd.Fault.CHECKSUM] += 1
        self.assertEqual(timeouts.exit_code(report), timeouts.EXIT_ATTENTION)
        report.summary.faults.clear()
        report.summary.truncated_file = True
        self.assertEqual(timeouts.exit_code(report), timeouts.EXIT_ATTENTION)

    def test_nested_untapped_and_stray_never_fail_the_run(self):
        entries = span(1, 3, "timeout-injury", 12, 50) + span(2, 4, "timeout", 20, 80)
        entries.sort(key=lambda e: e.seq)
        report = build(entries, hold(10, 80), hold(200, 260))
        self.assertEqual(timeouts.exit_code(report), timeouts.EXIT_CLEAN)


# -- the command ------------------------------------------------------------

TIME_OF_DAY = real.frame(39, " 2:24:54")
PLAY_CLOCK_OFFSET = 200


def play_clock(seconds: int) -> bytes:
    return real.frame(PLAY_CLOCK_OFFSET, f"{seconds:>5}   ")


def pcap_of(
    clock_writes: dict[int, int],
    *,
    seconds: int = 60,
    silent: range = range(0),
    extra: dict[int, bytes] | None = None,
    dport: int = cap.RTD_PORT,
) -> bytes:
    """One time-of-day frame every second, the play clock written at the given seconds."""
    records = []
    for second in range(seconds + 1):
        if second not in silent:
            records.append(cap.record(at(second), cap.rtd_packet(TIME_OF_DAY, dport=dport)))
        if second in clock_writes:
            records.append(cap.record(at(second) + 0.5, cap.rtd_packet(play_clock(clock_writes[second]), dport=dport)))
        if extra and second in extra:
            records.append(cap.record(at(second) + 0.6, cap.rtd_packet(extra[second], dport=dport)))
    return cap.pcap_bytes(records)


#: A countdown from 41 to 40, then a reset to 25 held for 40 s, then counting again.
HOLD_WRITES = {0: 41, 1: 40, 10: 25, 50: 24, 51: 23}


class Run:
    def __init__(self, entries: list[ann.Entry], capture: bytes, *args: str) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / "game.jsonl"
        self.log.write_text("".join(e.to_json() + "\n" for e in entries), encoding="utf-8")
        self.pcap = root / CAPTURE_NAME
        self.pcap.write_bytes(capture)
        self.sidecar = corrections.sidecar_path(self.log)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.code = timeouts.main([str(self.log), str(self.pcap), *args], out=self.out, err=self.err, tz=UTC)

    def again(self, *args: str) -> "Run":
        self.out, self.err = io.StringIO(), io.StringIO()
        self.code = timeouts.main([str(self.log), str(self.pcap), *args], out=self.out, err=self.err, tz=UTC)
        return self


class TestCommand(unittest.TestCase):
    def run_of(self, entries: list[ann.Entry], capture: bytes, *args: str) -> Run:
        run = Run(entries, capture, *args)
        self.addCleanup(run.tmp.cleanup)
        return run

    def late_tap(self) -> list[ann.Entry]:
        return span(1, 2, "timeout", 12, 70)

    def test_a_clean_run_exits_0_and_writes_nothing(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES))
        self.assertEqual(run.code, timeouts.EXIT_CLEAN, run.err.getvalue())
        self.assertIn("end-corrected", run.out.getvalue())
        self.assertFalse(run.sidecar.exists())

    def test_write_creates_the_sidecar_and_a_second_write_adds_nothing(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES), "--write")
        self.assertEqual(run.code, timeouts.EXIT_CLEAN, run.err.getvalue())
        (written,) = corrections.read_corrections(run.sidecar)
        assert isinstance(written, Supersede)
        self.assertEqual(written.seq, 2)
        # The play clock write lands half a second after the second's time-of-day frame.
        self.assertEqual(written.wall, rtd_replay.iso(at(50.5)))
        self.assertIn("wrote 1 correction", run.out.getvalue())
        before = run.sidecar.read_bytes()
        run.again("--write")
        self.assertEqual(run.sidecar.read_bytes(), before)
        self.assertIn("nothing new to write", run.out.getvalue())

    def test_a_hold_across_a_gap_is_untrusted_and_exits_1(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES, silent=range(20, 31)))
        self.assertEqual(run.code, timeouts.EXIT_ATTENTION)
        self.assertIn("untrusted", run.out.getvalue())

    def test_a_tap_with_no_stoppage_exits_1(self):
        run = self.run_of(span(1, 2, "timeout", 30 + 25, 58), pcap_of(HOLD_WRITES))
        self.assertEqual(run.code, timeouts.EXIT_ATTENTION)
        self.assertIn("no-stoppage", run.out.getvalue())

    def test_a_faulted_capture_exits_1(self):
        bad = real.frame(3, "x", checksum=b"00")
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES, extra={5: bad}))
        self.assertEqual(run.code, timeouts.EXIT_ATTENTION)

    def test_missing_dpkt_says_how_to_install_and_exits_2(self):
        import importlib

        import tacet

        saved = importlib.import_module("tacet.pcap")
        del tacet.pcap
        try:
            with mock.patch.dict(sys.modules, {"dpkt": None}):
                sys.modules.pop("tacet.pcap", None)
                err = io.StringIO()
                code = timeouts.main(["a.jsonl", "a.pcap"], out=io.StringIO(), err=err)
        finally:
            tacet.pcap = saved
            sys.modules["tacet.pcap"] = saved
        self.assertEqual(code, timeouts.EXIT_REFUSED)
        self.assertIn(".[analysis]", err.getvalue())

    def test_a_corrupt_log_exits_2(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES))
        run.log.write_text("{nope\n{nope\n", encoding="utf-8")
        run.again()
        self.assertEqual(run.code, timeouts.EXIT_REFUSED)
        self.assertIn("tacet-timeouts:", run.err.getvalue())

    def test_a_malformed_sidecar_exits_2_and_is_left_alone(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES))
        run.sidecar.write_text('{"v":1,"action":"delete"}\n', encoding="utf-8")
        before = run.sidecar.read_bytes()
        run.again("--write")
        self.assertEqual(run.code, timeouts.EXIT_REFUSED)
        self.assertEqual(run.sidecar.read_bytes(), before)
        self.assertIn("line 1", run.err.getvalue())

    def test_a_capture_with_no_rtd_datagrams_exits_2(self):
        other = cap.pcap_bytes([cap.record(START, cap.rtd_packet(TIME_OF_DAY, dport=9999))])
        run = self.run_of(self.late_tap(), other)
        self.assertEqual(run.code, timeouts.EXIT_REFUSED)
        self.assertIn("no RTD datagrams", run.err.getvalue())

    def test_an_unreadable_capture_and_a_missing_log_exit_2(self):
        run = self.run_of(self.late_tap(), b"this is not a capture at all, just text")
        self.assertEqual(run.code, timeouts.EXIT_REFUSED)
        err = io.StringIO()
        code = timeouts.main(["/nonexistent/none.jsonl", "/nonexistent/none.pcap"], out=io.StringIO(), err=err)
        self.assertEqual(code, timeouts.EXIT_REFUSED)

    def test_the_port_flag(self):
        other = 9999
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES, dport=other))
        self.assertEqual(run.code, timeouts.EXIT_REFUSED)
        run.again("--port", str(other))
        self.assertEqual(run.code, timeouts.EXIT_CLEAN, run.err.getvalue())

    def test_output_is_ascii(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES))
        self.assertTrue(run.out.getvalue().isascii())

    def test_runs_as_a_module(self):
        run = self.run_of(self.late_tap(), pcap_of(HOLD_WRITES))
        result = subprocess.run(
            [sys.executable, "-m", "tacet.timeouts", str(run.log), str(run.pcap)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VERDICT", result.stdout)
        self.assertFalse(run.sidecar.exists())

    def test_console_script_is_declared(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["tacet-timeouts"], "tacet.timeouts:main")


if __name__ == "__main__":
    unittest.main()
