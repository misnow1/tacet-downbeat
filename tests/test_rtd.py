import unittest

from tacet import rtd

from . import rtd_frames as real


def parse(raw: bytes) -> rtd.Frame | rtd.FrameFault:
    return rtd.parse_frame(raw)


def written(offset: int, text: str) -> rtd.Frame:
    result = parse(real.frame(offset, text))
    assert isinstance(result, rtd.Frame)
    return result


def only_frame(raw: bytes) -> rtd.Frame:
    result = parse(raw)
    assert isinstance(result, rtd.Frame), result
    return result


def fault_of(raw: bytes) -> rtd.Fault:
    result = parse(raw)
    assert isinstance(result, rtd.FrameFault), result
    return result.fault


REAL_FRAMES = (
    real.TIME_OF_DAY,
    real.PLAY_CLOCK_39,
    real.CLOCK_RUNNING,
    real.DOWN_2ND,
    real.TIMEOUT_CLOCK,
    real.COUNTDOWN,
    real.CLOCK_AT_ZERO,
    real.QUARTER_1,
    real.HOME_SCORE_7,
    real.GUEST_SCORE_10,
    real.PREGAME_FULL,
    real.FINAL_FULL,
)


class TestChecksum(unittest.TestCase):
    def test_checksum_of_the_time_of_day_frame_is_0f(self):
        self.assertEqual(rtd.checksum(real.TIME_OF_DAY[1:-3]), b"0F")
        self.assertEqual(len(real.TIME_OF_DAY), rtd.MIN_FRAME_LENGTH + 8)
        self.assertEqual(len(real.TIME_OF_DAY), 33)

    def test_checksum_covers_header_through_eot_not_payload_only(self):
        # design.md 8 says payload only; the wire says otherwise (#206). The
        # payload-only sum of this frame's text would be 99.
        payload_only = real.TIME_OF_DAY.replace(b"0F\x17", b"99\x17")
        self.assertEqual(fault_of(payload_only), rtd.Fault.CHECKSUM)

    def test_every_real_frame_parses_with_its_checksum(self):
        for raw in REAL_FRAMES:
            self.assertIsInstance(parse(raw), rtd.Frame, raw)

    def test_the_test_builder_agrees_with_the_wire(self):
        self.assertEqual(real.frame(39, " 2:24:54"), real.TIME_OF_DAY)
        self.assertEqual(real.frame(200, "   39   "), real.PLAY_CLOCK_39)


class TestParseFrame(unittest.TestCase):
    def test_parses_offset_and_text(self):
        tod = only_frame(real.TIME_OF_DAY)
        self.assertEqual((tod.prefix, tod.offset, tod.text), ("004210", 39, " 2:24:54"))
        clock = only_frame(real.PLAY_CLOCK_39)
        self.assertEqual((clock.offset, clock.text), (200, "   39   "))
        self.assertTrue(tod.is_write)

    def test_lowercase_checksum_is_a_checksum_fault(self):
        self.assertEqual(fault_of(real.TIME_OF_DAY.replace(b"0F\x17", b"0f\x17")), rtd.Fault.CHECKSUM)

    def test_corrupted_byte_is_a_checksum_fault(self):
        raw = bytearray(real.TIME_OF_DAY)
        raw[rtd.TEXT_AT] ^= 0x01
        self.assertEqual(fault_of(bytes(raw)), rtd.Fault.CHECKSUM)

    def test_a_fault_keeps_the_raw_bytes(self):
        raw = real.frame(3, "x", checksum=b"00")
        result = parse(raw)
        assert isinstance(result, rtd.FrameFault)
        self.assertEqual(result.raw, raw)

    def test_malformed_too_short(self):
        self.assertEqual(fault_of(real.TIME_OF_DAY[: rtd.MIN_FRAME_LENGTH - 1]), rtd.Fault.MALFORMED)
        self.assertEqual(fault_of(b""), rtd.Fault.MALFORMED)

    def test_malformed_control_bytes_missing_or_misplaced(self):
        for position in (0, rtd.SOH_AT, rtd.STX_AT, len(real.TIME_OF_DAY) - rtd.TRAILER_LENGTH, -1):
            raw = bytearray(real.TIME_OF_DAY)
            raw[position] = ord("A")
            self.assertEqual(fault_of(bytes(raw)), rtd.Fault.MALFORMED, position)

    def test_malformed_non_digit_offset(self):
        raw = real.TIME_OF_DAY.replace(b"0039", b"00x9")
        self.assertEqual(fault_of(raw), rtd.Fault.MALFORMED)

    def test_malformed_non_digit_header(self):
        raw = real.TIME_OF_DAY.replace(b"00000000", b"0000000x")
        self.assertEqual(fault_of(raw), rtd.Fault.MALFORMED)

    def test_malformed_non_digit_prefix(self):
        raw = real.TIME_OF_DAY.replace(b"004210", b"00421x")
        self.assertEqual(fault_of(raw), rtd.Fault.MALFORMED)

    def test_malformed_non_printable_text(self):
        self.assertEqual(fault_of(real.frame(3, "a\x00b")), rtd.Fault.MALFORMED)
        self.assertEqual(fault_of(real.frame(3, "a\x7fb")), rtd.Fault.MALFORMED)

    def test_other_prefix_is_a_frame_but_not_a_write(self):
        other = only_frame(real.frame(0, "abc", prefix=b"004010"))
        self.assertEqual(other.prefix, "004010")
        self.assertFalse(other.is_write)


class TestParseDatagram(unittest.TestCase):
    def test_empty_datagram_has_no_frames(self):
        self.assertEqual(rtd.parse_datagram(b""), [])

    def test_one_frame(self):
        self.assertEqual(rtd.parse_datagram(real.TIME_OF_DAY), [only_frame(real.TIME_OF_DAY)])

    def test_two_frames_in_one_datagram(self):
        result = rtd.parse_datagram(real.TIME_OF_DAY + real.PLAY_CLOCK_39)
        self.assertEqual([r.offset for r in result if isinstance(r, rtd.Frame)], [39, 200])

    def test_truncated_frame_at_end_of_datagram(self):
        cut = real.TIME_OF_DAY[:-5]
        result = rtd.parse_datagram(cut)
        self.assertEqual(result, [rtd.FrameFault(rtd.Fault.TRUNCATED, cut)])

    def test_truncated_frame_followed_by_good_frame(self):
        cut = real.TIME_OF_DAY[:-5]
        result = rtd.parse_datagram(cut + real.PLAY_CLOCK_39)
        self.assertEqual(result, [rtd.FrameFault(rtd.Fault.TRUNCATED, cut), only_frame(real.PLAY_CLOCK_39)])

    def test_garbage_before_between_and_after_frames(self):
        payload = b"ab" + real.TIME_OF_DAY + b"cde" + real.PLAY_CLOCK_39 + b"f"
        result = rtd.parse_datagram(payload)
        self.assertEqual(
            result,
            [
                rtd.FrameFault(rtd.Fault.GARBAGE, b"ab"),
                only_frame(real.TIME_OF_DAY),
                rtd.FrameFault(rtd.Fault.GARBAGE, b"cde"),
                only_frame(real.PLAY_CLOCK_39),
                rtd.FrameFault(rtd.Fault.GARBAGE, b"f"),
            ],
        )

    def test_garbage_alone_is_one_fault(self):
        self.assertEqual(rtd.parse_datagram(b"hello"), [rtd.FrameFault(rtd.Fault.GARBAGE, b"hello")])

    def test_a_bad_frame_does_not_hide_the_next(self):
        bad = real.frame(3, "x", checksum=b"00")
        result = rtd.parse_datagram(bad + real.TIME_OF_DAY)
        self.assertEqual([type(r) for r in result], [rtd.FrameFault, rtd.Frame])


class TestDisplay(unittest.TestCase):
    def test_fresh_display_reads_never_written_as_none(self):
        display = rtd.Display()
        self.assertTrue(all(value is None for value in display.snapshot().values()))

    def test_blank_is_not_never_written(self):
        display = rtd.Display()
        display.apply(only_frame(real.CLOCK_RUNNING))
        self.assertEqual(display.value(rtd.MAIN_CLOCK_STOPPED), " ")
        self.assertIsNone(display.value(rtd.MAIN_CLOCK))

    def test_partly_written_field_stays_none_until_complete(self):
        display = rtd.Display()
        display.apply(written(rtd.HOME_SCORE.offset, "  "))
        self.assertIsNone(display.value(rtd.HOME_SCORE))
        display.apply(written(rtd.HOME_SCORE.offset + 2, " 7"))
        self.assertEqual(display.value(rtd.HOME_SCORE), "   7")

    def test_a_partial_write_reports_no_change_for_an_incomplete_field(self):
        applied = rtd.Display().apply(written(rtd.HOME_SCORE.offset, "  "))
        assert isinstance(applied, rtd.Applied)
        self.assertEqual(applied.changes, ())

    def test_change_reports_old_none_on_first_write(self):
        display = rtd.Display()
        applied = display.apply(only_frame(real.TIME_OF_DAY))
        assert isinstance(applied, rtd.Applied)
        self.assertEqual(applied.changes, (rtd.FieldChange(rtd.TIME_OF_DAY, None, " 2:24:54"),))
        again = display.apply(written(39, " 2:24:55"))
        assert isinstance(again, rtd.Applied)
        self.assertEqual(again.changes, (rtd.FieldChange(rtd.TIME_OF_DAY, " 2:24:54", " 2:24:55"),))

    def test_rewriting_same_value_reports_no_change(self):
        display = rtd.Display()
        display.apply(only_frame(real.TIME_OF_DAY))
        applied = display.apply(only_frame(real.TIME_OF_DAY))
        assert isinstance(applied, rtd.Applied)
        self.assertEqual(applied.changes, ())

    def test_write_spanning_fields_reports_each_in_map_order(self):
        applied = rtd.Display().apply(only_frame(real.QUARTER_1))
        assert isinstance(applied, rtd.Applied)
        self.assertEqual(
            [(c.field.name, c.new) for c in applied.changes],
            [("quarter", " 1"), ("quarter_text", "1ST "), ("quarter_description", "1ST Quarter ")],
        )

    def test_write_beyond_buffer_is_refused_and_not_applied(self):
        display = rtd.Display()
        result = display.apply(written(350, "abcd"))
        self.assertEqual(result, rtd.FrameFault(rtd.Fault.BEYOND_BUFFER, b""))
        self.assertIsNone(display.value(rtd.BALL_ON_LABEL))

    def test_write_ending_exactly_at_buffer_end_is_applied(self):
        display = rtd.Display()
        result = display.apply(written(rtd.BALL_ON_LABEL.offset, "BALLON"))
        self.assertIsInstance(result, rtd.Applied)
        self.assertEqual(display.value(rtd.BALL_ON_LABEL), "BALLON")

    def test_unmapped_change_is_reported(self):
        display = rtd.Display()
        first = display.apply(written(115, "x"))
        assert isinstance(first, rtd.Applied)
        self.assertEqual(first.unmapped, ())
        second = display.apply(written(115, "y"))
        assert isinstance(second, rtd.Applied)
        self.assertEqual(second.unmapped, (115,))
        third = display.apply(written(115, "y"))
        assert isinstance(third, rtd.Applied)
        self.assertEqual(third.unmapped, ())

    def test_apply_rejects_non_write_frame(self):
        other = only_frame(real.frame(0, "abc", prefix=b"004010"))
        with self.assertRaises(ValueError):
            rtd.Display().apply(other)

    def test_a_field_that_does_not_fit_is_refused(self):
        with self.assertRaises(ValueError):
            rtd.Display(size=10, fields=(rtd.Field("wide", 5, 6),))


class TestFieldMap(unittest.TestCase):
    def full_write(self, raw: bytes) -> dict[str, str | None]:
        display = rtd.Display()
        display.apply(only_frame(raw))
        return display.snapshot()

    def test_field_map_pinned_against_pregame_full_write(self):
        values = self.full_write(real.PREGAME_FULL)
        self.assertEqual(values["main_clock"], "70:07")
        self.assertEqual(values["time_of_day"], " 2:24:53")
        self.assertEqual(values["home_name"], "VT" + " " * 18)
        self.assertEqual(values["guest_name"], "GT" + " " * 18)
        self.assertEqual(values["home_short_name"], "Tech      ")
        self.assertEqual(values["guest_short_name"], "GUEST     ")
        self.assertEqual(values["home_score"], "   0")
        self.assertEqual(values["guest_score"], "   0")
        self.assertEqual(values["home_timeouts"], "3")
        self.assertEqual(values["home_timeouts_copy"], "3")
        self.assertEqual(values["guest_timeouts"], "3")
        self.assertEqual(values["guest_timeouts_copy"], "3")
        self.assertEqual(values["quarter"], "  ")
        self.assertEqual(values["quarter_text"], "PRE ")
        self.assertEqual(values["quarter_description"], "Pregame     ")
        self.assertEqual(values["unknown_160"], "c")
        self.assertEqual(values["unknown_183"], "66016001001")
        self.assertEqual(values["play_clock"], "   40   ")
        self.assertEqual(values["ball_on_label"], "BALLON")
        self.assertNotIn(None, values.values())

    def test_field_map_pinned_against_final_full_write(self):
        values = self.full_write(real.FINAL_FULL)
        self.assertEqual(values["main_clock"], " 0:00")
        self.assertEqual(values["clock_at_zero"], "z")
        self.assertEqual(values["main_clock_stopped"], "s")
        self.assertEqual(values["home_score"], "  34")
        self.assertEqual(values["guest_score"], "  24")
        self.assertEqual(values["quarter"], " 4")
        self.assertEqual(values["quarter_text"], "4TH ")
        self.assertEqual(values["quarter_description"], "4TH Quarter ")
        self.assertEqual(values["home_possession"], "<POSS")
        self.assertEqual(values["guest_possession"], "     ")
        self.assertEqual(values["ball_on"], "25")
        self.assertEqual(values["down"], "4th")
        self.assertEqual(values["to_go"], "15")
        self.assertEqual(values["ball_on_side"], "H")
        self.assertEqual(values["ball_on_copy"], "25")
        self.assertEqual(values["ball_on_label"], "BALLON")

    def test_both_full_writes_leave_nothing_unmapped(self):
        display = rtd.Display()
        for raw in (real.PREGAME_FULL, real.FINAL_FULL):
            applied = display.apply(only_frame(raw))
            assert isinstance(applied, rtd.Applied)
            self.assertEqual(applied.unmapped, ())

    def test_fields_are_ordered_unique_non_overlapping_and_fit_the_buffer(self):
        names = [f.name for f in rtd.FIELDS]
        self.assertEqual(len(names), len(set(names)))
        for before, after in zip(rtd.FIELDS, rtd.FIELDS[1:], strict=False):
            self.assertLessEqual(before.end, after.offset, (before.name, after.name))
        for f in rtd.FIELDS:
            self.assertGreater(f.width, 0, f.name)
            self.assertLessEqual(f.end, rtd.BUFFER_SIZE, f.name)

    def test_every_field_constant_is_in_fields(self):
        constants = [v for v in vars(rtd).values() if isinstance(v, rtd.Field)]
        self.assertEqual(sorted(constants, key=lambda f: f.offset), list(rtd.FIELDS))

    def test_play_clock_is_published_offset_201(self):
        self.assertEqual(rtd.PLAY_CLOCK.offset + rtd.PUBLISHED_OFFSET_BASE, 201)
        self.assertEqual(rtd.MAIN_CLOCK_STOPPED.offset + rtd.PUBLISHED_OFFSET_BASE, 28)

    def test_buffer_size_is_the_full_write_length(self):
        for raw in (real.PREGAME_FULL, real.FINAL_FULL):
            self.assertEqual(len(only_frame(raw).text), rtd.BUFFER_SIZE)
            self.assertEqual(len(raw), rtd.MIN_FRAME_LENGTH + rtd.BUFFER_SIZE)

    def test_play_clock_digits_sit_at_203(self):
        display = rtd.Display()
        display.apply(only_frame(real.PLAY_CLOCK_39))
        value = display.value(rtd.PLAY_CLOCK)
        assert value is not None
        digits_at = rtd.PLAY_CLOCK.offset + value.index("39")
        self.assertEqual(digits_at, 203)

    def test_derived_positions_match_the_wire(self):
        self.assertEqual(
            (rtd.SOH_AT, rtd.PREFIX_AT, rtd.OFFSET_AT, rtd.STX_AT, rtd.TEXT_AT, rtd.TRAILER_LENGTH),
            (9, 10, 16, 20, 21, 4),
        )
        self.assertEqual(rtd.MIN_FRAME_LENGTH, 25)


if __name__ == "__main__":
    unittest.main()
