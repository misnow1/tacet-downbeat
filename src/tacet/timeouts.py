"""Lay a game's tapped timeouts beside the scoreboard's play clock, after the game (#205).

    tacet-timeouts GAME.jsonl CAPTURE.pcap [--write] [--port N]

The operator taps a timeout's start and kind but not its end: the end comes
late or from memory, and asking for more diligence does not fix it, because the
operator is alone in the booth and watching the conductor. The scoreboard knows
when play is ready again - the play clock counts down - and the capture of its
RTD stream (`tacet-rtd`, #210) has it to the tick.

For every tapped timeout this finds the stoppage (`tacet.stoppages`) its start
fell in, says what RTD makes of its end and its kind, and proposes corrections.
Nothing is written without `--write`, and the log itself is never edited: a
proposal goes to the corrections sidecar (`tacet.corrections`), where a hand
correction already present always wins.

Matching uses the tap's start only, because the end is the thing being
corrected. A tap lags its hold by a second or by two minutes, so a tap whose
start falls anywhere inside a hold, or `TAP_BEFORE_HOLD_SECONDS` before it,
belongs to it. The verdicts:

    agrees           the tapped end is within a tick of ready-for-play
    end-corrected    it is not; the proposal moves it to the first tick
    end-added        the span was never ended; the proposal adds the end
    nested           the tap ended inside a hold that a later tap ends
    untrusted        the hold has a doubt (capture start, source change, gap, too long)
    no-stoppage      the play clock was counting down when the timeout was tapped
    outside-capture  the capture does not cover the tap

A generic `timeout` whose hold saw exactly one team's timeouts-left drop is
proposed as that team's kind. A tap of any other kind that disagrees is
reported as a conflict and never corrected.

Exit 0: every tap matched a trusted hold and the capture is clean. Exit 1: a
tap was untrusted, unmatched or in conflict, or the capture faulted or ends
mid-record. Exit 2: it could not run. Proposals, nested taps, holds with no tap
and stray marks never fail the run.

The standard library only at import time: dpkt is imported when the capture is
read, so a machine without the `analysis` extra gets one clear line. This tool
reads no `tacet.toml`: it is not run on the box.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from . import annotations as ann
from . import corrections, rtd_replay, stoppages
from .annotations import Entry
from .corrections import Add, Correction, Supersede
from .stoppages import MIN_HOLD_SECONDS, Scan, Stoppage, Team, TeamTimeout

DPKT_MODULE = "dpkt"
DPKT_HINT = "tacet-timeouts needs dpkt: pip install -e '.[analysis]'"

EXIT_CLEAN = 0
EXIT_ATTENTION = 1
EXIT_REFUSED = 2

#: Every timeout key in the vocabulary; all spans. A test pins this against `annotations.EVENTS`.
TIMEOUT_KEYS = ("timeout", "timeout-official", "timeout-media", "timeout-injury", "timeout-home", "timeout-away")
#: The default button, and the announcer's wording: a timeout marked at once beats one that went unmarked.
GENERIC_TIMEOUT = "timeout"
#: RTD's guest team is the log's away team.
TEAM_KEYS: Mapping[Team, str] = {Team.HOME: "timeout-home", Team.GUEST: "timeout-away"}
#: What a correction from here says it came from.
SOURCE = "rtd"
#: No game 4 tap preceded its hold (the nearest followed it by 1 s); this allows a slower scoreboard operator.
TAP_BEFORE_HOLD_SECONDS = 5.0
#: One play clock tick.
END_AGREEMENT_SECONDS = 1.0

CLOCK_FORMAT = "%H:%M:%S"
NONE_WORD = "none"
NOT_APPLICABLE = "-"
INDENT = "  "


class Verdict(StrEnum):
    AGREES = "agrees"
    END_CORRECTED = "end-corrected"
    END_ADDED = "end-added"
    NESTED = "nested"
    UNTRUSTED = "untrusted"
    NO_STOPPAGE = "no-stoppage"
    OUTSIDE_CAPTURE = "outside-capture"


#: Verdicts that mean a person has to look.
NEEDS_ATTENTION = (Verdict.UNTRUSTED, Verdict.NO_STOPPAGE, Verdict.OUTSIDE_CAPTURE)


# -- the taps ---------------------------------------------------------------


def wall_time(entry: Entry) -> float:
    return datetime.fromisoformat(entry.wall).timestamp()


@dataclass(frozen=True)
class TappedTimeout:
    #: The start entry's event: a span's kind is how it began.
    key: str
    span_id: str
    start: Entry
    end: Entry | None

    @property
    def start_time(self) -> float:
        return wall_time(self.start)

    @property
    def end_time(self) -> float | None:
        return None if self.end is None else wall_time(self.end)


@dataclass(frozen=True)
class Taps:
    #: In the order they were started.
    timeouts: tuple[TappedTimeout, ...]
    #: Ends whose start is not in the log.
    orphan_ends: tuple[Entry, ...]


def tapped(entries: Iterable[Entry]) -> Taps:
    starts: dict[str, Entry] = {}
    ends: dict[str, Entry] = {}
    orphans: list[Entry] = []
    for entry in entries:
        if entry.event not in TIMEOUT_KEYS or entry.span_id is None:
            continue
        if entry.phase == ann.PHASE_START:
            starts[entry.span_id] = entry
        elif entry.span_id in starts:
            ends[entry.span_id] = entry
        else:
            orphans.append(entry)
    found = tuple(TappedTimeout(s.event, span_id, s, ends.get(span_id)) for span_id, s in starts.items())
    return Taps(found, tuple(orphans))


# -- the comparison ---------------------------------------------------------


@dataclass(frozen=True)
class Match:
    tap: TappedTimeout
    verdict: Verdict
    stoppage: Stoppage | None = None
    #: Other holds that began while this tap was still open.
    covered: tuple[Stoppage, ...] = ()
    #: The kind RTD gives a generic tap, when the hold names exactly one team.
    reclassify_to: str | None = None
    #: The team RTD saw when the tap's kind says otherwise.
    conflict: Team | None = None
    notes: tuple[str, ...] = ()

    @property
    def lead(self) -> float | None:
        """Seconds the tap came after its hold began."""
        return None if self.stoppage is None else self.tap.start_time - self.stoppage.start

    @property
    def shift(self) -> float | None:
        """Seconds from the tapped end to the corrected one."""
        end = self.tap.end_time
        if self.stoppage is None or end is None:
            return None
        return self.stoppage.end - end


@dataclass(frozen=True)
class Comparison:
    matches: tuple[Match, ...]
    #: Holds no tap matched, in order.
    untapped: tuple[Stoppage, ...]
    #: Team timeouts that fell outside every hold.
    stray_marks: tuple[TeamTimeout, ...]
    orphan_ends: tuple[Entry, ...]


def _hold_for(time: float, holds: Sequence[Stoppage]) -> Stoppage | None:
    near = [h for h in holds if h.start - TAP_BEFORE_HOLD_SECONDS <= time <= h.end]
    inside = [h for h in near if h.start <= time]
    pool = inside or near
    return min(pool, key=lambda h: h.start) if pool else None


def _covered(tap: TappedTimeout, hold: Stoppage, holds: Sequence[Stoppage]) -> tuple[Stoppage, ...]:
    end = tap.end_time
    if end is None:
        return ()
    return tuple(h for h in holds if h is not hold and tap.start_time < h.start <= end)


def _end_of(tap: TappedTimeout) -> float:
    end = tap.end_time
    return float("inf") if end is None else end


def _end_verdict(tap: TappedTimeout, hold: Stoppage, last: TappedTimeout) -> Verdict:
    end = tap.end_time
    if end is None:
        return Verdict.END_ADDED
    if end > hold.end + END_AGREEMENT_SECONDS:
        return Verdict.END_CORRECTED
    if tap is last and end < hold.end - END_AGREEMENT_SECONDS:
        return Verdict.END_CORRECTED
    if abs(end - hold.end) <= END_AGREEMENT_SECONDS:
        return Verdict.AGREES
    return Verdict.NESTED


def _kind(tap: TappedTimeout, hold: Stoppage) -> tuple[str | None, Team | None, tuple[str, ...]]:
    """(reclassify to, conflicting team, notes) from the team timeouts that fell in the hold."""
    teams = sorted({m.team for m in hold.marks})
    if len(teams) > 1:
        return None, None, ("RTD shows both teams' timeouts dropping in this hold",)
    if not teams:
        if tap.key in TEAM_KEYS.values():
            return None, None, (f"tapped {tap.key} but RTD shows no team timeout in this hold",)
        return None, None, ()
    team = teams[0]
    if tap.key == GENERIC_TIMEOUT:
        return TEAM_KEYS[team], None, ()
    if tap.key == TEAM_KEYS[team]:
        return None, None, ()
    return None, team, (f"tapped {tap.key} but RTD shows a {team.value} timeout",)


def _match(tap: TappedTimeout, hold: Stoppage, group: Sequence[TappedTimeout], holds: Sequence[Stoppage]) -> Match:
    covered = _covered(tap, hold, holds)
    if not hold.trusted:
        return Match(tap, Verdict.UNTRUSTED, hold, covered)
    last = max(group, key=lambda t: _end_of(t))
    reclassify, conflict, notes = _kind(tap, hold)
    return Match(tap, _end_verdict(tap, hold, last), hold, covered, reclassify, conflict, notes)


def compare(taps: Taps, scan: Scan, *, capture_start: float, capture_end: float) -> Comparison:
    holds = scan.stoppages
    found = [(tap, _hold_for(tap.start_time, holds)) for tap in taps.timeouts]
    groups: dict[int, list[TappedTimeout]] = {}
    for tap, hold in found:
        if hold is not None:
            groups.setdefault(id(hold), []).append(tap)
    matches: list[Match] = []
    for tap, hold in found:
        if hold is not None:
            matches.append(_match(tap, hold, groups[id(hold)], holds))
        elif capture_start <= tap.start_time <= capture_end:
            matches.append(Match(tap, Verdict.NO_STOPPAGE))
        else:
            matches.append(Match(tap, Verdict.OUTSIDE_CAPTURE))
    matched = {id(hold) for _, hold in found if hold is not None}
    attached = {mark for hold in holds for mark in hold.marks}
    return Comparison(
        tuple(matches),
        tuple(h for h in holds if id(h) not in matched),
        tuple(m for m in scan.team_timeouts if m not in attached),
        taps.orphan_ends,
    )


# -- the proposals ----------------------------------------------------------


def _seconds(seconds: float) -> int:
    return round(seconds)


def _end_reason(match: Match, hold: Stoppage) -> str:
    held = f"play clock counted down after a {_seconds(hold.held)} s hold"
    shift = match.shift
    if shift is None:
        return f"{held}; the span was never ended"
    way = "late" if shift < 0 else "early"
    return f"{held}; the tapped end was {_seconds(abs(shift))} s {way}"


def _end_proposal(match: Match, hold: Stoppage, capture: str) -> Correction | None:
    tap = match.tap
    evidence: dict[str, object] = {
        "capture": capture,
        "hold_start": rtd_replay.iso(hold.start),
        "hold_end": rtd_replay.iso(hold.end),
    }
    wall = rtd_replay.iso(hold.end)
    if match.verdict is Verdict.END_ADDED:
        return Add(
            event=tap.key,
            wall=wall,
            source=SOURCE,
            reason=_end_reason(match, hold),
            phase=ann.PHASE_END,
            span_id=tap.span_id,
            evidence=evidence,
        )
    if match.verdict is Verdict.END_CORRECTED and tap.end is not None:
        evidence["superseded_wall"] = tap.end.wall
        return Supersede(seq=tap.end.seq, source=SOURCE, reason=_end_reason(match, hold), wall=wall, evidence=evidence)
    return None


def _kind_proposal(match: Match, hold: Stoppage, capture: str) -> Correction | None:
    if match.reclassify_to is None:
        return None
    team = hold.marks[0].team
    return Supersede(
        seq=match.tap.start.seq,
        source=SOURCE,
        reason=f"the scoreboard's {team.value} timeouts left dropped during a {_seconds(hold.held)} s hold",
        event=match.reclassify_to,
        evidence={"capture": capture, "team": team.value, "team_timeout": rtd_replay.iso(hold.marks[0].time)},
    )


def propose(comparison: Comparison, *, capture: str) -> tuple[Correction, ...]:
    """What RTD says the log should say, ordered by the entry each correction is about."""
    found: list[tuple[int, Correction]] = []
    for match in comparison.matches:
        hold = match.stoppage
        if hold is None or match.verdict is Verdict.UNTRUSTED:
            continue
        for item in (_end_proposal(match, hold, capture), _kind_proposal(match, hold, capture)):
            if item is not None:
                seq = item.seq if isinstance(item, Supersede) else match.tap.start.seq
                found.append((seq, item))
    found.sort(key=lambda pair: pair[0])
    return tuple(item for _, item in found)


# -- the report -------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    log: Path
    capture: Path
    taps: Taps
    scan: Scan
    comparison: Comparison
    summary: rtd_replay.Summary
    #: Not yet in the sidecar.
    fresh: tuple[Correction, ...]
    #: Covered by a line already in the sidecar, whoever wrote it.
    already: tuple[Correction, ...]

    @property
    def sidecar(self) -> Path:
        return corrections.sidecar_path(self.log)


def build_report(
    entries: Sequence[Entry],
    scan: Scan,
    summary: rtd_replay.Summary,
    *,
    existing: Sequence[Correction],
    log: Path,
    capture: Path,
) -> Report:
    """The pure part: everything the command prints, from what it read."""
    taps = tapped(entries)
    first, last = summary.first_time, summary.last_time
    comparison = compare(
        taps,
        scan,
        capture_start=0.0 if first is None else first,
        capture_end=0.0 if last is None else last,
    )
    fresh, already = corrections.unrecorded(propose(comparison, capture=capture.name), existing)
    return Report(log, capture, taps, scan, comparison, summary, tuple(fresh), tuple(already))


def exit_code(report: Report) -> int:
    summary = report.summary
    if summary.faults or summary.truncated_file:
        return EXIT_ATTENTION
    matches = report.comparison.matches
    if any(m.verdict in NEEDS_ATTENTION or m.conflict is not None for m in matches):
        return EXIT_ATTENTION
    return EXIT_CLEAN


# -- rendering --------------------------------------------------------------


def _clock(time: float, tz: tzinfo | None) -> str:
    return datetime.fromtimestamp(time, tz).strftime(CLOCK_FORMAT)


def _span_text(start: float, end: float | None, tz: tzinfo | None) -> str:
    return f"{_clock(start, tz)}-{NOT_APPLICABLE if end is None else _clock(end, tz)}"


def _signed(seconds: float | None) -> str:
    return NOT_APPLICABLE if seconds is None else f"{_seconds(seconds):+d}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _teams_of(hold: Stoppage) -> str:
    teams = sorted({m.team.value for m in hold.marks})
    return f" ({'+'.join(teams)})" if teams else ""


def _verdict_text(match: Match, tz: tzinfo | None) -> str:
    text = match.verdict.value
    if match.verdict is Verdict.UNTRUSTED and match.stoppage is not None:
        text += f" ({', '.join(match.stoppage.doubts)})"
    if match.reclassify_to is not None:
        text += f"; reclassify to {match.reclassify_to}"
    if match.covered:
        text += f"; covers {', '.join(_clock(h.start, tz) for h in match.covered)}"
    if match.conflict is not None:
        text += "; KIND CONFLICT"
    return "; ".join([text, *match.notes])


def _seq_text(tap: TappedTimeout) -> str:
    return f"{tap.start.seq}-{'' if tap.end is None else tap.end.seq}"


_TABLE_HEADERS = ["seq", "kind", "tapped", "RTD hold", "lead", "shift", "verdict"]


def _match_row(match: Match, tz: tzinfo | None) -> list[str]:
    tap, hold = match.tap, match.stoppage
    held = NOT_APPLICABLE if hold is None else _span_text(hold.start, hold.end, tz) + _teams_of(hold)
    corrected = match.verdict in (Verdict.END_CORRECTED, Verdict.END_ADDED)
    return [
        _seq_text(tap),
        tap.key,
        _span_text(tap.start_time, tap.end_time, tz),
        held,
        _signed(match.lead),
        _signed(match.shift) if corrected else NOT_APPLICABLE,
        _verdict_text(match, tz),
    ]


def _table(rows: Sequence[list[str]]) -> list[str]:
    everything = [_TABLE_HEADERS, *rows]
    widths = [max(len(row[i]) for row in everything) for i in range(len(_TABLE_HEADERS))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in everything
    ]


def _hold_line(hold: Stoppage, tz: tzinfo | None) -> str:
    doubt = f"; {', '.join(hold.doubts)}" if hold.doubts else ""
    return f"{_span_text(hold.start, hold.end, tz)}  {_seconds(hold.held)} s{_teams_of(hold)}{doubt}"


def _listed(label: str, lines: Sequence[str]) -> list[str]:
    return [f"{label}:", *(INDENT + line for line in lines)] if lines else [f"{label}: {NONE_WORD}"]


def _header(report: Report, tz: tzinfo | None) -> list[str]:
    taps, summary = report.taps, report.summary
    open_spans = sum(1 for t in taps.timeouts if t.end is None)
    first, last = summary.first_time, summary.last_time
    window = NOT_APPLICABLE if first is None or last is None else _span_text(first, last, tz)
    zone = NOT_APPLICABLE if first is None else datetime.fromtimestamp(first, tz).astimezone(tz).tzname()
    lines = [
        f"log: {_ascii(report.log.name)}",
        f"{INDENT}{_plural(len(taps.timeouts), 'timeout span')}, {open_spans} open, "
        f"{_plural(len(taps.orphan_ends), 'orphan end')}",
        f"capture: {_ascii(report.capture.name)}",
        f"{INDENT}{window} ({zone}), {_plural(sum(summary.faults.values()), 'fault')}, "
        f"{_plural(len(summary.gaps), 'gap')}, {_plural(len(summary.sources), 'source')}",
    ]
    lines += [f"{INDENT}gap: {_clock(since, tz)} for {_seconds(seconds)} s" for since, seconds in summary.gaps]
    lines += [f"{INDENT}source {address} from {_clock(time, tz)}" for time, address in summary.sources]
    if summary.truncated_file:
        lines.append(f"{INDENT}the capture ends in the middle of a record")
    if report.scan.open_hold_since is not None:
        lines.append(f"{INDENT}the capture ends mid-hold from {_clock(report.scan.open_hold_since, tz)}")
    marks = [f"{m.team.value} {_clock(m.time, tz)}" for m in report.scan.team_timeouts]
    holds = _plural(len(report.scan.stoppages), "hold")
    lines.append(f"{holds} of {MIN_HOLD_SECONDS:g} s or more (play clock not counting down)")
    lines.append(f"team timeouts: {len(marks)}" + (f": {', '.join(marks)}" if marks else ""))
    return lines


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


_VERDICT_LABELS: Mapping[Verdict, str] = {
    Verdict.AGREES: "agree",
    Verdict.END_CORRECTED: "end corrected",
    Verdict.END_ADDED: "end added",
    Verdict.NESTED: "nested",
    Verdict.UNTRUSTED: "not checked",
    Verdict.NO_STOPPAGE: "with no stoppage",
    Verdict.OUTSIDE_CAPTURE: "outside the capture",
}


def _verdict_line(report: Report) -> str:
    matches = report.comparison.matches
    parts = []
    for verdict, label in _VERDICT_LABELS.items():
        count = sum(1 for m in matches if m.verdict is verdict)
        if count:
            parts.append(f"{count} {label}")
    conflicts = [m for m in matches if m.conflict is not None]
    if conflicts:
        parts.append(_plural(len(conflicts), "kind conflict"))
    flagged = [m.tap.start.seq for m in matches if m.verdict in NEEDS_ATTENTION or m.conflict is not None]
    text = f"VERDICT: {len(matches)} tapped" + (f"; {', '.join(parts)}" if parts else "")
    if flagged:
        text += f" (seq {', '.join(map(str, flagged))})"
    return text


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
    lines += _listed("stray team timeouts", [f"{m.team.value} {_clock(m.time, tz)}" for m in comparison.stray_marks])
    if comparison.matches:
        lines += ["", *_table([_match_row(m, tz) for m in comparison.matches]), ""]
    lines += _listed("holds with no tap", [_hold_line(h, tz) for h in comparison.untapped])
    orphans = [f"seq {e.seq} ({e.event}) at {_clock(wall_time(e), tz)}" for e in comparison.orphan_ends]
    lines += _listed("orphan ends", orphans)
    lines += _proposal_lines(report)
    lines.append(_verdict_line(report))
    return "\n".join(lines) + "\n"


# -- the command ------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tacet-timeouts",
        description="Compare a game's tapped timeouts with the scoreboard's play clock, and propose corrections.",
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


def _replayed(capture: Path, port: int) -> tuple[list[rtd_replay.Event], rtd_replay.Summary]:
    from . import pcap

    replay = rtd_replay.Replay(port=port)
    events: list[rtd_replay.Event] = []
    with capture.open("rb") as handle:
        reader = pcap.Capture(handle)
        for item in reader:
            events += replay.feed(item)
        replay.summary.truncated_file = reader.truncated
    return events, replay.summary


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
        events, summary = _replayed(args.capture, args.port)
    except (ann.AnnotationError, pcap.PcapFormatError, UnicodeDecodeError, OSError) as exc:
        print(f"tacet-timeouts: {_ascii(str(exc))}", file=err)
        return EXIT_REFUSED
    if summary.rtd_datagrams == 0:
        print(f"tacet-timeouts: no RTD datagrams on UDP port {args.port}", file=err)
        return EXIT_REFUSED
    scan = stoppages.scan(events)
    report = build_report(entries, scan, summary, existing=existing, log=args.log, capture=args.capture)
    print(render(report, tz=tz), end="", file=out)
    if args.write:
        try:
            corrections.append_corrections(sidecar, report.fresh)
        except OSError as exc:
            print(f"tacet-timeouts: {_ascii(str(exc))}", file=err)
            return EXIT_REFUSED
        print(_written_line(report), file=out)
    return exit_code(report)


def _written_line(report: Report) -> str:
    if not report.fresh:
        return "nothing new to write"
    return f"wrote {_plural(len(report.fresh), 'correction')} to {_ascii(str(report.sidecar))}"


if __name__ == "__main__":
    raise SystemExit(main())
