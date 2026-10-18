import io
import unittest

from tacet import pcap

from . import pcap_capture as cap
from . import rtd_frames as real

#: Datagram 1 of Game 4: where the whole of the real wire headers end and the payload begins.
HEADERS_LENGTH = len(real.REAL_HEADERS)
TIME = 1792261493.5


def reader_of(records: list[cap.Record], **kwargs) -> pcap.PcapReader:
    return pcap.PcapReader(io.BytesIO(cap.pcap_bytes(records, **kwargs)))


def decode(data: bytes) -> pcap.Datagram | pcap.Skip:
    return pcap.decode_ethernet(pcap.Packet(TIME, data, False))


class TestDecodeEthernet(unittest.TestCase):
    def test_reads_a_real_datagram(self):
        result = decode(real.REAL_HEADERS + real.PREGAME_FULL)
        assert isinstance(result, pcap.Datagram)
        self.assertEqual(result.source, "192.168.0.30")
        self.assertEqual((result.source_port, result.dest_port), (21000, 21000))
        self.assertEqual(result.payload, real.PREGAME_FULL)
        self.assertEqual(result.time, TIME)

    def test_df_flag_is_not_a_fragment(self):
        data = cap.ethernet(cap.ipv4(cap.udp(b"x"), flags_frag=cap.DONT_FRAGMENT))
        self.assertIsInstance(decode(data), pcap.Datagram)

    def test_mf_or_offset_is_a_fragment(self):
        for flags_frag in (cap.MORE_FRAGMENTS, 0x0001, cap.DONT_FRAGMENT | 0x0010):
            data = cap.ethernet(cap.ipv4(cap.udp(b"x"), flags_frag=flags_frag))
            self.assertIs(decode(data), pcap.Skip.FRAGMENT, hex(flags_frag))

    def test_vlan_tag_is_skipped_over(self):
        result = decode(cap.ethernet(cap.ipv4(cap.udp(b"hello")), vlan=True))
        assert isinstance(result, pcap.Datagram)
        self.assertEqual(result.payload, b"hello")

    def test_non_udp_skipped(self):
        data = cap.ethernet(cap.ipv4(b"\x00" * 20, proto=cap.PROTO_TCP))
        self.assertIs(decode(data), pcap.Skip.NOT_UDP)

    def test_arp_skipped_not_ipv4(self):
        data = cap.ethernet(b"\x00" * 28, ethertype=cap.ETHERTYPE_ARP)
        self.assertIs(decode(data), pcap.Skip.NOT_IPV4)

    def test_malformed_short_frame_skipped(self):
        self.assertIs(decode(b"\x00" * (pcap.ETHERNET_HEADER - 1)), pcap.Skip.MALFORMED)
        self.assertIs(decode(cap.ethernet(b"\x45\x00")), pcap.Skip.MALFORMED)
        self.assertIs(decode(cap.ethernet(cap.ipv4(b"\x00" * 4))), pcap.Skip.MALFORMED)

    def test_malformed_ip_header(self):
        good = bytearray(cap.ethernet(cap.ipv4(cap.udp(b"x"))))
        for first_byte in (0x65, 0x44):  # wrong version; IHL below the minimum
            good[pcap.ETHERNET_HEADER] = first_byte
            self.assertIs(decode(bytes(good)), pcap.Skip.MALFORMED, hex(first_byte))

    def test_udp_length_beyond_capture_is_clipped(self):
        data = cap.ethernet(cap.ipv4(cap.udp(b"abcdefgh")))[:-3]
        result = decode(data)
        assert isinstance(result, pcap.Datagram)
        self.assertEqual(result.payload, b"abcde")

    def test_udp_length_below_the_header_is_malformed(self):
        data = bytearray(cap.ethernet(cap.ipv4(cap.udp(b"abc"))))
        length_at = pcap.ETHERNET_HEADER + pcap.IPV4_MIN_HEADER + 4
        data[length_at : length_at + 2] = b"\x00\x04"
        self.assertIs(decode(bytes(data)), pcap.Skip.MALFORMED)

    def test_ethernet_padding_after_the_udp_length_is_dropped(self):
        result = decode(cap.ethernet(cap.ipv4(cap.udp(b"abc"))) + b"\x00" * 6)
        assert isinstance(result, pcap.Datagram)
        self.assertEqual(result.payload, b"abc")

    def test_real_header_constants(self):
        self.assertEqual(HEADERS_LENGTH, pcap.ETHERNET_HEADER + pcap.IPV4_MIN_HEADER + pcap.UDP_HEADER)


class TestPcapReader(unittest.TestCase):
    def test_reads_records_in_order(self):
        reader = reader_of([cap.record(1.25, b"aa"), cap.record(2.5, b"bbb")])
        packets = list(reader)
        self.assertEqual([(p.time, p.data, p.clipped) for p in packets], [(1.25, b"aa", False), (2.5, b"bbb", False)])
        self.assertEqual(reader.linktype, 1)
        self.assertFalse(reader.truncated)

    def test_nanosecond_and_big_endian_magics(self):
        for magic in (cap.LE_NSEC, cap.BE_USEC, cap.BE_NSEC):
            packets = list(reader_of([cap.record(3.5, b"x")], magic=magic))
            self.assertEqual([p.time for p in packets], [3.5], magic.hex())

    def test_pcapng_refused_clearly(self):
        with self.assertRaisesRegex(pcap.PcapFormatError, "pcapng is not supported.*editcap -F pcap"):
            pcap.PcapReader(io.BytesIO(cap.PCAPNG + b"\x00" * 40))

    def test_unknown_magic_refused(self):
        with self.assertRaisesRegex(pcap.PcapFormatError, "not a pcap file"):
            pcap.PcapReader(io.BytesIO(b"\x00" * 40))

    def test_wrong_major_version_refused(self):
        data = bytearray(cap.pcap_bytes([]))
        data[4] = 3
        with self.assertRaisesRegex(pcap.PcapFormatError, "version"):
            pcap.PcapReader(io.BytesIO(bytes(data)))

    def test_short_header_refused(self):
        with self.assertRaisesRegex(pcap.PcapFormatError, "shorter than"):
            pcap.PcapReader(io.BytesIO(cap.pcap_bytes([])[:10]))
        with self.assertRaises(pcap.PcapFormatError):
            pcap.PcapReader(io.BytesIO(b""))

    def test_truncated_tail_sets_truncated(self):
        whole = cap.pcap_bytes([cap.record(1.0, b"aaaa"), cap.record(2.0, b"bbbb")])
        for cut in (3, 10):  # mid-data, then mid-record-header
            reader = pcap.PcapReader(io.BytesIO(whole[:-cut]))
            self.assertEqual([p.data for p in reader], [b"aaaa"])
            self.assertTrue(reader.truncated, cut)

    def test_clipped_record_flag(self):
        packets = list(reader_of([cap.record(1.0, b"abc", orig_len=10), cap.record(2.0, b"abc")]))
        self.assertEqual([p.clipped for p in packets], [True, False])


class TestDatagrams(unittest.TestCase):
    def test_non_ethernet_linktype_refused(self):
        reader = reader_of([], linktype=113)
        with self.assertRaisesRegex(pcap.PcapFormatError, "linktype 113 is not Ethernet.*-i any"):
            pcap.datagrams(reader)

    def test_the_fcs_bits_above_the_linktype_are_ignored(self):
        reader = reader_of([], linktype=pcap.LINKTYPE_ETHERNET | (1 << 28))
        self.assertEqual(list(pcap.datagrams(reader)), [])

    def test_yields_datagrams_and_skips_in_order(self):
        reader = reader_of(
            [
                cap.record(1.0, cap.rtd_packet(b"one")),
                cap.record(2.0, cap.ethernet(b"\x00" * 28, ethertype=cap.ETHERTYPE_ARP)),
                cap.record(3.0, cap.rtd_packet(b"two")),
            ]
        )
        items = list(pcap.datagrams(reader))
        seen = [i.payload if isinstance(i, pcap.Datagram) else i for i in items]
        self.assertEqual(seen, [b"one", pcap.Skip.NOT_IPV4, b"two"])


if __name__ == "__main__":
    unittest.main()
