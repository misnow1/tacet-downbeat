"""#19, the pure half: when the box asks the operator to arm or stand down.

No clock, no App, no socket. `prompts` decides what the open prompt becomes and
which log entries that earns; `tacet.app` writes them.
"""

import ast
import dataclasses
import unittest
from pathlib import Path

import tacet
from tacet import annotations as ann
from tacet import prompts, state

IDLE = state.Machine(state=state.State.IDLE, level_known=True)
STANDING_DOWN = state.Machine(state=state.State.STANDING_DOWN, level_known=True)
OPEN = state.Machine(state=state.State.OPEN, level_known=True)
PENDING = state.Machine(state=state.State.RELEASING, pending_stand_down=True, level_known=True)

STAND_DOWN_PROMPT = prompts.OpenPrompt(seq=4, prompt=prompts.Prompt.STAND_DOWN, source=ann.BAND_EXITS_STANDS)
ARM_PROMPT = prompts.OpenPrompt(seq=7, prompt=prompts.Prompt.ARM, source=ann.BAND_ENTERS_STANDS)

PROMPT_KEYS = (
    prompts.PROMPT_RAISED,
    prompts.PROMPT_ACCEPTED,
    prompts.PROMPT_DISMISSED,
    prompts.PROMPT_RESOLVED,
    prompts.PROMPT_WITHDRAWN,
)


class TestWhatAsks(unittest.TestCase):
    def test_band_exits_stands_asks_to_stand_down(self):
        self.assertIs(prompts.asked_by(ann.BAND_EXITS_STANDS), prompts.Prompt.STAND_DOWN)

    def test_halftime_exodus_asks_to_stand_down(self):
        self.assertIs(prompts.asked_by(ann.HALFTIME_EXODUS), prompts.Prompt.STAND_DOWN)

    def test_band_enters_stands_asks_to_arm(self):
        self.assertIs(prompts.asked_by(ann.BAND_ENTERS_STANDS), prompts.Prompt.ARM)

    def test_an_annotation_with_no_question_asks_nothing(self):
        for key in ("touchdown", "out", "note", "up-whistle", "band-exits-stadium"):
            with self.subTest(key=key):
                self.assertIsNone(prompts.asked_by(key))

    def test_every_trigger_key_is_a_real_operator_button(self):
        for key in prompts.ASKS:
            with self.subTest(key=key):
                self.assertEqual(ann.operator_event(key).key, key)

    def test_the_trigger_keys_are_the_ones_the_vocabulary_spells(self):
        self.assertEqual(ann.BAND_ENTERS_STANDS, "band-enters-stands")
        self.assertEqual(ann.BAND_EXITS_STANDS, "band-exits-stands")
        self.assertEqual(ann.HALFTIME_EXODUS, "halftime-exodus")

    def test_no_trigger_moves_the_fader_by_itself(self):
        for key in prompts.ASKS:
            with self.subTest(key=key):
                self.assertIsNone(ann.lookup(key).action)


class TestTheKeysTheBoxWrites(unittest.TestCase):
    def test_every_prompt_key_is_in_the_vocabulary_and_is_box_only(self):
        for key in PROMPT_KEYS:
            with self.subTest(key=key):
                event = ann.lookup(key)
                self.assertFalse(event.button)
                self.assertIs(event.category, ann.Category.SESSION)
                self.assertIs(event.kind, ann.Kind.INSTANT)

    def test_the_five_keys_are_distinct(self):
        self.assertEqual(len(set(PROMPT_KEYS)), len(PROMPT_KEYS))

    def test_the_spellings_are_pinned(self):
        self.assertEqual(
            PROMPT_KEYS,
            ("prompt-raised", "prompt-accepted", "prompt-dismissed", "prompt-resolved", "prompt-withdrawn"),
        )


class TestWhenTheStateAlreadyMatches(unittest.TestCase):
    def test_stand_down_is_true_while_standing_down(self):
        self.assertTrue(prompts.already_true(prompts.Prompt.STAND_DOWN, STANDING_DOWN))

    def test_stand_down_is_true_while_one_is_pending(self):
        self.assertTrue(prompts.already_true(prompts.Prompt.STAND_DOWN, PENDING))

    def test_stand_down_is_not_true_of_an_armed_box(self):
        for machine_state in (state.State.IDLE, state.State.READY, state.State.OPEN, state.State.RELEASING):
            with self.subTest(state=machine_state):
                machine = state.Machine(state=machine_state, level_known=True)
                self.assertFalse(prompts.already_true(prompts.Prompt.STAND_DOWN, machine))

    def test_arm_is_true_in_every_state_but_standing_down(self):
        for machine_state in state.State:
            with self.subTest(state=machine_state):
                machine = state.Machine(state=machine_state, level_known=True)
                self.assertEqual(
                    prompts.already_true(prompts.Prompt.ARM, machine),
                    machine_state is not state.State.STANDING_DOWN,
                )

    def test_a_pending_stand_down_counts_as_armed_and_is_not_asked_about(self):
        # Deliberate (#19 plan, D3): the stand-down still lands, and any up
        # button cancels it. No new command exists for it.
        self.assertTrue(prompts.already_true(prompts.Prompt.ARM, PENDING))


class TestAsking(unittest.TestCase):
    def ask(self, current, source, machine, seq=1):
        return prompts.ask(current, source=source, machine=machine, seq=seq)

    def test_a_question_is_raised_with_the_seq_it_was_given(self):
        for seq in (1, 9):
            with self.subTest(seq=seq):
                decision = self.ask(None, ann.BAND_EXITS_STANDS, OPEN, seq=seq)
                self.assertEqual(
                    decision.prompt,
                    prompts.OpenPrompt(seq=seq, prompt=prompts.Prompt.STAND_DOWN, source=ann.BAND_EXITS_STANDS),
                )
                self.assertEqual(
                    decision.notes,
                    (
                        prompts.Note(
                            key=prompts.PROMPT_RAISED,
                            prompt=prompts.Prompt.STAND_DOWN,
                            seq=seq,
                            source=ann.BAND_EXITS_STANDS,
                        ),
                    ),
                )

    def test_an_arm_question_is_raised_while_standing_down(self):
        decision = self.ask(None, ann.BAND_ENTERS_STANDS, STANDING_DOWN)
        assert decision.prompt is not None
        self.assertIs(decision.prompt.prompt, prompts.Prompt.ARM)

    def test_a_stand_down_ask_while_already_standing_down_is_resolved_not_raised(self):
        decision = self.ask(None, ann.BAND_EXITS_STANDS, STANDING_DOWN)
        self.assertIsNone(decision.prompt)
        self.assertEqual([note.key for note in decision.notes], [prompts.PROMPT_RESOLVED])

    def test_a_stand_down_ask_while_a_stand_down_is_pending_is_resolved_not_raised(self):
        decision = self.ask(None, ann.HALFTIME_EXODUS, PENDING)
        self.assertIsNone(decision.prompt)
        self.assertEqual([note.key for note in decision.notes], [prompts.PROMPT_RESOLVED])

    def test_an_arm_ask_is_resolved_not_raised_in_every_state_but_standing_down(self):
        for machine_state in state.State:
            if machine_state is state.State.STANDING_DOWN:
                continue
            with self.subTest(state=machine_state):
                machine = state.Machine(state=machine_state, level_known=True)
                decision = self.ask(None, ann.BAND_ENTERS_STANDS, machine)
                self.assertIsNone(decision.prompt)
                self.assertEqual([note.key for note in decision.notes], [prompts.PROMPT_RESOLVED])

    def test_resolving_directly_writes_one_note_that_was_never_raised(self):
        decision = self.ask(None, ann.BAND_EXITS_STANDS, STANDING_DOWN, seq=5)
        self.assertEqual(
            decision.notes,
            (
                prompts.Note(
                    key=prompts.PROMPT_RESOLVED,
                    prompt=prompts.Prompt.STAND_DOWN,
                    seq=None,
                    source=ann.BAND_EXITS_STANDS,
                ),
            ),
        )

    def test_a_question_the_operator_contradicts_withdraws_the_open_one(self):
        # A Stand down prompt is open and the operator taps band-enters-stands:
        # Arm is already true, so nothing is raised - but the question on the
        # page is contradicted and comes down, logged. Resolved first (what
        # happened to this tap), withdrawn second.
        decision = self.ask(STAND_DOWN_PROMPT, ann.BAND_ENTERS_STANDS, OPEN, seq=99)
        self.assertIsNone(decision.prompt)
        self.assertEqual(
            decision.notes,
            (
                prompts.Note(
                    key=prompts.PROMPT_RESOLVED,
                    prompt=prompts.Prompt.ARM,
                    seq=None,
                    source=ann.BAND_ENTERS_STANDS,
                ),
                prompts.Note(
                    key=prompts.PROMPT_WITHDRAWN,
                    prompt=prompts.Prompt.STAND_DOWN,
                    seq=STAND_DOWN_PROMPT.seq,
                    source=STAND_DOWN_PROMPT.source,
                ),
            ),
        )

    def test_the_other_way_round_withdraws_an_arm_prompt(self):
        decision = self.ask(ARM_PROMPT, ann.BAND_EXITS_STANDS, STANDING_DOWN, seq=99)
        self.assertIsNone(decision.prompt)
        self.assertEqual([note.key for note in decision.notes], [prompts.PROMPT_RESOLVED, prompts.PROMPT_WITHDRAWN])
        self.assertEqual(decision.notes[1].seq, ARM_PROMPT.seq)

    def test_a_withdrawal_raises_nothing_in_its_place(self):
        # The other kind is already true, so its accept would change nothing,
        # which is exactly what a question is not to be raised for.
        for current, source, machine in (
            (STAND_DOWN_PROMPT, ann.BAND_ENTERS_STANDS, OPEN),
            (ARM_PROMPT, ann.BAND_EXITS_STANDS, STANDING_DOWN),
        ):
            with self.subTest(source=source):
                decision = self.ask(current, source, machine)
                self.assertIsNone(decision.prompt)
                self.assertNotIn(prompts.PROMPT_RAISED, [note.key for note in decision.notes])
                self.assertIsNone(decision.notes[0].replaced_by)
                self.assertIsNone(decision.notes[1].replaced_by)

    def test_a_newer_prompt_of_the_other_kind_replaces_the_open_one(self):
        # `ask` takes no view on whether the open prompt is consistent with
        # the machine (`settle` keeps it so), which is what lets this pin the
        # genuine replacement rule on its own: with two complementary kinds and
        # a consistent open prompt, the other kind is always already true, so
        # through the App a question is withdrawn and never replaced. A third
        # kind is what would reach this.
        decision = self.ask(ARM_PROMPT, ann.BAND_EXITS_STANDS, IDLE, seq=8)
        assert decision.prompt is not None
        self.assertEqual((decision.prompt.seq, decision.prompt.prompt), (8, prompts.Prompt.STAND_DOWN))
        withdrawn, raised = decision.notes
        self.assertEqual(
            withdrawn,
            prompts.Note(
                key=prompts.PROMPT_WITHDRAWN,
                prompt=prompts.Prompt.ARM,
                seq=ARM_PROMPT.seq,
                source=ARM_PROMPT.source,
                replaced_by=8,
            ),
        )
        self.assertEqual((raised.key, raised.seq), (prompts.PROMPT_RAISED, 8))

    def test_the_same_ask_twice_leaves_the_open_prompt_and_its_seq_alone(self):
        decision = self.ask(STAND_DOWN_PROMPT, ann.BAND_EXITS_STANDS, OPEN, seq=99)
        self.assertEqual(decision, prompts.Decision(prompt=STAND_DOWN_PROMPT))
        self.assertEqual(decision.notes, ())

    def test_a_different_trigger_of_the_same_kind_is_still_the_same_question(self):
        decision = self.ask(STAND_DOWN_PROMPT, ann.HALFTIME_EXODUS, OPEN, seq=99)
        self.assertEqual(decision, prompts.Decision(prompt=STAND_DOWN_PROMPT))

    def test_an_annotation_with_no_question_changes_nothing(self):
        decision = self.ask(STAND_DOWN_PROMPT, "touchdown", OPEN)
        self.assertEqual(decision, prompts.Decision(prompt=STAND_DOWN_PROMPT))
        self.assertEqual(self.ask(None, "touchdown", OPEN), prompts.Decision(prompt=None))


class TestSettling(unittest.TestCase):
    def test_settle_closes_an_open_prompt_once_the_state_matches(self):
        decision = prompts.settle(STAND_DOWN_PROMPT, STANDING_DOWN)
        self.assertIsNone(decision.prompt)
        self.assertEqual(
            decision.notes,
            (
                prompts.Note(
                    key=prompts.PROMPT_RESOLVED,
                    prompt=STAND_DOWN_PROMPT.prompt,
                    seq=STAND_DOWN_PROMPT.seq,
                    source=STAND_DOWN_PROMPT.source,
                ),
            ),
        )

    def test_settle_closes_an_arm_prompt_once_the_box_is_armed(self):
        decision = prompts.settle(ARM_PROMPT, IDLE)
        self.assertIsNone(decision.prompt)
        self.assertEqual([note.seq for note in decision.notes], [ARM_PROMPT.seq])

    def test_settle_leaves_a_prompt_whose_state_still_does_not_match(self):
        for prompt, machine in ((STAND_DOWN_PROMPT, OPEN), (ARM_PROMPT, STANDING_DOWN)):
            with self.subTest(prompt=prompt.prompt):
                self.assertEqual(prompts.settle(prompt, machine), prompts.Decision(prompt=prompt))

    def test_settle_with_no_open_prompt_does_nothing(self):
        for machine in (STANDING_DOWN, IDLE, OPEN, PENDING):
            with self.subTest(state=machine.state):
                self.assertEqual(prompts.settle(None, machine), prompts.Decision(prompt=None))


class TestAPromptCanNeverMoveAFader(unittest.TestCase):
    def test_a_decision_can_never_carry_a_fader_command(self):
        # The structural guarantee: a prompt is a question about duty, so what
        # it decides is which prompt is open and what to log, and nothing else.
        self.assertEqual({f.name for f in dataclasses.fields(prompts.Decision)}, {"prompt", "notes"})

    def test_prompts_imports_nothing_from_the_console_client(self):
        source = Path(prompts.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for name in imported:
            self.assertNotIn("dm7", name)
            self.assertNotIn("osc", name.split("."))

    def test_prompts_imports_only_the_standard_library_and_this_package(self):
        import sys

        source = Path(prompts.__file__).read_text(encoding="utf-8")
        allowed = sys.stdlib_module_names | {tacet.__name__}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.assertIn(node.module.split(".")[0], allowed)

    def test_the_module_says_what_a_prompt_may_not_do(self):
        doc = prompts.__doc__ or ""
        self.assertIn("never move a fader", doc)
        self.assertIn("never changes the machine by itself", doc)


if __name__ == "__main__":
    unittest.main()
