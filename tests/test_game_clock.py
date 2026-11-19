"""The live game clock's pure core (#235).

The Game 5 frames are the real ones, pulled from the capture by datagram number
into `tests/rtd_frames.py`; no test reads a pcap.
"""

import random
import unittest

from tacet import game_clock as gc
from tacet import rtd
from tacet.datagram import Datagram

from . import rtd_frames as real

SOURCE = real.GAME_5_SOURCE
OTHER_SOURCE = "192.168.0.26"
#: Where a replay starts on the box's monotonic clock; any number would do.
START = 1000.0
#: The longest gap between two datagrams in 66,775 across Games 4 and 5.
LONGEST_NORMAL_GAP = 1.109
TIME_OF_DAY = real.TIME_OF_DAY


def datagram(payload: bytes, at: float, source: str = SOURCE) -> Datagram:
    return Datagram(at, source, rtd.PORT, rtd.PORT, payload)


def replay(frames: tuple[tuple[int, float, bytes], ...], reader: gc.Reader | None = None) -> tuple[gc.Reader, float]:
    """Every frame in, at START plus its offset. Returns the reader and the last time."""
    reader = reader if reader is not None else gc.Reader()
    at = START
    for _number, offset, payload in frames:
        at = START + offset
        reader.feed(datagram(payload, at))
    return reader, at


def upto(number: int, frames: tuple[tuple[int, float, bytes], ...]) -> tuple[tuple[int, float, bytes], ...]:
    return tuple(f for f in frames if f[0] <= number)


class TestGame5(unittest.TestCase):
    def test_game_5_kickoff_replayed(self):
        kick = real.GAME_5_KICKOFF
        reader, at = replay(upto(14625, kick))
        view = reader.view(now=at)
        self.assertIs(view.status, gc.Status.LIVE)
        self.assertEqual(
            (view.clock, view.at_zero, view.running, view.quarter, view.period),
            ("0:00", True, False, None, None),
        )

        reader, at = replay(upto(14632, kick))
        view = reader.view(now=at)
        self.assertEqual((view.quarter, view.period), ("", "PRE"))

        reader, at = replay(upto(14634, kick))
        self.assertEqual(reader.view(now=at).clock, "15:00")

        reader, at = replay(upto(14637, kick))
        view = reader.view(now=at)
        self.assertEqual((view.quarter, view.period), ("1", "1ST"))

        reader, at = replay(upto(14783, kick))
        view = reader.view(now=at)
        self.assertEqual(
            (view.clock, view.running, view.at_zero, view.quarter, view.period), ("14:51", True, False, "1", "1ST")
        )
        self.assertEqual(view.source, SOURCE)

    def test_game_5_halftime_replayed(self):
        reader, at = replay(real.GAME_5_HALFTIME)
        view = reader.view(now=at)
        self.assertIs(view.status, gc.Status.LIVE)
        self.assertEqual((view.quarter, view.period), ("", "HALF"))
        self.assertEqual((view.clock, view.running, view.at_zero), ("20:00", True, False))

    def test_the_clock_is_known_before_it_is_known_to_run(self):
        # The 26-byte running-clock write (14783) reaches offsets 0-25: neither
        # the at-zero flag at 26 nor the stopped flag at 27.
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14783, START))
        view = reader.view(now=START)
        self.assertEqual((view.clock, view.running, view.at_zero), ("14:51", None, None))

    def test_the_run_up_write_reaches_the_at_zero_flag_but_not_the_stopped_flag(self):
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14634, START))
        view = reader.view(now=START)
        self.assertEqual((view.clock, view.running, view.at_zero), ("15:00", None, False))

    def test_live_while_only_time_of_day_arrives(self):
        reader, at = replay(upto(14625, real.GAME_5_KICKOFF))
        statuses = []
        for second in range(1, 7):
            now = at + second
            reader.feed(datagram(TIME_OF_DAY, now))
            statuses.append(reader.view(now=now).status)
            self.assertEqual(reader.view(now=now).clock, "0:00")
        self.assertEqual(set(statuses), {gc.Status.LIVE})


class TestStaleness(unittest.TestCase):
    def live_reader(self) -> gc.Reader:
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        return reader

    def test_stale_exactly_at_stale_seconds(self):
        reader = self.live_reader()
        just = START + gc.STALE_SECONDS - 0.001
        self.assertIs(reader.view(now=just).status, gc.Status.LIVE)
        view = reader.view(now=START + gc.STALE_SECONDS)
        self.assertIs(view.status, gc.Status.STALE)
        self.assertEqual(view.last_write_at, START)
        self.assertEqual(
            (view.clock, view.running, view.at_zero, view.quarter, view.period), (None, None, None, None, None)
        )
        self.assertEqual(view.source, SOURCE)

    def test_the_last_write_is_sent_only_when_not_live(self):
        reader = self.live_reader()
        self.assertIsNone(reader.view(now=START).last_write_at)

    def test_never_is_not_stale(self):
        reader = gc.Reader()
        for now in (0.0, START, START * 1000):
            view = reader.view(now=now)
            self.assertIs(view.status, gc.Status.NEVER)
            self.assertIsNone(view.last_write_at)
        self.assertEqual(reader.poll(now=START), ())

    def test_poll_reports_each_edge_once(self):
        reader = self.live_reader()
        self.assertEqual(reader.poll(now=START + 1), ())
        first = reader.poll(now=START + gc.STALE_SECONDS)
        self.assertEqual([t.view.status for t in first if isinstance(t, gc.StatusChanged)], [gc.Status.STALE])
        self.assertEqual(reader.poll(now=START + gc.STALE_SECONDS + 1), ())
        self.assertEqual(reader.poll(now=START + 100), ())

    def test_a_silence_nobody_polled_for_is_still_an_edge(self):
        reader = self.live_reader()
        fed = reader.feed(datagram(real.G5_14625, START + 60))
        statuses = [t.view.status for t in fed.transitions if isinstance(t, gc.StatusChanged)]
        self.assertEqual(statuses, [gc.Status.STALE, gc.Status.LIVE])

    def test_the_first_datagram_is_a_never_to_live_edge(self):
        reader = gc.Reader()
        fed = reader.feed(datagram(real.G5_14625, START))
        (edge,) = fed.transitions
        assert isinstance(edge, gc.StatusChanged)
        self.assertEqual((edge.previous, edge.view.status, edge.view.source), (gc.Status.NEVER, gc.Status.LIVE, SOURCE))
        self.assertTrue(fed.shown_changed)

    def test_an_unchanged_datagram_changes_nothing_shown(self):
        reader = self.live_reader()
        fed = reader.feed(datagram(real.G5_14625, START + 1))
        self.assertEqual(fed, gc.Fed(shown_changed=False, transitions=()))

    def test_game_4_outage_shape(self):
        reader = gc.Reader()
        seen: list[gc.Transition] = []
        seen += reader.feed(datagram(real.G5_14625, START, "192.168.0.30")).transitions
        seen += reader.feed(datagram(TIME_OF_DAY, START + 1, "192.168.0.30")).transitions
        # 53.5 s of nothing, found by the box's own tick.
        seen += reader.poll(now=START + 2)
        seen += reader.poll(now=START + 1 + gc.STALE_SECONDS)
        seen += reader.poll(now=START + 30)
        seen += reader.feed(datagram(TIME_OF_DAY, START + 1 + 53.5, "192.168.0.26")).transitions
        statuses = [t.view.status for t in seen if isinstance(t, gc.StatusChanged)]
        self.assertEqual(statuses, [gc.Status.LIVE, gc.Status.STALE, gc.Status.LIVE])
        moved = [t for t in seen if isinstance(t, gc.SourceChanged)]
        self.assertEqual([(m.old, m.new) for m in moved], [("192.168.0.30", "192.168.0.26")])


class TestSources(unittest.TestCase):
    def test_two_sources_withhold_the_clock_and_clear(self):
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        fed = reader.feed(datagram(real.G5_14625, START + 0.5, OTHER_SOURCE))
        view = reader.view(now=START + 0.5)
        self.assertIs(view.status, gc.Status.TWO_SOURCES)
        self.assertEqual(view.sources, (SOURCE, OTHER_SOURCE))
        self.assertEqual((view.clock, view.running, view.quarter, view.period), (None, None, None, None))
        self.assertEqual([t for t in fed.transitions if isinstance(t, gc.SourceChanged)], [])
        # One keeps writing; the other goes quiet and ages out of the window.
        end = START + 0.5 + gc.STALE_SECONDS
        for now in (START + 1.5, START + 2.5, START + 3.0, START + 3.5):
            reader.feed(datagram(TIME_OF_DAY, now, OTHER_SOURCE))
        view = reader.view(now=START + 3.5)
        self.assertIs(view.status, gc.Status.LIVE)
        self.assertEqual(view.sources, (OTHER_SOURCE,))
        self.assertEqual(view.clock, "0:00")
        self.assertGreater(end, START)

    def test_two_senders_alternating_do_not_flood_the_source_edges(self):
        reader = gc.Reader()
        edges: list[gc.Transition] = []
        for i in range(40):
            who = SOURCE if i % 2 == 0 else OTHER_SOURCE
            edges += reader.feed(datagram(TIME_OF_DAY, START + i * 0.1, who)).transitions
        self.assertEqual([e for e in edges if isinstance(e, gc.SourceChanged)], [])

    def test_a_source_that_takes_over_is_reported_once(self):
        reader = gc.Reader()
        reader.feed(datagram(TIME_OF_DAY, START))
        edges: list[gc.Transition] = []
        for i in range(10):
            edges += reader.feed(datagram(TIME_OF_DAY, START + 10 + i, OTHER_SOURCE)).transitions
        moved = [e for e in edges if isinstance(e, gc.SourceChanged)]
        self.assertEqual([(m.old, m.new) for m in moved], [(SOURCE, OTHER_SOURCE)])


class TestDefences(unittest.TestCase):
    def test_garbage_only_is_undecodable_not_live(self):
        reader = gc.Reader()
        for i in range(5):
            reader.feed(datagram(b"not rtd at all", START + i))
        view = reader.view(now=START + 4)
        self.assertIs(view.status, gc.Status.UNDECODABLE)
        self.assertEqual((view.clock, view.period), (None, None))
        self.assertGreater(reader.counts.faults, 0)
        self.assertEqual(reader.counts.writes, 0)
        # And silence after it is stale, not undecodable.
        self.assertIs(reader.view(now=START + 4 + gc.STALE_SECONDS).status, gc.Status.STALE)

    def test_a_checksum_fault_does_not_refresh_liveness(self):
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        broken = real.frame(0, "99:99", checksum=b"00")
        for i in range(1, 6):
            reader.feed(datagram(broken, START + i))
        view = reader.view(now=START + 5)
        self.assertIs(view.status, gc.Status.UNDECODABLE)
        self.assertEqual(view.last_write_at, START)

    def test_flood_drops_and_recovers(self):
        reader = gc.Reader(decodes_per_second=5)
        for i in range(8):
            reader.feed(datagram(real.G5_14625, START + i * 0.01))
        self.assertEqual(reader.counts.dropped, 3)
        self.assertEqual(reader.counts.writes, 5)
        now = START + 0.5
        self.assertIs(reader.view(now=now).status, gc.Status.FLOOD)
        self.assertIsNone(reader.view(now=now).clock)
        # Past the window the next second is decoded again, and the flood clears.
        later = START + 0.07 + gc.STALE_SECONDS
        reader.feed(datagram(real.G5_14625, later))
        self.assertIs(reader.view(now=later).status, gc.Status.LIVE)

    def test_flood_is_edge_reported_and_cleared_by_a_poll(self):
        reader = gc.Reader(decodes_per_second=1)
        edges: list[gc.Transition] = []
        for i in range(3):
            edges += reader.feed(datagram(real.G5_14625, START + i * 0.1)).transitions
        # The flood clears a window after its last drop, and by then the one
        # decoded write is stale too; the next one makes it live again.
        edges += reader.poll(now=START + 0.3 + gc.STALE_SECONDS)
        edges += reader.feed(datagram(real.G5_14625, START + 10)).transitions
        statuses = [e.view.status for e in edges if isinstance(e, gc.StatusChanged)]
        self.assertEqual(statuses, [gc.Status.LIVE, gc.Status.FLOOD, gc.Status.STALE, gc.Status.LIVE])

    def test_dropped_datagrams_are_not_decoded(self):
        reader = gc.Reader(decodes_per_second=1)
        reader.feed(datagram(real.G5_14625, START))
        before = reader.counts
        reader.feed(datagram(real.G5_14634, START + 0.1))
        self.assertEqual(reader.counts.writes, before.writes)
        self.assertEqual(reader.counts.dropped, 1)
        reader.poll(now=START + gc.STALE_SECONDS + 1)
        reader.feed(datagram(TIME_OF_DAY, START + 10))
        self.assertEqual(reader.view(now=START + 10).clock, "0:00")

    def test_oversize_counted_not_decoded(self):
        for size in (gc.MAX_DATAGRAM_BYTES + 1, 65_507):
            with self.subTest(size=size):
                reader = gc.Reader()
                # A well-formed write, padded past the cap by garbage after it.
                payload = real.G5_14625 + b"x" * (size - len(real.G5_14625))
                self.assertEqual(len(payload), size)
                reader.feed(datagram(payload, START))
                self.assertEqual(reader.counts.oversize, 1)
                self.assertEqual(reader.counts.writes, 0)
                self.assertIs(reader.view(now=START).status, gc.Status.UNDECODABLE)

    def test_refusals_inside_a_flood_do_not_settle(self):
        # S1: a drop or an oversize datagram costs a length and a budget check.
        # The drop that starts the flood is reported; the rest wait for the tick.
        reader = gc.Reader(decodes_per_second=1)
        reader.feed(datagram(real.G5_14625, START))
        first = reader.feed(datagram(real.G5_14625, START + 0.1))
        self.assertEqual(
            [t.view.status for t in first.transitions if isinstance(t, gc.StatusChanged)], [gc.Status.FLOOD]
        )
        calls: list[float] = []
        original = reader._recent

        def spy(now: float) -> tuple[str, ...]:
            calls.append(now)
            return original(now)

        reader._recent = spy  # type: ignore[method-assign]
        again = reader.feed(datagram(real.G5_14625, START + 0.2))
        big = reader.feed(datagram(b"x" * (gc.MAX_DATAGRAM_BYTES + 1), START + 0.3))
        self.assertEqual((again, big), (gc.Fed(False, ()), gc.Fed(False, ())))
        self.assertEqual(calls, [])
        self.assertEqual((reader.counts.dropped, reader.counts.oversize), (2, 1))
        # The tick still reports the edges.
        later = reader.poll(now=START + 0.3 + gc.STALE_SECONDS)
        self.assertTrue(later)

    def test_the_cap_itself_is_decoded(self):
        reader = gc.Reader()
        payload = real.G5_14625 + b"x" * (gc.MAX_DATAGRAM_BYTES - len(real.G5_14625))
        reader.feed(datagram(payload, START))
        self.assertEqual(reader.counts.oversize, 0)
        self.assertEqual(reader.counts.writes, 1)

    def test_every_truncation_of_a_full_write_never_raises(self):
        for cut in range(len(real.G5_14632) + 1):
            reader = gc.Reader()
            reader.feed(datagram(real.G5_14632[:cut], START))
            reader.view(now=START)
        # Cut from the front too, so a frame starts mid-stream.
        for cut in range(len(real.G5_14632) + 1):
            reader = gc.Reader()
            reader.feed(datagram(real.G5_14632[cut:], START))
            reader.view(now=START)

    def test_random_datagrams_never_raise(self):
        rng = random.Random(235)
        reader = gc.Reader()
        pieces = (real.G5_14632, real.G5_14625, real.G5_14637, bytes((rtd.SYN, rtd.ETB)))
        for i in range(2000):
            length = rng.randrange(0, 2 * gc.MAX_DATAGRAM_BYTES + 1)
            if i % 3 == 0:
                # Mostly real bytes, a few flipped: the shape most likely to get a long way in.
                blob = bytearray(rng.choice(pieces))
                for _ in range(rng.randrange(1, 4)):
                    blob[rng.randrange(len(blob))] = rng.randrange(256)
                payload = bytes(blob)
            else:
                payload = bytes(rng.randrange(256) for _ in range(length))
            at = START + i * 0.05
            reader.feed(datagram(payload, at, rng.choice((SOURCE, OTHER_SOURCE))))
            reader.view(now=at)
        self.assertEqual(reader.counts.datagrams, 2000)

    def test_bad_field_values_pass_through_as_text(self):
        reader = gc.Reader()
        reader.feed(datagram(real.frame(0, "AB:CD"), START))
        reader.feed(datagram(real.frame(26, "?x"), START))
        view = reader.view(now=START)
        self.assertEqual(view.clock, "AB:CD")
        self.assertIsNone(view.running)
        self.assertIsNone(view.at_zero)

    def test_blank_is_not_never_written(self):
        reader = gc.Reader()
        reader.feed(datagram(real.frame(141, "      "), START))
        view = reader.view(now=START)
        self.assertEqual((view.quarter, view.period), ("", ""))
        self.assertIsNone(view.clock)

    def test_a_non_write_frame_is_not_a_write(self):
        reader = gc.Reader()
        reader.feed(datagram(real.frame(0, "12:00", prefix=b"004211"), START))
        self.assertEqual(reader.counts.writes, 0)
        self.assertIs(reader.view(now=START).status, gc.Status.UNDECODABLE)

    def test_a_write_beyond_the_buffer_is_a_fault(self):
        reader = gc.Reader()
        reader.feed(datagram(real.frame(rtd.BUFFER_SIZE - 1, "ab"), START))
        self.assertEqual((reader.counts.faults, reader.counts.writes), (1, 0))

    def test_forget_display_keeps_timing_and_drops_values(self):
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        reader.forget_display()
        view = reader.view(now=START)
        self.assertIs(view.status, gc.Status.LIVE)
        self.assertIsNone(view.clock)


def status(
    *,
    down: bool = False,
    last_drop_at: float | None = 9.5,
    recent_sources: int = 2,
    last_write_at: float | None = 9.9,
    last_arrival_at: float | None = 9.9,
) -> gc.Status:
    """`status_at` at t=10 with everything wrong at once, so a test names what it clears."""
    return gc.status_at(
        now=10.0,
        stale_seconds=3.0,
        down=down,
        last_drop_at=last_drop_at,
        recent_sources=recent_sources,
        last_write_at=last_write_at,
        last_arrival_at=last_arrival_at,
    )


class TestDown(unittest.TestCase):
    def test_down_wins_and_is_sticky(self):
        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        edges = reader.fail("socket lost", now=START + 1)
        (edge,) = edges
        assert isinstance(edge, gc.StatusChanged)
        self.assertEqual(
            (edge.previous, edge.view.status, edge.view.detail), (gc.Status.LIVE, gc.Status.DOWN, "socket lost")
        )
        reader.feed(datagram(real.G5_14625, START + 2))
        self.assertIs(reader.view(now=START + 2).status, gc.Status.DOWN)
        self.assertEqual(reader.view(now=START + 2).clock, None)

    def test_precedence(self):
        self.assertIs(status(down=True), gc.Status.DOWN)
        self.assertIs(status(), gc.Status.FLOOD)
        self.assertIs(status(last_drop_at=None), gc.Status.TWO_SOURCES)
        self.assertIs(status(last_drop_at=None, recent_sources=1), gc.Status.LIVE)
        self.assertIs(status(last_drop_at=None, recent_sources=0, last_write_at=1.0), gc.Status.UNDECODABLE)
        self.assertIs(
            status(last_drop_at=None, recent_sources=0, last_write_at=1.0, last_arrival_at=1.0), gc.Status.STALE
        )
        self.assertIs(
            status(last_drop_at=None, recent_sources=0, last_write_at=None, last_arrival_at=None), gc.Status.NEVER
        )


class TestFlags(unittest.TestCase):
    def test_running_of(self):
        self.assertIs(gc.running_of("s"), False)
        self.assertIs(gc.running_of(" "), True)
        self.assertIsNone(gc.running_of("x"))
        self.assertIsNone(gc.running_of(None))

    def test_trimmed_keeps_blank_apart_from_never(self):
        self.assertEqual(gc.trimmed("  1"), "1")
        self.assertEqual(gc.trimmed("    "), "")
        self.assertIsNone(gc.trimmed(None))


class TestConstants(unittest.TestCase):
    def test_constants_derive_and_stale_exceeds_the_longest_normal_gap(self):
        self.assertEqual(gc.MAX_DATAGRAM_BYTES, 2 * (rtd.MIN_FRAME_LENGTH + rtd.BUFFER_SIZE))
        self.assertEqual(gc.MAX_DATAGRAM_BYTES, 756)
        # The largest datagram seen, the full write, fits with room to spare.
        self.assertLess(len(real.PREGAME_FULL), gc.MAX_DATAGRAM_BYTES)
        self.assertEqual(len(real.G5_14632), 378)
        self.assertGreater(gc.STALE_SECONDS, 2 * LONGEST_NORMAL_GAP)
        self.assertEqual(gc.STALE_SECONDS, 3.0)
        self.assertEqual(gc.DECODES_PER_SECOND, 50)
        self.assertEqual(rtd.PORT, 21000)

    def test_as_snapshot_is_json_safe_and_complete(self):
        import json

        reader = gc.Reader()
        reader.feed(datagram(real.G5_14625, START))
        snap = reader.view(now=START).as_snapshot()
        self.assertEqual(
            set(snap),
            {
                "status",
                "source",
                "sources",
                "last_write_at",
                "detail",
                "clock",
                "running",
                "at_zero",
                "quarter",
                "period",
            },
        )
        json.dumps(snap)
        self.assertEqual(reader.counts.as_data()["writes"], 1)


if __name__ == "__main__":
    unittest.main()
