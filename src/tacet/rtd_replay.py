"""Replay a pcap of the scoreboard's RTD stream through the decoder, after the game (#210).

    tacet-rtd CAPTURE.pcap [--summary] [--port N] [--gap SECONDS]

By default one JSON line per event, in capture order. Every row has a `kind`:

    field     a field changed: time, source, field, old, new
    source    the sending address changed (the first one is a change from null)
    gap       no RTD datagram for longer than --gap: time is the resume, since the last one
    fault     a frame or run of bytes that did not decode
    unmapped  a cell outside every known field changed (the field map is missing something)

`--summary` prints counts, sources, gaps and the final value of every field
instead. Exit 0: the capture decoded without a fault. Exit 1: a frame
faulted, the file ends mid-record, or no RTD datagram was found. Exit 2: the
capture could not be read at all. A gap or a source change is a fact about the
capture, not a fault.

Capture a classic pcap on a named interface: `tcpdump -i <interface> -w
game.pcap udp port 21000`. pcapng and `-i any` are refused. This tool reads no
`tacet.toml`: it is not run on the box.

Gap detection lives here and not in `tacet.rtd`, which keeps no clock.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from . import pcap, rtd

#: The UDP port Game 4's All Sport Pro sent to.
DEFAULT_PORT = 21000
#: The time of day is written every second; Game 4's normal spacing never passed 1.11 s.
DEFAULT_GAP_SECONDS = 5.0

EXIT_CLEAN = 0
EXIT_FAULTS = 1
EXIT_REFUSED = 2

#: How much of a bad frame a fault row quotes.
FAULT_RAW_LIMIT = 64
SECONDS_DECIMALS = 3
INDENT = "  "
NONE_WORD = "none"
NEVER_WRITTEN = "never written"


@dataclass(frozen=True)
class FieldEvent:
    time: float
    source: str
    change: rtd.FieldChange


@dataclass(frozen=True)
class SourceEvent:
    time: float
    old: str | None
    new: str


@dataclass(frozen=True)
class GapEvent:
    #: When the stream resumed.
    time: float
    #: When the last datagram before the gap arrived.
    since: float


@dataclass(frozen=True)
class FaultEvent:
    time: float
    source: str
    fault: rtd.FrameFault


@dataclass(frozen=True)
class UnmappedEvent:
    time: float
    source: str
    offsets: tuple[int, ...]


Event = FieldEvent | SourceEvent | GapEvent | FaultEvent | UnmappedEvent


@dataclass
class Summary:
    packets: int = 0
    rtd_datagrams: int = 0
    other_port: int = 0
    frames: int = 0
    writes: int = 0
    skipped: Counter[pcap.Skip] = field(default_factory=Counter)
    faults: Counter[rtd.Fault] = field(default_factory=Counter)
    other_types: Counter[str] = field(default_factory=Counter)
    #: (first seen, address), in the order they appeared.
    sources: list[tuple[float, str]] = field(default_factory=list)
    #: (since, seconds).
    gaps: list[tuple[float, float]] = field(default_factory=list)
    first_time: float | None = None
    last_time: float | None = None
    unmapped_offsets: set[int] = field(default_factory=set)
    truncated_file: bool = False


class Replay:
    """The pure core: datagrams in, events out. No file, no clock beyond the capture's own."""

    def __init__(self, *, port: int = DEFAULT_PORT, gap_seconds: float = DEFAULT_GAP_SECONDS) -> None:
        self._port = port
        self._gap_seconds = gap_seconds
        self._source: str | None = None
        self.display = rtd.Display()
        self.summary = Summary()

    def feed(self, item: pcap.Datagram | pcap.Skip) -> list[Event]:
        self.summary.packets += 1
        if isinstance(item, pcap.Skip):
            self.summary.skipped[item] += 1
            return []
        if item.dest_port != self._port:
            self.summary.other_port += 1
            return []
        events = self._note_arrival(item)
        for parsed in rtd.parse_datagram(item.payload):
            events += self._frame(item, parsed)
        return events

    def _note_arrival(self, item: pcap.Datagram) -> list[Event]:
        summary = self.summary
        events: list[Event] = []
        last = summary.last_time
        if last is not None and item.time - last > self._gap_seconds:
            summary.gaps.append((last, item.time - last))
            events.append(GapEvent(item.time, last))
        if item.source != self._source:
            summary.sources.append((item.time, item.source))
            events.append(SourceEvent(item.time, self._source, item.source))
            self._source = item.source
        summary.rtd_datagrams += 1
        if summary.first_time is None:
            summary.first_time = item.time
        summary.last_time = item.time
        return events

    def _frame(self, item: pcap.Datagram, parsed: rtd.Frame | rtd.FrameFault) -> list[Event]:
        summary = self.summary
        summary.frames += 1
        if isinstance(parsed, rtd.FrameFault):
            return [self._fault(item, parsed)]
        if not parsed.is_write:
            summary.other_types[parsed.prefix] += 1
            return []
        summary.writes += 1
        applied = self.display.apply(parsed)
        if isinstance(applied, rtd.FrameFault):
            return [self._fault(item, applied)]
        events: list[Event] = [FieldEvent(item.time, item.source, change) for change in applied.changes]
        if applied.unmapped:
            summary.unmapped_offsets.update(applied.unmapped)
            events.append(UnmappedEvent(item.time, item.source, applied.unmapped))
        return events

    def _fault(self, item: pcap.Datagram, fault: rtd.FrameFault) -> FaultEvent:
        self.summary.faults[fault.fault] += 1
        return FaultEvent(item.time, item.source, fault)


def iso(time: float) -> str:
    return datetime.fromtimestamp(time, UTC).isoformat(timespec="microseconds")


def _seconds(seconds: float) -> float:
    return round(seconds, SECONDS_DECIMALS)


def _row(event: Event) -> dict[str, object]:
    match event:
        case FieldEvent():
            return {
                "kind": "field",
                "time": iso(event.time),
                "source": event.source,
                "field": event.change.field.name,
                "old": event.change.old,
                "new": event.change.new,
            }
        case SourceEvent():
            return {"kind": "source", "time": iso(event.time), "old": event.old, "new": event.new}
        case GapEvent():
            return {
                "kind": "gap",
                "time": iso(event.time),
                "since": iso(event.since),
                "seconds": _seconds(event.time - event.since),
            }
        case FaultEvent():
            return {
                "kind": "fault",
                "time": iso(event.time),
                "source": event.source,
                "fault": event.fault.fault.value,
                "raw": event.fault.raw[:FAULT_RAW_LIMIT].decode("ascii", "backslashreplace"),
            }
        case UnmappedEvent():
            return {
                "kind": "unmapped",
                "time": iso(event.time),
                "source": event.source,
                "offsets": list(event.offsets),
            }


def event_json(event: Event) -> str:
    return json.dumps(_row(event))


def _counted(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or NONE_WORD


def _listed(label: str, lines: list[str]) -> list[str]:
    return [f"{label}:", *(INDENT + line for line in lines)] if lines else [f"{label}: {NONE_WORD}"]


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _gap_line(since: float, seconds: float) -> str:
    return f"{_seconds(seconds):.{SECONDS_DECIMALS}f} s from {iso(since)}"


def _value_line(name: str, value: str | None) -> str:
    return f"{name}: {NEVER_WRITTEN if value is None else json.dumps(value)}"


def render_summary(summary: Summary, final: Mapping[str, str | None], name: str) -> str:
    first = "-" if summary.first_time is None else iso(summary.first_time)
    last = "-" if summary.last_time is None else iso(summary.last_time)
    lines = [
        f"capture: {_ascii(name)}",
        f"packets: {summary.packets:,}",
        f"RTD datagrams: {summary.rtd_datagrams:,}",
        f"other-port datagrams: {summary.other_port:,}",
        f"frames: {summary.frames:,}",
        f"writes: {summary.writes:,}",
        f"faults: {_counted({k.value: v for k, v in summary.faults.items()})}",
        f"skipped: {_counted({k.value: v for k, v in summary.skipped.items()})}",
        f"other frame types: {_counted(summary.other_types)}",
        f"first RTD datagram: {first}",
        f"last RTD datagram: {last}",
        *_listed("sources", [f"{address} from {iso(time)}" for time, address in summary.sources]),
        *_listed("gaps", [_gap_line(since, seconds) for since, seconds in summary.gaps]),
        f"unmapped offsets: {', '.join(map(str, sorted(summary.unmapped_offsets))) or NONE_WORD}",
    ]
    if summary.truncated_file:
        lines.append("the capture ends in the middle of a record")
    lines.append("final values:")
    lines += [INDENT + _value_line(key, value) for key, value in final.items()]
    return "\n".join(lines) + "\n"


def positive_seconds(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"must be more than 0: {text!r}")
    return seconds


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tacet-rtd",
        description="Replay a pcap of the scoreboard's RTD stream: field changes as JSONL, or a summary.",
    )
    p.add_argument("capture", type=Path, help="a classic pcap file (tcpdump -w), not pcapng")
    p.add_argument("--summary", action="store_true", help="print the summary instead of one row per event")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP destination port (default {DEFAULT_PORT})")
    p.add_argument(
        "--gap",
        type=positive_seconds,
        default=DEFAULT_GAP_SECONDS,
        metavar="SECONDS",
        help=f"report a gap when no RTD datagram arrives for longer than this (default {DEFAULT_GAP_SECONDS})",
    )
    return p


def _replayed(args: argparse.Namespace, out: TextIO) -> Replay:
    replay = Replay(port=args.port, gap_seconds=args.gap)
    with args.capture.open("rb") as handle:
        reader = pcap.PcapReader(handle)
        for item in pcap.datagrams(reader):
            for event in replay.feed(item):
                if not args.summary:
                    print(event_json(event), file=out)
        replay.summary.truncated_file = reader.truncated
    return replay


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = parser().parse_args(argv)
    try:
        replay = _replayed(args, out)
    except BrokenPipeError:
        # `tacet-rtd x.pcap | head`: the reader left, which is not our failure.
        if out is sys.stdout:
            # Python flushes stdout at exit; point it at devnull so that does not fail too.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_CLEAN
    except (pcap.PcapFormatError, OSError) as exc:
        print(f"tacet-rtd: {_ascii(str(exc))}", file=err)
        return EXIT_REFUSED
    summary = replay.summary
    if args.summary:
        print(render_summary(summary, replay.display.snapshot(), args.capture.name), end="", file=out)
    if summary.rtd_datagrams == 0:
        print(f"tacet-rtd: no RTD datagrams on UDP port {args.port}", file=err)
    if summary.truncated_file:
        print("tacet-rtd: the capture ends in the middle of a record", file=err)
    faulted = bool(summary.faults) or summary.truncated_file or summary.rtd_datagrams == 0
    return EXIT_FAULTS if faulted else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
