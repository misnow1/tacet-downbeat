"""Is anything answering at the console's address? (#73)

The DM7's OSC is write-only (design.md 5.3): nothing the box can do proves that
the DM7 is at the address, that the port is right, or that a packet was
accepted. "Commanded, never confirmed" stands unchanged after this module. What
can be checked is narrower, and every word the box says about it stays inside
that:

    answered         an ICMP echo came back. Something at that IP is up and on
                     the network now. Not that it is the DM7, nor that the
                     port is right, nor that OSC is accepted.
    nothing-there    no echo, and the kernel's neighbour entry for the address
                     is still unresolved (or ping itself said the host is
                     down). Nothing has answered ARP: wrong address, wrong
                     adapter or VLAN, cable out, console off.
    no-answer        no echo, and the neighbour entry is resolved or could not
                     be read. Only that ping got no reply: the console may
                     ignore ICMP, or be gone while the box still holds its old
                     MAC. A resolved entry is never read as "something is
                     there" - an expired macOS entry keeps its MAC.
    could-not-check  ping missing, timed out, killed, unparseable, unknown
                     platform, any exception. Nothing is known.
    not-checked      no check has completed.

Why a ping and not an OSC message: the box's console socket never reads, the
spec does not say what a get would answer, and an OSC message to the console is
exactly what "faders only" rules out - one token away from a scene recall.

Why the neighbour table: on macOS a one-packet ping to an absent on-link
address never reports "Host is down" (ARP is retried only when there is a new
packet to send), so an unresolved neighbour entry after an unanswered ping is
the only honest "nothing there".

The second job is the keepalive. macOS drops an idle ARP entry after
`net.link.ether.inet.max_age` (20 minutes) and then holds the next packet for a
fresh ARP exchange; a snap open is one packet, so a lost exchange would sit
silent. A ping every few minutes keeps the entry warm. Linux uses a stale entry
at once, so there the check is the only point.

Standard library only, unprivileged (it runs the system `ping`, and on macOS
`arp -n`, as child processes), and outside the control path. It imports nothing
that can send to the console: not `socket`, not `tacet.osc`, `tacet.net` or
`tacet.dm7`, and no `importlib` or datagram/connection call to go round them.
`tests/test_dependency_policy.py` reads the source and fails if that changes.

While the last result is nothing-there or could-not-check the watch looks again
every `RECHECK_BAD_SECONDS`, so a fixed cable clears the warning promptly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class Platform(StrEnum):
    MACOS = "darwin"
    LINUX = "linux"


class Reach(StrEnum):
    NOT_CHECKED = "not-checked"
    ANSWERED = "answered"
    NO_ANSWER = "no-answer"
    NOTHING_THERE = "nothing-there"
    COULD_NOT_CHECK = "could-not-check"


class Trigger(StrEnum):
    STARTUP = "startup"
    ARM = "arm"
    KEEPALIVE = "keepalive"


class Neighbour(StrEnum):
    """What the kernel's neighbour table holds for the address."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


PING_EXECUTABLE = "ping"
ARP_EXECUTABLE = "arp"
#: Searched after $PATH: launchd and some shells leave sbin out, and macOS keeps
#: ping in /sbin and arp in /usr/sbin.
SYSTEM_DIRS = ("/sbin", "/usr/sbin", "/bin", "/usr/bin")
PROC_NET_ARP = Path("/proc/net/arp")

#: ping's own overall deadline, whole seconds (both platforms take an integer).
PING_DEADLINE_SECONDS = 2
#: The outer asyncio timeout that does not trust ping: a little longer than its own.
PING_GRACE_SECONDS = 1.0
PING_TIMEOUT_SECONDS = PING_DEADLINE_SECONDS + PING_GRACE_SECONDS
#: `arp -n <ip>` reads a table; it has no reason to take long.
ARP_TIMEOUT_SECONDS = 2.0

#: macOS `net.link.ether.inet.max_age` (sysctl, 2026-09-13 and 2026-10-05).
MACOS_ARP_MAX_AGE_SECONDS = 1200
#: How many keepalives fall inside one macOS ARP lifetime. A send after the
#: entry's link-layer reachability (~120 s) has lapsed makes macOS re-confirm
#: it, which resets max_age.
KEEPALIVES_PER_ARP_LIFETIME = 5
KEEPALIVE_SECONDS = MACOS_ARP_MAX_AGE_SECONDS // KEEPALIVES_PER_ARP_LIFETIME
#: While the last result is nothing-there or could-not-check, look again this
#: soon instead of waiting a whole keepalive. Those are the results that mean
#: "fix it": a cable put back or an address corrected should clear the red chip
#: within a moment of the fix, not four minutes later, and a quiet 30 s ping is
#: harmless. No-answer is not bad - the DM7 may simply ignore ping - so it
#: keeps the long interval. Always longer than one check can take.
RECHECK_BAD_SECONDS = 30
#: The results that earn the short interval.
BAD_RESULTS = frozenset({Reach.NOTHING_THERE, Reach.COULD_NOT_CHECK})
#: A check that came due while the fader was moving is retried this soon, not a
#: whole interval later: the arm check after an up-slow from STANDING DOWN (#89)
#: must not wait four minutes.
BUSY_RETRY_SECONDS = 1.0

#: Exit statuses that mean "sent, no reply": macOS ping(8) 2, iputils ping(8) 1.
#: Anything else non-zero is an error.
NO_REPLY_EXIT = {Platform.MACOS: 2, Platform.LINUX: 1}
#: Text a ping prints when the kernel or a router says nothing is there.
NOTHING_THERE_MARKERS = ("Host is down", "No route to host", "Destination Host Unreachable")
#: /proc/net/arp's Flags column: ATF_COM means resolved. Without it the entry is
#: incomplete or failed.
ATF_COM = 0x2
#: macOS `arp -n` for an unresolved entry.
BSD_INCOMPLETE = "(incomplete)"
BSD_NO_ENTRY = "-- no entry"
#: `/proc/net/arp`: IP, HW type, Flags, HW address, Mask, Device.
PROC_COLUMNS = 6
PROC_FLAGS_COLUMN = 2
PROC_MAC_COLUMN = 3
MS_PER_SECOND = 1000.0

#: The words. Pinned in tests and quoted in the docs; ASCII only.
ANSWERED = "answered ping"
NO_ANSWER = "did not answer ping"
NOTHING_THERE = "nothing at this address (no ARP reply)"
COULD_NOT_CHECK = "could not check"
NOT_CHECKED = "not checked yet"

# could-not-check details, and the notes that ride on the other results.
PING_NOT_FOUND = "ping is not installed or not on PATH"
UNSUPPORTED_PLATFORM = "no ping flags known for platform {platform!r}"
CHILD_TIMED_OUT = "{name} did not finish within {seconds:g}s and was stopped"
CHILD_COULD_NOT_RUN = "{name} could not run: {error}"
PING_FAILED = "ping failed (exit {code}): {detail}"
CHECK_FAILED = "the console check itself failed: {error}"
CHECK_STOPPED = "the console check stopped: {error}"
OFF_LINK = "not on a directly connected network"
NEIGHBOUR_UNRESOLVED = "the neighbour entry for this address is unresolved"


class CheckUnavailableError(Exception):
    """The child could not be run or did not finish. `str()` is the detail."""


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    async def run(self, argv: Sequence[str], *, timeout: float) -> ProcessOutput: ...


@dataclass(frozen=True)
class Tools:
    """What this machine has. Resolved once at startup, never at arm."""

    #: None: unsupported, so every check is could-not-check.
    platform: Platform | None
    #: Absolute path, or None.
    ping: str | None
    #: Absolute path, or None. macOS only; Linux reads /proc.
    arp: str | None


@dataclass(frozen=True)
class Check:
    reach: Reach
    trigger: Trigger | None
    host: str
    #: Monotonic, on the box's clock.
    at: float | None
    #: A could-not-check reason, or the ping/kernel line that decided it.
    detail: str | None = None
    rtt_ms: float | None = None
    #: Only when the neighbour table was read and held one. Forensic: never
    #: evidence that something is there.
    mac: str | None = None
    #: How long `create_subprocess_exec` held the loop (the fork cost).
    spawn_ms: float | None = None
    elapsed_ms: float | None = None

    def summary(self) -> str:
        """The banner row's text."""
        if self.reach is Reach.ANSWERED:
            return ANSWERED if self.rtt_ms is None else f"{ANSWERED} ({self.rtt_ms:.1f} ms)"
        if self.reach is Reach.NO_ANSWER:
            return NO_ANSWER
        if self.reach is Reach.NOTHING_THERE:
            return NOTHING_THERE
        if self.reach is Reach.COULD_NOT_CHECK:
            return COULD_NOT_CHECK if not self.detail else f"{COULD_NOT_CHECK}: {self.detail}"
        return NOT_CHECKED

    def as_data(self) -> dict[str, Any]:
        """The console-checked entry's data. Flat and JSON-safe."""
        return {
            "reach": str(self.reach),
            "trigger": None if self.trigger is None else str(self.trigger),
            "host": self.host,
            "detail": self.detail,
            "rtt_ms": self.rtt_ms,
            "mac": self.mac,
            "spawn_ms": self.spawn_ms,
            "elapsed_ms": self.elapsed_ms,
        }

    def as_snapshot(self) -> dict[str, Any]:
        """What the page needs. Not the host, which the page never shows."""
        return {
            "reach": str(self.reach),
            "detail": self.detail,
            "checked_at": self.at,
            "trigger": None if self.trigger is None else str(self.trigger),
        }

    @classmethod
    def not_checked(cls, host: str) -> Check:
        return cls(Reach.NOT_CHECKED, None, host, None)


# -- the pure core ----------------------------------------------------------


def platform_of(sys_platform: str) -> Platform | None:
    for platform in Platform:
        if sys_platform.startswith(platform.value):
            return platform
    return None


def find_tools(
    platform: Platform | None,
    *,
    which: Callable[..., str | None] = shutil.which,
    path: str | None = None,
) -> Tools:
    """Where ping (and, on macOS, arp) live. $PATH first, then the system dirs."""
    if platform is None:
        return Tools(platform=None, ping=None, arp=None)
    search = os.environ.get("PATH", "") if path is None else path

    def locate(name: str) -> str | None:
        return which(name, path=search) or which(name, path=os.pathsep.join(SYSTEM_DIRS))

    arp = locate(ARP_EXECUTABLE) if platform is Platform.MACOS else None
    return Tools(platform=platform, ping=locate(PING_EXECUTABLE), arp=arp)


def ping_command(tools: Tools, host: str, deadline: int = PING_DEADLINE_SECONDS) -> list[str] | None:
    """One packet, no reverse DNS, an overall deadline. Never `-W`: it is the
    per-reply wait, in milliseconds on macOS and seconds on Linux."""
    if tools.ping is None:
        return None
    if tools.platform is Platform.MACOS:
        return [tools.ping, "-c", "1", "-n", "-t", str(deadline), host]
    if tools.platform is Platform.LINUX:
        return [tools.ping, "-c", "1", "-n", "-w", str(deadline), host]
    return None


def arp_command(tools: Tools, host: str) -> list[str] | None:
    if tools.platform is Platform.MACOS and tools.arp is not None:
        return [tools.arp, "-n", host]
    return None


def parse_bsd_arp(returncode: int, stdout: str, host: str) -> tuple[Neighbour, str | None]:
    """macOS `arp -n <ip>`. The address is matched exactly inside its
    parentheses, so .12 never reads as .121."""
    quoted = re.escape(host)
    entry = re.search(rf"\({quoted}\) at (\(incomplete\)|[0-9A-Fa-f:]+)", stdout)
    if entry is not None:
        what = entry.group(1)
        if what == BSD_INCOMPLETE:
            return Neighbour.UNRESOLVED, None
        return Neighbour.RESOLVED, what
    if returncode != 0 and re.search(rf"\({quoted}\) {re.escape(BSD_NO_ENTRY)}", stdout):
        return Neighbour.ABSENT, None
    return Neighbour.UNREADABLE, None


def parse_proc_net_arp(text: str, host: str) -> tuple[Neighbour, str | None]:
    """Linux /proc/net/arp. Resolved on any interface wins."""
    lines = text.splitlines()[1:]
    if not lines:
        return Neighbour.UNREADABLE, None
    found: tuple[Neighbour, str | None] = (Neighbour.ABSENT, None)
    for line in lines:
        columns = line.split()
        if not columns or columns[0] != host:
            continue
        if len(columns) < PROC_COLUMNS:
            return Neighbour.UNREADABLE, None
        try:
            flags = int(columns[PROC_FLAGS_COLUMN], 16)
        except ValueError:
            return Neighbour.UNREADABLE, None
        if flags & ATF_COM:
            return Neighbour.RESOLVED, columns[PROC_MAC_COLUMN]
        found = (Neighbour.UNRESOLVED, None)
    return found


def parse_rtt(stdout: str) -> float | None:
    """The first `time=<float> ms`, which both platforms print."""
    found = re.search(r"time=([0-9.]+) ?ms", stdout)
    return None if found is None else float(found.group(1))


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _marker_line(output: ProcessOutput) -> str | None:
    for text in (_text(output.stderr), _text(output.stdout)):
        for line in text.splitlines():
            if any(marker in line for marker in NOTHING_THERE_MARKERS):
                return line.strip()
    return None


def classify(
    platform: Platform,
    output: ProcessOutput,
    neighbour: tuple[Neighbour, str | None] | None,
) -> tuple[Reach, str | None, str | None]:
    """`(reach, detail, mac)` from what ping and the neighbour table said."""
    if output.returncode == 0:
        return Reach.ANSWERED, None, None
    marker = _marker_line(output)
    if marker is not None:
        return Reach.NOTHING_THERE, marker, None
    if output.returncode == NO_REPLY_EXIT[platform]:
        if neighbour is None:
            return Reach.NO_ANSWER, None, None
        kind, mac = neighbour
        if kind is Neighbour.UNRESOLVED:
            return Reach.NOTHING_THERE, NEIGHBOUR_UNRESOLVED, None
        if kind is Neighbour.RESOLVED:
            return Reach.NO_ANSWER, None, mac
        if kind is Neighbour.ABSENT:
            return Reach.NO_ANSWER, OFF_LINK, None
        return Reach.NO_ANSWER, None, None
    detail = _first_line(_text(output.stderr)) or _first_line(_text(output.stdout))
    return Reach.COULD_NOT_CHECK, PING_FAILED.format(code=output.returncode, detail=detail), None


# -- the I/O shell ----------------------------------------------------------


class AsyncioRunner:
    """Runs a child off the event loop's thread, and never leaves one behind.

    A timeout and a cancellation both end the same way: the child is killed and
    reaped, so neither leaves a live process or a zombie.
    """

    def __init__(self) -> None:
        self.last_pid: int | None = None
        #: Time around the spawn: fork and exec happen on the loop's thread
        #: before it returns. Logged so Phase 2 can see what the fork costs.
        self.last_spawn_ms: float | None = None

    async def run(self, argv: Sequence[str], *, timeout: float) -> ProcessOutput:
        name = Path(argv[0]).name
        began = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as exc:
            raise CheckUnavailableError(CHILD_COULD_NOT_RUN.format(name=name, error=exc)) from exc
        self.last_spawn_ms = (time.perf_counter() - began) * MS_PER_SECOND
        self.last_pid = proc.pid
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await proc.communicate()
        except TimeoutError as exc:
            raise CheckUnavailableError(CHILD_TIMED_OUT.format(name=name, seconds=timeout)) from exc
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        return ProcessOutput(-1 if proc.returncode is None else proc.returncode, stdout, stderr)


def _read_proc_net_arp() -> str:
    return PROC_NET_ARP.read_text(encoding="ascii", errors="replace")


async def _neighbour(
    host: str,
    tools: Tools,
    runner: ProcessRunner,
    read_proc_arp: Callable[[], str],
) -> tuple[Neighbour, str | None] | None:
    """The kernel's entry for the address, or None. Never raises, and never
    turns a result into could-not-check: a failure here only means less is known."""
    try:
        if tools.platform is Platform.MACOS:
            argv = arp_command(tools, host)
            if argv is None:
                return None
            output = await runner.run(argv, timeout=ARP_TIMEOUT_SECONDS)
            return parse_bsd_arp(output.returncode, _text(output.stdout), host)
        if tools.platform is Platform.LINUX:
            return parse_proc_net_arp(read_proc_arp(), host)
    except Exception:
        return None
    return None


async def _ping_and_classify(
    host: str,
    tools: Tools,
    trigger: Trigger,
    runner: ProcessRunner,
    monotonic: Callable[[], float],
    read_proc_arp: Callable[[], str],
    began: float,
) -> Check:
    argv = ping_command(tools, host)
    if argv is None or tools.platform is None:
        missing = PING_NOT_FOUND if tools.platform is not None else UNSUPPORTED_PLATFORM.format(platform=sys.platform)
        return Check(Reach.COULD_NOT_CHECK, trigger, host, monotonic(), detail=missing)
    output = await runner.run(argv, timeout=PING_TIMEOUT_SECONDS)
    spawn_ms: float | None = getattr(runner, "last_spawn_ms", None)
    neighbour = None
    if output.returncode == NO_REPLY_EXIT[tools.platform] and _marker_line(output) is None:
        neighbour = await _neighbour(host, tools, runner, read_proc_arp)
    found, detail, mac = classify(tools.platform, output, neighbour)
    now = monotonic()
    return Check(
        found,
        trigger,
        host,
        now,
        detail=detail,
        rtt_ms=parse_rtt(_text(output.stdout)),
        mac=mac,
        spawn_ms=spawn_ms,
        elapsed_ms=(now - began) * MS_PER_SECOND,
    )


async def check(
    host: str,
    *,
    tools: Tools,
    trigger: Trigger,
    runner: ProcessRunner,
    monotonic: Callable[[], float] = time.monotonic,
    read_proc_arp: Callable[[], str] = _read_proc_net_arp,
) -> Check:
    """One check. Never raises, except to pass a cancellation on."""
    began = monotonic()
    try:
        return await _ping_and_classify(host, tools, trigger, runner, monotonic, read_proc_arp, began)
    except CheckUnavailableError as exc:
        return Check(Reach.COULD_NOT_CHECK, trigger, host, monotonic(), detail=str(exc))
    except Exception as exc:
        failed = CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}")
        return Check(Reach.COULD_NOT_CHECK, trigger, host, monotonic(), detail=failed)


def probe(
    host: str,
    tools: Tools,
    *,
    runner: ProcessRunner | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Check:
    """Once, at startup, before the main loop exists (#41). Never raises.

    Its own short `asyncio.run`, so startup and runtime share one code path.
    Called only from `serve.main`.
    """
    coroutine = check(
        host,
        tools=tools,
        trigger=Trigger.STARTUP,
        runner=AsyncioRunner() if runner is None else runner,
        monotonic=monotonic,
    )
    try:
        return asyncio.run(coroutine)
    except Exception as exc:
        coroutine.close()
        failed = CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}")
        return Check(Reach.COULD_NOT_CHECK, Trigger.STARTUP, host, monotonic(), detail=failed)


class Watch:
    """Runs a check when asked, and every `interval` seconds, for the life of the box.

    One task, so two checks can never overlap. A check is never started while
    the fader is moving: it is put off and tried again shortly. It never holds
    a reference to anything that can send to the console, and never raises.
    """

    def __init__(
        self,
        host: str,
        tools: Tools,
        *,
        runner: ProcessRunner,
        busy: Callable[[], bool],
        on_result: Callable[[Check], None],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = KEEPALIVE_SECONDS,
        busy_retry: float = BUSY_RETRY_SECONDS,
        first_due: float | None = None,
    ) -> None:
        self._host = host
        self._tools = tools
        self._runner = runner
        self._busy = busy
        self._on_result = on_result
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval = interval
        self._busy_retry = busy_retry
        self._due_at = monotonic() if first_due is None else first_due
        self._pending = Trigger.KEEPALIVE
        self._running = False
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._running

    def request(self, trigger: Trigger) -> None:
        """Due now. A no-op while a check is running: that check's result serves."""
        if self._running:
            return
        self._pending = trigger
        self._due_at = self._monotonic()
        self._wake.set()

    async def run(self) -> None:
        """Forever; cancelled at shutdown."""
        while True:
            try:
                await self._wait_until_due()
                if self._busy():
                    self._due_at = self._monotonic() + self._busy_retry
                    continue
                await self._check_once()
            except Exception as exc:
                self._deliver(self._failure(exc))
                self._due_at = self._monotonic() + self._busy_retry

    def _failure(self, exc: Exception) -> Check:
        failed = CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}")
        return Check(Reach.COULD_NOT_CHECK, self._pending, self._host, self._monotonic(), detail=failed)

    def _deliver(self, result: Check) -> None:
        """A callback that raises is a missed update, never a dead watch."""
        with contextlib.suppress(Exception):
            self._on_result(result)

    async def _wait_until_due(self) -> None:
        while self._due_at - self._monotonic() > 0:
            waiter = asyncio.ensure_future(self._wake.wait())
            sleeper = asyncio.ensure_future(self._sleep(self._due_at - self._monotonic()))
            try:
                await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                sleeper.cancel()
            self._wake.clear()

    async def _check_once(self) -> None:
        trigger = self._pending
        self._running = True
        try:
            result = await check(
                self._host,
                tools=self._tools,
                trigger=trigger,
                runner=self._runner,
                monotonic=self._monotonic,
            )
        finally:
            self._running = False
        self._pending = Trigger.KEEPALIVE
        wait = RECHECK_BAD_SECONDS if result.reach in BAD_RESULTS else self._interval
        self._due_at = self._monotonic() + wait
        self._deliver(result)
