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
        outcome = st.step(machine(), st.Event(st.Command.TRIGGER, gradual=True))
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertTrue(outcome.machine.riding_in)

    def test_a_ready_arms_and_readies_too(self):
        # #6: a forgotten Arm must not cost a heads-up either.
        outcome = send(machine(), st.Command.READY)
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
        outcome = send(machine(), st.Command.RELEASE)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertEqual(outcome.machine, machine())

    def test_a_detector_trigger_is_still_refused(self):
        outcome = send(machine(allow_detector=True), st.Command.TRIGGER, st.Source.DETECTOR)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertIsNone(outcome.fader)
        self.assertIn("standing down", outcome.refusal)

    def test_arming_and_standing_down_still_work(self):
        self.assertEqual(send(machine(), st.Command.ARM).machine.state, st.State.IDLE)
        self.assertFalse(send(machine(), st.Command.STAND_DOWN).changed)

    def test_the_bookkeeping_commands_change_nothing(self):
        for command in (st.Command.FADE_COMPLETE, st.Command.RIDE_IN_COMPLETE):
            with self.subTest(command):
                outcome = send(machine(), command)
                self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
                self.assertIsNone(outcome.fader)


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
        outcome = ride_in(armed())
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertTrue(outcome.machine.riding_in)

    def test_a_snap_open_is_not_a_ride_in(self):
        self.assertFalse(send(armed(), st.Command.TRIGGER).machine.riding_in)

    def test_a_fast_trigger_during_a_ride_in_snaps(self):
        outcome = send(opened(riding_in=True), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.machine.riding_in)

    def test_a_slow_trigger_during_a_ride_in_does_not_restart_it(self):
        outcome = ride_in(opened(riding_in=True))
        self.assertIsNone(outcome.fader)
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.machine.riding_in)

    def test_once_the_ride_in_lands_a_trigger_changes_nothing(self):
        landed = send(opened(riding_in=True), st.Command.RIDE_IN_COMPLETE)
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
                outcome = send(opened(riding_in=True), command)
                self.assertEqual(outcome.fader, st.FaderCommand.FADE)
                self.assertFalse(outcome.machine.riding_in)

    def test_a_failed_ride_in_is_no_longer_in_flight(self):
        outcome = send(opened(riding_in=True), st.Command.MOVE_FAILED)
        self.assertTrue(outcome.machine.stalled)
        self.assertFalse(outcome.machine.riding_in)

    def test_retrying_a_failed_open_takes_the_speed_of_the_retry(self):
        self.assertTrue(ride_in(opened(stalled=True)).machine.riding_in)
        self.assertFalse(send(opened(stalled=True, riding_in=True), st.Command.TRIGGER).machine.riding_in)

    def test_a_snap_back_from_a_fade_can_itself_ride_in(self):
        self.assertTrue(ride_in(releasing()).machine.riding_in)
        self.assertFalse(send(releasing(), st.Command.TRIGGER).machine.riding_in)

    def test_the_detector_gate_still_comes_first(self):
        outcome = send(opened(riding_in=True), st.Command.TRIGGER, source=st.Source.DETECTOR)
        self.assertIsNone(outcome.fader)
        self.assertTrue(outcome.machine.riding_in)


class TestReady(unittest.TestCase):
    """#6: ready, then go. A hold level short of target, waiting to see
    whether the band starts - not a ride to the open level."""

    def test_ready_from_idle_rides_to_the_hold_level(self):
        outcome = send(armed(), st.Command.READY)
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
        outcome = send(readying(), st.Command.RELEASE)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_a_stand_down_fades_rather_than_slamming(self):
        # Same caution as a stand-down from OPEN: the box cannot be sure the
        # band has not quietly started under the hold level.
        outcome = send(readying(), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.RELEASING)
        self.assertEqual(outcome.fader, st.FaderCommand.FADE)
        self.assertTrue(outcome.machine.pending_stand_down)

    def test_the_pending_stand_down_from_ready_lands_standing_down(self):
        outcome = send(readying(), st.Command.STAND_DOWN)
        outcome = st.step(outcome.machine, st.Event(st.Command.FADE_COMPLETE))
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_a_failed_ride_marks_the_machine_stalled(self):
        outcome = send(readying(), st.Command.MOVE_FAILED)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertTrue(outcome.machine.stalled)
        self.assertFalse(outcome.machine.riding_in)
        self.assertIsNone(outcome.fader)

    def test_ready_after_a_failed_ride_retries_it(self):
        outcome = send(readying(stalled=True), st.Command.READY)
        self.assertEqual(outcome.fader, st.FaderCommand.READY)
        self.assertFalse(outcome.machine.stalled)

    def test_a_healthy_ride_is_not_restarted_by_another_ready(self):
        outcome = send(readying(stalled=False), st.Command.READY)
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


class TestHandoff(unittest.TestCase):
    """#12: every ramp starts from `commanded_level`, which is fiction once
    StageMix might also be moving the fader. `handoff` is orthogonal to
    `state`, like `allow_detector` - the box can be in any state and also not
    trust its own belief about where the fader really is."""

    def test_handoff_is_a_mode_change_with_no_fader_move(self):
        outcome = send(opened(), st.Command.HANDOFF)
        self.assertTrue(outcome.machine.handoff)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.machine.state, st.State.OPEN)

    def test_handoff_a_second_time_changes_nothing(self):
        outcome = send(opened(handoff=True), st.Command.HANDOFF)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_handoff_is_legal_from_standing_down(self):
        # The operator drives in every state (#89) - handing off is about the
        # console, not the band, so it is no different.
        outcome = send(machine(), st.Command.HANDOFF)
        self.assertTrue(outcome.machine.handoff)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)

    def test_a_detector_cannot_hand_off_while_standing_down(self):
        outcome = send(machine(allow_detector=True), st.Command.HANDOFF, st.Source.DETECTOR)
        self.assertFalse(outcome.machine.handoff)
        self.assertIn("standing down", outcome.refusal)

    def test_a_detector_cannot_hand_off_before_phase_two(self):
        outcome = send(opened(), st.Command.HANDOFF, st.Source.DETECTOR)
        self.assertFalse(outcome.machine.handoff)
        self.assertIsNotNone(outcome.refusal)

    def test_handing_off_mid_fade_presumes_it_landed(self):
        # #12: the in-flight fade is cancelled by the shell the moment
        # StageMix might touch the fader too, so `state` has to stop claiming
        # to be fading, or it would sit in RELEASING forever - nothing will
        # ever fire FADE_COMPLETE for a fade that never ran.
        outcome = send(releasing(), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.IDLE)
        self.assertTrue(outcome.machine.handoff)

    def test_handing_off_mid_fade_honours_a_pending_stand_down(self):
        outcome = send(releasing(pending_stand_down=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_handing_off_mid_ride_in_presumes_it_arrived(self):
        outcome = send(opened(riding_in=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.OPEN)
        self.assertFalse(outcome.machine.riding_in)

    def test_handing_off_mid_ready_ride_presumes_it_arrived_too(self):
        outcome = send(readying(riding_in=True), st.Command.HANDOFF)
        self.assertEqual(outcome.machine.state, st.State.READY)
        self.assertFalse(outcome.machine.riding_in)

    def test_a_snap_trigger_passes_straight_through(self):
        # "Snap opens skip the question" - correct from any start.
        outcome = send(armed(handoff=True), st.Command.TRIGGER)
        self.assertEqual(outcome.fader, st.FaderCommand.OPEN)
        self.assertEqual(outcome.machine.state, st.State.OPEN)

    def test_a_snap_trigger_does_not_clear_handoff(self):
        outcome = send(armed(handoff=True), st.Command.TRIGGER)
        self.assertTrue(outcome.machine.handoff)

    def test_release_is_queued_not_refused(self):
        event = st.Event(st.Command.RELEASE, detail="out")
        outcome = st.step(opened(handoff=True), event)
        self.assertIsNone(outcome.fader)
        self.assertIsNone(outcome.refusal)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.machine.queued, event)
        self.assertEqual(outcome.machine.state, st.State.OPEN)

    def test_ready_is_queued_too(self):
        event = st.Event(st.Command.READY)
        outcome = st.step(armed(handoff=True), event)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.machine.queued, event)

    def test_a_gradual_trigger_is_queued(self):
        event = st.Event(st.Command.TRIGGER, gradual=True, detail="up-slow")
        outcome = st.step(armed(handoff=True), event)
        self.assertIsNone(outcome.fader)
        self.assertEqual(outcome.machine.queued, event)

    def test_a_second_blocked_tap_replaces_the_first(self):
        first = st.Event(st.Command.RELEASE, detail="out")
        second = st.Event(st.Command.RELEASE, detail="score-reversed")
        after_first = st.step(opened(handoff=True), first).machine
        after_second = st.step(after_first, second).machine
        self.assertEqual(after_second.queued, second)

    def test_stand_down_while_handed_off_goes_straight_to_standing_down(self):
        for start in (opened(handoff=True), readying(handoff=True), armed(handoff=True)):
            with self.subTest(state=start.state):
                outcome = send(start, st.Command.STAND_DOWN)
                self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
                self.assertIsNone(outcome.fader)
                self.assertTrue(outcome.machine.handoff)

    def test_stand_down_while_handed_off_discards_anything_queued(self):
        queued_event = st.Event(st.Command.RELEASE, detail="out")
        machine_with_queued = st.step(opened(handoff=True), queued_event).machine
        outcome = send(machine_with_queued, st.Command.STAND_DOWN)
        self.assertIsNone(outcome.machine.queued)

    def test_stand_down_from_releasing_while_handed_off_skips_the_wait(self):
        # Otherwise nothing would ever move it on: no fade is sent, so
        # FADE_COMPLETE never arrives.
        outcome = send(releasing(handoff=True, pending_stand_down=True), st.Command.STAND_DOWN)
        self.assertEqual(outcome.machine.state, st.State.STANDING_DOWN)
        self.assertFalse(outcome.machine.pending_stand_down)

    def test_take_back_up_with_nothing_queued_sends_no_packet(self):
        outcome = send(opened(handoff=True), st.Command.TAKE_BACK_UP)
        self.assertEqual(outcome.fader, st.FaderCommand.TAKE_BACK_UP)
        self.assertIsNone(outcome.replay)
        self.assertFalse(outcome.machine.handoff)

    def test_take_back_down_always_sends_its_packet(self):
        outcome = send(opened(handoff=True), st.Command.TAKE_BACK_DOWN)
        self.assertEqual(outcome.fader, st.FaderCommand.TAKE_BACK_DOWN)
        self.assertIsNone(outcome.replay)
        self.assertFalse(outcome.machine.handoff)

    def test_take_back_runs_whatever_was_queued(self):
        queued_event = st.Event(st.Command.RELEASE, detail="out")
        handed_off_with_queue = st.step(opened(handoff=True), queued_event).machine
        outcome = send(handed_off_with_queue, st.Command.TAKE_BACK_UP)
        self.assertEqual(outcome.replay, queued_event)
        self.assertIsNone(outcome.machine.queued)

    def test_taking_back_control_is_a_no_op_when_not_handed_off(self):
        for command in (st.Command.TAKE_BACK_UP, st.Command.TAKE_BACK_DOWN):
            with self.subTest(command=command):
                outcome = send(opened(), command)
                self.assertFalse(outcome.changed)
                self.assertIsNone(outcome.fader)

    def test_a_failed_take_back_can_be_retried(self):
        # The confirming packet itself did not reach the console. By the time
        # MOVE_FAILED marks the machine stalled, `handoff` is already false -
        # this is what lets the same answer be tapped again to retry it.
        for command, fader in (
            (st.Command.TAKE_BACK_UP, st.FaderCommand.TAKE_BACK_UP),
            (st.Command.TAKE_BACK_DOWN, st.FaderCommand.TAKE_BACK_DOWN),
        ):
            with self.subTest(command=command):
                outcome = send(opened(stalled=True), command)
                self.assertEqual(outcome.fader, fader)
                self.assertFalse(outcome.machine.stalled)

    def test_a_healthy_take_back_is_not_retried_by_a_stray_answer(self):
        # `handoff` already false and not stalled: nothing to retry.
        outcome = send(opened(), st.Command.TAKE_BACK_DOWN)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.fader)

    def test_the_why_line_says_stagemix_has_it(self):
        text = st.describe(opened(handoff=True))
        self.assertIn("stagemix", text.lower())

    def test_the_why_line_mentions_a_queued_tap(self):
        queued_event = st.Event(st.Command.RELEASE, detail="out")
        with_queue = st.step(opened(handoff=True), queued_event).machine
        self.assertIn("waiting", st.describe(with_queue).lower())
        self.assertNotIn("waiting", st.describe(opened(handoff=True)).lower())


class TestNeverMutes(unittest.TestCase):
    def test_the_only_fader_commands_are_open_ready_and_fade(self):
        # Faders only, never mutes. READY is a level short of target, not
        # silence, so it belongs on this list rather than being a third,
        # unwritten option (#6). TAKE_BACK_UP writes nothing at all - a belief
        # correction, not a fader move - and TAKE_BACK_DOWN is one packet to
        # -inf, never a mute (#12).
        self.assertEqual(
            set(st.FaderCommand),
            {
                st.FaderCommand.OPEN,
                st.FaderCommand.READY,
                st.FaderCommand.FADE,
                st.FaderCommand.TAKE_BACK_UP,
                st.FaderCommand.TAKE_BACK_DOWN,
            },
        )


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
