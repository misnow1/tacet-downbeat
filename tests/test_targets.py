"""#9: the standing target level, and the rule that caps it.

Pure functions over console units, so no socket, no clock and no app. The cap is
the safety half of the feature: +3 dB costs 3 dB of feedback margin against a
band PA that sits just behind the mics, and the number is set by an on-site
ring-out rather than guessed. It ships at unity until that has been done.
"""

import unittest

from tacet import dm7, targets


class TestTheShippedDefaults(unittest.TestCase):
    def test_the_presets_are_unity_then_two_quieter_steps(self):
        self.assertEqual(targets.DEFAULT_PRESETS_DB, (0.0, -3.0, -6.0))

    def test_the_cap_ships_at_unity_until_the_ring_out_has_been_done(self):
        self.assertEqual(targets.DEFAULT_MAX_TARGET_DB, 0.0)

    def test_plus_three_cannot_exist_by_accident(self):
        with self.assertRaises(targets.TargetError):
            targets.build((3.0, 0.0, -3.0, -6.0), targets.DEFAULT_MAX_TARGET_DB)

    def test_the_default_targets_are_the_shipped_presets_at_the_shipped_cap(self):
        self.assertEqual(
            targets.DEFAULT_TARGETS, targets.build(targets.DEFAULT_PRESETS_DB, targets.DEFAULT_MAX_TARGET_DB)
        )
        self.assertEqual(targets.DEFAULT_TARGETS.levels, (0, -300, -600))
        self.assertEqual(targets.DEFAULT_TARGETS.max_level, dm7.UNITY)


class TestBuild(unittest.TestCase):
    def test_a_preset_above_the_cap_raises_and_the_message_names_it(self):
        with self.assertRaises(targets.TargetError) as caught:
            targets.build((0.0, 1.5), 0.0)
        self.assertIn("1.5", str(caught.exception))

    def test_a_preset_equal_to_the_cap_is_allowed(self):
        built = targets.build((0.0, -3.0), 0.0)
        self.assertEqual(built.levels, (0, -300))

    def test_a_higher_cap_admits_a_higher_preset(self):
        built = targets.build((3.0, 0.0, -3.0), 3.0)
        self.assertEqual(built.levels, (300, 0, -300))

    def test_the_default_is_the_first_entry_not_the_loudest_and_not_the_cap(self):
        built = targets.build((-5.0, -2.0, -8.0), 3.0)
        self.assertEqual(built.default, -500)
        self.assertNotEqual(built.default, max(built.levels))
        self.assertNotEqual(built.default, built.max_level)

    def test_the_first_entry_is_the_default_even_when_it_is_the_quietest(self):
        self.assertEqual(targets.build((-6.0, 0.0), 0.0).default, -600)

    def test_a_duplicate_raises_and_the_message_names_it(self):
        with self.assertRaises(targets.TargetError) as caught:
            targets.build((0.0, -3.0, -3.0), 0.0)
        self.assertIn("-3.0", str(caught.exception))

    def test_two_spellings_of_one_console_level_are_a_duplicate(self):
        # Compared in console units, whose resolution is 0.01 dB: these are one
        # segment on the page and one value on the wire.
        with self.assertRaises(targets.TargetError):
            targets.build((-3.0, -3.001), 0.0)

    def test_an_empty_list_raises(self):
        with self.assertRaises(targets.TargetError):
            targets.build((), 0.0)

    def test_a_non_finite_preset_raises_rather_than_overflowing(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaises(targets.TargetError):
                targets.build((0.0, bad), 10.0)

    def test_a_preset_below_the_console_floor_raises(self):
        with self.assertRaises(targets.TargetError):
            targets.build((0.0, -400.0), 0.0)

    def test_a_cap_above_the_console_ceiling_raises(self):
        with self.assertRaises(targets.TargetError):
            targets.build((0.0,), 11.0)

    def test_build_is_pure(self):
        presets = [0.0, -3.0]
        first = targets.build(presets, 0.0)
        second = targets.build(presets, 0.0)
        self.assertEqual(first, second)
        self.assertEqual(presets, [0.0, -3.0])

    def test_the_levels_are_a_tuple_in_page_order(self):
        self.assertEqual(targets.build((-6.0, 0.0, -3.0), 0.0).levels, (-600, 0, -300))


class TestLevelFor(unittest.TestCase):
    def test_minus_three_db_is_exactly_minus_three_hundred(self):
        self.assertEqual(targets.level_for(-3.0), -300)

    def test_it_rounds_to_the_console_resolution(self):
        self.assertEqual(targets.level_for(-2.5), -250)
        self.assertEqual(targets.level_for(-3.004), -300)

    def test_it_is_derived_from_the_console_scaling(self):
        self.assertEqual(targets.level_for(1.0), dm7.UNITS_PER_DB)


class TestTheTargetsObject(unittest.TestCase):
    def setUp(self):
        self.built = targets.build((0.0, -3.0, -6.0), 0.0)

    def test_it_allows_exactly_the_presets(self):
        self.assertTrue(self.built.allows(-300))
        self.assertFalse(self.built.allows(-250))
        self.assertFalse(self.built.allows(300))
        self.assertFalse(self.built.allows(dm7.MINUS_INF))

    def test_it_reports_its_levels_in_db_in_page_order(self):
        self.assertEqual(self.built.db_values(), (0.0, -3.0, -6.0))

    def test_it_is_immutable(self):
        with self.assertRaises(AttributeError):
            self.built.max_level = 300  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
