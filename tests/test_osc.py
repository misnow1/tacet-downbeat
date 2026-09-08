import struct
import unittest

from tacet import osc


class TestEncodeString(unittest.TestCase):
    def test_padding_rounds_up_to_four_including_the_terminator(self):
        # A 3-char string plus its null exactly fills four bytes: no extra pad.
        self.assertEqual(osc._encode_string("abc"), b"abc\x00")
        # A 4-char string needs a whole extra word, because the null pushes it over.
        self.assertEqual(osc._encode_string("test"), b"test\x00\x00\x00\x00")
        self.assertEqual(osc._encode_string(""), b"\x00\x00\x00\x00")

    def test_every_encoded_string_is_a_multiple_of_four(self):
        for n in range(40):
            self.assertEqual(len(osc._encode_string("x" * n)) % 4, 0)


class TestEncodeMessage(unittest.TestCase):
    def test_matches_the_osc_spec_example(self):
        # /oscillator/4/frequency 440.0 — the worked example in the OSC 1.0 spec.
        packet = osc.encode_message("/oscillator/4/frequency", 440.0)
        self.assertEqual(
            packet,
            b"/oscillator/4/frequency\x00" + b",f\x00\x00" + struct.pack(">f", 440.0),
        )
        self.assertEqual(len(packet) % 4, 0)

    def test_dm7_fader_command(self):
        # The command this project actually sends. -2000 is -20.00 dB.
        packet = osc.encode_message(
            "/yosc:req/set/MIXER:Current/DCA/Fader/Level/2", -2000, tags="i"
        )
        address, args = osc.decode_packet(packet)
        self.assertEqual(address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/2")
        self.assertEqual(args, (-2000,))
        self.assertIn(b",i\x00\x00", packet)

    def test_bool_encodes_as_a_payloadless_tag_not_an_int(self):
        self.assertEqual(osc.encode_message("/a", True), b"/a\x00\x00,T\x00\x00")
        self.assertEqual(osc.encode_message("/a", False), b"/a\x00\x00,F\x00\x00")

    def test_explicit_tags_override_inference(self):
        # 0 would infer as int; the tag forces a float32 payload.
        packet = osc.encode_message("/a", 0, tags="f")
        self.assertEqual(packet[-4:], struct.pack(">f", 0.0))

    def test_address_must_be_absolute(self):
        with self.assertRaises(osc.OscError):
            osc.encode_message("bad", 1)

    def test_tag_count_must_match_argument_count(self):
        with self.assertRaises(osc.OscError):
            osc.encode_message("/a", 1, tags="ii")

    def test_int_out_of_range_is_reported_as_an_osc_error(self):
        with self.assertRaises(osc.OscError):
            osc.encode_message("/a", 2**40, tags="i")

    def test_uninferrable_type_is_rejected(self):
        with self.assertRaises(osc.OscError):
            osc.encode_message("/a", {"not": "encodable"})


class TestRoundTrip(unittest.TestCase):
    def test_mixed_argument_types_survive(self):
        packet = osc.encode_message("/mix", 1, 2.5, "three", b"\x04\x05")
        address, args = osc.decode_packet(packet)
        self.assertEqual(address, "/mix")
        self.assertEqual(args[0], 1)
        self.assertAlmostEqual(args[1], 2.5)
        self.assertEqual(args[2], "three")
        self.assertEqual(args[3], b"\x04\x05")

    def test_no_argument_message(self):
        self.assertEqual(osc.decode_packet(osc.encode_message("/go")), ("/go", ()))

    def test_blob_lengths_that_need_padding(self):
        for n in range(0, 9):
            packet = osc.encode_message("/b", bytes(range(n)))
            self.assertEqual(len(packet) % 4, 0, f"blob of {n} bytes misaligned")
            self.assertEqual(osc.decode_packet(packet).args[0], bytes(range(n)))

    def test_string_arguments_of_every_alignment(self):
        for n in range(0, 9):
            packet = osc.encode_message("/s", "x" * n)
            self.assertEqual(osc.decode_packet(packet).args[0], "x" * n)


class TestBundles(unittest.TestCase):
    def test_round_trip(self):
        inner = [osc.encode_message("/one", 1), osc.encode_message("/two", 2.0)]
        decoded = osc.decode_packet(osc.encode_bundle(inner))
        self.assertIsInstance(decoded, osc.Bundle)
        self.assertEqual(decoded.timetag, osc.IMMEDIATELY)
        self.assertEqual(decoded.elements[0], ("/one", (1,)))
        self.assertEqual(decoded.elements[1].address, "/two")

    def test_nested_bundles(self):
        # Reaper nests; make sure recursion terminates on the right thing.
        inner = osc.encode_bundle([osc.encode_message("/deep", 9)])
        decoded = osc.decode_packet(osc.encode_bundle([inner]))
        self.assertEqual(decoded.elements[0].elements[0], ("/deep", (9,)))

    def test_element_size_running_past_the_buffer_is_rejected(self):
        packet = bytearray(osc.encode_bundle([osc.encode_message("/a", 1)]))
        packet[16:20] = struct.pack(">i", 999)
        with self.assertRaises(osc.OscError):
            osc.decode_packet(bytes(packet))


class TestMalformedInput(unittest.TestCase):
    def test_unterminated_string(self):
        with self.assertRaises(osc.OscError):
            osc.decode_packet(b"/no-null-here")

    def test_type_tags_without_leading_comma(self):
        with self.assertRaises(osc.OscError):
            osc.decode_packet(b"/a\x00\x00i\x00\x00\x00\x00\x00\x00\x01")

    def test_unknown_type_tag(self):
        with self.assertRaises(osc.OscError):
            osc.decode_packet(b"/a\x00\x00,z\x00\x00")


if __name__ == "__main__":
    unittest.main()
