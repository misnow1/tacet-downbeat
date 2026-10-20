"""The two games the spike reads, and where their files are. Read-only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tacet import wav

REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO / "data" / "spike-217"
OUT_DIR = Path(__file__).resolve().parent / "out"
REAPER_MEDIA = Path.home() / "Reaper Media"
GAMES_DIR = Path.home() / "games"

#: Live MV tracks: 1 (conductor) and 3-14. Track 2 is a spare with no mic on it.
MV_TRACKS = (1, *range(3, 15))
WHISTLE_TRACKS = (1, 13, 14)
ANNOUNCER = 17
DJ = 23
HOKIEVISION = 24
TRUCKFX = 25
PILOT = 26
MAIN_PA = 28
#: The Hype PA Output (27) is deliberately absent: it carries the band mics.
PROGRAM_TRACKS = (DJ, HOKIEVISION, TRUCKFX)
ENVELOPE_TRACKS = (*MV_TRACKS, ANNOUNCER, *PROGRAM_TRACKS, MAIN_PA)
ALL_TRACKS = (*range(1, 15), 17, 23, 24, 25, 26, 28)


@dataclass(frozen=True)
class Game:
    id: str
    prefix: str
    rpp: Path
    log: Path
    pcap: Path | None
    media: Path
    stamp: str

    @property
    def spike_rpp(self) -> Path:
        return self.rpp.with_name(f"{self.rpp.stem}.spike-217{self.rpp.suffix}")


GAME_3 = Game(
    id="game-3",
    prefix="G3",
    rpp=REAPER_MEDIA / "20261002 Pitt" / "20261002 Pitt.RPP",
    log=GAMES_DIR / "20261002-pitt.jsonl",
    pcap=None,
    media=REAPER_MEDIA / "20261002 Pitt" / "Media",
    stamp="261002_1750",
)
GAME_4 = Game(
    id="game-4",
    prefix="G4",
    rpp=REAPER_MEDIA / "20261017 GT" / "20261017 GT.RPP",
    log=GAMES_DIR / "2026-10-17-gt.jsonl",
    pcap=REPO / "data" / "rtd-game-4-gt.pcap",
    media=REAPER_MEDIA / "20261017 GT" / "Media",
    stamp="261017_1402",
)
GAMES = {g.id: g for g in (GAME_3, GAME_4)}


def track_path(game: Game, number: int) -> Path:
    matches = sorted(game.media.glob(f"{number:02d}-*-{game.stamp}.wav"))
    if len(matches) != 1:
        raise RuntimeError(f"{game.id} track {number}: expected one file, found {len(matches)}")
    return matches[0]


def read_layout(path: Path) -> wav.WavLayout:
    with path.open("rb") as handle:
        return wav.read_layout(handle, path.stat().st_size)


def check_layouts(game: Game) -> wav.WavLayout:
    """Every track must share a time reference and rate, or nothing lines up."""
    layouts = {n: read_layout(track_path(game, n)) for n in ALL_TRACKS}
    first = layouts[ALL_TRACKS[0]]
    for n, layout in layouts.items():
        if (layout.time_reference, layout.sample_rate) != (first.time_reference, first.sample_rate):
            raise RuntimeError(f"{game.id} track {n}: time reference or rate differs from track {ALL_TRACKS[0]}")
    return first
