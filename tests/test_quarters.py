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
from tacet import corrections, periods, quarters, rtd, rtd_replay
from tacet.corrections import Add
from tacet.periods import Boundary, Doubt, Edge, Period, PeriodSpan
from tacet.quarters import PeriodVerdict, TapVerdict

from . import pcap_capture as cap
from . import rtd_frames as real

REPO_ROOT = Path(__file__).resolve().parent.parent
START = 1792261493.0
CAPTURE_START = START - 1000
CAPTURE_END = START + 5000
LOG_NAME = "game.jsonl"
CAPTURE_NAME = "game.pcap"
ZERO = Edge.CLOCK_AT_ZERO


def at(seconds: float) -> float:
    return START + seconds


def entry(
    seq: int,
    key: str,
    seconds: float,
    *,
    phase: str | None = None,
    span_id: str | None = None,
    delay: float | None = None,
) -> ann.Entry:
    event = ann.EVENTS[key]
    data = {} if delay is None else {"tap": {"delay": delay}}
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
        data=data,
    )


def tap(seq: int, seconds: float, delay: float | None = None) -> ann.Entry:
    return entry(seq, quarters.L2M_TAP, seconds, delay=delay)


def pspan(
    period: Period,
    start: float | None,
    end: float | None,
    *,
    start_doubts: tuple[Doubt, ...] = (),
    end_doubts: tuple[Doubt, ...] = (),
    own: tuple[Doubt, ...] = (),
    zeros: tuple[float, ...] = (),
) -> PeriodSpan:
    return PeriodSpan(
        period=period,
        shown=f"[{period.value}]",
        shown_from=at((start or 0) + 5),
        shown_to=None if end is None else at(end + 5),
        start=None if start is None else Boundary(at(start), ZERO, start_doubts),
        end=None if end is None else Boundary(at(end), ZERO, end_doubts),
        zeros=zeros or (() if end is None else (at(end),)),
        own_doubts=own,
    )


def game(**replace: PeriodSpan | None) -> list[PeriodSpan]:
    """A clean game: pregame ends at 0, then 100 s of each period."""
    spans = {
        "pre": pspan(Period.PREGAME, None, 0),
        "q1": pspan(Period.Q1, 0, 100),
        "q2": pspan(Period.Q2, 100, 200),
        "half": pspan(Period.HALFTIME, 200, 300),
        "q3": pspan(Period.Q3, 300, 400),
        "q4": pspan(Period.Q4, 400, 500),
    }
    for name, span in replace.items():
        if span is None:
            del spans[name]
        else:
            spans[name] = span
    return list(spans.values())


def build(
    entries: list[ann.Entry],
    spans: list[PeriodSpan],
    *,
    existing: tuple[corrections.Correction, ...] = (),
) -> quarters.Report:
    board = periods.Board(tuple(spans), ())
    summary = rtd_replay.Summary(
        rtd_datagrams=10, first_time=CAPTURE_START, last_time=CAPTURE_END, sources=[(CAPTURE_START, "192.168.0.30")]
    )
    return quarters.build_report(
        entries, board, summary, existing=list(existing), log=Path(LOG_NAME), capture=Path(CAPTURE_NAME)
    )


def verdicts(report: quarters.Report) -> dict[str, PeriodVerdict]:
    return {p.key: p.verdict for p in report.comparison.periods}


def adds(report: quarters.Report) -> list[Add]:
    found = [c for c in report.fresh + report.already if isinstance(c, Add)]
    assert len(found) == len(report.fresh) + len(report.already)
    return found


class TestKeys(unittest.TestCase):
    def test_the_period_keys_are_the_five_spans_in_order(self):
        self.assertEqual(list(quarters.PERIOD_KEYS.values()), ["q1", "q2", "halftime", "q3", "q4"])
        self.assertEqual(list(quarters.PERIOD_KEYS), [Period(k) for k in quarters.PERIOD_KEYS.values()])
        for key in quarters.PERIOD_KEYS.values():
            self.assertIs(ann.EVENTS[key].kind, ann.Kind.SPAN)
            self.assertFalse(ann.EVENTS[key].button)

    def test_last_two_minutes_is_a_retired_span_and_its_tap_a_button_instant(self):
        self.assertIs(ann.EVENTS[quarters.L2M_SPAN].kind, ann.Kind.SPAN)
        self.assertFalse(ann.EVENTS[quarters.L2M_SPAN].button)
        self.assertIs(ann.EVENTS[quarters.L2M_TAP].kind, ann.Kind.INSTANT)
        self.assertTrue(ann.EVENTS[quarters.L2M_TAP].button)

    def test_the_span_id_is_ours_and_never_a_log_id(self):
        self.assertEqual(quarters.period_span_id("q1"), "q1-rtd")
        self.assertNotEqual(quarters.period_span_id("q1"), ann.span_id_for("q1", 1))
        self.assertEqual(quarters.SOURCE, "rtd")

    def test_every_quarter_is_one_of_the_periods(self):
        self.assertEqual(quarters.QUARTERS, (Period.Q1, Period.Q2, Period.Q3, Period.Q4))


class TestPeriods(unittest.TestCase):
    def test_a_clean_game_adds_all_five(self):
        self.assertEqual(set(verdicts(build([], game())).values()), {PeriodVerdict.SPAN_ADDED})

    def test_never_shown_is_missing(self):
        self.assertEqual(verdicts(build([], game(q2=None)))["q2"], PeriodVerdict.MISSING)

    def test_a_capture_that_began_in_it_has_no_start(self):
        found = verdicts(build([], game(q1=pspan(Period.Q1, None, 100), pre=None)))
        self.assertEqual(found["q1"], PeriodVerdict.NO_START)

    def test_a_capture_that_ended_in_it_has_no_end(self):
        found = verdicts(build([], game(q4=pspan(Period.Q4, 400, None))))
        self.assertEqual(found["q4"], PeriodVerdict.NO_END)

    def test_a_doubt_makes_it_untrusted(self):
        for doubt in Doubt:
            found = verdicts(build([], game(q3=pspan(Period.Q3, 300, 400, end_doubts=(doubt,)))))
            self.assertEqual(found["q3"], PeriodVerdict.UNTRUSTED, doubt)

    def test_a_doubt_at_a_boundary_taints_both_periods_beside_it(self):
        found = verdicts(build([], game(q2=pspan(Period.Q2, 100, 200, end_doubts=(Doubt.INTERRUPTED,)))))
        self.assertEqual(found["q2"], PeriodVerdict.UNTRUSTED)

    def test_repeated_is_untrusted(self):
        found = verdicts(build([], game(q1=pspan(Period.Q1, 0, 100, own=(Doubt.REPEATED,)))))
        self.assertEqual(found["q1"], PeriodVerdict.UNTRUSTED)

    def test_a_log_that_already_starts_it_is_already_spanned(self):
        log = [entry(3, "q1", 5, phase=ann.PHASE_START, span_id="q1-3")]
        report = build(log, game())
        self.assertEqual(verdicts(report)["q1"], PeriodVerdict.ALREADY_SPANNED)
        self.assertEqual(len(adds(report)), 8)

    def test_a_sidecar_add_under_another_id_is_already_spanned(self):
        by_hand = Add(
            event="q1",
            wall=rtd_replay.iso(at(5)),
            source="post-game review",
            reason="seen",
            phase=ann.PHASE_START,
            span_id="q1-review",
        )
        report = build([], game(), existing=(by_hand,))
        self.assertEqual(verdicts(report)["q1"], PeriodVerdict.ALREADY_SPANNED)

    def test_our_own_id_in_the_sidecar_keeps_it_and_counts_as_already_in(self):
        first = build([], game())
        again = build([], game(), existing=tuple(first.fresh))
        self.assertEqual(verdicts(again)["q1"], PeriodVerdict.SPAN_ADDED)
        self.assertEqual((len(again.fresh), len(again.already)), (0, 10))

    def test_the_comparison_always_has_the_five_periods(self):
        self.assertEqual([p.key for p in build([], []).comparison.periods], ["q1", "q2", "halftime", "q3", "q4"])

    def test_pregame_and_other_showings_are_others(self):
        spans = [*game(), pspan(Period.OTHER, 500, None)]
        others = build([], spans).comparison.others
        self.assertEqual([s.period for s in others], [Period.PREGAME, Period.OTHER])


class TestProposals(unittest.TestCase):
    def test_two_adds_per_span_under_the_period_key(self):
        report = build([], game())
        found = adds(report)
        self.assertEqual(len(found), 10)
        q1_start, q1_end = found[0], found[1]
        self.assertEqual((q1_start.event, q1_start.phase, q1_start.span_id), ("q1", "start", "q1-rtd"))
        self.assertEqual((q1_end.event, q1_end.phase, q1_end.span_id), ("q1", "end", "q1-rtd"))
        self.assertEqual((q1_start.source, q1_end.source), ("rtd", "rtd"))
        self.assertEqual(q1_start.wall, rtd_replay.iso(at(0)))
        self.assertEqual(q1_end.wall, rtd_replay.iso(at(100)))
        assert q1_start.evidence is not None
        self.assertEqual(
            set(q1_start.evidence),
            {"capture", "start_by", "end_by", "quarter_from", "quarter_to", "clock_at_zero_rises"},
        )
        self.assertEqual(q1_start.evidence["capture"], CAPTURE_NAME)
        self.assertEqual(q1_start.evidence["start_by"], "clock-at-zero")

    def test_the_reason_names_the_edge(self):
        board_edge = pspan(Period.Q1, 0, 100)
        quarter_field = PeriodSpan(
            Period.Q1, "x", at(5), at(105), Boundary(at(5), Edge.QUARTER_FIELD), board_edge.end, board_edge.zeros
        )
        start = adds(build([], game(q1=quarter_field)))[0]
        self.assertIn("quarter field changing to it", start.reason)
        self.assertIn("the scoreboard showed q1", start.reason)
        self.assertIn("clock reaching zero", adds(build([], game()))[1].reason)

    def test_an_open_quarter_field_is_left_out_of_the_evidence(self):
        span = PeriodSpan(Period.Q1, "x", at(5), None, Boundary(at(0), ZERO), Boundary(at(100), ZERO), (at(100),))
        start = adds(build([], game(q1=span)))[0]
        assert start.evidence is not None
        self.assertNotIn("quarter_to", start.evidence)

    def test_every_add_survives_the_sidecar_round_trip(self):
        for found in adds(build([tap(1, 450)], game())):
            self.assertEqual(Add.from_json(found.to_json()), found)

    def test_they_come_in_start_order(self):
        walls = [a.wall for a in adds(build([tap(1, 450)], game()))]
        starts = [a.wall for a in adds(build([tap(1, 450)], game())) if a.phase == "start"]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(walls), 12)

    def test_nothing_untrusted_or_unbounded_is_proposed(self):
        spans = game(
            q1=pspan(Period.Q1, 0, 100, own=(Doubt.REPEATED,)),
            q2=pspan(Period.Q2, None, 200),
            q3=pspan(Period.Q3, 300, None),
            q4=None,
        )
        self.assertEqual([a.event for a in adds(build([], spans))], ["halftime", "halftime"])


class TestLastTwoMinutes(unittest.TestCase):
    def test_the_first_q4_tap_adds_a_span_under_the_span_key(self):
        report = build([tap(812, 450)], game())
        found = [a for a in adds(report) if a.event == "last-two-minutes"]
        self.assertEqual(len(found), 2)
        self.assertNotIn("last-two-minutes-tap", {a.event for a in adds(report)})
        start, end = found
        self.assertEqual((start.phase, end.phase), ("start", "end"))
        self.assertEqual(start.span_id, ann.span_id_for("last-two-minutes", 812))
        self.assertEqual(end.span_id, start.span_id)
        self.assertEqual(start.wall, rtd_replay.iso(at(450)))
        self.assertEqual(end.wall, rtd_replay.iso(at(500)))
        assert start.evidence is not None
        self.assertEqual(start.evidence["tap_seq"], 812)
        self.assertEqual(start.evidence["period"], "q4")
        self.assertIn("a single tap in q4", start.reason)

    def test_the_start_moves_back_by_the_logged_delay(self):
        found = [a for a in adds(build([tap(1, 450, delay=20.0)], game())) if a.event == "last-two-minutes"]
        self.assertEqual(found[0].wall, rtd_replay.iso(at(430)))

    def test_a_negative_or_missing_delay_does_not_move_the_start(self):
        for delay in (-5.0, None):
            found = [a for a in adds(build([tap(1, 450, delay=delay)], game())) if a.event == "last-two-minutes"]
            self.assertEqual(found[0].wall, rtd_replay.iso(at(450)))

    def test_a_second_tap_in_the_same_quarter_is_a_repeat(self):
        report = build([tap(1, 450), tap(2, 470)], game())
        self.assertEqual([t.verdict for t in report.comparison.taps], [TapVerdict.SPAN_ADDED, TapVerdict.REPEAT_TAP])
        self.assertEqual(len([a for a in adds(report) if a.event == "last-two-minutes"]), 2)

    def test_taps_in_different_quarters_each_get_a_pair(self):
        report = build([tap(1, 150), tap(2, 450)], game())
        self.assertEqual(len([a for a in adds(report) if a.event == "last-two-minutes"]), 4)

    def test_q1_and_q3_taps_get_pairs_too(self):
        report = build([tap(1, 50), tap(2, 350)], game())
        self.assertEqual({t.verdict for t in report.comparison.taps}, {TapVerdict.SPAN_ADDED})

    def test_halftime_and_pregame_are_not_in_a_quarter(self):
        report = build([tap(1, 250), tap(2, -50)], game())
        self.assertEqual({t.verdict for t in report.comparison.taps}, {TapVerdict.NOT_IN_A_QUARTER})

    def test_a_tap_after_the_game_is_not_in_a_quarter(self):
        report = build([tap(1, 600)], game())
        self.assertEqual(report.comparison.taps[0].verdict, TapVerdict.NOT_IN_A_QUARTER)

    def test_an_untrusted_end_is_untrusted(self):
        spans = game(q4=pspan(Period.Q4, 400, 500, end_doubts=(Doubt.INTERRUPTED,)))
        self.assertEqual(build([tap(1, 450)], spans).comparison.taps[0].verdict, TapVerdict.UNTRUSTED)

    def test_a_start_doubt_does_not_spoil_the_tap(self):
        spans = game(q4=pspan(Period.Q4, 400, 500, start_doubts=(Doubt.INTERRUPTED,)))
        self.assertEqual(build([tap(1, 450)], spans).comparison.taps[0].verdict, TapVerdict.SPAN_ADDED)

    def test_a_repeated_quarter_is_untrusted(self):
        spans = game(q4=pspan(Period.Q4, 400, 500, own=(Doubt.REPEATED,)))
        self.assertEqual(build([tap(1, 450)], spans).comparison.taps[0].verdict, TapVerdict.UNTRUSTED)

    def test_a_quarter_the_capture_ended_in_has_no_end(self):
        spans = game(q4=pspan(Period.Q4, 400, None))
        self.assertEqual(build([tap(1, 450)], spans).comparison.taps[0].verdict, TapVerdict.NO_END)

    def test_a_tap_outside_the_capture_is_outside_capture(self):
        report = build([tap(1, -5000), tap(2, 9000)], game())
        self.assertEqual({t.verdict for t in report.comparison.taps}, {TapVerdict.OUTSIDE_CAPTURE})

    def test_a_quarter_the_capture_began_in_still_gives_the_pair(self):
        spans = game(q1=pspan(Period.Q1, None, 100), pre=None)
        report = build([tap(1, 50)], spans)
        self.assertEqual(report.comparison.taps[0].verdict, TapVerdict.SPAN_ADDED)

    def test_the_older_spans_are_not_read(self):
        log = [
            entry(1, "last-two-minutes", 450, phase=ann.PHASE_START, span_id="last-two-minutes-1"),
            entry(2, "last-two-minutes", 480, phase=ann.PHASE_END, span_id="last-two-minutes-1"),
        ]
        report = build(log, game())
        self.assertEqual(report.comparison.taps, ())
        self.assertEqual(len(adds(report)), 10)
        self.assertEqual(report.taps, 0)

    def test_a_second_write_is_already_in(self):
        first = build([tap(1, 450)], game())
        again = build([tap(1, 450)], game(), existing=tuple(first.fresh))
        self.assertEqual((len(again.fresh), len(again.already)), (0, 12))


class TestRender(unittest.TestCase):
    def text(self, report: quarters.Report) -> str:
        return quarters.render(report, tz=UTC)

    def test_it_is_ascii_and_ends_with_a_verdict(self):
        text = self.text(build([tap(812, 450)], game()))
        self.assertTrue(text.isascii())
        self.assertTrue(text.endswith("\n"))
        self.assertIn("VERDICT: 5 periods; 5 span added; 1 last two minutes tap; 1 span added\n", text)

    def test_the_header_names_the_files_and_the_zone(self):
        text = self.text(build([tap(812, 450)], game()))
        self.assertIn(f"log: {LOG_NAME}", text)
        self.assertIn(f"capture: {CAPTURE_NAME}", text)
        self.assertIn("1 last two minutes tap", text)
        self.assertIn("(UTC), 0 faults, 0 gaps, 1 source", text)
        self.assertIn("board: 6 periods shown, 0 clock-at-zero rises", text)

    def test_a_period_row_shows_the_board_the_span_and_the_edges(self):
        text = self.text(build([], game()))
        clock = lambda seconds: rtd_replay.iso(at(seconds))[11:19]  # noqa: E731
        row = next(line for line in text.splitlines() if line.startswith("q1 "))
        self.assertIn(f"{clock(5)}-{clock(105)}", row)
        self.assertIn(f"{clock(0)}-{clock(100)}", row)
        self.assertIn("clock-at-zero  clock-at-zero  span-added", row)

    def test_an_untrusted_row_names_its_doubts_and_the_verdict_names_the_period(self):
        spans = game(q3=pspan(Period.Q3, 300, 400, end_doubts=(Doubt.INTERRUPTED, Doubt.SOURCE_CHANGED)))
        text = self.text(build([], spans))
        self.assertIn("untrusted (interrupted, source-changed)", text)
        self.assertIn("1 not checked (attention: q3)", text)

    def test_a_flicker_says_which_rise_was_used(self):
        spans = game(q2=pspan(Period.Q2, 100, 200, zeros=(at(190), at(200))))
        self.assertIn("2 clock-at-zero rises, the last used", self.text(build([], spans)))

    def test_a_missing_period_has_dashes(self):
        row = next(line for line in self.text(build([], game(q2=None))).splitlines() if line.startswith("q2 "))
        self.assertTrue(row.endswith("missing"))

    def test_other_periods_show_the_raw_text(self):
        overtime = PeriodSpan(Period.OTHER, " 5OT  ", at(510), None, Boundary(at(500), ZERO), None)
        text = self.text(build([], [*game(), overtime]))
        self.assertIn('pregame "[pregame]"', text)
        self.assertIn('other " 5OT  "', text)

    def test_the_taps_table_and_the_attention_list(self):
        text = self.text(build([tap(812, 450), tap(813, 250)], game()))
        self.assertIn("last two minutes taps:", text)
        row = next(line for line in text.splitlines() if line.startswith("812 "))
        self.assertIn("q4", row)
        self.assertTrue(row.endswith("span-added"))
        self.assertIn("(attention: seq 813)", text)

    def test_no_taps_says_none(self):
        self.assertIn("last two minutes taps: none", self.text(build([], game())))

    def test_the_proposals_and_the_write_hint(self):
        text = self.text(build([], game()))
        self.assertIn("10 corrections proposed, 0 already in game.corrections.jsonl", text)
        self.assertIn("run again with --write to append 10 to", text)

    def test_what_the_sidecar_already_holds_is_not_proposed_again(self):
        first = build([], game())
        text = self.text(build([], game(), existing=tuple(first.fresh)))
        self.assertIn("10 already in", text)
        self.assertNotIn("--write", text)

    def test_times_follow_the_zone(self):
        from datetime import timedelta, timezone

        report = build([], game())
        self.assertNotEqual(quarters.render(report, tz=timezone(timedelta(hours=-4))), self.text(report))


class TestExitCode(unittest.TestCase):
    def test_a_clean_report_is_0(self):
        self.assertEqual(quarters.exit_code(build([tap(1, 450)], game())), quarters.EXIT_CLEAN)

    def test_each_period_attention_verdict_is_1(self):
        cases = {
            "untrusted": pspan(Period.Q2, 100, 200, end_doubts=(Doubt.INTERRUPTED,)),
            "no-start": pspan(Period.Q2, None, 200),
            "no-end": pspan(Period.Q2, 100, None),
            "missing": None,
        }
        for name, span in cases.items():
            self.assertEqual(quarters.exit_code(build([], game(q2=span))), quarters.EXIT_ATTENTION, name)

    def test_each_tap_attention_verdict_is_1(self):
        cases = {
            "untrusted": ([tap(1, 450)], game(q4=pspan(Period.Q4, 400, 500, end_doubts=(Doubt.INTERRUPTED,)))),
            "no-end": ([tap(1, 450)], game(q4=pspan(Period.Q4, 400, None))),
            "not-in-a-quarter": ([tap(1, 250)], game()),
            "outside-capture": ([tap(1, 9000)], game()),
        }
        for name, (entries, spans) in cases.items():
            report = build(entries, spans)
            if name in ("untrusted", "no-end"):
                self.assertEqual(report.comparison.taps[0].verdict.value, name)
            self.assertEqual(quarters.exit_code(report), quarters.EXIT_ATTENTION, name)

    def test_faults_and_a_cut_off_file_are_1(self):
        report = build([], game())
        report.summary.truncated_file = True
        self.assertEqual(quarters.exit_code(report), quarters.EXIT_ATTENTION)
        clean = build([], game())
        clean.summary.faults.update([rtd_fault()])
        self.assertEqual(quarters.exit_code(clean), quarters.EXIT_ATTENTION)

    def test_already_spanned_repeat_taps_and_other_periods_never_fail(self):
        log = [entry(9, "q1", 5, phase=ann.PHASE_START, span_id="q1-9"), tap(1, 450), tap(2, 470)]
        overtime = PeriodSpan(Period.OTHER, " 5OT  ", at(510), None, Boundary(at(500), ZERO), None)
        report = build(log, [*game(), overtime])
        self.assertEqual(quarters.exit_code(report), quarters.EXIT_CLEAN)


def rtd_fault() -> rtd.Fault:
    return rtd.Fault.CHECKSUM


# -- the command ------------------------------------------------------------

TIME_OF_DAY = real.frame(39, " 2:24:54")
Z_OFFSET = 26
UNZ = real.frame(Z_OFFSET, " ")
ZERO_FLAG = real.frame(Z_OFFSET, "z")
QUARTER_OFFSET = 141
MAIN_CLOCK_OFFSET = 0


def shown(text: str) -> bytes:
    return real.frame(QUARTER_OFFSET, text)


#: A compressed game, second by second: a write lands half a second after the second's time-of-day frame.
GAME_WRITES: dict[int, list[bytes]] = {
    0: [UNZ, shown("  PRE ")],
    5: [ZERO_FLAG],
    7: [UNZ],
    9: [shown(" 11ST ")],
    100: [ZERO_FLAG],
    105: [UNZ],
    110: [shown(" 22ND ")],
    200: [ZERO_FLAG],
    205: [UNZ],
    210: [shown("  HALF")],
    300: [ZERO_FLAG],
    305: [UNZ],
    310: [shown(" 33RD ")],
    400: [ZERO_FLAG],
    405: [UNZ],
    410: [shown(" 44TH ")],
    500: [ZERO_FLAG],
}
GAME_SECONDS = 520
CLOCK_SLIP = {
    20: [real.frame(MAIN_CLOCK_OFFSET, "20:00")],
    21: [real.frame(MAIN_CLOCK_OFFSET, " 0:00")],
    60: [real.frame(MAIN_CLOCK_OFFSET, "20:00")],
}


def pcap_of(
    writes: dict[int, list[bytes]],
    *,
    seconds: int = GAME_SECONDS,
    silent: range = range(0),
    dport: int = cap.RTD_PORT,
) -> bytes:
    records = []
    for second in range(seconds + 1):
        if second not in silent:
            records.append(cap.record(at(second), cap.rtd_packet(TIME_OF_DAY, dport=dport)))
        for number, frame in enumerate(writes.get(second, [])):
            records.append(cap.record(at(second) + 0.5 + number / 100, cap.rtd_packet(frame, dport=dport)))
    return cap.pcap_bytes(records)


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
        self.code = 0
        self.again(*args)

    def again(self, *args: str) -> "Run":
        self.out, self.err = io.StringIO(), io.StringIO()
        self.code = quarters.main([str(self.log), str(self.pcap), *args], out=self.out, err=self.err, tz=UTC)
        return self


class TestCommand(unittest.TestCase):
    def run_of(self, entries: list[ann.Entry], capture: bytes, *args: str) -> Run:
        run = Run(entries, capture, *args)
        self.addCleanup(run.tmp.cleanup)
        return run

    def test_a_clean_run_exits_0_proposes_ten_and_writes_nothing(self):
        run = self.run_of([], pcap_of(GAME_WRITES))
        self.assertEqual(run.code, quarters.EXIT_CLEAN, run.err.getvalue() + run.out.getvalue())
        self.assertIn("10 corrections proposed", run.out.getvalue())
        self.assertFalse(run.sidecar.exists())

    def test_write_adds_ten_with_the_scoreboards_times_and_a_second_write_adds_nothing(self):
        run = self.run_of([], pcap_of(GAME_WRITES), "--write")
        self.assertEqual(run.code, quarters.EXIT_CLEAN, run.err.getvalue())
        written = corrections.read_corrections(run.sidecar)
        self.assertEqual(len(written), 10)
        assert isinstance(written[0], Add) and isinstance(written[1], Add)
        self.assertEqual((written[0].event, written[0].phase, written[0].span_id), ("q1", "start", "q1-rtd"))
        self.assertEqual(written[0].wall, rtd_replay.iso(at(5.5)))
        self.assertEqual(written[1].wall, rtd_replay.iso(at(100.5)))
        self.assertEqual([c.wall for c in written[-2:] if isinstance(c, Add)][1], rtd_replay.iso(at(500.5)))
        self.assertIn("wrote 10 corrections", run.out.getvalue())
        before = run.sidecar.read_bytes()
        run.again("--write")
        self.assertEqual(run.sidecar.read_bytes(), before)
        self.assertIn("nothing new to write", run.out.getvalue())
        self.assertEqual(run.code, quarters.EXIT_CLEAN)

    def test_a_last_two_minutes_tap_adds_two_more(self):
        entries = [entry(7, quarters.L2M_TAP, 450)]
        run = self.run_of(entries, pcap_of(GAME_WRITES), "--write")
        self.assertEqual(run.code, quarters.EXIT_CLEAN, run.err.getvalue())
        written = corrections.read_corrections(run.sidecar)
        self.assertEqual(len(written), 12)
        self.assertEqual(
            sum(1 for c in written if isinstance(c, Add) and c.span_id == ann.span_id_for("last-two-minutes", 7)), 2
        )

    def test_main_clock_slips_change_nothing(self):
        plain = self.run_of([], pcap_of(GAME_WRITES))
        slip = self.run_of([], pcap_of({**GAME_WRITES, **CLOCK_SLIP}))

        def scrub(run: Run) -> str:
            return run.out.getvalue().replace(str(run.log.parent), "")

        self.assertEqual(scrub(slip), scrub(plain))

    def test_silence_just_before_a_zero_makes_both_periods_beside_it_untrusted(self):
        run = self.run_of([], pcap_of(GAME_WRITES, silent=range(292, 300)))
        self.assertEqual(run.code, quarters.EXIT_ATTENTION)
        text = run.out.getvalue()
        self.assertIn("untrusted (interrupted)", text)
        self.assertIn("(attention: halftime, q3)", text)

    def test_a_faulted_capture_exits_1(self):
        bad = real.frame(3, "x", checksum=b"00")
        run = self.run_of([], pcap_of({**GAME_WRITES, 15: [bad]}))
        self.assertEqual(run.code, quarters.EXIT_ATTENTION)

    def test_missing_dpkt_says_how_to_install_and_exits_2(self):
        import importlib

        import tacet

        saved = importlib.import_module("tacet.pcap")
        del tacet.pcap
        try:
            with mock.patch.dict(sys.modules, {"dpkt": None}):
                sys.modules.pop("tacet.pcap", None)
                err = io.StringIO()
                code = quarters.main(["a.jsonl", "a.pcap"], out=io.StringIO(), err=err)
        finally:
            tacet.pcap = saved
            sys.modules["tacet.pcap"] = saved
        self.assertEqual(code, quarters.EXIT_REFUSED)
        self.assertIn(".[analysis]", err.getvalue())

    def test_a_corrupt_log_exits_2(self):
        run = self.run_of([], pcap_of(GAME_WRITES))
        run.log.write_text("{nope\n{nope\n", encoding="utf-8")
        run.again()
        self.assertEqual(run.code, quarters.EXIT_REFUSED)
        self.assertIn("tacet-quarters:", run.err.getvalue())

    def test_a_malformed_sidecar_exits_2_and_is_left_alone(self):
        run = self.run_of([], pcap_of(GAME_WRITES))
        run.sidecar.write_text('{"v":1,"action":"delete"}\n', encoding="utf-8")
        before = run.sidecar.read_bytes()
        run.again("--write")
        self.assertEqual(run.code, quarters.EXIT_REFUSED)
        self.assertEqual(run.sidecar.read_bytes(), before)
        self.assertIn("line 1", run.err.getvalue())

    def test_a_capture_with_no_rtd_datagrams_exits_2(self):
        other = cap.pcap_bytes([cap.record(START, cap.rtd_packet(TIME_OF_DAY, dport=9999))])
        run = self.run_of([], other)
        self.assertEqual(run.code, quarters.EXIT_REFUSED)
        self.assertIn("no RTD datagrams", run.err.getvalue())

    def test_an_unreadable_capture_and_a_missing_log_exit_2(self):
        run = self.run_of([], b"this is not a capture at all, just text")
        self.assertEqual(run.code, quarters.EXIT_REFUSED)
        err = io.StringIO()
        code = quarters.main(["/nonexistent/none.jsonl", "/nonexistent/none.pcap"], out=io.StringIO(), err=err)
        self.assertEqual(code, quarters.EXIT_REFUSED)

    def test_the_port_flag(self):
        other = 9999
        run = self.run_of([], pcap_of(GAME_WRITES, dport=other))
        self.assertEqual(run.code, quarters.EXIT_REFUSED)
        run.again("--port", str(other))
        self.assertEqual(run.code, quarters.EXIT_CLEAN, run.err.getvalue())

    def test_output_is_ascii(self):
        self.assertTrue(self.run_of([], pcap_of(GAME_WRITES)).out.getvalue().isascii())

    def test_runs_as_a_module(self):
        run = self.run_of([], pcap_of(GAME_WRITES))
        result = subprocess.run(
            [sys.executable, "-m", "tacet.quarters", str(run.log), str(run.pcap)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VERDICT", result.stdout)
        self.assertFalse(run.sidecar.exists())

    def test_console_script_is_declared(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["tacet-quarters"], "tacet.quarters:main")


if __name__ == "__main__":
    unittest.main()
