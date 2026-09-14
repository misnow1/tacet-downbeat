"""The game 2 log, as the box wrote it on 2026-09-12, read by today's code.

Every other log test reads what the current code has just written, so a change
to the writer and the reader together passes them all while leaving the one log
that matters unreadable. This file is that log, byte for byte - schema v1, 160
entries, no site values in it (the only free text is two notes, "Fed" and
"TD") - and it is never regenerated: `test_the_fixture_is_the_game_2_log_as_written`
fails if anyone tries.

A schema change that cannot read it is a schema change that needs a new `v`, a
reader for the old one, and this file kept.
"""

import hashlib
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tacet import annotations as ann
from tacet import markers, mirror

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAME_2_LOG = FIXTURES / "log-v1.jsonl"
GAME_2_SHA256 = "af91155bedb5bc62555e6a0a9cf217749dd2b5ac24f5ee55747105f97b456d32"
GAME_2_ENTRIES = 160
#: `recording-started` came second, after arming.
GAME_2_ANCHOR_SEQ = 2
#: Four quarters and two home timeouts.
GAME_2_REGIONS = 6


class TestTheFixture(unittest.TestCase):
    def test_the_fixture_is_the_game_2_log_as_written(self):
        self.assertEqual(hashlib.sha256(GAME_2_LOG.read_bytes()).hexdigest(), GAME_2_SHA256)

    def test_there_is_a_fixture_for_the_schema_this_build_writes(self):
        # Bumping `SCHEMA_VERSION` means adding a fixture written under it.
        self.assertTrue((FIXTURES / f"log-v{ann.SCHEMA_VERSION}.jsonl").exists())

    def test_every_schema_this_build_reads_has_a_fixture(self):
        for version in ann.READABLE_SCHEMAS:
            with self.subTest(version=version):
                self.assertTrue((FIXTURES / f"log-v{version}.jsonl").exists())


class TestAGame2LogStillWorks(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A copy: the box appends to whatever log it is given.
        self.path = Path(self._tmp.name) / "game.jsonl"
        shutil.copyfile(GAME_2_LOG, self.path)

    def test_a_game_2_log_still_reads_resumes_and_derives(self):
        entries = list(ann.read_entries(self.path))
        self.assertEqual(len(entries), GAME_2_ENTRIES)

        with ann.AnnotationLog(self.path) as log:
            # Resumed where game 2 left off: numbering continues, every span it
            # opened was closed, and the earlier recording is noticed.
            self.assertEqual(log.open_spans(), {})
            prior = log.prior_anchor
            assert prior is not None
            self.assertEqual(prior.seq, GAME_2_ANCHOR_SEQ)
            self.assertEqual(log.record("note").seq, GAME_2_ENTRIES + 1)

        anchor = markers.find_anchor(entries)
        derived = markers.derive(entries, anchor)
        self.assertEqual(anchor.seq, GAME_2_ANCHOR_SEQ)
        self.assertEqual(sum(1 for m in derived.markers if m.end is not None), GAME_2_REGIONS)
        self.assertEqual((derived.unclosed_spans, derived.orphan_ends, derived.extra_anchors), ((), (), ()))
        # Arming came before the recording, so it has nowhere on the timeline.
        self.assertEqual([e.event for e in derived.skipped_before_anchor], ["armed"])

    def test_every_key_in_the_game_2_log_still_resolves(self):
        # Keys are stable and machine-facing (annotations.VOCABULARY); labels
        # may be reworded, so only the key and its kind are held to.
        for entry in ann.read_entries(self.path):
            with self.subTest(event=entry.event):
                self.assertEqual(ann.lookup(entry.event).kind.value, entry.kind)

    def test_its_queue_can_be_rebuilt(self):
        rebuilt = mirror.rebuild_queue(ann.read_entries(self.path))
        self.assertEqual(len(rebuilt.splitlines()), GAME_2_ENTRIES)
