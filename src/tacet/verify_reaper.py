"""Find out what Reaper's OSC actually says.

`tacet.reaper.AddressMap` holds the stock `Default.ReaperOSC` names from memory.
A wrong network address or port fails loudly; a wrong OSC address fails
silently, because UDP is fire-and-forget and Reaper ignores paths it does not
recognise. This prints what Reaper really sends, so the guess can be replaced
with a fact.

    python -m tacet.verify_reaper --listen 20

Then, in Reaper: press play, stop, and record-arm. Paste the output back.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from collections import Counter
from typing import Any

from . import config, osc
from .net import UdpSender
from .reaper import (
    DEFAULT_ADDRESSES,
    DEFAULT_HOST,
    DEFAULT_RECEIVE_PORT,
    DEFAULT_SEND_PORT,
)

_SOCKET_TIMEOUT = 0.25


def _flatten(packet: osc.Message | osc.Bundle) -> list[osc.Message]:
    if isinstance(packet, osc.Message):
        return [packet]
    found: list[osc.Message] = []
    for element in packet.elements:
        found.extend(_flatten(element))
    return found


def _listen(port: str | int, host: str, seconds: float) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {}
    counts: Counter[str] = Counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(_SOCKET_TIMEOUT)
    try:
        sock.bind((host, int(port)))
    except OSError as exc:
        print(f"could not bind {host}:{port} - {exc}", file=sys.stderr)
        print("is another OSC client already listening there?", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"listening on {host}:{port} for {seconds:.0f}s - drive Reaper now\n")
    deadline = time.monotonic() + seconds
    with sock:
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                continue
            try:
                packet = osc.decode_packet(data)
            except osc.OscError as exc:
                print(f"  undecodable packet ({exc})")
                continue
            for message in _flatten(packet):
                counts[message.address] += 1
                if message.address not in seen:
                    seen[message.address] = list(message.args)
                    print(f"  {message.address}  {list(message.args)}")
    print()
    for address, count in counts.most_common():
        print(f"  {count:6d}  {address}")
    return seen


def _report(seen: dict[str, list[Any]]) -> None:
    print("\n--- does AddressMap match? ---")
    expected = {
        "record / recording": DEFAULT_ADDRESSES.record,
        "play / playing": DEFAULT_ADDRESSES.play,
        "position": DEFAULT_ADDRESSES.position,
    }
    ok = True
    for label, address in expected.items():
        if address in seen:
            print(f"  FOUND    {label:20} {address}  e.g. {seen[address]}")
        else:
            ok = False
            print(f"  MISSING  {label:20} {address}")
    if not seen:
        print("\nNothing arrived at all. Check Reaper's OSC device is enabled and")
        print("that its 'device port' matches --recv-port.")
    elif not ok:
        print("\nSome addresses did not appear. Send me the list above and I will")
        print("correct AddressMap - it is configuration precisely for this case.")
    else:
        print("\nAddressMap looks right.")


#: The Reaper half of the config file. `--listen` is a duration here, not the
#: UI's bind address, so `ui.listen` must not reach it -- same name, different
#: quantity, and that collision is why this mapping is written out rather than
#: derived from the flag names.
CONFIG_MAPPING = {
    "host": "reaper.host",
    "send_port": "reaper.send_port",
    "recv_port": "reaper.receive_port",
}


def parser() -> argparse.ArgumentParser:
    """Built separately from `main` so the tests can reach it without Reaper."""
    p = argparse.ArgumentParser(description=__doc__)
    config.add_config_argument(p)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--send-port", type=int, default=DEFAULT_SEND_PORT)
    p.add_argument("--recv-port", type=int, default=DEFAULT_RECEIVE_PORT)
    p.add_argument("--listen", type=float, default=20.0, metavar="SECONDS")
    p.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="send this OSC address before listening; repeatable",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args, config_path = config.resolve_or_exit(parser(), CONFIG_MAPPING, argv)
    if config_path:
        print(f"config: {config_path}")

    for address in args.send:
        UdpSender(args.host, args.send_port).send(osc.encode_message(address))
        print(f"sent {address} to {args.host}:{args.send_port}")

    _report(_listen(args.recv_port, args.host, args.listen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
