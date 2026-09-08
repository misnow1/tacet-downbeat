"""Putting OSC packets on the wire.

Shared by the console client and the Reaper client. Both speak OSC over UDP to a
fixed host and port, and neither gets a reply worth waiting for - the DM7 cannot
send one at all (design.md 5.3), and Reaper's feedback arrives on its own
socket rather than as a response.

The `Sender` seam exists so tests record packets instead of opening sockets, and
so a future transport - a redundant path, a replay harness - drops in unchanged.
"""

from __future__ import annotations

import socket
from typing import Protocol, runtime_checkable


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
        self.host = host
        self.port = port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, packet: bytes) -> None:
        try:
            self._socket.sendto(packet, (self.host, self.port))
        except OSError as exc:
            raise TransportError(f"could not send to {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        self._socket.close()
