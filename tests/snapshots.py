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
import dataclasses
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tacet import annotations as ann
from tacet import dm7, moves, osc, provenance, reaper
from tacet.app import App
from tacet.net import TransportError
from tacet.state import Machine
from tests.disk import Disk
from tests.reaper_stream import METER

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREFIX = "snapshot-"
SUFFIX = ".json"
#: The move curves the page's `moveDbAt` is held to (#154): a description, and
#: the steps `dm7.ramp_steps` takes for it, so a drift on either side fails.
CURVES_PATH = FIXTURES / "move-curves.json"
#: Stands in for the temporary directory the log was written to, so a fixture
#: does not change with the machine that generated it.
LOG_DIR = "/games"
#: Where the injected clock starts. Arbitrary; far from zero like a real one.
CLOCK_START = 5000.0
#: How long READY's ride takes in the curves fixture. Arbitrary, but not the
#: slow open's length, so a mix-up between the two shows.
READY_SECONDS = 4.0
#: The retarget ride, as the box ships it (#128).
RETARGET_SECONDS = dm7.DEFAULT_RETARGET_RIDE_SECONDS
#: A playhead that renders as a recognisable timecode, 0:12:34.500, and that
#: float32 - which OSC carries - holds exactly.
POSITION = 754.5
#: Long enough that no Reaper feedback counts as current any more.
SILENCE = reaper.DEFAULT_FEEDBACK_TIMEOUT * 30


#: What every fixture box says it runs (#157): a clean checkout. Fixed, so a
#: fixture never depends on the checkout that generated it.
CLEAN = provenance.Provenance(
    source=provenance.Source.CHECKOUT,
    commit="0123456789abcdef0123456789abcdef01234567",
    branch="main",
    detached=False,
    dirty=False,
    untracked=0,
    worktree=False,
    path="/checkout",
    error=None,
)
#: The faults page also runs a fix on the day: uncommitted changes, from a worktree.
DIRTY = dataclasses.replace(CLEAN, branch="157-fix", dirty=True, worktree=True)


class _Sender:
    def send(self, packet: bytes) -> None:
        pass


class _Unreachable:
    def send(self, packet: bytes) -> None:
        raise TransportError("no route to host")


@dataclass
class Box:
    app: App
    log: ann.AnnotationLog
    clock: list[float]
    disk: Disk

    def reaper_says(self, address: str, value: float) -> None:
        self.app.handle_recorder_packet(osc.encode_message(address, value))


def _build(
    root: Path,
    *,
    console: _Sender | _Unreachable,
    machine: Machine | None = None,
    code: provenance.Provenance = CLEAN,
) -> Box:
    clock = [CLOCK_START]

    async def tick(seconds: float) -> None:
        # The console needs its own clock to actually advance during a move -
        # otherwise its ramp-scheduling loop sees the same "due" step forever
        # and never finishes. Harmless before `sent_at` existed, since nothing
        # read the console's clock; #12 made the mismatch a real bug (fixture
        # values computed against a live wall clock, and non-reproducible).
        clock[0] += seconds

    disk = Disk()
    log = ann.AnnotationLog(root / "game.jsonl", opener=disk.open).open()
    app = App(
        console=dm7.Dm7Client("192.0.2.1", dca=3, sender=console, monotonic=lambda: clock[0], sleep=tick),
        log=log,
        recorder=reaper.ReaperClient(sender=_Sender(), monotonic=lambda: clock[0]),
        monotonic=lambda: clock[0],
        machine=machine,
        provenance=code,
    )
    return Box(app=app, log=log, clock=clock, disk=disk)


#: The machine for every state that got somewhere by arming. At cold boot the
#: box does not know where the fader is and refuses to arm (#107), so a fixture
#: that arms says up front that the level was already known - the way a box
#: that had been told is.
def _known() -> Machine:
    return Machine(level_known=True)


async def standing_down(root: Path) -> dict[str, Any]:
    """The boot state. Reaper configured, and never heard from. The level is
    deliberately unknown: this fixture IS the cold-boot page (#107), and must
    stay honest about it."""
    return _build(root, console=_Sender()).app.snapshot()


async def open_recording(root: Path) -> dict[str, Any]:
    """Armed, open at unity, Reaper rolling, a media timeout under way."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.annotate("up-whistle")
    box.reaper_says("/record", 1.0)
    box.reaper_says("/play", 1.0)
    box.reaper_says("/time", POSITION)
    await box.app.start_span("timeout-media")
    return box.app.snapshot()


async def parked_unreported(root: Path) -> dict[str, Any]:
    """Reaper open and parked with its audio device running: meters stream and
    nothing is said about the transport, which Reaper announces only when it
    changes. The normal pregame page after #163 - the button is live."""
    box = _build(root, console=_Sender(), machine=_known())
    # Listened to for a full timeout: the box will not call a transport parked
    # before that, so the button is live only now.
    for _ in range(2):
        box.reaper_says(METER, 0.0)
        box.clock[0] += reaper.DEFAULT_FEEDBACK_TIMEOUT / 2
    box.reaper_says(METER, 0.0)
    return box.app.snapshot()


async def releasing(root: Path) -> dict[str, Any]:
    """The push that follows FADE OUT, before the ramp has taken a step. Tapped
    as `out`, so the move says which button started it (#154)."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.trigger()
    await box.app.annotate("out")
    snapshot = box.app.snapshot()
    box.app._cancel_move()
    return snapshot


async def riding(root: Path) -> dict[str, Any]:
    """The push that follows Up slow, before the ride has taken a step (#154)."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.annotate("up-slow")
    snapshot = box.app.snapshot()
    box.app._cancel_move()
    return snapshot


async def target_stored(root: Path) -> dict[str, Any]:
    """Open, fading out after Out; the operator taps -3 dB. A fade still ends at
    -inf, so the tap stores and says so (#153, #128)."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.annotate("up-whistle")
    await box.app.annotate("out")
    await box.app.set_target(-3.0)
    snapshot = box.app.snapshot()
    box.app._cancel_move()
    return snapshot


async def retargeting(root: Path) -> dict[str, Any]:
    """Open at unity; the operator taps -3 dB and the fader rides there (#128).
    The push that follows the tap, before the ride has taken a step."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.annotate("up-whistle")
    await box.app.set_target(-3.0)
    snapshot = box.app.snapshot()
    box.app._cancel_move()
    return snapshot


async def prompt_open(root: Path) -> dict[str, Any]:
    """The box has asked: the band left the stands mid-game, and a Stand down
    question is on the page, unanswered. Produced the way the box produces it -
    an operator tap on `band-exits-stands` - never assembled by hand (#19)."""
    box = _build(root, console=_Sender(), machine=_known())
    await box.app.arm()
    await box.app.annotate("up-whistle")
    await box.app.annotate(ann.BAND_EXITS_STANDS)
    return box.app.snapshot()


async def prompt_arm_refused(root: Path) -> dict[str, Any]:
    """Cold boot: the box does not know where the fader is (#107). The operator
    taps `band-enters-stands`, raising the Arm question, and accepts it - which
    the box refuses, since arming would claim a closed DCA it cannot vouch for.
    The question stays open under the same seq, with the refusal on the page,
    rather than vanishing on a tap that changed nothing (#19)."""
    box = _build(root, console=_Sender())
    await box.app.annotate(ann.BAND_ENTERS_STANDS)
    seq = box.app.snapshot()["prompt"]["seq"]
    await box.app.accept_prompt(seq)
    return box.app.snapshot()


async def faults(root: Path) -> dict[str, Any]:
    """Everything the page has a way of saying is wrong, at once.

    The snap open's send never left the box, and it was absolute, so the
    level goes back to unknown (#116): this fixture's fader reads unknown too,
    for real, not just at cold boot."""
    box = _build(root, console=_Unreachable(), machine=_known(), code=DIRTY)
    await box.app.arm()
    box.reaper_says("/record", 1.0)
    box.clock[0] += SILENCE
    box.log.flush()  # so the disk fills after `armed` is saved, not before
    box.disk.full = True
    await box.app.annotate("up-drums")
    await box.app.start_recording()
    # Both entries failed on the writer thread; wait for it to say so (#41).
    box.log.flush()
    return box.app.snapshot()


STATES: dict[str, Callable[[Path], Awaitable[dict[str, Any]]]] = {
    "standing-down": standing_down,
    "open-recording": open_recording,
    "parked-unreported": parked_unreported,
    "releasing": releasing,
    "riding": riding,
    "target-stored": target_stored,
    "retargeting": retargeting,
    "prompt": prompt_open,
    "prompt-arm": prompt_arm_refused,
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


def _curve(
    name: str, kind: moves.MoveKind, start: int, end: int, seconds: float, taper: dm7.Taper | None
) -> dict[str, Any]:
    """One named move and the steps the console would take for it."""
    description = moves.MoveDescription(
        seq=1,
        kind=kind,
        by=None,
        start=start,
        end=end,
        seconds=seconds,
        started_at=0.0,
        floor=dm7.DEFAULT_FADE_FLOOR,
        taper=taper,
    )
    steps = dm7.ramp_steps(start, end, seconds, fade_floor=dm7.DEFAULT_FADE_FLOOR, taper=taper)
    return {
        "name": name,
        "move": description.as_data(),
        "steps": [[offset, None if level == dm7.MINUS_INF else dm7.to_db(level)] for offset, level in steps],
    }


def _units(db: float) -> int:
    return round(db * dm7.UNITS_PER_DB)


def curves() -> list[dict[str, Any]]:
    """The cases `moveDbAt` must reproduce: every shape and every edge."""
    fade, ride = moves.MoveKind.FADE, moves.MoveKind.RIDE
    taper = dm7.RIDE_IN_TAPER
    return [
        _curve("fade from unity", fade, dm7.UNITY, dm7.MINUS_INF, dm7.DEFAULT_FADE_SECONDS, None),
        _curve("fade from -3 dB", fade, _units(-3.0), dm7.MINUS_INF, dm7.DEFAULT_FADE_SECONDS, None),
        _curve("fade from below the floor", fade, _units(-70.0), dm7.MINUS_INF, dm7.DEFAULT_FADE_SECONDS, None),
        _curve("up slow from -inf", ride, dm7.MINUS_INF, dm7.UNITY, dm7.DEFAULT_SLOW_OPEN_SECONDS, taper),
        _curve("ready from -inf to -15 dB", ride, dm7.MINUS_INF, _units(-15.0), READY_SECONDS, taper),
        _curve("ride from -10 dB to unity", ride, _units(-10.0), dm7.UNITY, dm7.DEFAULT_SLOW_OPEN_SECONDS, taper),
        _curve("ready down from unity to -15 dB", ride, dm7.UNITY, _units(-15.0), READY_SECONDS, taper),
        _curve("ride from -inf to below the floor", ride, dm7.MINUS_INF, _units(-70.0), READY_SECONDS, taper),
        _curve("retarget down from unity to -3 dB", ride, dm7.UNITY, _units(-3.0), RETARGET_SECONDS, taper),
        _curve("retarget up from -6 dB to unity", ride, _units(-6.0), dm7.UNITY, RETARGET_SECONDS, taper),
        _curve("retarget the hold from -15 to -21 dB", ride, _units(-15.0), _units(-21.0), RETARGET_SECONDS, taper),
        _curve("retarget mid ride-in from -40 dB to -3 dB", ride, _units(-40.0), _units(-3.0), RETARGET_SECONDS, taper),
    ]


def _curves_text() -> str:
    return json.dumps(curves(), indent=2, sort_keys=True) + "\n"


def generate() -> dict[Path, str]:
    """Every fixture, as `App` would write it now."""
    generated = asyncio.run(_generate())
    generated[CURVES_PATH] = _curves_text()
    return generated


def existing() -> set[Path]:
    found = set(FIXTURES.glob(f"{PREFIX}*{SUFFIX}"))
    if CURVES_PATH.exists():
        found.add(CURVES_PATH)
    return found


def main() -> int:
    generated = generate()
    for path in existing() - set(generated):
        if not path.name.startswith(PREFIX):
            continue
        path.unlink()
        print(f"removed {path.name}")
    for path, text in generated.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
