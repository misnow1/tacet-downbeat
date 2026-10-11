import io
import unittest

from tacet import wav

from .pilot_wav import pcm24, wav_bytes

RATE = 48_000
TWO_WORDS = (2 << 32) | 5


def layout_of(data: bytes) -> wav.WavLayout:
    return wav.read_layout(io.BytesIO(data), len(data))


class TestReadLayout(unittest.TestCase):
    def test_finds_the_data_chunk_after_bext_and_junk(self):
        data = wav_bytes(pcm24([0, 1, 2]), rate=RATE, chunks_before_data=[(b"junk", bytes(28))])
        layout = layout_of(data)
        self.assertEqual(layout.data_offset, 690)
        self.assertEqual(layout.data_bytes, 9)
        self.assertEqual((layout.sample_rate, layout.channels, layout.bits), (RATE, 1, 24))
        self.assertFalse(layout.truncated)

    def test_odd_sized_chunk_is_padded(self):
        data = wav_bytes(pcm24([0]), rate=RATE, chunks_before_data=[(b"odd ", b"abc")])
        # bext ends at 646; the odd chunk is 8 + 3 + 1 pad, then the data header.
        self.assertEqual(layout_of(data).data_offset, 646 + 12 + 8)

    def test_time_reference_places_the_file(self):
        rate = 1000
        reference = 100 * rate
        layout = layout_of(wav_bytes(pcm24([0]), rate=rate, time_reference=reference))
        self.assertEqual(layout.start_seconds, 100.0)
        high = layout_of(wav_bytes(pcm24([0]), rate=RATE, time_reference=TWO_WORDS))
        self.assertEqual(high.time_reference, TWO_WORDS)

    def test_duration_is_whole_frames(self):
        layout = layout_of(wav_bytes(pcm24([0] * RATE), rate=RATE))
        self.assertEqual(layout.duration_seconds, 1.0)


class TestRefusals(unittest.TestCase):
    def assert_refused(self, data: bytes, *needles: str) -> None:
        with self.assertRaises(wav.WavFormatError) as caught:
            layout_of(data)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))

    def test_refuses_a_file_without_bext(self):
        self.assert_refused(wav_bytes(pcm24([0]), rate=RATE, time_reference=None), "bext")

    def test_refuses_non_riff(self):
        self.assert_refused(b"OggS" + bytes(40), "RIFF")

    def test_refuses_rf64(self):
        self.assert_refused(b"RF64" + bytes(40), "RF64")

    def test_refuses_stereo(self):
        self.assert_refused(wav_bytes(bytes(6), rate=RATE, channels=2), "2 channels")

    def test_refuses_16_bit(self):
        self.assert_refused(wav_bytes(bytes(2), rate=RATE, bits=16), "16-bit")

    def test_refuses_non_pcm(self):
        self.assert_refused(wav_bytes(bytes(3), rate=RATE, format_tag=3), "format tag 3")

    def test_refuses_a_file_with_no_data(self):
        self.assert_refused(wav_bytes(b"", rate=RATE)[:-8], "no data chunk")


class TestTruncated(unittest.TestCase):
    def test_truncated_data_uses_what_is_there(self):
        full = wav_bytes(pcm24([1, 2, 3, 4]), rate=RATE, declared_data_bytes=12000)
        cut = full[:-2]  # 3 whole frames and a partial one
        layout = layout_of(cut)
        self.assertTrue(layout.truncated)
        self.assertEqual(layout.data_bytes, 9)


if __name__ == "__main__":
    unittest.main()
