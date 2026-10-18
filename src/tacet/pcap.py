"""Read a classic libpcap file and decode the UDP datagrams in it (#210).

Standard library only, like `tacet.rtd`. The reader takes a handle the caller
opened, as `wav.read_layout` does, and streams: a game capture is gigabytes.

Only the classic format is read, which is what `tcpdump -w` writes. pcapng is
refused with a message saying how to get a classic file. Only Ethernet frames
are read, with or without an 802.1Q tag; a capture taken with `tcpdump -i any`
is Linux cooked (SLL) and is refused too, because its frames carry no Ethernet
header to decode.
"""

from __future__ import annotations

import enum
import ipaddress
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

LINKTYPE_ETHERNET = 1
#: The top bits of the network field carry FCS information, not the link type.
LINKTYPE_MASK = 0xFFFF
SUPPORTED_MAJOR = 2

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_VLAN = 0x8100

MAGIC_LENGTH = 4
GLOBAL_HEADER = 24
RECORD_HEADER = 16
ETHERNET_HEADER = 14
VLAN_TAG = 4
IPV4_MIN_HEADER = 20
UDP_HEADER = 8

IP_PROTO_UDP = 17
#: More-fragments flag and fragment offset. The don't-fragment bit (0x4000) is set
#: on every RTD packet and is not a fragment.
IP_FRAGMENT_MASK = 0x3FFF
IPV4_VERSION = 4
IHL_UNIT = 4
IHL_MASK = 0x0F
VERSION_SHIFT = 4

USEC = 1_000_000
NSEC = 1_000_000_000

PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"
PCAPNG_HINT = "pcapng is not supported; capture with tcpdump -w (classic pcap) or convert with editcap -F pcap"

#: The four raw magics, as stored in the file: struct byte order and the divisor for the sub-second field.
_MAGICS: dict[bytes, tuple[str, int]] = {
    bytes.fromhex("d4c3b2a1"): ("<", USEC),
    bytes.fromhex("a1b2c3d4"): (">", USEC),
    bytes.fromhex("4d3cb2a1"): ("<", NSEC),
    bytes.fromhex("a1b23c4d"): (">", NSEC),
}

#: After the magic: major, minor, thiszone, sigfigs, snaplen, network.
_GLOBAL_FIELDS = "HHiIII"
_RECORD_FIELDS = "IIII"
_ETHERTYPE = struct.Struct("!H")
#: version and IHL, TOS, total length, id, flags and fragment offset, TTL, protocol, checksum, source, destination.
_IPV4 = struct.Struct("!BBHHHBBH4s4s")
#: source port, destination port, length, checksum.
_UDP = struct.Struct("!HHHH")
_ETHERTYPE_AT = ETHERNET_HEADER - _ETHERTYPE.size


class PcapFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    time: float
    data: bytes
    #: The capture kept fewer bytes than the packet had (incl_len < orig_len).
    clipped: bool


class PcapReader:
    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        header = handle.read(GLOBAL_HEADER)
        magic = header[:MAGIC_LENGTH]
        if magic == PCAPNG_MAGIC:
            raise PcapFormatError(PCAPNG_HINT)
        if len(header) < MAGIC_LENGTH or magic not in _MAGICS:
            raise PcapFormatError("not a pcap file")
        if len(header) < GLOBAL_HEADER:
            raise PcapFormatError(f"pcap header is shorter than {GLOBAL_HEADER} bytes")
        self._order, self._divisor = _MAGICS[magic]
        major, _minor, _zone, _sigfigs, _snaplen, network = struct.unpack(
            f"{self._order}{_GLOBAL_FIELDS}", header[MAGIC_LENGTH:]
        )
        if major != SUPPORTED_MAJOR:
            raise PcapFormatError(f"pcap version {major} is not supported")
        self.linktype: int = network
        #: Set once iteration reaches a record the file ends in the middle of.
        self.truncated = False

    def __iter__(self) -> Iterator[Packet]:
        record = struct.Struct(f"{self._order}{_RECORD_FIELDS}")
        while header := self._handle.read(RECORD_HEADER):
            if len(header) < RECORD_HEADER:
                self.truncated = True
                return
            seconds, fraction, included, original = record.unpack(header)
            data = self._handle.read(included)
            if len(data) < included:
                self.truncated = True
                return
            yield Packet(seconds + fraction / self._divisor, data, included < original)


class Skip(enum.Enum):
    NOT_IPV4 = "not-ipv4"
    NOT_UDP = "not-udp"
    FRAGMENT = "fragment"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Datagram:
    time: float
    source: str
    source_port: int
    dest_port: int
    payload: bytes


def _ethertype_and_offset(data: bytes) -> tuple[int, int] | None:
    """The EtherType after any 802.1Q tags, and where the IP header starts."""
    at = _ETHERTYPE_AT
    while True:
        if len(data) < at + _ETHERTYPE.size:
            return None
        (ethertype,) = _ETHERTYPE.unpack_from(data, at)
        at += _ETHERTYPE.size
        if ethertype != ETHERTYPE_VLAN:
            return ethertype, at
        at += VLAN_TAG - _ETHERTYPE.size


def decode_ethernet(packet: Packet) -> Datagram | Skip:
    """The UDP datagram in an Ethernet frame, or why there is not one."""
    found = _ethertype_and_offset(packet.data)
    if found is None:
        return Skip.MALFORMED
    ethertype, at = found
    if ethertype != ETHERTYPE_IPV4:
        return Skip.NOT_IPV4
    ip = packet.data[at:]
    if len(ip) < IPV4_MIN_HEADER:
        return Skip.MALFORMED
    version_ihl, _tos, _total, _ident, flags_frag, _ttl, proto, _sum, source, _dest = _IPV4.unpack_from(ip)
    header_length = (version_ihl & IHL_MASK) * IHL_UNIT
    if version_ihl >> VERSION_SHIFT != IPV4_VERSION or header_length < IPV4_MIN_HEADER or len(ip) < header_length:
        return Skip.MALFORMED
    if proto != IP_PROTO_UDP:
        return Skip.NOT_UDP
    if flags_frag & IP_FRAGMENT_MASK:
        return Skip.FRAGMENT
    udp = ip[header_length:]
    if len(udp) < UDP_HEADER:
        return Skip.MALFORMED
    source_port, dest_port, length, _checksum = _UDP.unpack_from(udp)
    if length < UDP_HEADER:
        return Skip.MALFORMED
    return Datagram(packet.time, str(ipaddress.IPv4Address(source)), source_port, dest_port, udp[UDP_HEADER:length])


def _decoded(reader: PcapReader) -> Iterator[Datagram | Skip]:
    for packet in reader:
        yield decode_ethernet(packet)


def datagrams(reader: PcapReader) -> Iterator[Datagram | Skip]:
    """Every packet in the capture, decoded or skipped. Refuses a non-Ethernet capture at once."""
    linktype = reader.linktype & LINKTYPE_MASK
    if linktype != LINKTYPE_ETHERNET:
        raise PcapFormatError(f"linktype {linktype} is not Ethernet; capture on a named interface, not -i any")
    return _decoded(reader)
