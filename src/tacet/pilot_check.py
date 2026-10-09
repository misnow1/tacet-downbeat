"""Check a game's ramps against its pilot recording, after the game (#191).

    tacet-pilot-check GAME.jsonl "Pilot Reference.wav"

Exit 0: every ramp was checked and none flagged. Exit 1: something was flagged,
or a ramp could not be checked. Exit 2: the check could not run at all.

A log with no recording anchor (Game 3's) needs `--take-start`, the wall time
the recording began. This tool reads no `tacet.toml`: it is not run on the box.

The standard library only at import time: numpy is imported when the check
runs, so a machine without the `analysis` extra gets one clear line instead of
a traceback.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import annotations as ann
from . import markers, wav

EXIT_CLEAN = 0
EXIT_FLAGGED = 1
EXIT_REFUSED = 2

NUMPY_MODULE = "numpy"
NUMPY_HINT = "tacet-pilot-check needs numpy: pip install -e '.[analysis]'"
TAKE_START_EXAMPLE = "2026-10-02T21:50:42.45Z"


def aware_datetime(text: str) -> datetime:
    """An ISO 8601 wall time that names its timezone; a naive one is ambiguous."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r}") from None
    if when.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} has no timezone; add Z for UTC or an offset such as -04:00")
    return when


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tacet-pilot-check",
        description="Flag box ramps that started from a belief the pilot recording contradicts.",
    )
    p.add_argument("log", type=Path, help="the box's annotation log (JSONL)")
    p.add_argument("pilot", type=Path, help="the Pilot Reference track: 24-bit mono BWF WAV")
    p.add_argument(
        "--take-start",
        type=aware_datetime,
        default=None,
        metavar="WALL",
        help=(
            "wall time the recording started, with a timezone; only for a log with no recording anchor "
            f"(Game 3's was {TAKE_START_EXAMPLE}, #180)"
        ),
    )
    return p


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = parser().parse_args(argv)
    try:
        from . import pilot
    except ModuleNotFoundError as exc:
        if exc.name != NUMPY_MODULE:
            raise
        print(NUMPY_HINT, file=err)
        return EXIT_REFUSED
    try:
        entries = list(ann.read_entries(args.log))
        anchor = pilot.resolve_anchor(entries, args.take_start)
        take = pilot.take_of(entries, anchor)
        layout, envelope = pilot.read_envelope(args.pilot)
        report = pilot.check(envelope, take, layout)
    except (
        ann.CorruptLogError,
        markers.NoAnchorError,
        pilot.PilotCheckError,
        wav.WavFormatError,
        OSError,
    ) as exc:
        print(f"tacet-pilot-check: {_ascii(str(exc))}", file=err)
        return EXIT_REFUSED
    print(pilot.render(report, tz=None, source=_ascii(args.pilot.name)), end="", file=out)
    return EXIT_FLAGGED if report.flagged or report.unchecked else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
