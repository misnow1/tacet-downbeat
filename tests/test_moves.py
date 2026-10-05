"""The move description the page animates from (#154). Pure: no loop, no socket."""

from __future__ import annotations

import json
import unittest

from tacet import dm7, moves

#: The keys the page reads. Adding one is a contract change for app.js, so it
#: is pinned here rather than discovered on the iPad.
PAGE_CONTRACT = {"seq", "kind", "by", "from_db", "to_db", "seconds", "started_at", "floor_db", "knee"}

#: Arbitrary, distinct values, so a swapped pair of fields cannot pass.
SECONDS = 2.0
STARTED_AT = 5000.02


def fade(start: int = dm7.UNITY, *, by: str | None = "out") -> moves.MoveDescription:
    return moves.MoveDescription(
        seq=1,
        kind=moves.MoveKind.FADE,
        by=by,
        start=start,
        end=dm7.MINUS_INF,
        seconds=SECONDS,
        started_at=STARTED_AT,
        floor=dm7.DEFAULT_FADE_FLOOR,
        taper=None,
    )


def ride(*, by: str | None = "up-slow") -> moves.MoveDescription:
    return moves.MoveDescription(
        seq=2,
        kind=moves.MoveKind.RIDE,
        by=by,
        start=dm7.MINUS_INF,
        end=dm7.UNITY,
        seconds=1.5,
        started_at=STARTED_AT,
        floor=dm7.DEFAULT_FADE_FLOOR,
        taper=dm7.RIDE_IN_TAPER,
    )


class TestAMoveDescription(unittest.TestCase):
    def test_a_fade_is_described_in_db_with_minus_inf_as_null(self):
        data = fade().as_data()
        self.assertEqual(data["from_db"], 0.0)
        self.assertIsNone(data["to_db"])
        self.assertEqual(data["floor_db"], -60.0)
        self.assertIsNone(data["knee"])
        self.assertEqual(data["kind"], "fade")

    def test_a_ride_carries_its_knee(self):
        data = ride().as_data()
        self.assertEqual(data["knee"], {"db": -20.0, "fraction": 0.15})
        self.assertIsNone(data["from_db"])
        self.assertEqual(data["to_db"], 0.0)
        self.assertEqual(data["kind"], "ride")

    def test_the_page_contract_is_exactly_these_keys(self):
        self.assertEqual(set(fade().as_data()), PAGE_CONTRACT)
        self.assertEqual(set(ride().as_data()), PAGE_CONTRACT)

    def test_an_empty_by_is_null(self):
        self.assertIsNone(fade(by="").as_data()["by"])
        self.assertIsNone(fade(by=None).as_data()["by"])
        self.assertEqual(fade(by="out").as_data()["by"], "out")

    def test_the_timing_is_passed_through(self):
        data = fade().as_data()
        self.assertEqual(data["seconds"], SECONDS)
        self.assertEqual(data["started_at"], STARTED_AT)
        self.assertEqual(data["seq"], 1)

    def test_the_description_survives_json(self):
        for description in (fade(), ride()):
            json.dumps(description.as_data(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
