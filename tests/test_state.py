import unittest

from tacet import state as st


def machine(**kwargs):
    return st.Machine(**kwargs)


def armed(**kwargs):
    return st.Machine(state=st.State.IDLE, **kwargs)


def opened(**kwargs):
    return st.Machine(state=st.State.OPEN, **kwargs)


def releasing(**kwargs):
    return st.Machine(state=st.State.RELEASING, **kwargs)


def send(m, command, source=st.Source.OPERATOR, detail=""):
    return st.step(m, st.Event(command, source=source, detail=detail))


class TestBoot(unittest.TestCase):
    def test_boots_standing_down(self):
        # design.md 6.3: pregame, halftime, band not in the stands.
        self.assertEqual(st.Machine().state, st.State.STANDING_DOWN)

    def test_nothing_opens_the_fader_before_arming(self):
        # The safety property: an unarmed box cannot move the fader, whatever
        # it is told.
        for command in st.Command:
            for source in st.Source:
                outcome = send(machine(allow_detector=True), command, source)
                self.assertIsNone(outcome.fader, f"{command}/{source} moved the fader while standing down")

    def test_a_trigger_while_standing_down_is_refused(self):
        outcome = send(machine(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertIsNotNone(outcome.refusal)


class TestArming(unittest.TestCase):
    def test_arming_moves_to_idle(self):
        self.assertEqual(send(machine(), st.Command.ARM).machine.state, st.State.IDLE)

    def test_arming_does_not_touch_the_fader(self):
        # Announce, don't surprise: a mode change is not a fader move.
        self.assertIsNone(send(machine(), st.Command.ARM).fader)

    def test_arming_twice_is_harmless(self):
        outcome = send(armed(), st.Command.ARM)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertFalse(outcome.changed)


class TestOpening(unittest.TestCase):
    def test_a_trigger_opens(self):
        outcome = send(armed(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_trigger_while_open_changes_nothing(self):
        outcome = send(opened(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)


class TestReleasing(unittest.TestCase):
    def test_release_starts_the_fade(self):
        outcome = send(opened(), st.Command.RELEASE)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)

    def test_a_trigger_during_the_fade_snaps_back_to_open(self):
        # design.md 6.3. This is what makes a reactive close survivable.
        outcome = send(releasing(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_the_fade_completing_returns_to_idle(self):
        outcome = send(releasing(), st.Command.FADE_COMPLETE)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)

    def test_releasing_twice_does_not_restart_the_fade(self):
        outcome = send(releasing(), st.Command.RELEASE)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_release_while_idle_is_harmless(self):
        outcome = send(armed(), st.Command.RELEASE)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)


class TestStandingDown(unittest.TestCase):
    def test_standing_down_from_idle_is_immediate(self):
        outcome = send(armed(), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_standing_down_while_open_fades_rather_than_slamming(self):
        outcome = send(opened(), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_pending_stand_down_lands_when_the_fade_finishes(self):
        outcome = send(opened(), st.Command.STAND_DOWN)
        outcome = st.step(outcome.machine, st.Event(st.Command.FADE_COMPLETE))
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_a_trigger_cancels_a_pending_stand_down(self):
        # The band started playing again. Standing down would now be wrong.
        outcome = send(releasing(pending_stand_down=True), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.machine.pending_stand_down)


class TestDetectorGate(unittest.TestCase):
    """CLAUDE.md: the fader does not move autonomously before Phase 2.

    Enforced here rather than by remembering. Phase 0's buttons are
    operator-initiated and are not the same thing.
    """

    def test_a_detector_trigger_is_refused_by_default(self):
        outcome = send(armed(), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)
        self.assertIsNotNone(outcome.refusal)

    def test_the_refusal_says_why(self):
        outcome = send(armed(), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertIn("phase", outcome.refusal.lower())

    def test_no_detector_command_moves_the_fader_by_default(self):
        for command in st.Command:
            for start in (machine(), armed(), opened(), releasing()):
                outcome = send(start, command, st.Source.DETECTOR)
                self.assertIsNone(outcome.fader, f"{command} from {start.state}")

    def test_the_operator_is_never_gated(self):
        outcome = send(armed(), st.Command.TRIGGER, st.Source.OPERATOR)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_detector_trigger_works_once_phase_two_is_declared(self):
        outcome = send(armed(allow_detector=True), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_the_gate_survives_transitions(self):
        m = armed(allow_detector=True)
        m = send(m, st.Command.TRIGGER).machine
        m = send(m, st.Command.RELEASE).machine
        self.assertTrue(m.allow_detector)


class TestAFailedMoveCanBeRetried(unittest.TestCase):
    """A send that fails mid-move leaves the fader short of where the state says.

    Without this, RELEASING swallowed every further RELEASE as "already fading"
    and OPEN swallowed every TRIGGER, so a fade that died at -14 dB could only be
    escaped by slamming the band up to unity first.
    """

    def test_a_failed_fade_marks_the_machine_stalled(self):
        outcome = send(releasing(), st.Command.MOVE_FAILED)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertTrue(outcome.machine.stalled)
        self.assertIsNone(outcome.fader)

    def test_a_failed_open_marks_the_machine_stalled(self):
        outcome = send(opened(), st.Command.MOVE_FAILED)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertTrue(outcome.machine.stalled)

    def test_release_after_a_failed_fade_fades_again(self):
        outcome = send(releasing(stalled=True), st.Command.RELEASE)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertFalse(outcome.machine.stalled)

    def test_trigger_after_a_failed_open_opens_again(self):
        outcome = send(opened(stalled=True), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertFalse(outcome.machine.stalled)

    def test_a_healthy_fade_is_still_not_restarted_by_another_release(self):
        # The panic taps from game 2: six on "out" in seven seconds. Retrying is
        # for a move that failed, never for one still running.
        outcome = send(releasing(stalled=False), st.Command.RELEASE)
        self.assertIsNone(outcome.fader)

    def test_a_retry_keeps_a_pending_stand_down(self):
        outcome = send(releasing(stalled=True, pending_stand_down=True), st.Command.RELEASE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_other_direction_clears_the_stall_too(self):
        # Snapping back open after a failed fade is a new move; the old failure
        # no longer describes the fader.
        self.assertFalse(send(releasing(stalled=True), st.Command.TRIGGER).machine.stalled)
        self.assertFalse(send(opened(stalled=True), st.Command.RELEASE).machine.stalled)

    def test_a_completed_fade_clears_the_stall(self):
        self.assertFalse(send(releasing(stalled=True), st.Command.FADE_COMPLETE).machine.stalled)

    def test_a_failure_while_closed_changes_nothing(self):
        for m in (machine(), armed()):
            outcome = send(m, st.Command.MOVE_FAILED)
            self.assertFalse(outcome.changed)
            self.assertFalse(outcome.machine.stalled)

    def test_the_why_line_says_to_tap_again(self):
        text = st.describe(releasing(stalled=True))
        self.assertIn("did not finish", text)
        self.assertNotEqual(text, st.describe(releasing()))


class TestNeverMutes(unittest.TestCase):
    def test_the_only_fader_commands_are_open_and_fade(self):
        # Faders only, never mutes. There is no mute to reach for.
        self.assertEqual(set(st.FaderCommand), {st.FaderCommand.OPEN, st.FaderCommand.FADE})


class TestWhyLine(unittest.TestCase):
    def test_every_state_has_a_plain_language_description(self):
        for value in st.State:
            text = st.describe(machine(state=value))
            self.assertTrue(text)
            self.assertNotIn("_", text, "should read as prose, not an enum name")

    def test_a_pending_stand_down_is_mentioned(self):
        text = st.describe(releasing(pending_stand_down=True))
        self.assertIn("stand", text.lower())

    def test_the_description_says_when_the_detector_is_live(self):
        self.assertNotEqual(st.describe(armed(allow_detector=True)), st.describe(armed()))


if __name__ == "__main__":
    unittest.main()
