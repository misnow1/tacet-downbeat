"""Real snapshots for the page's tests, written by the real `App`.

The page's tests used to build their own snapshot by hand, and it drifted: it
was missing fields the box sends, and nothing on this side pinned the shape. A
change to the snapshot passed both suites while the page misread it (#38).

So the box writes a handful of states to `tests/fixtures/snapshot-<state>.json`,
`tests/test_snapshot_fixtures.py` fails when they no longer match what `App`
produces, and `tests/test_app_js.mjs` loads them instead of a copy. A snapshot
change therefore shows up as a fixture diff in review, and the page's tests run
against it.

    make snapshots      (or: python -m tests.snapshots)

Everything here is deterministic: the clock is injected, no ramp is left to run
in real time, and the temporary log directory is written as `LOG_DIR`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tacet import annotations as ann
from tacet import dm7, osc, reaper
from tacet.app import App
from tacet.net import TransportError
from tests.disk import Disk

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREFIX = "snapshot-"
SUFFIX = ".json"
#: Stands in for the temporary directory the log was written to, so a fixture
#: does not change with the machine that generated it.
LOG_DIR = "/games"
#: Where the injected clock starts. Arbitrary; far from zero like a real one.
CLOCK_START = 5000.0
#: A playhead that renders as a recognisable timecode, 0:12:34.500, and that
#: float32 - which OSC carries - holds exactly.
POSITION = 754.5
#: Long enough that no Reaper feedback counts as current any more.
SILENCE = reaper.DEFAULT_FEEDBACK_TIMEOUT * 30


class _Sender:
    def send(self, packet: bytes) -> None:
        pass


class _Unreachable:
    def send(self, packet: bytes) -> None:
        raise TransportError("no route to host")


@dataclass
class Box:
    app: App
    clock: list[float]
    disk: Disk

    def reaper_says(self, address: str, value: float) -> None:
        self.app.handle_recorder_packet(osc.encode_message(address, value))


def _build(root: Path, *, console: _Sender | _Unreachable) -> Box:
    clock = [CLOCK_START]
    disk = Disk()
    log = ann.AnnotationLog(root / "game.jsonl", opener=disk.open).open()
    app = App(
        console=dm7.Dm7Client("192.0.2.1", dca=3, sender=console),
        log=log,
        recorder=reaper.ReaperClient(sender=_Sender(), monotonic=lambda: clock[0]),
        monotonic=lambda: clock[0],
    )
    return Box(app=app, clock=clock, disk=disk)


async def standing_down(root: Path) -> dict[str, Any]:
    """The boot state. Reaper configured, and never heard from."""
    return _build(root, console=_Sender()).app.snapshot()


async def open_recording(root: Path) -> dict[str, Any]:
    """Armed, open at unity, Reaper rolling, a quarter under way."""
    box = _build(root, console=_Sender())
    await box.app.arm()
    await box.app.annotate("up-whistle")
    box.reaper_says("/record", 1.0)
    box.reaper_says("/play", 1.0)
    box.reaper_says("/time", POSITION)
    await box.app.start_span("q2")
    return box.app.snapshot()


async def releasing(root: Path) -> dict[str, Any]:
    """The push that follows FADE OUT, before the ramp has taken a step."""
    box = _build(root, console=_Sender())
    await box.app.arm()
    await box.app.trigger()
    await box.app.release()
    snapshot = box.app.snapshot()
    box.app._cancel_move()
    return snapshot


async def faults(root: Path) -> dict[str, Any]:
    """Everything the page has a way of saying is wrong, at once."""
    box = _build(root, console=_Unreachable())
    await box.app.arm()
    box.reaper_says("/record", 1.0)
    box.clock[0] += SILENCE
    box.disk.full = True
    await box.app.annotate("up-drums")
    await box.app.start_recording()
    return box.app.snapshot()


STATES: dict[str, Callable[[Path], Awaitable[dict[str, Any]]]] = {
    "standing-down": standing_down,
    "open-recording": open_recording,
    "releasing": releasing,
    "faults": faults,
}


def fixture_path(state: str) -> Path:
    return FIXTURES / f"{PREFIX}{state}{SUFFIX}"


def _text(snapshot: dict[str, Any], root: Path) -> str:
    text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    return text.replace(str(root), LOG_DIR)


async def _generate() -> dict[Path, str]:
    generated: dict[Path, str] = {}
    for state, build in STATES.items():
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated[fixture_path(state)] = _text(await build(root), root)
    return generated


def generate() -> dict[Path, str]:
    """Every fixture, as `App` would write it now."""
    return asyncio.run(_generate())


def existing() -> set[Path]:
    return set(FIXTURES.glob(f"{PREFIX}*{SUFFIX}"))


def main() -> int:
    generated = generate()
    for path in existing() - set(generated):
        path.unlink()
        print(f"removed {path.name}")
    for path, text in generated.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
