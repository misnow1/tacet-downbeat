"""Lay the scoreboard's quarters and halftime beside a game's log, after the game (#212).

    tacet-quarters GAME.jsonl CAPTURE.pcap [--write] [--port N]

Quarters and halftime are no longer tapped (#155), and Last two minutes is a
single tap with no end (#216). The scoreboard knows where every period ends -
its clock reaches zero and the board raises a flag - and the capture of its RTD
stream (`tacet-rtd`, #210) has it to the tick.

For each of Q1 to Q4 and halftime this finds the period (`tacet.periods`) and
proposes its span as two sidecar `add` lines under the period's own key, with
the span id `<key>-rtd` (`q1-rtd`). The periods tile the game: each starts at
the clock-at-zero that ended the one before, so Q1 starts at the pregame
countdown's zero and Q3 at halftime's. Where no flag was seen the quarter field
changing is the boundary instead.

For each `last-two-minutes-tap`, the first in a quarter (Q1 to Q4, any of
them) is proposed as a span under the retired key `last-two-minutes` (an `add`
of a span needs a phase and a span id, and an instant forbids them): it starts
at the tap less its logged delay and ends where the period does. Games 2 to 4
carry `last-two-minutes` spans of their own; those are not read here.

This never reads the main clock: its value slipped to 20:00 at Game 4 and says
nothing about where a period ends. Nothing is written without `--write`, and
the log itself is never edited: a proposal goes to the corrections sidecar
(`tacet.corrections`), where a hand correction already present always wins.

The period verdicts:

    span-added       a trusted start and end; the proposal adds the span
    already-spanned  the log, or a sidecar add under another id, already starts it
    untrusted        a doubt: a gap or a new source near a boundary, or shown twice
    no-start         the capture began in it
    no-end           the capture ended in it
    missing          the board never showed it

The tap verdicts:

    span-added       the first tap in a quarter whose end is trusted
    repeat-tap       a later tap in the same quarter
    untrusted        the quarter's end has a doubt
    no-end           the capture ended before the quarter did
    not-in-a-quarter pregame, halftime, overtime or after the game
    outside-capture  the capture does not cover the tap

Overtime, FINAL and any period the board shows that is not one of those are
listed as other periods and never proposed or failed on.

Exit 0: every period and tap was proposed or already there, and the capture is
clean. Exit 1: a period or tap needs a look, or the capture faulted or ends
mid-record. Exit 2: it could not run. Already-spanned periods, repeat taps and
other periods never fail the run.

The standard library only at import time: dpkt is imported when the capture is
read, so a machine without the `analysis` extra gets one clear line. This tool
reads no `tacet.toml`: it is not run on the box.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import TextIO, TypeVar

from . import annotations as ann
from . import corrections, periods, rtd_replay
from .annotations import Entry
from .corrections import Add, Correction
from .periods import Board, Edge, Period, PeriodSpan
from .taps import delay_of
from .timeouts import _ascii, _clock, _listed, _plural, _span_text, wall_time

DPKT_MODULE = "dpkt"
DPKT_HINT = "tacet-quarters needs dpkt: pip install -e '.[analysis]'"

EXIT_CLEAN = 0
EXIT_ATTENTION = 1
EXIT_REFUSED = 2

#: What a correction from here says it came from.
SOURCE = "rtd"
#: The span key of each period, in game order. A test pins these against `annotations.EVENTS`.
PERIOD_KEYS: Mapping[Period, str] = {
    Period.Q1: "q1",
    Period.Q2: "q2",
    Period.HALFTIME: "halftime",
    Period.Q3: "q3",
    Period.Q4: "q4",
}
#: Where a Last two minutes tap may fall.
QUARTERS: tuple[Period, ...] = (Period.Q1, Period.Q2, Period.Q3, Period.Q4)
L2M_TAP = "last-two-minutes-tap"
#: The retired span key: an add of a span needs a phase and a span id, and an instant forbids them (#216).
L2M_SPAN = "last-two-minutes"
#: A period span has no tap to take an id from, and log ids are always `<key>-<seq>`, so this cannot collide.
PERIOD_SPAN_SUFFIX = SOURCE

START_REASONS: Mapping[Edge, str] = {
    Edge.CLOCK_AT_ZERO: "the start is the clock reaching zero as the period before it ended",
    Edge.QUARTER_FIELD: "the start is the scoreboard's quarter field changing to it",
}
END_REASONS: Mapping[Edge, str] = {
    Edge.CLOCK_AT_ZERO: "the end is the clock reaching zero",
    Edge.QUARTER_FIELD: "the end is the scoreboard's quarter field moving on",
}

CLOCK_FORMAT = "%H:%M:%S"
NONE_WORD = "none"
NOT_APPLICABLE = "-"
INDENT = "  "

V = TypeVar("V")


class PeriodVerdict(StrEnum):
    SPAN_ADDED = "span-added"
    ALREADY_SPANNED = "already-spanned"
    UNTRUSTED = "untrusted"
    NO_START = "no-start"
    NO_END = "no-end"
    MISSING = "missing"


#: Verdicts that mean a person has to look.
PERIOD_ATTENTION = (
    PeriodVerdict.UNTRUSTED,
    PeriodVerdict.NO_START,
    PeriodVerdict.NO_END,
    PeriodVerdict.MISSING,
)


class TapVerdict(StrEnum):
    SPAN_ADDED = "span-added"
    REPEAT_TAP = "repeat-tap"
    UNTRUSTED = "untrusted"
    NO_END = "no-end"
    NOT_IN_A_QUARTER = "not-in-a-quarter"
    OUTSIDE_CAPTURE = "outside-capture"


TAP_ATTENTION = (
    TapVerdict.UNTRUSTED,
    TapVerdict.NO_END,
    TapVerdict.NOT_IN_A_QUARTER,
    TapVerdict.OUTSIDE_CAPTURE,
)


def period_span_id(key: str) -> str:
    return f"{key}-{PERIOD_SPAN_SUFFIX}"


# -- the comparison ---------------------------------------------------------


@dataclass(frozen=True)
class PeriodMatch:
    key: str
    verdict: PeriodVerdict
    #: The first showing of that period.
    span: PeriodSpan | None = None


@dataclass(frozen=True)
class TapMatch:
    tap: Entry
    verdict: TapVerdict
    period: PeriodSpan | None = None

    @property
    def start_time(self) -> float:
        """The tap's own time less its logged delay; a negative or missing delay moves nothing."""
        return wall_time(self.tap) - max(0.0, delay_of(self.tap.data) or 0.0)


@dataclass(frozen=True)
class Comparison:
    #: Always the five periods, in `PERIOD_KEYS` order.
    periods: tuple[PeriodMatch, ...]
    #: In log order.
    taps: tuple[TapMatch, ...]
    #: The pregame and other showings, which are never proposed.
    others: tuple[PeriodSpan, ...]


def l2m_taps(entries: Sequence[Entry]) -> tuple[Entry, ...]:
    """The Last two minutes taps, in log order. The older `last-two-minutes` spans are not read."""
    return tuple(e for e in entries if e.event == L2M_TAP)


def _spanned_elsewhere(key: str, entries: Sequence[Entry], existing: Sequence[Correction]) -> bool:
    """The log or the sidecar already starts this period, under an id that is not ours.

    Our own id in the sidecar is left to `corrections.unrecorded`, so a rerun says "already in".
    """
    if any(e.event == key and e.phase == ann.PHASE_START for e in entries):
        return True
    ours = period_span_id(key)
    return any(
        isinstance(c, Add) and c.event == key and c.phase == ann.PHASE_START and c.span_id != ours for c in existing
    )


def _span_at(time: float, spans: Sequence[PeriodSpan]) -> PeriodSpan | None:
    for span in spans:
        lo = span.start.time if span.start is not None else span.shown_from
        hi = span.end.time if span.end is not None else float("inf")
        if lo <= time < hi:
            return span
    return None


def _period_verdict(span: PeriodSpan | None, elsewhere: bool) -> PeriodVerdict:
    if span is None:
        return PeriodVerdict.MISSING
    if elsewhere:
        return PeriodVerdict.ALREADY_SPANNED
    if span.start is None:
        return PeriodVerdict.NO_START
    if span.end is None:
        return PeriodVerdict.NO_END
    return PeriodVerdict.UNTRUSTED if span.doubts else PeriodVerdict.SPAN_ADDED


def _tap_verdict(span: PeriodSpan | None, repeat: bool) -> TapVerdict:
    if span is None or span.period not in QUARTERS:
        return TapVerdict.NOT_IN_A_QUARTER
    if repeat:
        return TapVerdict.REPEAT_TAP
    if span.end is None:
        return TapVerdict.NO_END
    if span.end.doubts or span.own_doubts:
        return TapVerdict.UNTRUSTED
    return TapVerdict.SPAN_ADDED


def compare(
    entries: Sequence[Entry],
    board: Board,
    existing: Sequence[Correction],
    *,
    capture_start: float,
    capture_end: float,
) -> Comparison:
    first: dict[Period, PeriodSpan] = {}
    for showing in board.spans:
        first.setdefault(showing.period, showing)
    found = tuple(
        PeriodMatch(
            key,
            _period_verdict(first.get(period), _spanned_elsewhere(key, entries, existing)),
            first.get(period),
        )
        for period, key in PERIOD_KEYS.items()
    )
    tapped: set[int] = set()
    matches: list[TapMatch] = []
    for tap in l2m_taps(entries):
        time = wall_time(tap)
        if not capture_start <= time <= capture_end:
            matches.append(TapMatch(tap, TapVerdict.OUTSIDE_CAPTURE))
            continue
        span = _span_at(time, board.spans)
        index = -1 if span is None else board.spans.index(span)
        matches.append(TapMatch(tap, _tap_verdict(span, index in tapped), span))
        tapped.add(index)
    others = tuple(s for s in board.spans if s.period is Period.PREGAME or s.period is Period.OTHER)
    return Comparison(found, tuple(matches), others)


# -- the proposals ----------------------------------------------------------


def _period_adds(match: PeriodMatch, capture: str) -> tuple[Add, Add]:
    span = match.span
    assert span is not None and span.start is not None and span.end is not None
    evidence: dict[str, object] = {
        "capture": capture,
        "start_by": span.start.edge.value,
        "end_by": span.end.edge.value,
        "quarter_from": rtd_replay.iso(span.shown_from),
        "clock_at_zero_rises": len(span.zeros),
    }
    if span.shown_to is not None:
        evidence["quarter_to"] = rtd_replay.iso(span.shown_to)
    shown = f"the scoreboard showed {match.key}"

    def add(phase: str, boundary: periods.Boundary, why: str) -> Add:
        return Add(
            event=match.key,
            wall=rtd_replay.iso(boundary.time),
            source=SOURCE,
            reason=f"{shown}; {why}",
            phase=phase,
            span_id=period_span_id(match.key),
            evidence=evidence,
        )

    return (
        add(ann.PHASE_START, span.start, START_REASONS[span.start.edge]),
        add(ann.PHASE_END, span.end, END_REASONS[span.end.edge]),
    )


def _tap_adds(match: TapMatch, capture: str) -> tuple[Add, Add]:
    span = match.period
    assert span is not None and span.end is not None
    key = PERIOD_KEYS[span.period]
    evidence: dict[str, object] = {
        "capture": capture,
        "period": key,
        "period_end": rtd_replay.iso(span.end.time),
        "end_by": span.end.edge.value,
        "tap_seq": match.tap.seq,
    }
    span_id = ann.span_id_for(L2M_SPAN, match.tap.seq)
    in_period = f"a single tap in {key}"

    def add(phase: str, wall: float, why: str) -> Add:
        return Add(
            event=L2M_SPAN,
            wall=rtd_replay.iso(wall),
            source=SOURCE,
            reason=f"{in_period}; {why}",
            phase=phase,
            span_id=span_id,
            evidence=evidence,
        )

    return (
        add(ann.PHASE_START, match.start_time, "the start is the tap"),
        add(ann.PHASE_END, span.end.time, f"the end is the end of the period ({END_REASONS[span.end.edge]})"),
    )


def propose(comparison: Comparison, *, capture: str) -> tuple[Add, ...]:
    """What RTD says the log should say: a pair of adds per span, ordered by the span's start."""
    found: list[tuple[float, tuple[Add, Add]]] = []
    for period in comparison.periods:
        if period.verdict is PeriodVerdict.SPAN_ADDED and period.span is not None:
            pair = _period_adds(period, capture)
            found.append((_wall_of(pair[0]), pair))
    for tap in comparison.taps:
        if tap.verdict is TapVerdict.SPAN_ADDED:
            pair = _tap_adds(tap, capture)
            found.append((_wall_of(pair[0]), pair))
    found.sort(key=lambda item: item[0])
    return tuple(add for _, pair in found for add in pair)


def _wall_of(add: Add) -> float:
    return datetime.fromisoformat(add.wall).timestamp()


# -- the report -------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    log: Path
    capture: Path
    board: Board
    comparison: Comparison
    summary: rtd_replay.Summary
    #: Not yet in the sidecar.
    fresh: tuple[Correction, ...]
    #: Covered by a line already in the sidecar, whoever wrote it.
    already: tuple[Correction, ...]
    taps: int = 0

    @property
    def sidecar(self) -> Path:
        return corrections.sidecar_path(self.log)


def build_report(
    entries: Sequence[Entry],
    board: Board,
    summary: rtd_replay.Summary,
    *,
    existing: Sequence[Correction],
    log: Path,
    capture: Path,
) -> Report:
    """The pure part: everything the command prints, from what it read."""
    first, last = summary.first_time, summary.last_time
    comparison = compare(
        entries,
        board,
        existing,
        capture_start=0.0 if first is None else first,
        capture_end=0.0 if last is None else last,
    )
    fresh, already = corrections.unrecorded(propose(comparison, capture=capture.name), existing)
    return Report(log, capture, board, comparison, summary, tuple(fresh), tuple(already), len(l2m_taps(entries)))


def exit_code(report: Report) -> int:
    summary = report.summary
    if summary.faults or summary.truncated_file:
        return EXIT_ATTENTION
    comparison = report.comparison
    if any(p.verdict in PERIOD_ATTENTION for p in comparison.periods):
        return EXIT_ATTENTION
    if any(t.verdict in TAP_ATTENTION for t in comparison.taps):
        return EXIT_ATTENTION
    return EXIT_CLEAN


# -- rendering --------------------------------------------------------------


def _period_cells(match: PeriodMatch, tz: tzinfo | None) -> list[str]:
    span = match.span
    if span is None:
        return [match.key, *([NOT_APPLICABLE] * 4), match.verdict.value]
    start = NOT_APPLICABLE if span.start is None else _clock(span.start.time, tz)
    end = NOT_APPLICABLE if span.end is None else _clock(span.end.time, tz)
    text = match.verdict.value
    if match.verdict is PeriodVerdict.UNTRUSTED:
        text += f" ({', '.join(span.doubts)})"
    if len(span.zeros) > 1:
        text += f"; {len(span.zeros)} clock-at-zero rises, the last used"
    return [
        match.key,
        _span_text(span.shown_from, span.shown_to, tz),
        f"{start}-{end}",
        NOT_APPLICABLE if span.start is None else span.start.edge.value,
        NOT_APPLICABLE if span.end is None else span.end.edge.value,
        text,
    ]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    everything = [list(headers), *rows]
    widths = [max(len(row[i]) for row in everything) for i in range(len(headers))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in everything
    ]


_PERIOD_HEADERS = ["period", "board", "span", "start", "end", "verdict"]
_TAP_HEADERS = ["seq", "tapped", "period", "period end", "verdict"]


def _tap_cells(match: TapMatch, tz: tzinfo | None) -> list[str]:
    span = match.period
    key = NOT_APPLICABLE if span is None else PERIOD_KEYS.get(span.period, span.period.value)
    end = NOT_APPLICABLE if span is None or span.end is None else _clock(span.end.time, tz)
    text = match.verdict.value
    if match.verdict is TapVerdict.UNTRUSTED and span is not None:
        text += f" ({', '.join(span.end.doubts if span.end else ())})"
    return [str(match.tap.seq), _clock(wall_time(match.tap), tz), key, end, text]


def _header(report: Report, tz: tzinfo | None) -> list[str]:
    summary = report.summary
    first, last = summary.first_time, summary.last_time
    window = NOT_APPLICABLE if first is None or last is None else _span_text(first, last, tz)
    zone = NOT_APPLICABLE if first is None else datetime.fromtimestamp(first, tz).astimezone(tz).tzname()
    lines = [
        f"log: {_ascii(report.log.name)}",
        f"{INDENT}{_plural(report.taps, 'last two minutes tap')}",
        f"capture: {_ascii(report.capture.name)}",
        f"{INDENT}{window} ({zone}), {_plural(sum(summary.faults.values()), 'fault')}, "
        f"{_plural(len(summary.gaps), 'gap')}, {_plural(len(summary.sources), 'source')}",
    ]
    lines += [f"{INDENT}gap: {_clock(since, tz)} for {round(seconds)} s" for since, seconds in summary.gaps]
    lines += [f"{INDENT}source {address} from {_clock(time, tz)}" for time, address in summary.sources]
    if summary.truncated_file:
        lines.append(f"{INDENT}the capture ends in the middle of a record")
    board = report.board
    lines.append(
        f"board: {_plural(len(board.spans), 'period')} shown, {_plural(len(board.zeros), 'clock-at-zero rise')}"
    )
    return lines


_PERIOD_LABELS: Mapping[PeriodVerdict, str] = {
    PeriodVerdict.SPAN_ADDED: "span added",
    PeriodVerdict.ALREADY_SPANNED: "already spanned",
    PeriodVerdict.UNTRUSTED: "not checked",
    PeriodVerdict.NO_START: "with no start",
    PeriodVerdict.NO_END: "with no end",
    PeriodVerdict.MISSING: "missing",
}
_TAP_LABELS: Mapping[TapVerdict, str] = {
    TapVerdict.SPAN_ADDED: "span added",
    TapVerdict.REPEAT_TAP: "repeat tap",
    TapVerdict.UNTRUSTED: "not checked",
    TapVerdict.NO_END: "with no end",
    TapVerdict.NOT_IN_A_QUARTER: "not in a quarter",
    TapVerdict.OUTSIDE_CAPTURE: "outside the capture",
}


def _counted(verdicts: Sequence[V], labels: Mapping[V, str]) -> list[str]:
    return [f"{n} {label}" for v, label in labels.items() if (n := sum(1 for x in verdicts if x is v))]


def _verdict_line(report: Report) -> str:
    comparison = report.comparison
    parts = [
        _plural(len(comparison.periods), "period"),
        *_counted([p.verdict for p in comparison.periods], _PERIOD_LABELS),
    ]
    if comparison.taps:
        parts.append(_plural(len(comparison.taps), "last two minutes tap"))
        parts += _counted([t.verdict for t in comparison.taps], _TAP_LABELS)
    flagged = [p.key for p in comparison.periods if p.verdict in PERIOD_ATTENTION]
    flagged += [f"seq {t.tap.seq}" for t in comparison.taps if t.verdict in TAP_ATTENTION]
    text = f"VERDICT: {'; '.join(parts)}"
    return text + (f" (attention: {', '.join(flagged)})" if flagged else "")


def _proposal_lines(report: Report) -> list[str]:
    fresh, already = len(report.fresh), len(report.already)
    text = f"{_plural(fresh + already, 'correction')} proposed, {already} already in {_ascii(report.sidecar.name)}"
    lines = [text]
    if fresh:
        lines.append(f"run again with --write to append {fresh} to {_ascii(str(report.sidecar))}")
    return lines


def render(report: Report, *, tz: tzinfo | None) -> str:
    """The report as plain ASCII text."""
    comparison = report.comparison
    lines = _header(report, tz)
    lines += ["", *_table(_PERIOD_HEADERS, [_period_cells(p, tz) for p in comparison.periods]), ""]
    others = [
        f"{s.period.value} {json.dumps(s.shown)} {_span_text(s.shown_from, s.shown_to, tz)}" for s in comparison.others
    ]
    lines += _listed("other periods shown", others)
    if comparison.taps:
        lines += _listed_table("last two minutes taps", _TAP_HEADERS, [_tap_cells(t, tz) for t in comparison.taps])
    else:
        lines.append(f"last two minutes taps: {NONE_WORD}")
    lines += _proposal_lines(report)
    lines.append(_verdict_line(report))
    return "\n".join(lines) + "\n"


def _listed_table(label: str, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [f"{label}:", *_table(headers, rows)]


# -- the command ------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tacet-quarters",
        description="Find a game's quarters and halftime from the scoreboard, and propose them as sidecar adds.",
    )
    p.add_argument("log", type=Path, help="the box's annotation log (JSONL)")
    p.add_argument("capture", type=Path, help="a pcap or pcapng capture of the RTD stream")
    p.add_argument(
        "--write",
        action="store_true",
        help="append the proposed corrections to the sidecar beside the log (nothing is written without this)",
    )
    port = rtd_replay.DEFAULT_PORT
    p.add_argument("--port", type=int, default=port, help=f"UDP destination port (default {port})")
    return p


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    tz: tzinfo | None = None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = parser().parse_args(argv)
    try:
        from . import pcap
    except ModuleNotFoundError as exc:
        if exc.name != DPKT_MODULE:
            raise
        print(DPKT_HINT, file=err)
        return EXIT_REFUSED
    sidecar = corrections.sidecar_path(args.log)
    try:
        entries = list(ann.read_entries(args.log))
        existing = corrections.read_corrections(sidecar)
        events, summary = rtd_replay.read_capture(args.capture, port=args.port)
    except (ann.AnnotationError, pcap.PcapFormatError, UnicodeDecodeError, OSError) as exc:
        print(f"tacet-quarters: {_ascii(str(exc))}", file=err)
        return EXIT_REFUSED
    if summary.rtd_datagrams == 0:
        print(f"tacet-quarters: no RTD datagrams on UDP port {args.port}", file=err)
        return EXIT_REFUSED
    board = periods.scan(events)
    report = build_report(entries, board, summary, existing=existing, log=args.log, capture=args.capture)
    print(render(report, tz=tz), end="", file=out)
    if args.write:
        try:
            corrections.append_corrections(sidecar, report.fresh)
        except OSError as exc:
            print(f"tacet-quarters: {_ascii(str(exc))}", file=err)
            return EXIT_REFUSED
        print(_written_line(report), file=out)
    return exit_code(report)


def _written_line(report: Report) -> str:
    if not report.fresh:
        return "nothing new to write"
    return f"wrote {_plural(len(report.fresh), 'correction')} to {_ascii(str(report.sidecar))}"


if __name__ == "__main__":
    raise SystemExit(main())
