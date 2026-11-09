"""The mirror's status report (#204): what Reaper says about its project, and
how far the box believes it.

Pure core first (parse, observe, assess), then the thin watch around a read.
"""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from tacet import mirror_report as mr

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = FIXTURES / "report-v1.report"
#: The same report from a script that also says what it is (#223).
GOLDEN_CHECKSUM = FIXTURES / "report-v1-checksum.report"
#: Every byte value once; the Lua side hashes the same file (see test_script_check).
CHECKSUM_INPUT = FIXTURES / "script-checksum-input.bin"
CHECKSUM_OF_INPUT = "90a458c5"
SUM = "0badf00d"
OTHER_SUM = "deadbeef"
HOME = "/Users/x"
SAVED = "/Users/x/Reaper Media/20261114 X/20261114 X.RPP"
MEDIA = "/Users/x/Reaper Media/20261114 X/Media"
DOCS = "/Users/x/Documents/REAPER Media"


def report(
    seq: int,
    project: str | None = SAVED,
    record_path: str | None = MEDIA,
    script_checksum: str | None = None,
) -> mr.Report:
    fields: list[tuple[str, str]] = [("version", "1"), ("seq", str(seq))]
    if project is not None:
        fields.append(("project", project))
    if record_path is not None:
        fields.append(("record_path", record_path))
    if script_checksum is not None:
        fields.append(("script_checksum", script_checksum))
    return mr.Report(
        version=1,
        seq=seq,
        project=project,
        record_path=record_path,
        fields=tuple(fields),
        script_checksum=script_checksum,
    )


def witnessed(*reads: mr.Report | mr.Unreadable | None, start: float = 0.0, step: float = 1.0) -> mr.Observed:
    observed: mr.Observed | None = None
    now = start
    for read in reads:
        observed = mr.observe(observed, read, now)
        now += step
    assert observed is not None
    return observed


class TestParsing(unittest.TestCase):
    def test_the_golden_file_parses(self):
        parsed = mr.parse_report(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(parsed.version, 1)
        self.assertEqual(parsed.seq, 42)
        self.assertEqual(parsed.project, SAVED)
        self.assertEqual(parsed.record_path, MEDIA)

    def test_unknown_keys_are_ignored_and_kept(self):
        parsed = mr.parse_report(GOLDEN.read_text(encoding="utf-8") + "armed\t1\n")
        self.assertEqual(parsed.fields[-1], ("armed", "1"))
        self.assertEqual(parsed.seq, 42)

    def test_empty_project_and_missing_project_differ(self):
        empty = mr.parse_report("version\t1\nseq\t1\nproject\t\nrecord_path\t/m\n")
        missing = mr.parse_report("version\t1\nseq\t1\nrecord_path\t/m\n")
        self.assertEqual(empty.project, "")
        self.assertIsNone(missing.project)

    def test_a_newer_version_is_refused(self):
        with self.assertRaises(mr.ReportError):
            mr.parse_report(f"version\t{mr.SUPPORTED_VERSION + 1}\nseq\t1\n")

    def test_garbage_is_refused(self):
        for text in ("", "nonsense", "version\t1\n", "version\tx\nseq\t1\n", "version\t1\nseq\tx\n"):
            with self.subTest(text), self.assertRaises(mr.ReportError):
                mr.parse_report(text)

    def test_the_path_sits_beside_the_queue(self):
        self.assertEqual(mr.report_path(Path("/q/queue.tsv")), Path("/q/queue.tsv.report"))


class TestTheScriptChecksum(unittest.TestCase):
    def test_the_published_fnv1a_vectors(self):
        self.assertEqual(mr.source_checksum(b""), "811c9dc5")
        self.assertEqual(mr.source_checksum(b"a"), "e40c292c")
        self.assertEqual(mr.source_checksum(b"foobar"), "bf9cf968")

    def test_every_byte_value_file(self):
        self.assertEqual(mr.source_checksum(CHECKSUM_INPUT.read_bytes()), CHECKSUM_OF_INPUT)

    def test_it_is_eight_lowercase_hex_digits(self):
        self.assertRegex(mr.source_checksum(b"x" * 1000), r"^[0-9a-f]{8}$")

    def test_the_new_golden_file_carries_it(self):
        parsed = mr.parse_report(GOLDEN_CHECKSUM.read_text(encoding="utf-8"))
        self.assertEqual(parsed.script_checksum, CHECKSUM_OF_INPUT)
        self.assertEqual(parsed.fields[-1], (mr.KEY_SCRIPT_CHECKSUM, CHECKSUM_OF_INPUT))
        self.assertEqual(parsed.project, SAVED)

    def test_the_old_golden_file_has_no_checksum(self):
        self.assertIsNone(mr.parse_report(GOLDEN.read_text(encoding="utf-8")).script_checksum)

    def test_an_empty_value_differs_from_an_absent_key(self):
        empty = mr.parse_report("version\t1\nseq\t1\nscript_checksum\t\n")
        absent = mr.parse_report("version\t1\nseq\t1\n")
        self.assertEqual(empty.script_checksum, "")
        self.assertIsNone(absent.script_checksum)

    def test_the_format_version_is_still_one(self):
        self.assertEqual(mr.SUPPORTED_VERSION, 1)


class TestHowOldTheRunningScriptIs(unittest.TestCase):
    def age(self, checksum: str | None, expected: str | None, *, project: str | None = SAVED) -> mr.ScriptAge:
        observed = witnessed(report(1), report(2, project=project, script_checksum=checksum))
        return mr.assess(observed, 1.0, HOME, expected_script=expected).script

    def test_a_match_is_current(self):
        self.assertEqual(self.age(SUM, SUM), mr.ScriptAge.CURRENT)

    def test_a_mismatch_is_older(self):
        self.assertEqual(self.age(OTHER_SUM, SUM), mr.ScriptAge.OLDER)

    def test_no_key_is_older(self):
        self.assertEqual(self.age(None, SUM), mr.ScriptAge.OLDER)

    def test_an_empty_value_is_unchecked(self):
        self.assertEqual(self.age("", SUM), mr.ScriptAge.UNCHECKED)

    def test_no_expected_value_is_unchecked(self):
        self.assertEqual(self.age(SUM, None), mr.ScriptAge.UNCHECKED)
        self.assertEqual(self.age(None, None), mr.ScriptAge.UNCHECKED)

    def test_it_is_assessed_for_unsaved_too(self):
        self.assertEqual(self.age(OTHER_SUM, SUM, project=""), mr.ScriptAge.OLDER)
        self.assertEqual(self.age(SUM, SUM, project=""), mr.ScriptAge.CURRENT)

    def test_unknown_status_is_unchecked_for_every_reason(self):
        cases = {
            mr.Reason.NO_REPORT: mr.Observed.empty(),
            mr.Reason.STOPPED: witnessed(report(1), report(2, script_checksum=OTHER_SUM)),
            mr.Reason.UNREADABLE: witnessed(report(1), mr.Unreadable("x")),
        }
        for reason, observed in cases.items():
            now = 1.0 + mr.STALE_SECONDS if reason is mr.Reason.STOPPED else 1.0
            with self.subTest(reason):
                got = mr.assess(observed, now, HOME, expected_script=SUM)
                self.assertEqual(got.reason, reason)
                self.assertEqual(got.script, mr.ScriptAge.UNCHECKED)

    def test_a_report_with_no_project_is_unchecked(self):
        self.assertEqual(self.age(OTHER_SUM, SUM, project=None), mr.ScriptAge.UNCHECKED)


class TestWhatTheBoxBelieves(unittest.TestCase):
    def check(self, observed: mr.Observed | None, now: float) -> mr.ProjectCheck:
        return mr.assess(observed if observed is not None else mr.Observed.empty(), now, HOME)

    def test_before_any_read_there_is_no_report(self):
        got = self.check(None, 0.0)
        self.assertEqual((got.status, got.reason), (mr.Status.UNKNOWN, mr.Reason.NO_REPORT))

    def test_the_first_read_is_a_baseline_not_news(self):
        got = self.check(witnessed(report(5)), 0.0)
        self.assertEqual((got.status, got.reason), (mr.Status.UNKNOWN, mr.Reason.NO_REPORT))

    def test_the_same_seq_never_becomes_trusted(self):
        got = self.check(witnessed(report(5), report(5), report(5)), 3.0)
        self.assertEqual(got.reason, mr.Reason.NO_REPORT)

    def test_a_witnessed_change_is_trusted(self):
        got = self.check(witnessed(report(5), report(6)), 1.0)
        self.assertEqual(got.status, mr.Status.SAVED)
        self.assertEqual(got.record_path, MEDIA)
        self.assertEqual(got.project, SAVED)

    def test_unsaved_is_an_empty_project_and_nothing_else(self):
        got = self.check(witnessed(report(1), report(2, project="", record_path=DOCS)), 1.0)
        self.assertEqual(got.status, mr.Status.UNSAVED)
        self.assertEqual(got.record_path, DOCS)

    def test_unsaved_with_no_path_still_says_unsaved(self):
        got = self.check(witnessed(report(1), report(2, project="", record_path="")), 1.0)
        self.assertEqual(got.status, mr.Status.UNSAVED)
        self.assertIsNone(got.record_path)

    def test_a_missing_project_key_is_unknown(self):
        got = self.check(witnessed(report(1), report(2, project=None)), 1.0)
        self.assertEqual((got.status, got.reason), (mr.Status.UNKNOWN, mr.Reason.UNREADABLE))

    def test_saved_with_no_path_is_unknown(self):
        got = self.check(witnessed(report(1), report(2, record_path=None)), 1.0)
        self.assertEqual((got.status, got.reason), (mr.Status.UNKNOWN, mr.Reason.UNREADABLE))

    def test_stale_at_exactly_the_limit(self):
        observed = witnessed(report(1), report(2), start=100.0)
        self.assertEqual(self.check(observed, 101.0 + mr.STALE_SECONDS - 0.001).status, mr.Status.SAVED)
        got = self.check(observed, 101.0 + mr.STALE_SECONDS)
        self.assertEqual((got.status, got.reason), (mr.Status.UNKNOWN, mr.Reason.STOPPED))

    def test_a_deleted_file_ages_to_stale(self):
        observed = witnessed(report(1), report(2), None, None, start=0.0)
        self.assertEqual(self.check(observed, 3.0).status, mr.Status.SAVED)
        self.assertEqual(self.check(observed, 1.0 + mr.STALE_SECONDS).reason, mr.Reason.STOPPED)

    def test_a_new_seq_after_a_gap_is_trusted_again(self):
        observed = witnessed(report(1), report(2), report(3), start=0.0, step=20.0)
        self.assertEqual(self.check(observed, 40.0).status, mr.Status.SAVED)

    def test_an_unreadable_file_is_unreadable_until_a_good_one_replaces_it(self):
        bad = mr.Unreadable("garbage")
        observed = witnessed(report(1), report(2), bad)
        self.assertEqual(self.check(observed, 2.0).reason, mr.Reason.UNREADABLE)
        observed = mr.observe(observed, report(3), 3.0)
        self.assertEqual(self.check(observed, 3.0).status, mr.Status.SAVED)

    def test_an_unreadable_first_read_is_unreadable(self):
        self.assertEqual(self.check(witnessed(mr.Unreadable("x")), 0.0).reason, mr.Reason.UNREADABLE)


class TestTheSnapshot(unittest.TestCase):
    def test_home_is_shortened_and_seq_is_left_out(self):
        got = mr.assess(witnessed(report(1), report(2)), 1.0, HOME).as_snapshot()
        self.assertEqual(
            got,
            {
                "status": "saved",
                "reason": None,
                "record_path": "~/Reaper Media/20261114 X/Media",
                "project": "~/Reaper Media/20261114 X/20261114 X.RPP",
                "script": "unchecked",
            },
        )
        self.assertNotIn("seq", got)

    def test_only_a_whole_leading_component_is_shortened(self):
        check = mr.ProjectCheck(mr.Status.SAVED, None, "/Users/xavier/Media", None, home=HOME)
        self.assertEqual(check.as_snapshot()["record_path"], "/Users/xavier/Media")

    def test_unknown_carries_a_reason_and_no_paths(self):
        got = mr.assess(mr.Observed.empty(), 0.0, HOME).as_snapshot()
        want = {"status": "unknown", "reason": "no-report", "record_path": None, "project": None}
        self.assertEqual(got, {**want, "script": "unchecked"})

    def test_the_script_age_is_in_the_snapshot(self):
        observed = witnessed(report(1), report(2, script_checksum=OTHER_SUM))
        got = mr.assess(observed, 1.0, HOME, expected_script=SUM).as_snapshot()
        self.assertEqual(got["script"], "older")


class TestTheShell(unittest.TestCase):
    def test_a_missing_file_reads_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mr.read_report(Path(tmp) / "none.report"))

    def test_a_good_file_reads_and_a_bad_one_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.report"
            path.write_bytes(GOLDEN.read_bytes())
            got = mr.read_report(path)
            assert isinstance(got, mr.Report)
            self.assertEqual(got.seq, 42)
            path.write_bytes(b"\xff\xfe")
            self.assertIsInstance(mr.read_report(path), mr.Unreadable)


class TestTheWatch(unittest.IsolatedAsyncioTestCase):
    async def run_watch(self, read, rounds: int = 2) -> list[mr.Observed]:
        seen: list[mr.Observed] = []
        calls = 0

        async def sleep(_seconds: float) -> None:
            nonlocal calls
            calls += 1
            if calls >= rounds:
                raise asyncio.CancelledError

        watch = mr.Watch(read, seen.append, clock=lambda: float(calls), sleep=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await watch.run()
        return seen

    async def test_the_read_runs_off_the_loop_thread(self):
        threads: list[int] = []

        def read() -> mr.Report | None:
            threads.append(threading.get_ident())
            return None

        await self.run_watch(read)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_a_raising_read_does_not_kill_the_watch(self):
        attempts = 0

        def read() -> mr.Report | None:
            nonlocal attempts
            attempts += 1
            raise OSError("disk gone")

        seen = await self.run_watch(read, rounds=3)
        self.assertEqual(attempts, 3)
        self.assertTrue(seen[-1].unreadable)

    async def test_a_raising_callback_does_not_kill_the_watch(self):
        def boom(_observed: mr.Observed) -> None:
            raise RuntimeError("callback")

        calls = 0

        async def sleep(_seconds: float) -> None:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise asyncio.CancelledError

        watch = mr.Watch(lambda: None, boom, clock=lambda: 0.0, sleep=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await watch.run()
        self.assertEqual(calls, 3)

    async def test_each_read_is_observed_in_order(self):
        reads = iter([report(1), report(2)])
        seen = await self.run_watch(lambda: next(reads), rounds=2)
        self.assertEqual([o.seq for o in seen], [1, 2])
        self.assertIsNotNone(seen[-1].changed_at)


if __name__ == "__main__":
    unittest.main()
