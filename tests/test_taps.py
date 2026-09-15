import math
import unittest

from tacet import annotations, taps


class TestReadingATap(unittest.TestCase):
    def test_no_stamp_is_not_an_error(self):
        # A page cached from before #11, or curl.
        self.assertIsNone(taps.read_tap({}))
        self.assertIsNone(taps.read_tap({"tap": None}))
        self.assertIsNone(taps.read_tap(None))

    def test_a_stamp_with_an_estimate(self):
        tap = taps.read_tap({"tap": {"at": 1_757_700_000.5, "offset": 1_757_690_000.0, "uncertainty": 0.02}})
        self.assertEqual(tap, taps.Tap(at=1_757_700_000.5, offset=1_757_690_000.0, uncertainty=0.02))
        assert tap is not None
        self.assertTrue(tap.estimated)

    def test_a_stamp_without_an_estimate_yet(self):
        tap = taps.read_tap({"tap": {"at": 12.0, "offset": None, "uncertainty": None}})
        self.assertEqual(tap, taps.Tap(at=12.0))
        assert tap is not None
        self.assertFalse(tap.estimated)

    def test_a_malformed_stamp_is_refused(self):
        cases = {
            "not an object": {"tap": 3},
            "no time": {"tap": {}},
            "a string time": {"tap": {"at": "12"}},
            "a boolean time": {"tap": {"at": True}},
            "an infinite time": {"tap": {"at": math.inf}},
            "an offset alone": {"tap": {"at": 1.0, "offset": 2.0}},
            "an uncertainty alone": {"tap": {"at": 1.0, "uncertainty": 2.0}},
            "a negative uncertainty": {"tap": {"at": 1.0, "offset": 0.0, "uncertainty": -0.1}},
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(taps.TapError):
                taps.read_tap(body)

    def test_a_tap_error_is_an_annotation_error(self):
        # So the routes that already turn those into a 400 refuse it the same way.
        self.assertTrue(issubclass(taps.TapError, annotations.AnnotationError))


class TestPlacingATapOnTheBoxClock(unittest.TestCase):
    def test_the_offset_moves_the_page_clock_onto_the_box_clock(self):
        # Page clock 1000 s ahead of box monotonic; tapped at page 1500, so box
        # 500; received at box 503.
        timing = taps.timing(taps.Tap(at=1500.0, offset=1000.0, uncertainty=0.05), received=503.0)
        self.assertEqual(timing, taps.TapTiming(received=503.0, tapped=500.0, delay=3.0, uncertainty=0.05))

    def test_no_estimate_is_stamped_as_received_and_says_no_more(self):
        timing = taps.timing(taps.Tap(at=1500.0), received=503.0)
        self.assertEqual(timing, taps.TapTiming(received=503.0))

    def test_no_tap_at_all_is_stamped_as_received(self):
        self.assertEqual(taps.timing(None, received=7.0), taps.TapTiming(received=7.0))

    def test_a_small_negative_delay_is_kept_not_hidden(self):
        timing = taps.timing(taps.Tap(at=1000.01, offset=1000.0, uncertainty=0.02), received=0.0)
        assert timing.delay is not None and timing.uncertainty is not None
        self.assertLess(timing.delay, 0)
        self.assertLess(abs(timing.delay), timing.uncertainty)

    def test_as_data_is_what_the_log_carries(self):
        data = taps.TapTiming(received=5.0, tapped=4.0, delay=1.0, uncertainty=0.1).as_data()
        self.assertEqual(data, {"tapped": 4.0, "received": 5.0, "delay": 1.0, "uncertainty": 0.1})


class TestReadingADelayBack(unittest.TestCase):
    def test_a_logged_delay(self):
        self.assertEqual(taps.delay_of({"tap": {"delay": 2.5}}), 2.5)

    def test_anything_else_is_no_delay(self):
        for data in ({}, {"tap": None}, {"tap": {"delay": None}}, {"tap": {"delay": "2"}}, {"tap": 3}, None):
            with self.subTest(data):
                self.assertIsNone(taps.delay_of(data))


if __name__ == "__main__":
    unittest.main()
