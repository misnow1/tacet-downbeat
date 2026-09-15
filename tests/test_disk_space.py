import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tacet import disk

AUDIO = Path("/Volumes/band")
LOG = Path("/Users/op/games/game.jsonl")
QUEUE = Path("/Users/op/queue.tsv")

#: Game 2 (2026-09-12): 16 channels, and `Media/` came to 39.7 GB with peaks.
GAME_2_CHANNELS = 16
GAME_2_MEDIA_BYTES = 39.7 * disk.BYTES_PER_GB


def plan(**overrides: Any) -> disk.Plan:
    values: dict[str, Any] = {
        "audio_path": AUDIO,
        "channels": GAME_2_CHANNELS,
        "hours": disk.DEFAULT_GAME_HOURS,
        "log_path": LOG,
        "queue_path": QUEUE,
        "override": False,
    }
    values.update(overrides)
    return disk.Plan(**values)


def volume(path, free, device=1):
    return disk.Volume(path=path, free=free, device=device)


PLENTY = 500 * disk.BYTES_PER_GB


class TestTheEstimate(unittest.TestCase):
    def test_a_channel_is_144_kb_a_second(self):
        # 48 kHz x 24-bit mono WAV, the figure in #53.
        self.assertEqual(disk.BYTES_PER_CHANNEL_SECOND, 144_000)
        self.assertEqual(disk.SAMPLE_RATE, 48_000)
        self.assertEqual(disk.BYTES_PER_SAMPLE, 3)

    def test_the_issue_table_per_hour(self):
        for channels, gb_per_hour in ((16, 8.3), (22, 11.4)):
            with self.subTest(channels=channels):
                raw = channels * disk.BYTES_PER_CHANNEL_SECOND * disk.SECONDS_PER_HOUR
                self.assertAlmostEqual(raw / disk.BYTES_PER_GB, gb_per_hour, places=1)

    def test_game_2_is_never_underestimated_and_is_within_the_overhead(self):
        required = disk.required_bytes(GAME_2_CHANNELS, disk.DEFAULT_GAME_HOURS)
        self.assertGreater(required, GAME_2_MEDIA_BYTES)
        raw = required / (1 + disk.OVERHEAD)
        self.assertLess(raw, GAME_2_MEDIA_BYTES * (1 + disk.OVERHEAD))

    def test_more_channels_or_hours_need_more(self):
        base = disk.required_bytes(16, 5.0)
        self.assertGreater(disk.required_bytes(22, 5.0), base)
        self.assertGreater(disk.required_bytes(16, 6.0), base)


def refused(verdict: disk.Verdict) -> str:
    assert verdict.refusal is not None, "expected a refusal"
    return verdict.refusal


class TestTheVerdict(unittest.TestCase):
    def assess(self, the_plan, audio=None, others=()):
        if audio is None and the_plan.audio_path is not None:
            audio = volume(the_plan.audio_path, PLENTY)
        return disk.assess(the_plan, audio, list(others) or [volume(LOG.parent, PLENTY)])

    def test_enough_space_starts_and_says_how_much(self):
        verdict = self.assess(plan())
        self.assertIsNone(verdict.refusal)
        self.assertIn("500.0 GB free on /Volumes/band", verdict.summary)
        need = disk.gigabytes(disk.required_bytes(16, 5.0))
        self.assertIn(f"need ~{need} for 16 ch x 5.0 h", verdict.summary)

    def test_insufficient_space_refuses_and_names_the_override(self):
        short = disk.required_bytes(16, 5.0) - 1
        verdict = self.assess(plan(), audio=volume(AUDIO, short))
        self.assertIsNotNone(verdict.refusal)
        self.assertIn(disk.OVERRIDE_FLAG, refused(verdict))
        self.assertIn(str(AUDIO), refused(verdict))
        self.assertIn("16 ch x 5.0 h", refused(verdict))

    def test_an_unset_audio_path_refuses_and_names_the_override(self):
        verdict = self.assess(plan(audio_path=None))
        self.assertIn("capture.audio_path", refused(verdict))
        self.assertIn(disk.OVERRIDE_FLAG, refused(verdict))

    def test_an_unmounted_audio_path_is_refused(self):
        verdict = disk.assess(plan(), None, [volume(LOG.parent, PLENTY)])
        self.assertIn(str(AUDIO), refused(verdict))
        self.assertIn("mounted", refused(verdict))
        self.assertIn(disk.OVERRIDE_FLAG, refused(verdict))

    def test_channels_are_required_with_an_audio_path(self):
        verdict = self.assess(plan(channels=None))
        self.assertIn("capture.channels", refused(verdict))

    def test_nonsense_channels_or_hours_are_refused(self):
        for bad in (plan(channels=0), plan(hours=0.0), plan(hours=-1.0)):
            with self.subTest(bad):
                self.assertIsNotNone(self.assess(bad).refusal)

    def test_a_full_log_volume_refuses(self):
        # Small files, but a full disk tears them (#26).
        others = [volume(LOG.parent, disk.MIN_LOG_FREE_BYTES - 1, device=2)]
        verdict = self.assess(plan(), others=others)
        self.assertIn(str(LOG.parent), refused(verdict))
        self.assertIn(disk.OVERRIDE_FLAG, refused(verdict))

    def test_a_separate_log_volume_is_reported(self):
        others = [volume(LOG.parent, PLENTY, device=2)]
        verdict = self.assess(plan(), audio=volume(AUDIO, PLENTY, device=1), others=others)
        self.assertTrue(any(str(LOG.parent) in note for note in verdict.notes))

    def test_a_log_on_the_recording_volume_is_not_repeated(self):
        others = [volume(LOG.parent, PLENTY, device=1)]
        verdict = self.assess(plan(), audio=volume(AUDIO, PLENTY, device=1), others=others)
        self.assertEqual(verdict.notes, ())

    def test_the_override_flag_starts_anyway_and_says_so(self):
        short = volume(AUDIO, 1)
        verdict = self.assess(plan(override=True), audio=short)
        self.assertIsNone(verdict.refusal)
        self.assertIn("free on /Volumes/band", verdict.summary)
        self.assertTrue(any(disk.OVERRIDE_FLAG in note for note in verdict.notes))

    def test_the_override_with_nothing_to_measure_says_it_did_not_check(self):
        verdict = self.assess(plan(override=True, audio_path=None))
        self.assertIsNone(verdict.refusal)
        self.assertIn("not checked", verdict.summary)
        self.assertIn(disk.OVERRIDE_FLAG, verdict.summary)


class TestMeasuring(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_directory_is_measured(self):
        measured = disk.measure(self.root)
        assert measured is not None
        self.assertEqual(measured.path, self.root)
        self.assertGreater(measured.free, 0)

    def test_a_missing_path_is_not_measured(self):
        # Not the directory underneath it: that is the local disk (#53).
        self.assertIsNone(disk.measure(self.root / "not-mounted"))

    def test_a_file_is_not_a_recording_directory(self):
        path = self.root / "file"
        path.write_text("", encoding="utf-8")
        self.assertIsNone(disk.measure(path))

    def test_a_log_not_yet_created_is_measured_where_it_will_be(self):
        measured = disk.measure_for_file(self.root / "games" / "2026-10-02.jsonl")
        self.assertEqual(measured.path, self.root)

    def test_check_measures_what_the_plan_names(self):
        verdict = disk.check(
            plan(audio_path=self.root, channels=1, hours=0.001, log_path=self.root / "g.jsonl", queue_path=None)
        )
        self.assertIsNone(verdict.refusal, verdict.refusal)
        self.assertIn(str(self.root), verdict.summary)


if __name__ == "__main__":
    unittest.main()
