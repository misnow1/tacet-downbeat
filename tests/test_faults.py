import unittest

from tacet import faults

KIND = faults.FaultKind.TAKE_STOPPED
MESSAGE = "it stopped"


class TestTheLatch(unittest.TestCase):
    def test_nothing_is_latched_to_begin_with(self):
        latch = faults.FaultLatch()
        self.assertIsNone(latch.latched(KIND))
        self.assertEqual(latch.as_snapshot(), [])

    def test_latching_a_kind_gives_it_an_id_and_the_time(self):
        latch, fault = faults.FaultLatch().latch(KIND, MESSAGE, now=12.5)
        assert fault is not None
        self.assertEqual((fault.kind, fault.id, fault.raised_at, fault.message), (KIND, 1, 12.5, MESSAGE))
        self.assertEqual(latch.latched(KIND), fault)

    def test_a_second_latch_of_the_same_kind_is_a_no_op(self):
        latch, _ = faults.FaultLatch().latch(KIND, MESSAGE, now=1.0)
        again, second = latch.latch(KIND, MESSAGE, now=2.0)
        self.assertIsNone(second)
        self.assertEqual(again, latch)
        self.assertEqual(again.next_id, 2)

    def test_clearing_by_id_unlatches_and_returns_the_fault(self):
        latch, fault = faults.FaultLatch().latch(KIND, MESSAGE, now=1.0)
        assert fault is not None
        cleared, got = latch.clear(fault.id)
        self.assertEqual(got, fault)
        self.assertIsNone(cleared.latched(KIND))

    def test_a_stale_id_clears_nothing(self):
        latch, _ = faults.FaultLatch().latch(KIND, MESSAGE, now=1.0)
        same, got = latch.clear(99)
        self.assertIsNone(got)
        self.assertEqual(same, latch)

    def test_a_relatch_after_a_clear_has_a_new_id_and_the_old_id_cannot_clear_it(self):
        latch, first = faults.FaultLatch().latch(KIND, MESSAGE, now=1.0)
        assert first is not None
        latch, _ = latch.clear(first.id)
        latch, second = latch.latch(KIND, MESSAGE, now=5.0)
        assert second is not None
        self.assertEqual(second.id, first.id + 1)
        held, got = latch.clear(first.id)
        self.assertIsNone(got)
        self.assertEqual(held.latched(KIND), second)

    def test_different_kinds_latch_together_each_with_its_own_id(self):
        latch = faults.FaultLatch()
        latch, dante = latch.latch(faults.FaultKind.INTERFACE_DANTE, MESSAGE, now=1.0)
        latch, control = latch.latch(faults.FaultKind.INTERFACE_CONTROL, MESSAGE, now=1.0)
        assert dante is not None and control is not None
        self.assertEqual((dante.id, control.id), (1, 2))
        self.assertEqual(
            {f.kind for f in latch.faults}, {faults.FaultKind.INTERFACE_DANTE, faults.FaultKind.INTERFACE_CONTROL}
        )
        cleared, _ = latch.clear(dante.id)
        self.assertIsNone(cleared.latched(faults.FaultKind.INTERFACE_DANTE))
        self.assertEqual(cleared.latched(faults.FaultKind.INTERFACE_CONTROL), control)

    def test_the_interface_kinds_are_pinned_strings(self):
        self.assertEqual(
            [faults.FaultKind.INTERFACE_DANTE, faults.FaultKind.INTERFACE_CONTROL, faults.FaultKind.INTERFACE_SCORING],
            ["interface-dante", "interface-control", "interface-scoring"],
        )

    def test_a_latch_seeded_with_next_id_starts_there(self):
        latch, fault = faults.FaultLatch(next_id=8).latch(KIND, MESSAGE, now=1.0)
        assert fault is not None
        self.assertEqual((fault.id, latch.next_id), (8, 9))

    def test_the_log_and_the_page_shapes(self):
        latch, fault = faults.FaultLatch().latch(KIND, MESSAGE, now=3.0)
        assert fault is not None
        self.assertEqual(fault.as_data(), {"fault": "take-stopped", "id": 1})
        self.assertEqual(fault.as_snapshot(), {"id": 1, "kind": "take-stopped", "message": MESSAGE, "raised_at": 3.0})
        self.assertEqual(latch.as_snapshot(), [fault.as_snapshot()])
