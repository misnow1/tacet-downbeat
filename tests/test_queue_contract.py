"""The queue files the box and the Reaper mirror are both held to (#39).

See `tests/queue_fixtures.py`. The Lua half of the contract is the replay in
`reaper/test_tacet_mirror.lua`.
"""

import hashlib
import unittest
from pathlib import Path

from tacet import annotations as ann
from tacet import markers, mirror
from tests import queue_fixtures as qf

LUA_TESTS = Path(__file__).resolve().parent.parent / "reaper" / "test_tacet_mirror.lua"
REGENERATE = "the queue contract has changed; run `python -m tests.queue_fixtures` and review the diff"

#: The game 2 queue is derived from a log that never changes, so it may only
#: change when the queue format does - which is a change to the Lua mirror too.
GAME_2_QUEUE_SHA256 = "67b05f14b198f10687778ae651a2f91eeda31534fd15e358bbfae6d0b723cbe6"


class TestQueueFixtures(unittest.TestCase):
    generated: dict[Path, str]

    @classmethod
    def setUpClass(cls):
        cls.generated = qf.generate()

    def test_queue_fixture_matches_rebuild(self):
        for path, text in self.generated.items():
            with self.subTest(fixture=path.name):
                self.assertTrue(path.exists(), f"{path.name} is missing; {REGENERATE}")
                self.assertEqual(path.read_text(encoding="utf-8"), text, REGENERATE)

    def test_the_game_2_queue_is_pinned_byte_for_byte(self):
        # Not only "matches what the code writes now": a writer changed
        # together with its fixture passes that. This is the queue as the game
        # 2 log gives it, and changing it is changing the format.
        data = qf.queue_path(qf.GAME_2).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), GAME_2_QUEUE_SHA256)

    def test_the_game_2_marks_are_what_derive_places(self):
        # The live path (a mirror placing marks as lines arrive) and the
        # offline path (`markers.derive` from the log) must agree on every mark.
        # Not on order: derive sorts by position, so a region sits where its
        # span began, while a live mirror adds it when the span ends - and
        # Reaper orders markers by position whichever way they arrived. The one
        # difference allowed is the one derive reports: entries before the
        # recording started have nowhere on its timeline.
        entries = list(ann.read_entries(qf.GAME_2_LOG))
        derived = markers.derive(entries, markers.find_anchor(entries))
        offline = [(qf.REGION if m.is_region else qf.MARKER, m.name) for m in derived.markers]
        skipped = [(qf.MARKER, ann.marker_name(e)) for e in derived.skipped_before_anchor]
        live = qf.placements(qf.queue_path(qf.GAME_2).read_text(encoding="utf-8"))
        self.assertEqual(sorted(live), sorted(skipped + offline))

    def test_every_event_is_in_the_vocabulary_queue(self):
        names = {name for _, name in qf.placements(qf.queue_path(qf.VOCABULARY).read_text(encoding="utf-8"))}
        for event in ann.VOCABULARY:
            with self.subTest(key=event.key):
                self.assertIn(f"{event.category.value}{ann.MARKER_DELIMITER}{event.key}", names)

    def test_every_fixture_line_parses_strictly(self):
        for name in (qf.GAME_2, qf.VOCABULARY):
            for line in qf.queue_path(name).read_text(encoding="utf-8").splitlines():
                with self.subTest(fixture=name, line=line):
                    mirror.parse_queue_line(line)

    def test_the_lua_tests_replay_every_fixture(self):
        source = LUA_TESTS.read_text(encoding="utf-8")
        for name in (qf.GAME_2, qf.VOCABULARY):
            with self.subTest(fixture=name):
                self.assertTrue(f'"{name}"' in source, f"{LUA_TESTS.name} does not replay {name!r}")
