import contextlib
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from tacet import pcap, rtd, rtd_replay

from . import pcap_capture as cap
from . import rtd_frames as real

REPO_ROOT = Path(__file__).resolve().parent.parent
START = 1792261493.0
SOURCE_30, SOURCE_26, SOURCE_31 = "192.168.0.30", "192.168.0.26", "192.168.0.31"
OTHER_PORT = 9999


def datagram(time: float, payload: bytes, *, source: str = SOURCE_30, port: int = rtd_replay.DEFAULT_PORT):
    return pcap.Datagram(time, source, port, port, payload)


def feed_all(items, **kwargs) -> tuple[rtd_replay.Replay, list[rtd_replay.Event]]:
    replay = rtd_replay.Replay(**kwargs)
    events: list[rtd_replay.Event] = []
    for item in items:
        events += replay.feed(item)
    return replay, events


def rows(events) -> list[dict[str, object]]:
    return [json.loads(rtd_replay.event_json(e)) for e in events]


def of_kind(events, kind: str) -> list[dict[str, object]]:
    return [r for r in rows(events) if r["kind"] == kind]


class TestEvents(unittest.TestCase):
    def test_field_rows_carry_time_source_field_old_new(self):
        _, events = feed_all([datagram(START, real.TIME_OF_DAY)])
        (row,) = of_kind(events, "field")
        self.assertEqual(
            row,
            {
                "kind": "field",
                "time": rtd_replay.iso(START),
                "source": SOURCE_30,
                "field": "time_of_day",
                "old": None,
                "new": " 2:24:54",
            },
        )
        self.assertEqual(list(row), ["kind", "time", "source", "field", "old", "new"])

    def test_first_datagram_emits_source_from_null(self):
        _, events = feed_all([datagram(START, real.TIME_OF_DAY), datagram(START + 1, real.TIME_OF_DAY)])
        (row,) = of_kind(events, "source")
        self.assertEqual(row, {"kind": "source", "time": rtd_replay.iso(START), "old": None, "new": SOURCE_30})
        self.assertEqual(list(row), ["kind", "time", "old", "new"])

    def test_source_change_reported_once_per_change(self):
        items = [
            datagram(START, real.TIME_OF_DAY, source=SOURCE_30),
            datagram(START + 1, real.TIME_OF_DAY, source=SOURCE_30),
            datagram(START + 2, real.TIME_OF_DAY, source=SOURCE_26),
            datagram(START + 3, real.TIME_OF_DAY, source=SOURCE_31),
        ]
        replay, events = feed_all(items)
        self.assertEqual(
            [(r["old"], r["new"]) for r in of_kind(events, "source")],
            [
                (None, SOURCE_30),
                (SOURCE_30, SOURCE_26),
                (SOURCE_26, SOURCE_31),
            ],
        )
        self.assertEqual([s for _, s in replay.summary.sources], [SOURCE_30, SOURCE_26, SOURCE_31])

    def test_gap_over_threshold_reported_with_since(self):
        replay, events = feed_all([datagram(START, real.TIME_OF_DAY), datagram(START + 53.5, real.TIME_OF_DAY)])
        (row,) = of_kind(events, "gap")
        self.assertEqual(
            row,
            {"kind": "gap", "time": rtd_replay.iso(START + 53.5), "since": rtd_replay.iso(START), "seconds": 53.5},
        )
        self.assertEqual(list(row), ["kind", "time", "since", "seconds"])
        self.assertEqual(replay.summary.gaps, [(START, 53.5)])

    def test_gap_at_threshold_not_reported(self):
        gap = rtd_replay.DEFAULT_GAP_SECONDS
        _, events = feed_all([datagram(START, real.TIME_OF_DAY), datagram(START + gap, real.TIME_OF_DAY)])
        self.assertEqual(of_kind(events, "gap"), [])

    def test_gap_flag_changes_the_threshold(self):
        items = [datagram(START, real.TIME_OF_DAY), datagram(START + 2, real.TIME_OF_DAY)]
        _, events = feed_all(items, gap_seconds=1.5)
        self.assertEqual(len(of_kind(events, "gap")), 1)

    def test_backwards_time_not_a_gap(self):
        _, events = feed_all([datagram(START + 100, real.TIME_OF_DAY), datagram(START, real.TIME_OF_DAY)])
        self.assertEqual(of_kind(events, "gap"), [])

    def test_checksum_fault_is_a_row_and_not_applied(self):
        bad = real.frame(39, " 2:24:54", checksum=b"00")
        replay, events = feed_all([datagram(START, bad)])
        (row,) = of_kind(events, "fault")
        self.assertEqual(row["fault"], "checksum")
        self.assertEqual(row["source"], SOURCE_30)
        self.assertEqual(list(row), ["kind", "time", "source", "fault", "raw"])
        self.assertEqual(row["raw"], bad.decode("ascii", "backslashreplace"))
        self.assertEqual(of_kind(events, "field"), [])
        self.assertIsNone(replay.display.value(rtd.TIME_OF_DAY))

    def test_fault_raw_is_limited(self):
        junk = b"x" * (rtd_replay.FAULT_RAW_LIMIT * 2)
        _, events = feed_all([datagram(START, junk)])
        (row,) = of_kind(events, "fault")
        self.assertEqual(row["fault"], "garbage")
        self.assertEqual(len(str(row["raw"])), rtd_replay.FAULT_RAW_LIMIT)

    def test_beyond_buffer_is_a_fault_row(self):
        replay, events = feed_all([datagram(START, real.frame(350, "abcd"))])
        (row,) = of_kind(events, "fault")
        self.assertEqual(row["fault"], "beyond-buffer")
        self.assertIn("0350", str(row["raw"]))
        self.assertEqual(replay.summary.faults[rtd.Fault.BEYOND_BUFFER], 1)

    def test_unmapped_row(self):
        _, events = feed_all([datagram(START, real.frame(115, "x")), datagram(START + 1, real.frame(115, "y"))])
        (row,) = of_kind(events, "unmapped")
        expected = {"kind": "unmapped", "time": rtd_replay.iso(START + 1), "source": SOURCE_30, "offsets": [115]}
        self.assertEqual(row, expected)
        self.assertEqual(list(row), ["kind", "time", "source", "offsets"])

    def test_other_port_counted_not_decoded(self):
        replay, events = feed_all([datagram(START, real.TIME_OF_DAY, port=OTHER_PORT)])
        self.assertEqual(events, [])
        self.assertEqual((replay.summary.other_port, replay.summary.rtd_datagrams), (1, 0))

    def test_skips_counted_by_reason(self):
        replay, events = feed_all([pcap.Skip.NOT_IPV4, pcap.Skip.NOT_IPV4, pcap.Skip.FRAGMENT])
        self.assertEqual(events, [])
        self.assertEqual(replay.summary.skipped, {pcap.Skip.NOT_IPV4: 2, pcap.Skip.FRAGMENT: 1})
        self.assertEqual(replay.summary.packets, 3)

    def test_other_frame_types_are_counted_not_applied(self):
        other = real.frame(0, "abc", prefix=b"004010")
        replay, events = feed_all([datagram(START, other)])
        self.assertEqual(of_kind(events, "field"), [])
        self.assertEqual(replay.summary.other_types, {"004010": 1})
        self.assertEqual((replay.summary.frames, replay.summary.writes), (1, 0))

    def test_counts_for_a_clean_run(self):
        replay, _ = feed_all([datagram(START, real.TIME_OF_DAY + real.PLAY_CLOCK_39)])
        summary = replay.summary
        self.assertEqual((summary.packets, summary.rtd_datagrams, summary.frames, summary.writes), (1, 1, 2, 2))
        self.assertEqual((summary.first_time, summary.last_time), (START, START))
        self.assertEqual(sum(summary.faults.values()), 0)

    def test_the_final_display_follows_the_stream(self):
        replay, _ = feed_all([datagram(START, real.FINAL_FULL)])
        self.assertEqual(replay.display.value(rtd.HOME_SCORE), "  34")


class TestSummary(unittest.TestCase):
    def test_summary_lists_counts_sources_gaps_and_final_values(self):
        items = [
            datagram(START, real.PREGAME_FULL, source=SOURCE_30),
            datagram(START + 60, real.TIME_OF_DAY, source=SOURCE_26),
            datagram(START + 61, real.frame(39, "x", checksum=b"00"), source=SOURCE_26),
            pcap.Skip.NOT_IPV4,
        ]
        replay, _ = feed_all(items)
        text = rtd_replay.render_summary(replay.summary, replay.display.snapshot(), "game.pcap")
        self.assertTrue(text.isascii())
        for expected in (
            "game.pcap",
            "packets: 4",
            "RTD datagrams: 3",
            "frames: 3",
            "writes: 2",
            "checksum 1",
            "not-ipv4 1",
            f"{SOURCE_30} from {rtd_replay.iso(START)}",
            f"{SOURCE_26} from {rtd_replay.iso(START + 60)}",
            f"60.000 s from {rtd_replay.iso(START)}",
            'time_of_day: " 2:24:54"',
            'home_name: "VT                  "',
        ):
            self.assertIn(expected, text)

    def test_summary_says_never_written_for_a_field_the_stream_never_set(self):
        replay, _ = feed_all([datagram(START, real.TIME_OF_DAY)])
        text = rtd_replay.render_summary(replay.summary, replay.display.snapshot(), "game.pcap")
        self.assertIn("timeout_clock: never written", text)
        self.assertNotIn("time_of_day: never written", text)

    def test_summary_of_a_quiet_capture_says_none(self):
        replay = rtd_replay.Replay()
        text = rtd_replay.render_summary(replay.summary, replay.display.snapshot(), "empty.pcap")
        self.assertIn("faults: none", text)
        self.assertIn("gaps: none", text)
        self.assertIn("unmapped offsets: none", text)

    def test_summary_names_a_truncated_file(self):
        replay = rtd_replay.Replay()
        replay.summary.truncated_file = True
        text = rtd_replay.render_summary(replay.summary, replay.display.snapshot(), "cut.pcap")
        self.assertIn("ends in the middle of a record", text)


class Run:
    def __init__(self, capture: bytes, *args: str, name: str = "game.pcap") -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(capture)
            self.out, self.err = io.StringIO(), io.StringIO()
            self.code = rtd_replay.main([str(path), *args], out=self.out, err=self.err)


def clean_capture(*payloads: bytes, magic: bytes = cap.LE_USEC) -> bytes:
    records = [cap.record(START + i, cap.rtd_packet(p)) for i, p in enumerate(payloads)]
    return cap.pcap_bytes(records, magic=magic)


class TestCommand(unittest.TestCase):
    def test_clean_capture_exits_0(self):
        run = Run(clean_capture(real.TIME_OF_DAY, real.PLAY_CLOCK_39))
        self.assertEqual(run.code, rtd_replay.EXIT_CLEAN, run.err.getvalue())
        kinds = [json.loads(line)["kind"] for line in run.out.getvalue().splitlines()]
        self.assertEqual(kinds, ["source", "field", "field"])

    def test_summary_flag_prints_only_the_summary(self):
        run = Run(clean_capture(real.TIME_OF_DAY), "--summary")
        self.assertEqual(run.code, rtd_replay.EXIT_CLEAN)
        self.assertIn("game.pcap", run.out.getvalue())
        self.assertNotIn('"kind"', run.out.getvalue())

    def test_fault_exits_1(self):
        run = Run(clean_capture(real.TIME_OF_DAY, real.frame(3, "x", checksum=b"00")))
        self.assertEqual(run.code, rtd_replay.EXIT_FAULTS)

    def test_no_rtd_datagrams_exits_1_and_says_so(self):
        run = Run(cap.pcap_bytes([cap.record(START, cap.rtd_packet(real.TIME_OF_DAY, dport=OTHER_PORT))]))
        self.assertEqual(run.code, rtd_replay.EXIT_FAULTS)
        self.assertIn("no RTD datagrams", run.err.getvalue())

    def test_truncated_file_exits_1(self):
        run = Run(clean_capture(real.TIME_OF_DAY, real.PLAY_CLOCK_39)[:-3])
        self.assertEqual(run.code, rtd_replay.EXIT_FAULTS)
        self.assertIn("middle of a record", run.err.getvalue())

    def test_pcapng_exits_2(self):
        run = Run(cap.PCAPNG + b"\x00" * 40)
        self.assertEqual(run.code, rtd_replay.EXIT_REFUSED)
        self.assertIn("pcapng is not supported", run.err.getvalue())
        self.assertTrue(run.err.getvalue().startswith("tacet-rtd: "))

    def test_non_ethernet_capture_exits_2(self):
        run = Run(cap.pcap_bytes([], linktype=113))
        self.assertEqual(run.code, rtd_replay.EXIT_REFUSED)
        self.assertIn("-i any", run.err.getvalue())

    def test_missing_file_exits_2(self):
        err = io.StringIO()
        code = rtd_replay.main(["/nonexistent/none.pcap"], out=io.StringIO(), err=err)
        self.assertEqual(code, rtd_replay.EXIT_REFUSED)
        self.assertIn("tacet-rtd:", err.getvalue())

    def test_port_flag(self):
        capture = cap.pcap_bytes([cap.record(START, cap.rtd_packet(real.TIME_OF_DAY, dport=OTHER_PORT))])
        run = Run(capture, "--port", str(OTHER_PORT))
        self.assertEqual(run.code, rtd_replay.EXIT_CLEAN, run.err.getvalue())
        self.assertIn('"time_of_day"', run.out.getvalue())

    def test_gap_flag(self):
        run = Run(clean_capture(real.TIME_OF_DAY, real.TIME_OF_DAY), "--gap", "0.5")
        self.assertIn('"kind": "gap"', run.out.getvalue())

    def test_output_is_ascii(self):
        run = Run(clean_capture(real.TIME_OF_DAY, real.frame(3, "x", checksum=b"00"), b"\xffjunk\x80"))
        self.assertTrue(run.out.getvalue().isascii())
        self.assertIn("\\xff", run.out.getvalue())

    def test_a_missing_argument_is_an_argparse_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rtd_replay.main([])

    def test_gap_must_be_positive(self):
        for bad in ("0", "-1"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rtd_replay.main(["x.pcap", "--gap", bad])

    def test_a_closed_pipe_is_not_a_refusal(self):
        class ClosedPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "game.pcap"
            path.write_bytes(clean_capture(real.TIME_OF_DAY))
            code = rtd_replay.main([str(path)], out=ClosedPipe(), err=io.StringIO())
        self.assertEqual(code, rtd_replay.EXIT_CLEAN)

    def test_console_script_is_declared(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["tacet-rtd"], "tacet.rtd_replay:main")


if __name__ == "__main__":
    unittest.main()
