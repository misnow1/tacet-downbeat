"""Answer the fader questions that need the console in front of you.

The spec gives `min -32768 / max 1000 / scaling 100`, implying any value in
hundredths of a dB, but the parameter notes point at Table 1, a 43-entry list.
If only Table 1 values are accepted, a fade is 32 steps rather than a smooth
ramp (design.md section 7).

Nothing here can confirm anything: the DM7's OSC is write-only, so the console
is the only display. Read it.

    python -m tacet.verify_dm7 --host 10.0.0.5 --dca 3 --granularity
"""

from __future__ import annotations

import argparse
import asyncio

from .dm7 import DEFAULT_PORT, MINUS_INF, TABLE_1, UNITY, Dm7Client, to_db

#: Deliberately not a Table 1 value. -15.50 dB sits between -15 and -16.
PROBE_LEVEL = -1550
_SETTLE_SECONDS = 2.0


async def _granularity(client: Dm7Client) -> None:
    print("\n--- fader granularity ---")
    print(f"sending {PROBE_LEVEL} ({to_db(PROBE_LEVEL):+.2f} dB), which is NOT in Table 1")
    client.send_level(PROBE_LEVEL)
    await asyncio.sleep(_SETTLE_SECONDS)
    print("\nRead the DCA on the console:")
    print("  -15.50  -> arbitrary values accepted; the fade can be a smooth ramp")
    print("  -16.00  -> snapped to Table 1; set Dm7Client(quantized=True)")
    print("  no move -> wrong address, IP or port; nothing here can detect that")


async def _fade(client: Dm7Client, seconds: float) -> None:
    print(f"\n--- {seconds:.0f}s fade from unity ---")
    client.send_level(UNITY)
    await asyncio.sleep(1.0)
    await client.fade_out(seconds=seconds)
    print(f"sent {client.sent_count} packets; commanded level is now {client.commanded_level}")
    print("watch for stepping. Smooth means arbitrary values are accepted.")


async def _run(args: argparse.Namespace) -> None:
    client = Dm7Client(args.host, args.port, dca=args.dca, quantized=args.quantized)
    print(f"DM7 at {args.host}:{args.port}, DCA {args.dca}")
    print(f"Table 1 has {len(TABLE_1)} values, {MINUS_INF} to {TABLE_1[-1]}")
    try:
        if args.granularity:
            await _granularity(client)
        if args.fade:
            await _fade(client, args.fade)
        if args.level is not None:
            client.send_level(args.level)
            print(f"sent {args.level} ({to_db(args.level):+.2f} dB)")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="the console's For Mixer Control IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dca", type=int, required=True)
    parser.add_argument("--granularity", action="store_true", help="the -1550 probe")
    parser.add_argument("--fade", type=float, metavar="SECONDS")
    parser.add_argument("--level", type=int, help="send one level and exit")
    parser.add_argument("--quantized", action="store_true", help="snap to Table 1")
    args = parser.parse_args(argv)

    if not (args.granularity or args.fade or args.level is not None):
        parser.error("choose --granularity, --fade or --level")
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
