import unittest

from tacet import serve


class TestStopConfirmation(unittest.TestCase):
    """Ctrl-C is two presses, deliberately.

    It used to be two by accident: the interrupt arrived inside
    `runner.cleanup()`, which waits on websocket handlers, so any connected
    browser made that the common case. It took the rest of the shutdown with it
    - neither the log nor the queue was closed - and printed a page of traceback
    at whoever was standing there. Nothing was lost only because both fsync per
    line, which is an earlier decision covering for this one.
    """

    def test_the_first_press_does_not_stop(self):
        self.assertFalse(serve.confirms_stop(100.0, None))

    def test_a_second_press_inside_the_window_does(self):
        self.assertTrue(serve.confirms_stop(102.0, 100.0))

    def test_the_boundary_confirms(self):
        self.assertTrue(serve.confirms_stop(100.0 + serve.STOP_CONFIRM_SECONDS, 100.0))

    def test_a_second_press_after_the_window_does_not(self):
        self.assertFalse(serve.confirms_stop(100.1 + serve.STOP_CONFIRM_SECONDS, 100.0))

    def test_two_presses_a_game_apart_are_not_a_pair(self):
        # The reason the window exists at all. A confirmation that never expires
        # would let a stray Ctrl-C in the first quarter combine with an
        # unrelated one in the fourth and end the capture.
        self.assertFalse(serve.confirms_stop(100.0 + 3 * 3600, 100.0))


class TestStopWarning(unittest.TestCase):
    def test_it_says_the_recording_keeps_going(self):
        # The expensive assumption. Each home game is a single irreplaceable
        # sample, and stopping the box looks like stopping everything.
        self.assertIn("does not stop the recording", serve.STOP_WARNING)

    def test_it_says_the_fader_does_not_move(self):
        # The other one. Nothing fades on the way out; the console keeps its
        # last commanded level and the operator has the iPad.
        self.assertIn("does not move the fader", serve.STOP_WARNING)

    def test_it_names_the_window_it_is_describing(self):
        self.assertIn(f"{serve.STOP_CONFIRM_SECONDS:.0f}s", serve.STOP_WARNING)
