"""Putting OSC packets on the wire.

Shared by the console client and the Reaper client. Both speak OSC over UDP to a
fixed host and port, and neither gets a reply worth waiting for - the DM7 cannot
send one at all (design.md 5.3), and Reaper's feedback arrives on its own
socket rather than as a response.

The `Sender` seam exists so tests record packets instead of opening sockets, and
so a future transport - a redundant path, a replay harness - drops in unchanged.

The host is an IPv4 literal, never a name (#71). `sendto` looks a name up again
for every packet, a lookup has no timeout, and a slow one would stall the
fader. `UdpSender` refuses a name at construction; the config and the flags
refuse one earlier, at load, with words for the operator.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Protocol, runtime_checkable

NOT_AN_IPV4_ADDRESS = "UdpSender takes an IPv4 address, got {host!r}: a name would be looked up on every packet (#71)"


def is_ipv4_literal(host: str) -> bool:
    """True for a dotted-quad IPv4 address. A name, an IPv6 literal and a
    leading-zero octet are all False. Pure: nothing is resolved."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


class TransportError(RuntimeError):
    """A packet could not be sent. Surface it; never swallow it."""


class Sender(Protocol):
    def send(self, packet: bytes) -> None:
        """Send one packet, or raise `TransportError`."""


@runtime_checkable
class ClosableSender(Protocol):
    def send(self, packet: bytes) -> None: ...

    def close(self) -> None: ...


class UdpSender:
    """Fire and forget. Nothing is read back on this socket."""

    def __init__(self, host: str, port: int) -> None:
        if not is_ipv4_literal(host):
            raise ValueError(NOT_AN_IPV4_ADDRESS.format(host=host))
        self.host = host
        self.port = port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, packet: bytes) -> None:
        # Every failure, not just OSError. An out-of-range port raises
        # OverflowError, and anything that is not a TransportError escapes the
        # handlers that turn a failed send into a visible fault (#72).
        try:
            self._socket.sendto(packet, (self.host, self.port))
        except Exception as exc:
            raise TransportError(f"could not send to {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        self._socket.close()
