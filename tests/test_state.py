import unittest

from tacet import state as st


def machine(**kwargs):
    return st.Machine(**kwargs)


def armed(**kwargs):
    return st.Machine(state=st.State.IDLE, **kwargs)


def readying(**kwargs):
    return st.Machine(state=st.State.READY, **kwargs)


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

    def test_nothing_the_detector_says_moves_the_fader_before_arming(self):
        # The safety property, which is about the detector and not about the
        # operator (#89): a box that is standing down is not on duty, and the
        # detector cannot put it on duty.
        for command in st.Command:
            outcome = send(machine(allow_detector=True), command, st.Source.DETECTOR)
            self.assertIsNone(outcome.fader, f"{command} let the detector move the fader while standing down")
            self.assertIsNotNone(outcome.refusal, f"{command} was not refused")


class TestTheOperatorDrivesWhileStandingDown(unittest.TestCase):
    """#89: standing down used to refuse every operator command but ARM, so a
    forgotten Arm was a missed downbeat. Principle 5: the operator is a
    supervisor, not a fallback, and keeps override authority permanently."""

    def test_an_open_arms_and_opens(self):
        outcome = send(machine(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertIsNone(outcome.refusal)

    def test_the_why_line_says_the_open_armed_it(self):
        outcome = send(machine(), st.Command.TRIGGER)
        self.assertTrue(outcome.machine.armed_by_operator)
        self.assertIn("armed", st.describe(outcome.machine).lower())

    def test_a_ride_in_arms_and_opens_too(self):
        outcome = st.step(machine(level_known=True), st.Event(st.Command.TRIGGER, gradual=True))
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertTrue(outcome.machine.riding_in)

    def test_a_ready_arms_and_readies_too(self):
        # #6: a forgotten Arm must not cost a heads-up either.
        outcome = send(machine(level_known=True), st.Command.READY)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertEqual(outcome.fader, st.FaderCommand.READY)
        self.assertTrue(outcome.machine.armed_by_operator)
        self.assertIsNone(outcome.refusal)

    def test_an_ordinary_open_is_not_marked_as_arming_anything(self):
        outcome = send(armed(), st.Command.TRIGGER)
        self.assertFalse(outcome.machine.armed_by_operator)
        self.assertNotIn("armed by", st.describe(outcome.machine).lower())

    def test_the_mark_is_gone_once_the_fader_leaves(self):
        opened_by_tap = send(machine(), st.Command.TRIGGER).machine
        self.assertFalse(send(opened_by_tap, st.Command.RELEASE).machine.armed_by_operator)

    def test_a_close_moves_the_fader_and_changes_nothing(self):
        # A close says nothing about whether the band is in the stands, so the
        # box neither arms nor re-stands-down around the fade.
        outcome = send(machine(level_known=True), st.Command.RELEASE)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertEqual(outcome.machine, machine(level_known=True))

    def test_a_detector_trigger_is_still_refused(self):
        outcome = send(machine(allow_detector=True), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertIsNone(outcome.fader)
        self.assertIn("standing down", outcome.refusal)

    def test_arming_and_standing_down_still_work(self):
        self.assertEqual(send(machine(level_known=True), st.Command.ARM).machine.state, st.State.IDLE)
        self.assertFalse(send(machine(), st.Command.STAND_DOWN).changed)

    def test_the_bookkeeping_commands_change_nothing(self):
        for command in (st.Command.FADE_COMPLETE, st.Command.RIDE_IN_COMPLETE):
            with self.subTest(command):
                outcome = send(machine(), command)
                self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
                self.assertIsNone(outcome.fader)


class TestArming(unittest.TestCase):
    def test_arming_moves_to_idle(self):
        self.assertEqual(send(machine(level_known=True), st.Command.ARM).machine.state, st.State.IDLE)

    def test_arming_does_not_touch_the_fader(self):
        # Announce, don't surprise: a mode change is not a fader move.
        self.assertIsNone(send(machine(level_known=True), st.Command.ARM).fader)

    def test_arming_twice_is_harmless(self):
        outcome = send(armed(level_known=True), st.Command.ARM)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertFalse(outcome.changed)


class TestOpening(unittest.TestCase):
    def test_a_trigger_opens(self):
        outcome = send(armed(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_trigger_while_open_changes_nothing(self):
        outcome = send(opened(level_known=True), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)


class TestReleasing(unittest.TestCase):
    def test_release_starts_the_fade(self):
        outcome = send(opened(level_known=True), st.Command.RELEASE)
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
        outcome = send(releasing(level_known=True), st.Command.RELEASE)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_release_while_idle_is_harmless(self):
        outcome = send(armed(level_known=True), st.Command.RELEASE)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)


class TestStandingDown(unittest.TestCase):
    def test_standing_down_from_idle_is_immediate(self):
        outcome = send(armed(), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_standing_down_while_open_fades_rather_than_slamming(self):
        outcome = send(opened(level_known=True), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_pending_stand_down_lands_when_the_fade_finishes(self):
        outcome = send(opened(level_known=True), st.Command.STAND_DOWN)
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
            for start in (machine(), armed(), readying(), opened(), releasing()):
                outcome = send(start, command, st.Source.DETECTOR)
                self.assertIsNone(outcome.fader, f"{command} from {start.state}")

    def test_the_operator_is_never_gated(self):
        outcome = send(armed(), st.Command.TRIGGER, st.Source.OPERATOR)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_detector_trigger_works_once_phase_two_is_declared(self):
        outcome = send(armed(allow_detector=True), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_unlike_trigger_ready_stays_refused_once_phase_two_is_declared(self):
        # #6: READY is entered on a prediction, which only a watching human
        # can make. Confirming sound is already present is what TRIGGER is for
        # once Phase 2 is declared; guessing sound is about to start never
        # becomes the detector's job.
        outcome = send(armed(allow_detector=True), st.Command.READY, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)
        self.assertIn("operator-only", outcome.refusal)

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
        outcome = send(releasing(stalled=True, level_known=True), st.Command.RELEASE)
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
        outcome = send(releasing(stalled=False, level_known=True), st.Command.RELEASE)
        self.assertIsNone(outcome.fader)

    def test_a_retry_keeps_a_pending_stand_down(self):
        outcome = send(releasing(stalled=True, pending_stand_down=True, level_known=True), st.Command.RELEASE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_other_direction_clears_the_stall_too(self):
        # Snapping back open after a failed fade is a new move; the old failure
        # no longer describes the fader.
        self.assertFalse(send(releasing(stalled=True), st.Command.TRIGGER).machine.stalled)
        self.assertFalse(send(opened(stalled=True, level_known=True), st.Command.RELEASE).machine.stalled)

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


def ride_in(m):
    """A trigger that opens gradually: `up-slow`."""
    return st.step(m, st.Event(st.Command.TRIGGER, gradual=True))


class TestAFastOpenSnapsDuringARideIn(unittest.TestCase):
    """#45, decided: snap.

    A ride-in exists to have the fader up before the band comes in. The whistle
    or the drums say the band is coming in now, so the rest of the ride-in has
    nothing left to do and the downbeat wins.
    """

    def test_a_ride_in_opens_and_is_marked_in_flight(self):
        outcome = ride_in(armed(level_known=True))
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertTrue(outcome.machine.riding_in)

    def test_a_snap_open_is_not_a_ride_in(self):
        self.assertFalse(send(armed(), st.Command.TRIGGER).machine.riding_in)

    def test_a_fast_trigger_during_a_ride_in_snaps(self):
        outcome = send(opened(riding_in=True, level_known=True), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.machine.riding_in)

    def test_a_slow_trigger_during_a_ride_in_does_not_restart_it(self):
        outcome = ride_in(opened(riding_in=True, level_known=True))
        self.assertIsNone(outcome.fader)
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.machine.riding_in)

    def test_once_the_ride_in_lands_a_trigger_changes_nothing(self):
        landed = send(opened(riding_in=True, level_known=True), st.Command.RIDE_IN_COMPLETE)
        self.assertEqual(landed.machine.state, st.State.OPEN)
        self.assertFalse(landed.machine.riding_in)
        self.assertIsNone(landed.fader)
        self.assertIsNone(send(landed.machine, st.Command.TRIGGER).fader)

    def test_a_ride_in_landing_anywhere_else_changes_nothing(self):
        # A late report from a ride-in that was already replaced.
        for m in (machine(), armed(), readying(), opened(), releasing()):
            with self.subTest(state=m.state):
                outcome = send(m, st.Command.RIDE_IN_COMPLETE)
                self.assertFalse(outcome.changed)
                self.assertIsNone(outcome.fader)

    def test_a_close_ends_the_ride_in(self):
        for command in (st.Command.RELEASE, st.Command.STAND_DOWN):
            with self.subTest(command=command):
                outcome = send(opened(riding_in=True, level_known=True), command)
                self.assertEqual(outcome.fader, st.FaderCommand.FADE)
                self.assertFalse(outcome.machine.riding_in)

    def test_a_failed_ride_in_is_no_longer_in_flight(self):
        outcome = send(opened(riding_in=True), st.Command.MOVE_FAILED)
        self.assertTrue(outcome.machine.stalled)
        self.assertFalse(outcome.machine.riding_in)

    def test_retrying_a_failed_open_takes_the_speed_of_the_retry(self):
        self.assertTrue(ride_in(opened(stalled=True, level_known=True)).machine.riding_in)
        self.assertFalse(
            send(opened(stalled=True, riding_in=True, level_known=True), st.Command.TRIGGER).machine.riding_in
        )

    def test_a_snap_back_from_a_fade_can_itself_ride_in(self):
        self.assertTrue(ride_in(releasing(level_known=True)).machine.riding_in)
        self.assertFalse(send(releasing(), st.Command.TRIGGER).machine.riding_in)

    def test_the_detector_gate_still_comes_first(self):
        outcome = send(opened(riding_in=True), st.Command.TRIGGER, source=st.Source.DETECTOR)
        self.assertIsNone(outcome.fader)
        self.assertTrue(outcome.machine.riding_in)


class TestReady(unittest.TestCase):
    """#6: ready, then go. A hold level short of target, waiting to see
    whether the band starts - not a ride to the open level."""

    def test_ready_from_idle_rides_to_the_hold_level(self):
        outcome = send(armed(level_known=True), st.Command.READY)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertEqual(outcome.fader, st.FaderCommand.READY)
        self.assertTrue(outcome.machine.riding_in)

    def test_any_trigger_commits_to_open_fast(self):
        outcome = send(readying(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_trigger_commits_whether_or_not_the_ride_has_landed(self):
        # Unlike OPEN's own ride-in, READY does not distinguish mid-ride from
        # settled: any trigger takes it the rest of the way either way.
        mid_ride = send(readying(), st.Command.TRIGGER)
        landed = send(readying(riding_in=False), st.Command.TRIGGER)
        self.assertEqual(mid_ride.machine.state, st.State.OPEN)
        self.assertEqual(landed.machine.state, st.State.OPEN)

    def test_score_reversed_fades_like_an_ordinary_close(self):
        outcome = send(readying(level_known=True), st.Command.RELEASE)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_a_stand_down_fades_rather_than_slamming(self):
        # Same caution as a stand-down from OPEN: the box cannot be sure the
        # band has not quietly started under the hold level.
        outcome = send(readying(level_known=True), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_pending_stand_down_from_ready_lands_standing_down(self):
        outcome = send(readying(level_known=True), st.Command.STAND_DOWN)
        outcome = st.step(outcome.machine, st.Event(st.Command.FADE_COMPLETE))
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_a_failed_ride_marks_the_machine_stalled(self):
        outcome = send(readying(), st.Command.MOVE_FAILED)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertTrue(outcome.machine.stalled)
        self.assertFalse(outcome.machine.riding_in)
        self.assertIsNone(outcome.fader)

    def test_ready_after_a_failed_ride_retries_it(self):
        outcome = send(readying(stalled=True, level_known=True), st.Command.READY)
        self.assertEqual(outcome.fader, st.FaderCommand.READY)
        self.assertFalse(outcome.machine.stalled)

    def test_a_healthy_ride_is_not_restarted_by_another_ready(self):
        outcome = send(readying(stalled=False, level_known=True), st.Command.READY)
        self.assertIsNone(outcome.fader)
        self.assertFalse(outcome.changed)

    def test_the_ride_landing_is_bookkeeping_only(self):
        landed = send(readying(riding_in=True), st.Command.RIDE_IN_COMPLETE)
        self.assertEqual(landed.machine.state, st.State.READY)
        self.assertFalse(landed.machine.riding_in)
        self.assertIsNone(landed.fader)

    def test_a_detector_ready_is_refused(self):
        outcome = send(armed(allow_detector=True), st.Command.READY, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.fader)
        self.assertIsNotNone(outcome.refusal)

    def test_the_why_line_names_the_state(self):
        text = st.describe(readying())
        self.assertIn("hold level", text.lower())


ALL_STATES = (st.State.STANDING_DOWN, st.State.IDLE, st.State.READY, st.State.OPEN, st.State.RELEASING)

#: What may leave the box while the level is unknown: only commands that are
#: correct from any belief (#107). Everything else ramps from a number that may
#: be fiction.
ABSOLUTE_FADER_COMMANDS = {
    st.FaderCommand.OPEN,
    st.FaderCommand.CLOSE_NOW,
    st.FaderCommand.REPORT_READY,
}


def unknown(state, **kwargs):
    return st.Machine(state=state, level_known=False, **kwargs)


class TestTheLevelIsNotKnownUntilSomethingAbsoluteSaysSo(unittest.TestCase):
    """#107: the box's belief about the fader is exactly as untrustworthy at
    cold boot (no feedback exists, #18) as after handing the DCA to StageMix.
    Both are the same fact: it does not know where the fader is."""

    def test_a_fresh_machine_does_not_know(self):
        self.assertFalse(st.Machine().level_known)

    def test_a_snap_open_marks_the_level_known_from_any_start(self):
        for value in ALL_STATES:
            with self.subTest(state=value):
                outcome = send(unknown(value), st.Command.TRIGGER)
                self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
                self.assertTrue(outcome.machine.level_known)

    def test_an_instant_close_marks_the_level_known_from_any_start(self):
        for value in ALL_STATES:
            with self.subTest(state=value):
                outcome = send(unknown(value), st.Command.CLOSE_NOW)
                self.assertEqual(outcome.fader, st.FaderCommand.CLOSE_NOW)
                self.assertTrue(outcome.machine.level_known)

    def test_an_instant_close_lands_closed(self):
        # The state must match what was commanded: fail visible.
        expected = {
            st.State.OPEN: st.State.IDLE,
            st.State.READY: st.State.IDLE,
            st.State.IDLE: st.State.IDLE,
            st.State.STANDING_DOWN: st.State.STANDING_DOWN,
        }
        for start, landing in expected.items():
            with self.subTest(state=start):
                self.assertEqual(send(unknown(start), st.Command.CLOSE_NOW).machine.state, landing)

    def test_an_instant_close_honours_a_pending_stand_down(self):
        outcome = send(unknown(st.State.RELEASING, pending_stand_down=True), st.Command.CLOSE_NOW)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_an_instant_close_from_releasing_lands_idle(self):
        outcome = send(unknown(st.State.RELEASING), st.Command.CLOSE_NOW)
        self.assertEqual(outcome.machine.state, st.State.IDLE)

    def test_an_instant_close_clears_everything_that_described_a_move(self):
        start = st.Machine(
            state=st.State.OPEN,
            level_known=True,
            stalled=True,
            riding_in=True,
            armed_by_operator=True,
        )
        after = send(start, st.Command.CLOSE_NOW).machine
        self.assertFalse(after.stalled)
        self.assertFalse(after.riding_in)
        self.assertFalse(after.armed_by_operator)

    def test_an_instant_close_always_sends_its_packet(self):
        # Gated by nothing: not the state, not the belief, not a stall, not a
        # ride, not a pending stand-down. It is the one move always available.
        for value in ALL_STATES:
            for known in (True, False):
                for flags in ({}, {"stalled": True}, {"riding_in": True}, {"pending_stand_down": True}):
                    with self.subTest(state=value, known=known, flags=flags):
                        start = st.Machine(state=value, level_known=known, **flags)
                        outcome = send(start, st.Command.CLOSE_NOW)
                        self.assertEqual(outcome.fader, st.FaderCommand.CLOSE_NOW)
                        self.assertIsNone(outcome.refusal)

    def test_an_instant_close_is_never_the_two_second_fade(self):
        # The fade ramps from a number that may be fiction, which is the one
        # thing this whole feature exists to prevent.
        self.assertNotEqual(send(unknown(st.State.OPEN), st.Command.CLOSE_NOW).fader, st.FaderCommand.FADE)

    def test_a_detector_cannot_close_now_before_phase_two(self):
        # The same gate as every other command; see TestDetectorGate.
        for value in ALL_STATES:
            with self.subTest(state=value):
                outcome = send(unknown(value), st.Command.CLOSE_NOW, st.Source.DETECTOR)
                self.assertIsNone(outcome.fader)
                self.assertIsNotNone(outcome.refusal)

    def test_the_detector_cannot_slam_the_fader_shut(self):
        # CLAUDE.md principle 3: the machine cannot see the arms come down, so
        # every close is reactive and the 2 s fade is what makes that
        # survivable. The instant close is the operator's; a detector that
        # reached it, once Phase 2 is declared, would cut the band on a
        # misread. Refused with its own reason, not READY's.
        for value in (st.State.IDLE, st.State.READY, st.State.OPEN, st.State.RELEASING):
            for known in (True, False):
                with self.subTest(state=value, known=known):
                    start = st.Machine(state=value, level_known=known, allow_detector=True)
                    outcome = send(start, st.Command.CLOSE_NOW, st.Source.DETECTOR)
                    self.assertIsNone(outcome.fader)
                    self.assertFalse(outcome.changed)
                    self.assertEqual(outcome.refusal, st.DETECTOR_CLOSE_IS_THE_FADE)
                    self.assertNotIn("READY", outcome.refusal)

    def test_the_only_fader_commands_a_detector_can_ever_cause_are_open_and_fade(self):
        # The constraint, asserted across every command and state once Phase 2
        # is declared. HANDOFF from a detector can still flip `level_known`
        # (a hole from #12, deliberately not fixed here), but it sends nothing,
        # so it does not trip this.
        allowed = {None, st.FaderCommand.OPEN, st.FaderCommand.FADE}
        for command in st.Command:
            for gradual in (False, True):
                for value in ALL_STATES:
                    for known in (True, False):
                        with self.subTest(command=command, gradual=gradual, state=value, known=known):
                            start = st.Machine(state=value, level_known=known, allow_detector=True)
                            outcome = st.step(start, st.Event(command, source=st.Source.DETECTOR, gradual=gradual))
                            self.assertIn(outcome.fader, allowed)

    def test_a_snap_open_while_open_and_unknown_still_sends_and_marks_known(self):
        # "Already open" is only a belief while the level is unknown, and the
        # snap is the one way to make it true - this is how the operator says
        # "it is at target" from OPEN, where REPORT_READY would say READY.
        outcome = send(unknown(st.State.OPEN), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertTrue(outcome.machine.level_known)
        self.assertTrue(outcome.changed)

    def test_a_trigger_while_open_and_known_still_only_confirms(self):
        self.assertIsNone(send(opened(level_known=True), st.Command.TRIGGER).fader)

    def test_handoff_makes_the_level_unknown(self):
        outcome = send(opened(level_known=True), st.Command.HANDOFF)
        self.assertFalse(outcome.machine.level_known)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.machine.state, st.State.OPEN)

    def test_handoff_while_already_unknown_changes_nothing(self):
        outcome = send(opened(level_known=False), st.Command.HANDOFF)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_handoff_is_legal_from_standing_down(self):
        # The operator drives in every state (#89) - handing off is about the
        # console, not the band, so it is no different.
        outcome = send(machine(level_known=True), st.Command.HANDOFF)
        self.assertFalse(outcome.machine.level_known)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_a_detector_cannot_hand_off_while_standing_down(self):
        outcome = send(machine(allow_detector=True, level_known=True), st.Command.HANDOFF, st.Source.DETECTOR)
        self.assertTrue(outcome.machine.level_known)
        self.assertIn("standing down", outcome.refusal)

    def test_a_detector_cannot_hand_off_before_phase_two(self):
        outcome = send(opened(level_known=True), st.Command.HANDOFF, st.Source.DETECTOR)
        self.assertTrue(outcome.machine.level_known)
        self.assertIsNotNone(outcome.refusal)

    def test_handing_off_mid_fade_presumes_it_landed(self):
        # #12: the in-flight fade is cancelled by the shell the moment
        # StageMix might touch the fader too, so `state` has to stop claiming
        # to be fading, or it would sit in RELEASING forever - nothing will
        # ever fire FADE_COMPLETE for a fade that never ran.
        outcome = send(releasing(level_known=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertFalse(outcome.machine.level_known)

    def test_handing_off_mid_fade_honours_a_pending_stand_down(self):
        outcome = send(releasing(level_known=True, pending_stand_down=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_handing_off_mid_ride_in_presumes_it_arrived(self):
        outcome = send(opened(level_known=True, riding_in=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.machine.riding_in)

    def test_handing_off_mid_ready_ride_presumes_it_arrived_too(self):
        outcome = send(readying(level_known=True, riding_in=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertFalse(outcome.machine.riding_in)

    def test_the_relative_moves_do_not_make_a_level_known(self):
        # A fade ramps from the belief; it cannot make the belief true.
        self.assertFalse(send(unknown(st.State.OPEN), st.Command.FADE_COMPLETE).machine.level_known)
        self.assertFalse(send(unknown(st.State.OPEN), st.Command.MOVE_FAILED).machine.level_known)
        self.assertFalse(send(unknown(st.State.OPEN, riding_in=True), st.Command.RIDE_IN_COMPLETE).machine.level_known)
        self.assertFalse(send(unknown(st.State.OPEN), st.Command.STAND_DOWN).machine.level_known)

    def test_a_relative_move_keeps_a_known_level_known(self):
        for command in (st.Command.RELEASE, st.Command.READY, st.Command.STAND_DOWN, st.Command.ARM):
            with self.subTest(command=command):
                self.assertTrue(send(armed(level_known=True), command).machine.level_known)

    def test_an_absolute_command_that_delivered_nothing_still_marks_the_level_known(self):
        # A known residual, pinned rather than fixed (#107, follow-up filed
        # separately): `step` is pure and cannot know whether the packet went
        # out. The shell reports a failed send as MOVE_FAILED, which sets
        # `stalled`, and that is loud - but it does not un-know the level.
        opened_machine = send(unknown(st.State.IDLE), st.Command.TRIGGER).machine
        after_failure = send(opened_machine, st.Command.MOVE_FAILED).machine
        self.assertTrue(after_failure.stalled)
        self.assertTrue(after_failure.level_known)


class TestArmingNeedsAKnownLevel(unittest.TestCase):
    """Bare ARM is a pure mode change that sends nothing, on the unstated
    assumption that IDLE means closed. That is true only if the box knows where
    the fader is (#107)."""

    def test_a_bare_arm_is_refused_while_the_level_is_unknown(self):
        outcome = send(machine(), st.Command.ARM)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.refusal, st.UNKNOWN_LEVEL_ARM)

    def test_the_refusal_names_the_fix(self):
        self.assertIn("close it now", st.UNKNOWN_LEVEL_ARM)

    def test_arm_works_once_the_level_is_known(self):
        outcome = send(machine(level_known=True), st.Command.ARM)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertIsNone(outcome.refusal)

    def test_the_cold_boot_ritual_is_close_now_then_arm(self):
        closed = send(machine(), st.Command.CLOSE_NOW).machine
        self.assertEqual(closed.state, st.State.STANDING_DOWN)
        self.assertEqual(send(closed, st.Command.ARM).machine.state, st.State.IDLE)

    def test_an_open_still_arms_the_box_while_the_level_is_unknown(self):
        # The #89 pin: STANDING DOWN never blocks the operator, and a forgotten
        # Arm must not cost a downbeat. An open is absolute, so it passes.
        outcome = send(machine(), st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertTrue(outcome.machine.armed_by_operator)
        self.assertTrue(outcome.machine.level_known)
        self.assertIsNone(outcome.refusal)

    def test_a_ready_report_still_arms_the_box_while_the_level_is_unknown(self):
        # The #6 pin: a forgotten Arm must not cost a heads-up either.
        outcome = send(machine(), st.Command.REPORT_READY)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertTrue(outcome.machine.armed_by_operator)
        self.assertIsNone(outcome.refusal)


class TestRelativeMovesNeedAKnownLevel(unittest.TestCase):
    """#107: RELEASE, READY and a gradual TRIGGER ramp from `commanded_level`.
    While the box does not know where the fader is, they are refused - not
    queued: there is no fader intention worth holding onto, and the machine is
    the only place every caller (routes, verify_dm7, a cached page, the Phase 2
    detector) passes through."""

    def test_a_fade_is_refused_not_queued(self):
        outcome = send(unknown(st.State.OPEN), st.Command.RELEASE)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.refusal, st.UNKNOWN_LEVEL_MOVE)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.machine, unknown(st.State.OPEN))

    def test_a_ready_ride_is_refused(self):
        for value in (st.State.IDLE, st.State.STANDING_DOWN):
            with self.subTest(state=value):
                outcome = send(unknown(value), st.Command.READY)
                self.assertIsNone(outcome.fader)
                self.assertEqual(outcome.refusal, st.UNKNOWN_LEVEL_MOVE)
                self.assertEqual(outcome.machine.state, value)

    def test_a_gradual_trigger_is_refused(self):
        for value in (st.State.IDLE, st.State.STANDING_DOWN, st.State.RELEASING):
            with self.subTest(state=value):
                outcome = ride_in(unknown(value))
                self.assertIsNone(outcome.fader)
                self.assertEqual(outcome.refusal, st.UNKNOWN_LEVEL_MOVE)
                self.assertEqual(outcome.machine.state, value)

    def test_the_refusal_names_the_fix(self):
        self.assertIn("close it now", st.UNKNOWN_LEVEL_MOVE)

    def test_a_snap_trigger_passes_straight_through(self):
        # "Snap opens skip the question" - correct from any start.
        outcome = send(unknown(st.State.IDLE), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)

    def test_the_same_moves_pass_once_the_level_is_known(self):
        self.assertEqual(send(opened(level_known=True), st.Command.RELEASE).fader, st.FaderCommand.FADE)
        self.assertEqual(send(armed(level_known=True), st.Command.READY).fader, st.FaderCommand.READY)
        self.assertEqual(ride_in(armed(level_known=True)).fader, st.FaderCommand.OPEN)

    def test_stand_down_is_never_refused_while_unknown(self):
        # Changes state only and sends nothing: there is no real fader move
        # for RELEASING to be waiting on, so it goes straight to STANDING_DOWN.
        for start in (st.State.IDLE, st.State.READY, st.State.OPEN, st.State.RELEASING):
            with self.subTest(state=start):
                outcome = send(unknown(start, pending_stand_down=start is st.State.RELEASING), st.Command.STAND_DOWN)
                self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
                self.assertFalse(outcome.machine.pending_stand_down)
                self.assertIsNone(outcome.fader)
                self.assertIsNone(outcome.refusal)
                self.assertFalse(outcome.machine.level_known)

    def test_a_stand_down_while_already_standing_down_changes_nothing(self):
        outcome = send(unknown(st.State.STANDING_DOWN), st.Command.STAND_DOWN)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_nothing_but_an_absolute_command_leaves_the_box_while_unknown(self):
        # The constraint, asserted directly across every command and state.
        for command in st.Command:
            for gradual in (False, True):
                for value in ALL_STATES:
                    for flags in ({}, {"stalled": True}, {"riding_in": True}, {"pending_stand_down": True}):
                        with self.subTest(command=command, gradual=gradual, state=value, flags=flags):
                            outcome = st.step(unknown(value, **flags), st.Event(command, gradual=gradual))
                            if outcome.fader is None:
                                continue
                            self.assertIn(outcome.fader, ABSOLUTE_FADER_COMMANDS)
                            if outcome.fader is st.FaderCommand.OPEN:
                                self.assertIs(command, st.Command.TRIGGER)
                                self.assertFalse(gradual)


class TestReportReady(unittest.TestCase):
    """READY is the one state that cannot be reached by driving to it: it has
    no fast form (see `_readying`), so from an unknown level the operator can
    only report that the fader is already there (#107)."""

    def test_it_lands_in_ready_and_marks_the_level_known(self):
        outcome = send(unknown(st.State.IDLE), st.Command.REPORT_READY)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertTrue(outcome.machine.level_known)
        self.assertEqual(outcome.fader, st.FaderCommand.REPORT_READY)
        self.assertIsNone(outcome.refusal)

    def test_it_lands_in_ready_from_every_state(self):
        for value in ALL_STATES:
            with self.subTest(state=value):
                outcome = send(unknown(value), st.Command.REPORT_READY)
                self.assertEqual(outcome.machine.state, st.State.READY)

    def test_it_arms_from_standing_down(self):
        outcome = send(unknown(st.State.STANDING_DOWN), st.Command.REPORT_READY)
        self.assertTrue(outcome.machine.armed_by_operator)

    def test_it_does_not_arm_from_an_armed_state(self):
        outcome = send(unknown(st.State.IDLE), st.Command.REPORT_READY)
        self.assertFalse(outcome.machine.armed_by_operator)

    def test_it_is_refused_once_the_level_is_known(self):
        # Otherwise it is a state jump to READY with no fader move, claiming a
        # hold level the box knows the fader is not at. If the operator means
        # it, HANDOFF first.
        outcome = send(armed(level_known=True), st.Command.REPORT_READY)
        self.assertIsNone(outcome.fader)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.refusal, st.LEVEL_ALREADY_KNOWN)
        self.assertEqual(outcome.machine.state, st.State.IDLE)

    def test_it_is_not_a_ride(self):
        # Nothing is moving, so nothing is riding in.
        self.assertFalse(send(unknown(st.State.IDLE), st.Command.REPORT_READY).machine.riding_in)

    def test_it_ends_a_pending_stand_down_and_a_stall(self):
        outcome = send(unknown(st.State.RELEASING, pending_stand_down=True, stalled=True), st.Command.REPORT_READY)
        self.assertFalse(outcome.machine.pending_stand_down)
        self.assertFalse(outcome.machine.stalled)

    def test_a_detector_can_never_report_it(self):
        for allow in (False, True):
            for value in ALL_STATES:
                with self.subTest(allow_detector=allow, state=value):
                    outcome = send(unknown(value, allow_detector=allow), st.Command.REPORT_READY, st.Source.DETECTOR)
                    self.assertIsNone(outcome.fader)
                    self.assertFalse(outcome.changed)
                    self.assertIn("operator-only", outcome.refusal)

    def test_a_trigger_from_it_commits_to_open_like_any_ready(self):
        reported = send(unknown(st.State.IDLE), st.Command.REPORT_READY).machine
        outcome = send(reported, st.Command.TRIGGER)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)

    def test_a_fade_from_it_is_ordinary_because_the_level_is_known_now(self):
        reported = send(unknown(st.State.IDLE), st.Command.REPORT_READY).machine
        self.assertEqual(send(reported, st.Command.RELEASE).fader, st.FaderCommand.FADE)


class TestNeverMutes(unittest.TestCase):
    def test_the_only_fader_commands_are_open_ready_fade_close_now_and_report_ready(self):
        # Faders only, never mutes. READY is a level short of target, not
        # silence, so it belongs on this list rather than being a third,
        # unwritten option (#6). REPORT_READY writes nothing at all - a belief
        # correction, not a fader move - and CLOSE_NOW is one packet to -inf,
        # never a mute (#12, #107).
        self.assertEqual(
            set(st.FaderCommand),
            {
                st.FaderCommand.OPEN,
                st.FaderCommand.READY,
                st.FaderCommand.FADE,
                st.FaderCommand.CLOSE_NOW,
                st.FaderCommand.REPORT_READY,
            },
        )

    def test_there_is_no_queue_left_to_hide_a_fader_tap_in(self):
        # #107: a tap blocked for want of a known level is refused, and said so.
        # A queued one ran later, on a belief the operator had not looked at.
        self.assertFalse(hasattr(st.Machine(), "queued"))
        self.assertFalse(hasattr(st.Outcome(machine=st.Machine()), "replay"))
        self.assertFalse(hasattr(st.Machine(), "handoff"))
        self.assertNotIn("TAKE_BACK_CONFIRMED", {c.name for c in st.Command})
        self.assertNotIn("TAKE_BACK_UP", {c.name for c in st.Command})
        self.assertNotIn("TAKE_BACK_DOWN", {c.name for c in st.Command})
        self.assertNotIn("TAKE_BACK_UP", {c.name for c in st.FaderCommand})
        self.assertNotIn("TAKE_BACK_DOWN", {c.name for c in st.FaderCommand})

    def test_the_two_enums_agree_on_the_belief_commands(self):
        for name in ("CLOSE_NOW", "REPORT_READY"):
            self.assertEqual(st.Command[name].value, st.FaderCommand[name].value)


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

    def test_it_says_the_level_is_unknown_and_how_to_say_where_it_is(self):
        for value in st.State:
            with self.subTest(state=value):
                text = st.describe(unknown(value)).lower()
                self.assertIn("does not know where the fader", text)
                self.assertIn("close it now", text)

    def test_it_does_not_say_so_once_the_level_is_known(self):
        for value in st.State:
            with self.subTest(state=value):
                self.assertNotIn("does not know", st.describe(st.Machine(state=value, level_known=True)).lower())

    def test_it_never_blames_stagemix(self):
        # The machine cannot tell a handoff from a cold boot, so it must not
        # say which it was. The cause is in the log.
        for value in st.State:
            for known in (True, False):
                with self.subTest(state=value, known=known):
                    self.assertNotIn("stagemix", st.describe(st.Machine(state=value, level_known=known)).lower())

    def test_it_never_mentions_a_waiting_tap(self):
        for value in st.State:
            for known in (True, False):
                with self.subTest(state=value, known=known):
                    self.assertNotIn("waiting on", st.describe(st.Machine(state=value, level_known=known)).lower())


if __name__ == "__main__":
    unittest.main()
