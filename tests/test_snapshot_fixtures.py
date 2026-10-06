"""The page's snapshot fixtures are what the box sends today (#38).

See `tests/snapshots.py`. A failure here means `App.snapshot()` changed: run
`make snapshots`, read the diff it makes to the fixtures, and let
`make test-js` show whether the page still reads it.
"""

import json
import unittest
from pathlib import Path

from tacet import reaper
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

    def snapshots_only(self):
        """The snapshots, not the move curves, which are not a snapshot."""
        return {path: text for path, text in self.generated.items() if path.name.startswith(snapshots.PREFIX)}

    def test_the_move_curves_fixture_is_generated(self):
        self.assertIn(snapshots.CURVES_PATH, self.generated)

    def test_no_machine_path_leaks_into_a_fixture(self):
        for path, text in self.snapshots_only().items():
            with self.subTest(fixture=path.name):
                self.assertEqual(json.loads(text)["log"]["path"], f"{snapshots.LOG_DIR}/game.jsonl")

    def test_every_fixture_says_what_code_the_box_runs(self):
        for path, text in self.snapshots_only().items():
            with self.subTest(fixture=path.name):
                code = json.loads(text)["provenance"]
                self.assertEqual(set(code), {"source", "dirty", "where", "error"})
                # Only the fix on the day is dirty; no machine path is in any of them.
                self.assertEqual(code["dirty"], path.name == "snapshot-faults.json")

    def test_each_state_is_the_state_it_is_named_for(self):
        states = {path.name: json.loads(text) for path, text in self.snapshots_only().items()}
        self.assertEqual(states["snapshot-standing-down.json"]["state"], "standing-down")
        self.assertEqual(states["snapshot-open-recording.json"]["recording"]["liveness"], "live")
        self.assertEqual(states["snapshot-releasing.json"]["fader"]["target"], -32768)
        # Reaper never heard from: the page with Reaper closed (#163).
        self.assertEqual(states["snapshot-standing-down.json"]["recording"]["refusal"], reaper.RECORD_REFUSED_SILENT)
        parked = states["snapshot-parked-unreported.json"]["recording"]
        self.assertEqual(parked["liveness"], "live")
        self.assertFalse(parked["known"])
        self.assertTrue(parked["can_start"])
        self.assertIsNone(parked["refusal"])
        releasing = states["snapshot-releasing.json"]["fader"]
        self.assertEqual(releasing["move"]["by"], "out")
        self.assertEqual(releasing["move"]["kind"], "fade")
        self.assertTrue(releasing["moving"])
        riding = states["snapshot-riding.json"]
        self.assertEqual(riding["state"], "open")
        self.assertEqual(riding["fader"]["move"]["kind"], "ride")
        self.assertEqual(riding["fader"]["move"]["by"], "up-slow")
        self.assertIsNotNone(riding["fader"]["move"]["knee"])
        stored = states["snapshot-target-stored.json"]
        self.assertEqual(stored["state"], "releasing")
        self.assertEqual(stored["fader"]["move"]["by"], "out")
        self.assertEqual(stored["target"]["db"], -3.0)
        self.assertEqual(stored["target"]["level"], -300)
        self.assertEqual(stored["target"]["stored"], {"because": "releasing", "db": -3.0})
        self.assertIsNone(stored["refusal"])
        retargeting = states["snapshot-retargeting.json"]
        self.assertEqual(retargeting["state"], "open")
        self.assertEqual(retargeting["fader"]["move"]["kind"], "ride")
        self.assertEqual(retargeting["fader"]["move"]["by"], "target-set")
        self.assertEqual(retargeting["fader"]["move"]["to_db"], -3.0)
        self.assertEqual(retargeting["fader"]["move"]["seconds"], 1.0)
        self.assertIsNotNone(retargeting["fader"]["move"]["knee"])
        self.assertIsNone(retargeting["target"]["stored"])
        prompt = states["snapshot-prompt.json"]
        self.assertEqual(prompt["state"], "open")
        self.assertEqual(prompt["prompt"], {"seq": 1, "kind": "stand-down", "source": "band-exits-stands"})
        prompt_arm = states["snapshot-prompt-arm.json"]
        self.assertEqual(prompt_arm["state"], "standing-down")
        self.assertEqual(prompt_arm["prompt"], {"seq": 1, "kind": "arm", "source": "band-enters-stands"})
        self.assertIsNotNone(prompt_arm["refusal"])
        self.assertFalse(prompt_arm["fader"]["level_known"])
        self.assertFalse(prompt_arm["duty"]["armed"])
        self.assertIsNone(prompt_arm["duty"]["since"])
        faults = states["snapshot-faults.json"]
        self.assertFalse(faults["fader"]["healthy"])
        self.assertFalse(faults["log"]["healthy"])
        self.assertEqual(faults["recording"]["liveness"], "lost")
        self.assertIn("stopped answering", faults["recording"]["refusal"])
        self.assertIsNotNone(faults["refusal"])
        # The console is unreachable and the snap open delivered nothing, so
        # the page must not also show a confident number (#116).
        self.assertFalse(faults["fader"]["level_known"])

    #: The fixtures whose page does not know where the fader is: the cold-boot
    #: page, which has told the console nothing, the faults page, whose snap
    #: open never left the box (#116), and the refused-arm page, which is a
    #: cold boot too (#19).
    UNKNOWN_LEVEL = frozenset({"snapshot-standing-down.json", "snapshot-faults.json", "snapshot-prompt-arm.json"})

    def test_only_a_page_that_never_delivered_a_level_says_it_does_not_know(self):
        # #107: the boot fixture IS the cold-boot page and stays honest about
        # it; every state that got somewhere by arming was told the level -
        # unless, since #116, the delivery that would have told it failed.
        states = {path.name: json.loads(text) for path, text in self.snapshots_only().items()}
        for name, snapshot in states.items():
            with self.subTest(fixture=name):
                self.assertEqual(snapshot["fader"]["level_known"], name not in self.UNKNOWN_LEVEL)
                self.assertNotIn("handoff", snapshot)
                self.assertNotIn("queued", snapshot)

    def test_every_fixture_says_whether_it_is_asking_and_whether_it_is_on_duty(self):
        # #19: `prompt` is the question on the page, or null; `duty` is when the
        # box last armed or stood down on its own monotonic clock, or null.
        for path, text in self.snapshots_only().items():
            snapshot = json.loads(text)
            with self.subTest(fixture=path.name):
                self.assertIn("prompt", snapshot)
                self.assertEqual(set(snapshot["duty"]), {"armed", "since"})
                self.assertEqual(snapshot["duty"]["armed"], snapshot["state"] != "standing-down")
                asking = path.name in {"snapshot-prompt.json", "snapshot-prompt-arm.json"}
                self.assertEqual(snapshot["prompt"] is not None, asking)

    #: The fixtures with a move in flight; every other one has nothing moving.
    MOVING = frozenset(
        {
            "snapshot-releasing.json",
            "snapshot-riding.json",
            "snapshot-retargeting.json",
            "snapshot-target-stored.json",
        }
    )

    def test_every_fixture_says_whether_a_move_is_in_flight(self):
        # #154: `fader.move` is the description the page animates from, and
        # `moving` is derived from it, so the two never disagree.
        for path, text in self.snapshots_only().items():
            fader = json.loads(text)["fader"]
            with self.subTest(fixture=path.name):
                self.assertIn("move", fader)
                self.assertEqual(fader["move"] is not None, path.name in self.MOVING)
                self.assertEqual(fader["moving"], fader["move"] is not None)

    def test_every_fixture_says_whether_a_target_tap_only_stored(self):
        # #153: `target.stored` is the note under the segments, or null. Only a
        # tap made while the fader is up leaves one.
        for path, text in self.snapshots_only().items():
            target = json.loads(text)["target"]
            with self.subTest(fixture=path.name):
                self.assertIn("stored", target)
                self.assertEqual(target["stored"] is not None, path.name == "snapshot-target-stored.json")

    def test_the_page_tests_load_the_fixtures_rather_than_a_copy(self):
        source = PAGE_TESTS.read_text(encoding="utf-8")
        self.assertIn(snapshots.PREFIX, source)
        self.assertIn("fixtures", source)
