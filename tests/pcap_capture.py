"""Build classic pcap files, and the Ethernet, IPv4 and UDP inside them, for the tests (#210)."""

import struct

LE_USEC = bytes.fromhex("d4c3b2a1")
BE_USEC = bytes.fromhex("a1b2c3d4")
LE_NSEC = bytes.fromhex("4d3cb2a1")
BE_NSEC = bytes.fromhex("a1b23c4d")
PCAPNG = bytes.fromhex("0a0d0d0a")

#: The magic tells the byte order; the fraction is microseconds or nanoseconds.
BYTE_ORDER = {LE_USEC: "<", LE_NSEC: "<", BE_USEC: ">", BE_NSEC: ">"}
FRACTION = {LE_USEC: 10**6, BE_USEC: 10**6, LE_NSEC: 10**9, BE_NSEC: 10**9}

VERSION = (2, 4)
SNAPLEN = 65535
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_VLAN = 0x8100
ETHERTYPE_ARP = 0x0806
PROTO_UDP = 17
PROTO_TCP = 6
DONT_FRAGMENT = 0x4000
MORE_FRAGMENTS = 0x2000
RTD_PORT = 21000
BROADCAST_MAC = bytes.fromhex("ffffffffffff")
SOURCE_MAC = bytes.fromhex("92ed636c905a")
BROADCAST_IP = bytes((255, 255, 255, 255))
IPV4_VERSION_AND_IHL = 0x45
TTL = 64

Record = tuple[float, bytes, int | None]


def record(time: float, data: bytes, orig_len: int | None = None) -> Record:
    return (time, data, orig_len)


def pcap_bytes(records: list[Record], *, magic: bytes = LE_USEC, linktype: int = 1) -> bytes:
    order, fraction = BYTE_ORDER[magic], FRACTION[magic]
    out = magic + struct.pack(f"{order}HHiIII", *VERSION, 0, 0, SNAPLEN, linktype)
    for time, data, orig_len in records:
        seconds = int(time)
        out += struct.pack(
            f"{order}IIII",
            seconds,
            round((time - seconds) * fraction),
            len(data),
            len(data) if orig_len is None else orig_len,
        )
        out += data
    return out


def udp(payload: bytes, *, sport: int = RTD_PORT, dport: int = RTD_PORT) -> bytes:
    return struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload


def ipv4(
    payload: bytes,
    *,
    src: tuple[int, int, int, int] = (192, 168, 0, 30),
    proto: int = PROTO_UDP,
    flags_frag: int = DONT_FRAGMENT,
) -> bytes:
    header = struct.pack(
        "!BBHHHBBH4s4s",
        IPV4_VERSION_AND_IHL,
        0,
        20 + len(payload),
        0,
        flags_frag,
        TTL,
        proto,
        0,
        bytes(src),
        BROADCAST_IP,
    )
    return header + payload


def ethernet(payload: bytes, *, vlan: bool = False, ethertype: int = ETHERTYPE_IPV4) -> bytes:
    head = BROADCAST_MAC + SOURCE_MAC
    if vlan:
        return head + struct.pack("!HHH", ETHERTYPE_VLAN, 1, ethertype) + payload
    return head + struct.pack("!H", ethertype) + payload


def rtd_packet(payload: bytes, *, src: tuple[int, int, int, int] = (192, 168, 0, 30), dport: int = RTD_PORT) -> bytes:
    """A broadcast RTD datagram as it appears on the wire."""
    return ethernet(ipv4(udp(payload, dport=dport), src=src))
