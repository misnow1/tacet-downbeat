"""What the capture reader hands the replay, and why it sometimes hands nothing (#210).

The replay's input contract. It lives in a module of its own, standard library
only, so that the dpkt adapter (`tacet.pcap`) and the pure core
(`tacet.rtd_replay`) share it without either importing the other. That matters
when `rtd_replay` runs as `python -m`: it is then `__main__`, and a module that
imported it back would get a second copy whose `Skip` is not the same class.
"""

import enum
from dataclasses import dataclass


class Skip(enum.Enum):
    """Why a captured frame yielded no datagram.

    NOT_IPV4 also covers an IPv4 header that could not be parsed.
    """

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
