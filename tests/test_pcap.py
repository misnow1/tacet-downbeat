import io
import unittest

from tacet import pcap
from tacet.datagram import Datagram, Skip

from . import pcap_capture as cap
from . import rtd_frames as real

TIME = 1792261493.5
ETHERNET = pcap.LINKTYPE_ETHERNET
IPV4_AT = 14
UDP_AT = IPV4_AT + 20


def capture_of(data: bytes) -> pcap.Capture:
    return pcap.Capture(io.BytesIO(data))


def decode(data: bytes, linktype: int = ETHERNET) -> Datagram | Skip:
    return pcap.decode_frame(linktype, TIME, data)


def udp_ethernet(payload: bytes, **kwargs) -> bytes:
    return cap.ethernet(cap.ipv4(cap.udp(payload), **kwargs))


class TestDecodeFrame(unittest.TestCase):
    def test_reads_a_real_datagram(self):
        result = decode(real.REAL_HEADERS + real.PREGAME_FULL)
        assert isinstance(result, Datagram)
        self.assertEqual(result.source, "192.168.0.30")
        self.assertEqual((result.source_port, result.dest_port), (21000, 21000))
        self.assertEqual(result.payload, real.PREGAME_FULL)
        self.assertEqual(result.time, TIME)

    def test_df_flag_is_not_a_fragment(self):
        self.assertIsInstance(decode(udp_ethernet(b"x", flags_frag=cap.DONT_FRAGMENT)), Datagram)

    def test_mf_or_offset_is_a_fragment(self):
        for flags_frag in (cap.MORE_FRAGMENTS, 0x0001, cap.DONT_FRAGMENT | 0x0010):
            self.assertIs(decode(udp_ethernet(b"x", flags_frag=flags_frag)), Skip.FRAGMENT, hex(flags_frag))

    def test_vlan_tag_is_skipped_over(self):
        result = decode(cap.ethernet(cap.ipv4(cap.udp(b"hello")), vlan=True))
        assert isinstance(result, Datagram)
        self.assertEqual(result.payload, b"hello")

    def test_non_udp_skipped(self):
        data = cap.ethernet(cap.ipv4(b"\x00" * 20, proto=cap.PROTO_TCP))
        self.assertIs(decode(data), Skip.NOT_UDP)

    def test_arp_skipped_not_ipv4(self):
        data = cap.ethernet(b"\x00" * 28, ethertype=cap.ETHERTYPE_ARP)
        self.assertIs(decode(data), Skip.NOT_IPV4)

    def test_malformed_short_frame_skipped(self):
        self.assertIs(decode(b"\x00" * 13), Skip.MALFORMED)

    def test_a_short_mpls_frame_is_malformed_not_an_exception(self):
        # dpkt raises IndexError on this one, not UnpackError.
        data = bytes.fromhex("ffffffffffff92ed636c905a884745000a96f51f600040118371c0a8001effffffff")
        self.assertIs(decode(data), Skip.MALFORMED)

    def test_an_ip_header_cut_short_counts_as_not_ipv4(self):
        # dpkt hands back the bytes undecoded, which this adapter cannot tell from a
        # non-IP payload (see Skip.NOT_IPV4).
        self.assertIs(decode(cap.ethernet(b"\x45\x00")), Skip.NOT_IPV4)

    def test_an_ip_header_dpkt_cannot_parse_counts_as_not_ipv4(self):
        good = bytearray(udp_ethernet(b"x"))
        for first_byte in (0x44, 0x65):  # IHL below the minimum; version 6
            good[IPV4_AT] = first_byte
            self.assertIs(decode(bytes(good)), Skip.NOT_IPV4, hex(first_byte))

    def test_udp_length_beyond_capture_is_clipped(self):
        result = decode(udp_ethernet(b"abcdefgh")[:-3])
        assert isinstance(result, Datagram)
        self.assertEqual(result.payload, b"abcde")

    def test_udp_length_below_the_header_is_malformed(self):
        data = bytearray(udp_ethernet(b"abc"))
        data[UDP_AT + 4 : UDP_AT + 6] = b"\x00\x04"
        self.assertIs(decode(bytes(data)), Skip.MALFORMED)

    def test_a_udp_header_cut_short_is_malformed(self):
        self.assertIs(decode(udp_ethernet(b"abc")[: UDP_AT + 4]), Skip.MALFORMED)

    def test_ethernet_padding_after_the_udp_length_is_dropped(self):
        result = decode(udp_ethernet(b"abc") + b"\x00" * 6)
        assert isinstance(result, Datagram)
        self.assertEqual(result.payload, b"abc")

    def test_no_dpkt_value_escapes(self):
        result = decode(real.REAL_HEADERS + real.PREGAME_FULL)
        assert isinstance(result, Datagram)
        self.assertEqual(
            [type(v) for v in (result.time, result.source, result.source_port, result.dest_port, result.payload)],
            [float, str, int, int, bytes],
        )


class TestCapture(unittest.TestCase):
    def test_reads_datagrams_in_order_with_times(self):
        records = [cap.record(1.25, cap.rtd_packet(b"aa")), cap.record(2.5, cap.rtd_packet(b"b"))]
        capture = capture_of(cap.pcap_bytes(records))
        items = list(capture)
        self.assertEqual([(i.time, i.payload) for i in items if isinstance(i, Datagram)], [(1.25, b"aa"), (2.5, b"b")])
        self.assertEqual(capture.linktype, ETHERNET)
        self.assertFalse(capture.truncated)

    def test_nanosecond_and_big_endian_magics(self):
        for magic in (cap.LE_NSEC, cap.BE_USEC, cap.BE_NSEC):
            (item,) = list(capture_of(cap.pcap_bytes([cap.record(3.5, cap.rtd_packet(b"x"))], magic=magic)))
            assert isinstance(item, Datagram)
            self.assertEqual(item.time, 3.5, magic.hex())
            self.assertIs(type(item.time), float, magic.hex())

    def test_reads_pcapng(self):
        capture = capture_of(cap.pcapng_bytes([cap.record(1.5, cap.rtd_packet(b"ng"))]))
        (item,) = list(capture)
        assert isinstance(item, Datagram)
        self.assertEqual((item.time, item.payload), (1.5, b"ng"))

    def test_reads_linux_cooked_sll(self):
        ip = cap.ipv4(cap.udp(b"cooked"))
        capture = capture_of(cap.pcap_bytes([cap.record(1.0, cap.sll(ip))], linktype=cap.LINKTYPE_SLL))
        (item,) = list(capture)
        assert isinstance(item, Datagram)
        self.assertEqual((item.source, item.payload), ("192.168.0.30", b"cooked"))

    def test_reads_linux_cooked_sll2(self):
        ip = cap.ipv4(cap.udp(b"cooked2"))
        capture = capture_of(cap.pcap_bytes([cap.record(1.0, cap.sll2(ip))], linktype=cap.LINKTYPE_SLL2))
        (item,) = list(capture)
        assert isinstance(item, Datagram)
        self.assertEqual((item.source, item.payload), ("192.168.0.30", b"cooked2"))

    def test_unknown_magic_refused(self):
        with self.assertRaisesRegex(pcap.PcapFormatError, "not a pcap or pcapng file"):
            capture_of(b"\x00" * 40)

    def test_short_header_refused(self):
        for data in (cap.pcap_bytes([])[:10], b""):
            with self.assertRaisesRegex(pcap.PcapFormatError, "not a pcap or pcapng file"):
                capture_of(data)

    def test_a_clean_file_is_not_truncated(self):
        capture = capture_of(cap.pcap_bytes([cap.record(1.0, cap.rtd_packet(b"a"))]))
        list(capture)
        self.assertFalse(capture.truncated)

    def test_truncated_tail_sets_truncated(self):
        whole = cap.pcap_bytes([cap.record(1.0, cap.rtd_packet(b"aaaa")), cap.record(2.0, cap.rtd_packet(b"bbbb"))])
        for cut in (3, len(cap.rtd_packet(b"bbbb")) + 10):  # mid-body, then mid-record-header
            capture = capture_of(whole[:-cut])
            items = list(capture)
            self.assertTrue(capture.truncated, cut)
            first = items[0]
            assert isinstance(first, Datagram)
            self.assertEqual(first.payload, b"aaaa")

    def test_unsupported_linktype_refused(self):
        with self.assertRaisesRegex(pcap.PcapFormatError, r"link type 105 is not supported.*\(1\).*113.*276"):
            capture_of(cap.pcap_bytes([], linktype=105))

    def test_the_fcs_bits_above_the_linktype_are_ignored(self):
        capture = capture_of(cap.pcap_bytes([], linktype=pcap.LINKTYPE_ETHERNET | (1 << 28)))
        self.assertEqual(capture.linktype, ETHERNET)
        self.assertEqual(list(capture), [])

    def test_yields_datagrams_and_skips_in_order(self):
        capture = capture_of(
            cap.pcap_bytes(
                [
                    cap.record(1.0, cap.rtd_packet(b"one")),
                    cap.record(2.0, cap.ethernet(b"\x00" * 28, ethertype=cap.ETHERTYPE_ARP)),
                    cap.record(3.0, cap.rtd_packet(b"two")),
                ]
            )
        )
        seen = [i.payload if isinstance(i, Datagram) else i for i in capture]
        self.assertEqual(seen, [b"one", Skip.NOT_IPV4, b"two"])


if __name__ == "__main__":
    unittest.main()
