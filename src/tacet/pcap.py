"""Read a pcap or pcapng capture and decode the UDP datagrams in it (#210).

This is the only module that imports dpkt, an optional extra (`.[analysis]`)
kept off the box's control path. dpkt ships no type information, so everything
that leaves here is converted to a concrete type: nothing untyped escapes.

Ethernet (with or without 802.1Q) and Linux cooked captures (SLL and SLL2,
what `tcpdump -i any` writes) are read; any other link type is refused. A
pcapng file takes its link type from its first interface.

dpkt is quiet about damage, so this adapter has to watch for it. A layer it
cannot decode becomes raw bytes rather than raising, hence the `isinstance`
checks. A file cut off inside a record body is yielded short with no flag, hence
`_ShortReadWatch`; one cut off inside a record header raises, and is caught.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

import dpkt
import dpkt.ethernet
import dpkt.ip
import dpkt.pcap
import dpkt.pcapng
import dpkt.sll
import dpkt.sll2
import dpkt.udp

from .rtd_replay import Datagram, Skip

#: The top bits of the link type field carry FCS information, not the link type.
LINKTYPE_MASK = 0xFFFF
LINKTYPE_ETHERNET = 1
#: `tcpdump -i any`, older libpcap.
LINKTYPE_LINUX_SLL = 113
#: `tcpdump -i any`, libpcap 1.10 and later.
LINKTYPE_LINUX_SLL2 = 276
IPV4_VERSION = 4
UDP_HEADER = 8
NOT_A_CAPTURE = "not a pcap or pcapng file"

_LINK_DECODERS: dict[int, Callable[[bytes], Any]] = {
    LINKTYPE_ETHERNET: dpkt.ethernet.Ethernet,
    LINKTYPE_LINUX_SLL: dpkt.sll.SLL,
    LINKTYPE_LINUX_SLL2: dpkt.sll2.SLL2,
}


class PcapFormatError(ValueError):
    pass


def decode_frame(linktype: int, time: float, frame: bytes) -> Datagram | Skip:
    """The UDP datagram in one captured frame, or why there is not one."""
    try:
        link = _LINK_DECODERS[linktype](frame)
    except (dpkt.UnpackError, KeyError):
        return Skip.MALFORMED
    ip = link.data
    # An IPv4 header dpkt could not parse arrives as raw bytes and is counted here too.
    if not isinstance(ip, dpkt.ip.IP) or ip.v != IPV4_VERSION:
        return Skip.NOT_IPV4
    if ip.p != dpkt.ip.IP_PROTO_UDP:
        return Skip.NOT_UDP
    if ip.mf or ip.offset:
        return Skip.FRAGMENT
    udp = ip.data
    if not isinstance(udp, dpkt.udp.UDP) or udp.ulen < UDP_HEADER:
        return Skip.MALFORMED
    return Datagram(
        time,
        str(ipaddress.IPv4Address(bytes(ip.src))),
        int(udp.sport),
        int(udp.dport),
        bytes(udp.data[: udp.ulen - UDP_HEADER]),
    )


class _ShortReadWatch:
    """Passes reads through and notes a read that returned fewer bytes than asked for.

    On a regular file that happens only at the end, so a short read inside a
    record means the file was cut off. A clean end returns nothing at all.
    """

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.short = False

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        if 0 < len(data) < size:
            self.short = True
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)


class Capture:
    def __init__(self, handle: BinaryIO) -> None:
        self._watch = _ShortReadWatch(handle)
        try:
            self._reader = dpkt.pcap.UniversalReader(self._watch)
        except (ValueError, dpkt.UnpackError):
            raise PcapFormatError(NOT_A_CAPTURE) from None
        self._watch.short = False
        self.linktype: int = int(self._reader.datalink()) & LINKTYPE_MASK
        if self.linktype not in _LINK_DECODERS:
            raise PcapFormatError(
                f"link type {self.linktype} is not supported; tacet-rtd reads Ethernet ({LINKTYPE_ETHERNET}) "
                f"and Linux cooked captures (SLL {LINKTYPE_LINUX_SLL}, SLL2 {LINKTYPE_LINUX_SLL2})"
            )
        #: Valid once iteration has finished: the file ended in the middle of a record.
        self.truncated = False

    def __iter__(self) -> Iterator[Datagram | Skip]:
        records = iter(self._reader)
        while True:
            try:
                stamp, frame = next(records)
            except StopIteration:
                break
            except dpkt.UnpackError:
                self.truncated = True
                break
            # pcap gives a float; nanosecond pcap gives a Decimal.
            yield decode_frame(self.linktype, float(stamp), bytes(frame))
        if self._watch.short:
            self.truncated = True
