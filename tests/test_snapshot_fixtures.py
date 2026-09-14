"""The page's snapshot fixtures are what the box sends today (#38).

See `tests/snapshots.py`. A failure here means `App.snapshot()` changed: run
`make snapshots`, read the diff it makes to the fixtures, and let
`make test-js` show whether the page still reads it.
"""

import json
import unittest
from pathlib import Path

from tests import snapshots

PAGE_TESTS = Path(__file__).resolve().parent / "test_app_js.mjs"
REGENERATE = "App.snapshot() has changed; run `make snapshots` and review the fixture diff"


class TestSnapshotFixtures(unittest.TestCase):
    generated: dict[Path, str]

    @classmethod
    def setUpClass(cls):
        cls.generated = snapshots.generate()

    def test_snapshot_fixtures_are_current(self):
        for path, text in self.generated.items():
            with self.subTest(fixture=path.name):
                self.assertTrue(path.exists(), f"{path.name} is missing; {REGENERATE}")
                self.assertEqual(path.read_text(encoding="utf-8"), text, REGENERATE)

    def test_there_are_no_fixtures_for_states_that_are_gone(self):
        self.assertEqual(snapshots.existing(), set(self.generated), REGENERATE)

    def test_generating_twice_gives_the_same_bytes(self):
        # A fixture that changed on its own would fail CI at random.
        self.assertEqual(snapshots.generate(), self.generated)

    def test_no_machine_path_leaks_into_a_fixture(self):
        for path, text in self.generated.items():
            with self.subTest(fixture=path.name):
                self.assertEqual(json.loads(text)["log"]["path"], f"{snapshots.LOG_DIR}/game.jsonl")

    def test_each_state_is_the_state_it_is_named_for(self):
        states = {path.name: json.loads(text) for path, text in self.generated.items()}
        self.assertEqual(states["snapshot-standing-down.json"]["state"], "standing-down")
        self.assertEqual(states["snapshot-open-recording.json"]["recording"]["liveness"], "live")
        self.assertEqual(states["snapshot-releasing.json"]["fader"]["target"], -32768)
        faults = states["snapshot-faults.json"]
        self.assertFalse(faults["fader"]["healthy"])
        self.assertFalse(faults["log"]["healthy"])
        self.assertEqual(faults["recording"]["liveness"], "lost")
        self.assertIsNotNone(faults["refusal"])

    def test_the_page_tests_load_the_fixtures_rather_than_a_copy(self):
        source = PAGE_TESTS.read_text(encoding="utf-8")
        self.assertIn(snapshots.PREFIX, source)
        self.assertIn("fixtures", source)
