import contextlib
import importlib
import io
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tacet import dm7, pilot_check
from tacet.annotations import Entry

from .pilot_wav import commanded, plain, tone, wav_bytes

RATE = 8000
REPO_ROOT = Path(__file__).resolve().parent.parent
WALL = "2026-10-02T22:{minute:02d}:00+00:00"
BEFORE_TAKE = "2026-10-02T21:00:00Z"


def pilot_wav() -> bytes:
    """Silent, then a -12 dBFS pilot 90 ms after a stamp at 1.0 s, until 90 ms after one at 4.0 s."""
    return wav_bytes(tone([(1.09, None), (3.0, -12.0), (0.91, None)], rate=RATE), rate=RATE, time_reference=0)


def log_entries(*, fade_level: int = 0, anchored: bool = True, fade_at: float = 4.0) -> list[Entry]:
    entries = [plain(1, WALL.format(minute=0), "recording-started")] if anchored else []
    entries += [
        commanded(2, WALL.format(minute=1), 1.0, "open", "up-drums", dm7.MINUS_INF, 0),
        commanded(3, WALL.format(minute=2), fade_at, "fade", "out", fade_level, dm7.MINUS_INF),
    ]
    return entries


class Run:
    def __init__(self, entries: Sequence[Entry], wav: bytes | None = None, *args: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "game.jsonl"
            log.write_text("".join(e.to_json() + "\n" for e in entries), encoding="utf-8")
            pilot = Path(tmp) / "pilot.wav"
            pilot.write_bytes(pilot_wav() if wav is None else wav)
            self.out, self.err = io.StringIO(), io.StringIO()
            self.code = pilot_check.main([str(log), str(pilot), *args], out=self.out, err=self.err)


class TestPilotCheck(unittest.TestCase):
    def test_clean_take_exits_0(self):
        run = Run(log_entries())
        self.assertEqual(run.code, pilot_check.EXIT_CLEAN, run.err.getvalue())
        self.assertIn("none flagged", run.out.getvalue())

    def test_flagged_take_exits_1_and_names_the_seq(self):
        run = Run(log_entries(fade_level=-600))
        self.assertEqual(run.code, pilot_check.EXIT_FLAGGED)
        self.assertIn("seq 3", run.out.getvalue())

    def test_unchecked_ramp_exits_1(self):
        run = Run(log_entries(fade_at=100.0))
        self.assertEqual(run.code, pilot_check.EXIT_FLAGGED)
        self.assertIn("could not be checked", run.out.getvalue())

    def test_a_take_that_ended_early_exits_1(self):
        entries = log_entries()
        later = commanded(5, WALL.format(minute=4), 4.0, "fade", "out", 0, dm7.MINUS_INF)
        second = plain(4, WALL.format(minute=3), "recording-started")
        run = Run([*entries, second, later])
        self.assertEqual(run.code, pilot_check.EXIT_FLAGGED)
        self.assertIn("TAKE ENDED by seq 4", run.out.getvalue())
        self.assertIn("could not be checked", run.out.getvalue())

    def test_a_malformed_ramp_is_a_row_and_exit_1(self):
        entries = log_entries()
        bad = Entry(**{**entries[-1].as_dict(), "seq": 9, "data": {**entries[-1].data, "target": "x"}})
        run = Run([*entries, bad])
        self.assertEqual(run.code, pilot_check.EXIT_FLAGGED)
        self.assertIn("not a console level", run.out.getvalue())

    def test_log_without_anchor_exits_2_and_names_take_start(self):
        run = Run(log_entries(anchored=False))
        self.assertEqual(run.code, pilot_check.EXIT_REFUSED)
        self.assertIn("--take-start", run.err.getvalue())

    def test_take_start_supplies_the_anchor(self):
        run = Run(log_entries(anchored=False), None, "--take-start", BEFORE_TAKE)
        self.assertEqual(run.code, pilot_check.EXIT_CLEAN, run.err.getvalue())

    def test_naive_take_start_is_rejected(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            pilot_check.main(["a.jsonl", "b.wav", "--take-start", "2026-10-02T21:00:00"])
        self.assertEqual(caught.exception.code, pilot_check.EXIT_REFUSED)
        self.assertIn("timezone", err.getvalue())

    def test_take_start_with_a_logged_anchor_exits_2(self):
        run = Run(log_entries(), None, "--take-start", BEFORE_TAKE)
        self.assertEqual(run.code, pilot_check.EXIT_REFUSED)
        self.assertIn("--take-start", run.err.getvalue())

    def test_unreadable_wav_exits_2(self):
        run = Run(log_entries(), b"not a wav at all")
        self.assertEqual(run.code, pilot_check.EXIT_REFUSED)
        self.assertIn("RIFF", run.err.getvalue())

    def test_corrupt_log_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "game.jsonl"
            log.write_text("{not json}\n{}\n", encoding="utf-8")
            pilot = Path(tmp) / "pilot.wav"
            pilot.write_bytes(pilot_wav())
            err = io.StringIO()
            code = pilot_check.main([str(log), str(pilot)], out=io.StringIO(), err=err)
        self.assertEqual(code, pilot_check.EXIT_REFUSED)
        self.assertIn("line 1", err.getvalue())

    def test_missing_file_exits_2(self):
        err = io.StringIO()
        code = pilot_check.main(["/nonexistent/game.jsonl", "/nonexistent/p.wav"], out=io.StringIO(), err=err)
        self.assertEqual(code, pilot_check.EXIT_REFUSED)

    def test_missing_numpy_says_how_to_install(self):
        import tacet

        saved = importlib.import_module("tacet.pilot")
        del tacet.pilot
        try:
            with mock.patch.dict(sys.modules, {"numpy": None}):
                sys.modules.pop("tacet.pilot", None)
                err = io.StringIO()
                code = pilot_check.main(["a.jsonl", "b.wav"], out=io.StringIO(), err=err)
        finally:
            tacet.pilot = saved
            sys.modules["tacet.pilot"] = saved
        self.assertEqual(code, pilot_check.EXIT_REFUSED)
        self.assertIn(".[analysis]", err.getvalue())

    def test_console_script_is_declared(self):
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["tacet-pilot-check"], "tacet.pilot_check:main")


if __name__ == "__main__":
    unittest.main()
