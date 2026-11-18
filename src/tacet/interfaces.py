"""Are the box's network adapters there, with a link and an address? (#234)

The box reaches the world through USB adapters: Dante (the audio), the control
VLAN (the console, the page) and the scoreboard's RTD stream. A pulled USB
plug or a dead cable leaves nothing on the page that says why Reaper recorded
nothing or the console stopped hearing. This module reads the laptop's own
adapter table and names what is wrong, by role. It observes only: it moves no
fader, sends nothing to the console or to Reaper, and opens no socket.

A role is identified by `(MAC, VLAN tag)`, never by the interface's name. macOS
renumbers USB adapters (`en7` becomes `en9` after a replug), so a name is not a
site fact, but a MAC is. A VLAN interface shares its parent's MAC: scoring is a
VLAN on the control adapter, and macOS lists it as `vlan0` with `en7`'s MAC. So
`{mac, vlan}` means "the VLAN interface with that tag on that adapter, whatever
it is called", and a bare MAC means the untagged adapter itself.

    present      the adapter (or VLAN interface) is there, its link is up, and
                 - for control and scoring - it has a real IPv4 address. A
                 169.254/16 address is the OS giving up on DHCP, not an address
                 anyone configured, so it does not count. Dante needs no
                 address: present with a link is enough.
    missing      nothing with that MAC (and tag) is on this laptop.
    link-down    the adapter is there, but its cable or switch port is dead. A
                 VLAN interface counts as up only while its parent's link is
                 up too.
    no-address   link up, but no usable IPv4 address.
    no-vlan      the parent adapter is there, but no VLAN interface with that
                 tag.

What it cannot see: an adapter that is enumerated, reports `status: active` and
is electrically dead. TAKE STOPPED (#233) and the ping chip (#73) remain the
backstops for that.

A read that fails is *could not check* and never reads as present. A role
raises a fault on the edge from present (or never seen) to not present, judged
against the last successful reading; a read that could not check neither
raises nor resets that edge.

Standard library only, unprivileged (it runs `ifconfig -a` on macOS and
`ip -d -j addr show` on Linux as a child process, through `tacet.reach`'s
runner), and outside the control path. It imports nothing that can send to the
console: not `socket`, not `tacet.osc`, `tacet.net`, `tacet.dm7`,
`tacet.state` or `tacet.targets`. `tests/test_dependency_policy.py` reads the
source and fails if that changes. It must not import `tacet.config`, which
imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from . import faults, reach


class Role(StrEnum):
    DANTE = "dante"
    CONTROL = "control"
    SCORING = "scoring"


class Status(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    LINK_DOWN = "link-down"
    NO_ADDRESS = "no-address"
    NO_VLAN = "no-vlan"


class Trigger(StrEnum):
    STARTUP = "startup"
    POLL = "poll"


#: Roles that need a real IPv4 address to be of use. Dante is audio, not IP.
NEEDS_IPV4: frozenset[Role] = frozenset({Role.CONTROL, Role.SCORING})

#: How the roles are named to the operator.
ROLE_LABELS: Mapping[Role, str] = {
    Role.DANTE: "Dante",
    Role.CONTROL: "control VLAN",
    Role.SCORING: "scoreboard (RTD)",
}

FAULT_KINDS: Mapping[Role, faults.FaultKind] = {
    Role.DANTE: faults.FaultKind.INTERFACE_DANTE,
    Role.CONTROL: faults.FaultKind.INTERFACE_CONTROL,
    Role.SCORING: faults.FaultKind.INTERFACE_SCORING,
}


def role_for(kind: faults.FaultKind) -> Role | None:
    """The role a fault kind belongs to, or None for a kind that is not an adapter's."""
    for role, each in FAULT_KINDS.items():
        if each is kind:
            return role
    return None


#: A read every few seconds. Longer than one read can take, so two never overlap.
POLL_SECONDS = 5.0
#: `ifconfig -a` reads a table; it has no reason to take long.
READ_TIMEOUT_SECONDS = 2.0

IFCONFIG_EXECUTABLE = "ifconfig"
IFCONFIG_ARGS = ("-a",)
IP_EXECUTABLE = "ip"
IP_ARGS = ("-d", "-j", "addr", "show")

#: A MAC is six octets of one or two hex digits each.
MAC_OCTETS = 6
MAC_OCTET = re.compile(r"[0-9a-fA-F]{1,2}")
MAC_SEPARATORS = re.compile(r"[:-]")
MAC_JOINER = ":"
#: 802.1Q: a tag is 12 bits, and 0 and 4095 are reserved.
VLAN_MIN = 1
VLAN_MAX = (1 << 12) - 2

STATUS_ACTIVE = "active"
FLAG_UP = "UP"
FLAG_RUNNING = "RUNNING"
#: iproute2's name for "the cable has a carrier".
FLAG_CARRIER = "LOWER_UP"
FAMILY_IPV4 = "inet"
KIND_VLAN = "vlan"
KIND_BRIDGE = "bridge"
NO_PARENT = "<none>"

HEADER = re.compile(r"^(\S+): flags=\w+<([^>]*)>")
ETHER_LINE = re.compile(r"^\tether (\S+)")
INET_LINE = re.compile(r"^\tinet (\d+\.\d+\.\d+\.\d+)")
STATUS_LINE = re.compile(r"^\tstatus: (\S+)")
VLAN_LINE = re.compile(r"^\tvlan: (\d+) parent interface: (\S+)")
BRIDGE_LINE = re.compile(r"^\t(?:Configuration:|member: )")

MS_PER_SECOND = 1000.0

#: The words. Pinned in tests and quoted in the docs; ASCII only.
FAULT_MISSING = "Network adapter missing: the {label} adapter ({mac}) is not on this laptop. {advice}"
FAULT_LINK_DOWN = (
    "Network adapter has no link: the {label} adapter ({name}, {mac}) is plugged in, "
    "but its cable or switch port is dead. {advice}"
)
FAULT_NO_ADDRESS = (
    "Network adapter has no address: the {label} adapter ({name}, {mac}) has a link "
    "but no IPv4 address. Check it goes to the right switch, and System Settings > Network. {advice}"
)
FAULT_NO_VLAN = (
    "Network adapter has no VLAN interface: the {label} role needs VLAN {vlan} on {name} ({mac}), "
    "and there is none. Recreate it in System Settings > Network, Manage Virtual Interfaces. {advice}"
)
#: Only true of a physical failure, so only MISSING and LINK_DOWN carry it.
SHARED_CABLE = "Same adapter and cable as the {other} role: if that role's banner is up too, both have this one cause."
SHARED_CABLE_STATUSES = frozenset({Status.MISSING, Status.LINK_DOWN})
ADVICE: Mapping[Role, str] = {
    Role.DANTE: (
        "Check its USB plug and its cable to the Dante switch. Without it Reaper records nothing: "
        "once it is back, stop Reaper's transport, press OK in Settings > Audio > Device, "
        "and start a new take."
    ),
    Role.CONTROL: (
        "Check its USB plug and its cable to the control switch. The console and the page go through it; "
        "ride the fader from StageMix meanwhile."
    ),
    Role.SCORING: "Check its USB plug and its cable. Only the game data is affected.",
}

#: How a bad role reads in the banner rows.
STATUS_WORDS: Mapping[Status, str] = {
    Status.MISSING: "MISSING",
    Status.LINK_DOWN: "no link",
    Status.NO_ADDRESS: "no IPv4 address",
    Status.NO_VLAN: "no VLAN interface on {name}",
}
NO_IPV4_DANTE = "no IPv4 address (not needed for Dante)"

# could-not-check details.
TOOL_NOT_FOUND = "{name} is not installed or not on PATH"
UNSUPPORTED_PLATFORM = "no adapter command known for platform {platform!r}"
READ_FAILED = "{name} failed (exit {code}): {detail}"
NO_ADAPTERS = "{name} listed no adapters with a MAC address"
UNREADABLE = "{name} output could not be read: {error}"
CHECK_FAILED = "the adapter check itself failed: {error}"
WATCH_STOPPED = "the adapter check stopped: {error}"
COULD_NOT_CHECK = "could not check"


class ParseError(Exception):
    """The adapter listing is not what the parser knows. `str()` is the detail."""


class ExpectedError(Exception):
    """A configured adapter that is not one. `str()` completes "<key> ...";
    the config module puts the file and the key in front of it."""


# -- what the config says a role is -----------------------------------------


@dataclass(frozen=True)
class Expected:
    """One role's adapter: a MAC, and the VLAN tag when it is a tagged interface."""

    mac: str
    vlan: int | None = None

    def describe(self) -> str:
        return self.mac if self.vlan is None else f"{self.mac} vlan {self.vlan}"


def canonical_mac(text: str) -> str | None:
    """Lowercase, zero-padded, colon-separated; None for anything else.

    `ifconfig` pads its octets and `arp` does not, and a person types either
    with `-` or `:`, so every spelling is brought to one before comparing.
    """
    parts = MAC_SEPARATORS.split(text.strip())
    if len(parts) != MAC_OCTETS or not all(MAC_OCTET.fullmatch(part) for part in parts):
        return None
    return MAC_JOINER.join(f"{int(part, 16):02x}" for part in parts)


#: The subkeys of a role written as a table.
EXPECTED_KEYS = ("mac", "vlan")
BAD_MAC = "must be a MAC address like 00:1c:42:0a:0b:0c, got {value!r}"
BAD_VLAN = f"vlan must be a whole number from {VLAN_MIN} to {VLAN_MAX}, got {{value!r}}"
NEEDS_MAC = "must give mac = ... when it is a table, got {value!r}"
UNKNOWN_SUBKEY = "has no key {key!r}. Known keys: {known}"
BAD_SHAPE = "must be a MAC address string or a table {{ mac = ..., vlan = ... }}, got {value!r}"


def expected_from_config(value: object) -> Expected:
    """A MAC string, or a table with `mac` and an optional `vlan`. Raises
    `ExpectedError` with what is wrong."""
    if isinstance(value, str):
        return Expected(mac=_mac_or_raise(value))
    if not isinstance(value, Mapping):
        raise ExpectedError(BAD_SHAPE.format(value=value))
    for key in value:
        if key not in EXPECTED_KEYS:
            raise ExpectedError(UNKNOWN_SUBKEY.format(key=key, known=", ".join(EXPECTED_KEYS)))
    if "mac" not in value:
        raise ExpectedError(NEEDS_MAC.format(value=dict(value)))
    mac = _mac_or_raise(value["mac"])
    tag = value.get("vlan")
    if "vlan" not in value:
        return Expected(mac=mac)
    # A bool is an int in Python, and `vlan = true` is a typo worth refusing.
    if isinstance(tag, bool) or not isinstance(tag, int) or not VLAN_MIN <= tag <= VLAN_MAX:
        raise ExpectedError(BAD_VLAN.format(value=tag))
    return Expected(mac=mac, vlan=tag)


def _mac_or_raise(value: object) -> str:
    mac = canonical_mac(value) if isinstance(value, str) else None
    if mac is None:
        raise ExpectedError(BAD_MAC.format(value=value))
    return mac


# -- what the laptop says it has --------------------------------------------


@dataclass(frozen=True)
class Adapter:
    name: str
    mac: str
    #: The administrative UP flag.
    up: bool
    #: macOS `status: active`, or Linux `LOWER_UP`: a carrier is there.
    active: bool
    ipv4: tuple[str, ...]
    bridge: bool = False
    vlan: int | None = None
    parent: str | None = None

    @property
    def link_up(self) -> bool:
        return self.up and self.active


def usable_ipv4(address: str) -> bool:
    """A real address, not the 169.254/16 the OS gives itself when DHCP fails."""
    try:
        return not ipaddress.IPv4Address(address).is_link_local
    except ValueError:
        return False


def _first_address(addresses: tuple[str, ...]) -> str | None:
    for address in addresses:
        if usable_ipv4(address):
            return address
    return addresses[0] if addresses else None


def parse_ifconfig(text: str) -> tuple[Adapter, ...]:
    """macOS `ifconfig -a`. Only lines that start with one tab are read, so the
    bridge's `Configuration:` block (`id <mac>` under two tabs) is never taken
    for an adapter's address. `RUNNING` is not a link on macOS: `en1` is
    RUNNING with `status: inactive`, so the link comes from the status line."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if HEADER.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    if not blocks:
        raise ParseError("no interface headers found")
    adapters = [adapter for block in blocks if (adapter := _ifconfig_block(block)) is not None]
    return tuple(adapters)


def _ifconfig_block(lines: list[str]) -> Adapter | None:
    header = HEADER.match(lines[0])
    assert header is not None
    name = header.group(1)
    flags = header.group(2).split(",")
    mac: str | None = None
    ipv4: list[str] = []
    status: str | None = None
    bridge = False
    vlan: int | None = None
    parent: str | None = None
    for line in lines[1:]:
        if (found := ETHER_LINE.match(line)) is not None:
            mac = canonical_mac(found.group(1))
        elif (found := INET_LINE.match(line)) is not None:
            ipv4.append(found.group(1))
        elif (found := STATUS_LINE.match(line)) is not None:
            status = found.group(1)
        elif (found := VLAN_LINE.match(line)) is not None:
            vlan = int(found.group(1))
            parent = None if found.group(2) == NO_PARENT else found.group(2)
        elif BRIDGE_LINE.match(line):
            bridge = True
    if mac is None:
        return None
    active = status == STATUS_ACTIVE if status is not None else FLAG_RUNNING in flags
    return Adapter(
        name=name,
        mac=mac,
        up=FLAG_UP in flags,
        active=active,
        ipv4=tuple(ipv4),
        bridge=bridge,
        vlan=vlan,
        parent=parent,
    )


def parse_ip_json(text: str) -> tuple[Adapter, ...]:
    """Linux `ip -d -j addr show`."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ParseError("expected a list of interfaces")
    adapters: list[Adapter] = []
    for item in data:
        if not isinstance(item, dict):
            raise ParseError("expected each interface to be an object")
        adapter = _ip_item(item)
        if adapter is not None:
            adapters.append(adapter)
    return tuple(adapters)


def _ip_item(item: dict[str, Any]) -> Adapter | None:
    name = item.get("ifname")
    address = item.get("address")
    if not isinstance(name, str):
        raise ParseError("an interface has no name")
    mac = canonical_mac(address) if isinstance(address, str) else None
    if mac is None:
        return None
    raw_flags = item.get("flags")
    flags = [flag for flag in raw_flags if isinstance(flag, str)] if isinstance(raw_flags, list) else []
    raw_info = item.get("addr_info")
    infos = [info for info in raw_info if isinstance(info, dict)] if isinstance(raw_info, list) else []
    ipv4 = tuple(
        info["local"] for info in infos if info.get("family") == FAMILY_IPV4 and isinstance(info.get("local"), str)
    )
    linkinfo = item.get("linkinfo")
    kind = linkinfo.get("info_kind") if isinstance(linkinfo, dict) else None
    vlan: int | None = None
    parent: str | None = None
    if kind == KIND_VLAN and isinstance(linkinfo, dict):
        data = linkinfo.get("info_data")
        tag = data.get("id") if isinstance(data, dict) else None
        vlan = tag if isinstance(tag, int) and not isinstance(tag, bool) else None
        link = item.get("link")
        parent = link if isinstance(link, str) else None
    return Adapter(
        name=name,
        mac=mac,
        up=FLAG_UP in flags,
        active=FLAG_CARRIER in flags,
        ipv4=ipv4,
        bridge=kind == KIND_BRIDGE,
        vlan=vlan,
        parent=parent,
    )


# -- finding a role among the adapters --------------------------------------


@dataclass(frozen=True)
class Found:
    role: Role
    mac: str
    vlan: int | None
    status: Status
    #: The interface it was found as, which can change between replugs.
    name: str | None
    ipv4: tuple[str, ...] = ()

    @property
    def address(self) -> str | None:
        """The first usable IPv4 address, else the first, else None."""
        return _first_address(self.ipv4)

    def as_data(self) -> dict[str, Any]:
        """Flat and JSON-safe: the fault's data, the snapshot's role entry."""
        return {
            "role": self.role.value,
            "mac": self.mac,
            "vlan": self.vlan,
            "status": self.status.value,
            "name": self.name,
            "ipv4": self.address,
        }


#: Best first. A role with a candidate in each state is reported as the best.
_RANK: Mapping[Status, int] = {Status.PRESENT: 0, Status.NO_ADDRESS: 1, Status.LINK_DOWN: 2}


def _judge(role: Role, candidate: Adapter, adapters: tuple[Adapter, ...]) -> Status:
    link_up = candidate.link_up
    if candidate.vlan is not None:
        parent = next((a for a in adapters if a.name == candidate.parent), None)
        link_up = link_up and parent is not None and parent.link_up
    if not link_up:
        return Status.LINK_DOWN
    if role in NEEDS_IPV4 and not any(usable_ipv4(address) for address in candidate.ipv4):
        return Status.NO_ADDRESS
    return Status.PRESENT


def find(role: Role, expected: Expected, adapters: tuple[Adapter, ...]) -> Found:
    """Where `role`'s adapter is among `adapters`, by MAC and VLAN tag."""
    same_mac = [a for a in adapters if a.mac == expected.mac]
    if expected.vlan is None:
        candidates = [a for a in same_mac if a.vlan is None and not a.bridge]
    else:
        candidates = [a for a in same_mac if a.vlan == expected.vlan]
    if not candidates:
        parents = [a for a in same_mac if a.vlan is None and not a.bridge]
        if expected.vlan is not None and parents:
            return Found(role, expected.mac, expected.vlan, Status.NO_VLAN, parents[0].name)
        return Found(role, expected.mac, expected.vlan, Status.MISSING, None)
    judged = [(_judge(role, candidate, adapters), index, candidate) for index, candidate in enumerate(candidates)]
    status, _, best = min(judged, key=lambda item: (_RANK[item[0]], item[1]))
    return Found(role, expected.mac, expected.vlan, status, best.name, best.ipv4)


def assess(expected: Mapping[Role, Expected], adapters: tuple[Adapter, ...]) -> tuple[Found, ...]:
    """Every configured role, in Role order."""
    return tuple(find(role, expected[role], adapters) for role in Role if role in expected)


def fault_message(found: Found, *, shares_with: Role | None) -> str:
    """What the banner says, fixed when the fault is raised. ASCII."""
    label = ROLE_LABELS[found.role]
    advice = ADVICE[found.role]
    mac = found.mac if found.vlan is None else f"{found.mac}, VLAN {found.vlan}"
    name = found.name or ""
    if found.status is Status.MISSING:
        text = FAULT_MISSING.format(label=label, mac=mac, advice=advice)
    elif found.status is Status.LINK_DOWN:
        text = FAULT_LINK_DOWN.format(label=label, name=name, mac=mac, advice=advice)
    elif found.status is Status.NO_ADDRESS:
        text = FAULT_NO_ADDRESS.format(label=label, name=name, mac=mac, advice=advice)
    elif found.status is Status.NO_VLAN:
        text = FAULT_NO_VLAN.format(label=label, vlan=found.vlan, name=name, mac=found.mac, advice=advice)
    else:
        raise ValueError(f"{found.role.value} is {found.status.value}: there is nothing to say")
    if shares_with is not None and found.status in SHARED_CABLE_STATUSES:
        text = f"{text} {SHARED_CABLE.format(other=ROLE_LABELS[shares_with])}"
    return text


def status_word(found: Found) -> str:
    """The short words the banner rows use for a role that is not present."""
    return STATUS_WORDS[found.status].format(name=found.name)


# -- the readings, and what changed between them ----------------------------


@dataclass(frozen=True)
class Reading:
    trigger: Trigger
    #: Monotonic, on the box's clock.
    at: float
    #: None when the adapters could not be read; `detail` then says why.
    adapters: tuple[Adapter, ...] | None
    detail: str | None = None
    #: How long the runner held the loop to start the child (the fork cost).
    spawn_ms: float | None = None
    elapsed_ms: float | None = None


@dataclass(frozen=True)
class Observation:
    #: Roles that went from present (or never seen) to not present, in Role order.
    went_bad: tuple[Found, ...]
    #: Whether anything the page or the log shows moved.
    changed: bool


class CheckState(StrEnum):
    NOT_CONFIGURED = "not-configured"
    NOT_CHECKED = "not-checked"
    COULD_NOT_CHECK = "could-not-check"
    CHECKED = "checked"


def _could_not_state(reading: Reading | None) -> str | None:
    return None if reading is None or reading.adapters is not None else (reading.detail or COULD_NOT_CHECK)


@dataclass(frozen=True)
class Tracker:
    """What the box last knew of its adapters. Immutable: `observe` returns the next."""

    expected: Mapping[Role, Expected]
    #: The last successful assessment. Empty until one has been made.
    found: tuple[Found, ...] = ()
    #: The last reading of any kind.
    last: Reading | None = None

    def observe(self, reading: Reading) -> tuple[Tracker, Observation]:
        could_not_before = _could_not_state(self.last)
        could_not_now = _could_not_state(reading)
        if reading.adapters is None:
            return replace(self, last=reading), Observation((), could_not_before != could_not_now)
        found = assess(self.expected, reading.adapters)
        before = {f.role: f for f in self.found}
        went_bad = tuple(
            f
            for f in found
            if f.status is not Status.PRESENT and (f.role not in before or before[f.role].status is Status.PRESENT)
        )
        changed = found != self.found or could_not_before != could_not_now
        return replace(self, found=found, last=reading), Observation(went_bad, changed)

    def bad(self, role: Role) -> bool:
        """Whether the last successful reading found `role` not present. False
        for a role never read."""
        return any(f.role is role and f.status is not Status.PRESENT for f in self.found)

    def shares_with(self, role: Role) -> Role | None:
        """Another configured role on the same MAC: one adapter, one cable."""
        mine = self.expected.get(role)
        if mine is None:
            return None
        for other in Role:
            theirs = self.expected.get(other)
            if other is not role and theirs is not None and theirs.mac == mine.mac:
                return other
        return None

    @property
    def state(self) -> CheckState:
        if not self.expected:
            return CheckState.NOT_CONFIGURED
        if self.last is None:
            return CheckState.NOT_CHECKED
        if self.last.adapters is None:
            return CheckState.COULD_NOT_CHECK
        return CheckState.CHECKED

    def as_snapshot(self) -> dict[str, Any]:
        """What the page needs. On could-not-check the roles keep their last known values."""
        return {
            "status": self.state.value,
            "detail": None if self.last is None else self.last.detail,
            "checked_at": None if self.last is None else self.last.at,
            "roles": [f.as_data() for f in self.found],
        }

    def as_data(self) -> dict[str, Any]:
        """The interfaces-checked entry's data. Flat and JSON-safe."""
        last = self.last
        return {
            "trigger": None if last is None else last.trigger.value,
            "detail": None if last is None else last.detail,
            "roles": [f.as_data() for f in self.found],
            "spawn_ms": None if last is None else last.spawn_ms,
            "elapsed_ms": None if last is None else last.elapsed_ms,
        }


# -- the I/O shell ----------------------------------------------------------


@dataclass(frozen=True)
class Tools:
    """What this machine has. Resolved once at startup."""

    #: None: unsupported, so every read is could-not-check.
    platform: reach.Platform | None
    #: Absolute paths, or None. macOS reads ifconfig; Linux reads ip.
    ifconfig: str | None
    ip: str | None


def find_tools(
    platform: reach.Platform | None,
    *,
    which: Callable[..., str | None] = shutil.which,
    path: str | None = None,
) -> Tools:
    """Where ifconfig (macOS) or ip (Linux) lives. $PATH first, then the system dirs."""
    if platform is None:
        return Tools(platform=None, ifconfig=None, ip=None)
    search = os.environ.get("PATH", "") if path is None else path

    def locate(name: str) -> str | None:
        return which(name, path=search) or which(name, path=os.pathsep.join(reach.SYSTEM_DIRS))

    if platform is reach.Platform.MACOS:
        return Tools(platform=platform, ifconfig=locate(IFCONFIG_EXECUTABLE), ip=None)
    return Tools(platform=platform, ifconfig=None, ip=locate(IP_EXECUTABLE))


def command(tools: Tools) -> list[str] | None:
    if tools.platform is reach.Platform.MACOS and tools.ifconfig is not None:
        return [tools.ifconfig, *IFCONFIG_ARGS]
    if tools.platform is reach.Platform.LINUX and tools.ip is not None:
        return [tools.ip, *IP_ARGS]
    return None


def _tool_name(tools: Tools) -> str:
    return IFCONFIG_EXECUTABLE if tools.platform is reach.Platform.MACOS else IP_EXECUTABLE


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _could_not(
    trigger: Trigger,
    now: float,
    detail: str,
    spawn_ms: float | None = None,
    elapsed_ms: float | None = None,
) -> Reading:
    return Reading(trigger, now, None, detail=detail, spawn_ms=spawn_ms, elapsed_ms=elapsed_ms)


async def _read_adapters(
    tools: Tools, trigger: Trigger, runner: reach.ProcessRunner, monotonic: Callable[[], float], began: float
) -> Reading:
    if tools.platform is None:
        return _could_not(trigger, monotonic(), UNSUPPORTED_PLATFORM.format(platform=sys.platform))
    name = _tool_name(tools)
    argv = command(tools)
    if argv is None:
        return _could_not(trigger, monotonic(), TOOL_NOT_FOUND.format(name=name))
    output = await runner.run(argv, timeout=READ_TIMEOUT_SECONDS)
    spawn_ms: float | None = getattr(runner, "last_spawn_ms", None)
    now = monotonic()
    elapsed_ms = (now - began) * MS_PER_SECOND
    if output.returncode != 0:
        detail = _first_line(_text(output.stderr)) or _first_line(_text(output.stdout))
        return _could_not(
            trigger, now, READ_FAILED.format(name=name, code=output.returncode, detail=detail), spawn_ms, elapsed_ms
        )
    parse = parse_ifconfig if tools.platform is reach.Platform.MACOS else parse_ip_json
    try:
        adapters = parse(_text(output.stdout))
    except ParseError as exc:
        return _could_not(trigger, now, UNREADABLE.format(name=name, error=exc), spawn_ms, elapsed_ms)
    if not adapters:
        return _could_not(trigger, now, NO_ADAPTERS.format(name=name), spawn_ms, elapsed_ms)
    return Reading(trigger, now, adapters, spawn_ms=spawn_ms, elapsed_ms=elapsed_ms)


async def read(
    tools: Tools,
    *,
    trigger: Trigger,
    runner: reach.ProcessRunner,
    monotonic: Callable[[], float] = time.monotonic,
) -> Reading:
    """One reading. Never raises, except to pass a cancellation on."""
    began = monotonic()
    try:
        return await _read_adapters(tools, trigger, runner, monotonic, began)
    except reach.CheckUnavailableError as exc:
        return _could_not(trigger, monotonic(), str(exc))
    except Exception as exc:
        return _could_not(trigger, monotonic(), CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}"))


def startup_reading(
    tools: Tools,
    *,
    runner: reach.ProcessRunner | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Reading:
    """Once, at startup, before the main loop exists (#41). Never raises.

    Its own short `asyncio.run`, so startup and runtime share one code path.
    """
    coroutine = read(
        tools,
        trigger=Trigger.STARTUP,
        runner=reach.AsyncioRunner() if runner is None else runner,
        monotonic=monotonic,
    )
    try:
        return asyncio.run(coroutine)
    except Exception as exc:
        coroutine.close()
        failed = CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}")
        return _could_not(Trigger.STARTUP, monotonic(), failed)


class Watch:
    """Reads the adapters every `interval` seconds for the life of the box.

    One task, so two reads can never overlap. A read is never started while the
    fader is moving: it is put off and tried again shortly. It holds nothing
    that can send to the console, and never raises.
    """

    def __init__(
        self,
        tools: Tools,
        *,
        runner: reach.ProcessRunner,
        busy: Callable[[], bool],
        on_result: Callable[[Reading], None],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = POLL_SECONDS,
        busy_retry: float = reach.BUSY_RETRY_SECONDS,
    ) -> None:
        self._tools = tools
        self._runner = runner
        self._busy = busy
        self._on_result = on_result
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval = interval
        self._busy_retry = busy_retry

    async def run(self) -> None:
        """Forever; cancelled at shutdown. The startup reading came first, so
        the first poll is one interval away."""
        delay = self._interval
        while True:
            await self._sleep(delay)
            delay = self._interval
            try:
                if self._busy():
                    delay = self._busy_retry
                    continue
                result = await read(self._tools, trigger=Trigger.POLL, runner=self._runner, monotonic=self._monotonic)
            except Exception as exc:
                failed = CHECK_FAILED.format(error=f"{type(exc).__name__}: {exc}")
                result = _could_not(Trigger.POLL, self._monotonic(), failed)
            self._deliver(result)

    def _deliver(self, result: Reading) -> None:
        """A callback that raises is a missed update, never a dead watch."""
        with contextlib.suppress(Exception):
            self._on_result(result)
