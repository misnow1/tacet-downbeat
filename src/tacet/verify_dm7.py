"""Answer the fader questions that need the console in front of you.

The spec gives `min -32768 / max 1000 / scaling 100`, implying any value in
hundredths of a dB, but the parameter notes point at Table 1, a 43-entry list.
If only Table 1 values are accepted, a fade is 32 steps rather than a smooth
ramp (design.md section 7).

Nothing here can confirm anything: the DM7's OSC is write-only, so the console
is the only display. Read it.

    python -m tacet.verify_dm7 --host 10.0.0.5 --dca 3 --granularity

`--host`, `--port`, `--dca` and `--quantized` can come from the config file
instead, as `console.host`, `console.port`, `console.dca` and
`console.quantized`. With a `tacet.toml` in the working directory -- or named
by `--config PATH` or `$TACET_CONFIG` -- that shortens to:

    python -m tacet.verify_dm7 --granularity

`--granularity`, `--fade` and `--level` are deliberately NOT config keys. They
choose what this tool does to a console, and a file able to select one of them
would move a fader nobody asked to move. See `tacet.config`.
"""

from __future__ import annotations

import argparse
import asyncio

from . import config
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


#: The console half of the config file. `--fade` is deliberately not here: in
#: this tool it chooses whether to run the fade probe at all, so a value from a
#: file would fire one at a console nobody asked to move.
CONFIG_MAPPING = {
    "host": "console.host",
    "port": "console.port",
    "dca": "console.dca",
    "quantized": "console.quantized",
}


def parser() -> argparse.ArgumentParser:
    """Built separately from `main` so the tests can reach it without a console."""
    # Raw, so the example commands survive --help intact. See tacet.serve.
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    config.add_config_argument(p)
    p.add_argument("--host", help="the console's For Mixer Control IP")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--dca", type=int)
    p.add_argument("--granularity", action="store_true", help="the -1550 probe")
    p.add_argument("--fade", type=float, metavar="SECONDS")
    p.add_argument("--level", type=int, help="send one level and exit")
    p.add_argument("--quantized", action=argparse.BooleanOptionalAction, default=False, help="snap to Table 1")
    return p


def main(argv: list[str] | None = None) -> int:
    parser_ = parser()
    args, config_path = config.resolve_or_exit(parser_, CONFIG_MAPPING, argv)
    config.require(parser_, args, CONFIG_MAPPING, "host", "dca")

    if not (args.granularity or args.fade or args.level is not None):
        parser_.error("choose --granularity, --fade or --level")
    if config_path:
        print(f"config: {config_path}")
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
