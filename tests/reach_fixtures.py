"""Shared fakes for the console check (#73): a runner that never forks, and a
clock that only moves when a test moves it."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from tacet import reach

HOST = "192.0.2.1"
PING = "/sbin/ping"
ARP = "/usr/sbin/arp"
MAC_TOOLS = reach.Tools(platform=reach.Platform.MACOS, ping=PING, arp=ARP)
LINUX_TOOLS = reach.Tools(platform=reach.Platform.LINUX, ping="/bin/ping", arp=None)

#: Captured on the dev Mac (plan-73 section 12), host addresses aside.
BSD_INCOMPLETE = "? (192.0.2.1) at (incomplete) on en0 ifscope [ethernet]\n"
BSD_RESOLVED = "? (192.0.2.1) at ce:71:fc:ab:d6:7a on en0 ifscope [ethernet]\n"
BSD_NO_ENTRY = "192.0.2.1 (192.0.2.1) -- no entry\n"

ANSWER_TEXT = "64 bytes from 192.0.2.1: icmp_seq=0 ttl=64 time=1.234 ms\n"

ANSWERED = reach.ProcessOutput(0, ANSWER_TEXT.encode(), b"")
MAC_NO_REPLY = reach.ProcessOutput(2, b"1 packets transmitted, 0 packets received, 100.0% packet loss\n", b"")
LINUX_NO_REPLY = reach.ProcessOutput(1, b"1 packets transmitted, 0 received, 100% packet loss\n", b"")


def out(text: str, code: int = 0) -> reach.ProcessOutput:
    return reach.ProcessOutput(code, text.encode(), b"")


class FakeRunner:
    """Answers by executable. A value that is an Exception is raised."""

    def __init__(
        self,
        ping: reach.ProcessOutput | Exception = ANSWERED,
        arp: reach.ProcessOutput | Exception | None = None,
    ) -> None:
        self.ping = ping
        self.arp = out(BSD_RESOLVED) if arp is None else arp
        self.calls: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, timeout: float) -> reach.ProcessOutput:
        self.calls.append(list(argv))
        result = self.ping if argv[0] == PING or argv[0] == LINUX_TOOLS.ping else self.arp
        if isinstance(result, Exception):
            raise result
        return result


class GatedRunner(FakeRunner):
    """A runner whose ping waits for the test to let it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.gate = asyncio.Event()

    async def run(self, argv: Sequence[str], *, timeout: float) -> reach.ProcessOutput:
        self.started += 1
        await self.gate.wait()
        return await super().run(argv, timeout=timeout)


class ManualClock:
    """Time that moves only on `advance`, with sleeps that really wait for it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def settle(self) -> None:
        for _ in range(60):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        # Wake sleepers in order, so a loop that sleeps again inside the
        # window is woken again, as real time would.
        while True:
            await self.settle()
            due = sorted(
                (at, f) for at, f in self._sleepers if at <= target and not f.done()
            )
            if not due:
                break
            at, future = due[0]
            self.now = max(self.now, at)
            future.set_result(None)
        self.now = target
        await self.settle()
