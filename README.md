# tacet-downbeat

[![CI](https://github.com/misnow1/tacet-downbeat/actions/workflows/ci.yml/badge.svg)](https://github.com/misnow1/tacet-downbeat/actions/workflows/ci.yml)

Automates the marching band microphone DCA on a Yamaha DM7C during home football
games. The band mics feed a PA separate from the main stadium PA and are
currently ridden by hand from an iPad.

The system does not decide whether the band *may* play — the band director is
the compliance layer and is bound by the same rule. It only tracks whether the
band **is** playing, and amplifies accordingly.

## Status

**Phase 0 / 1. The detector drives nothing.**

| Phase | | |
|---|---|---|
| 0 | Wired OSC control path + web UI | Standalone value; ships regardless |
| 1 | Shadow mode — compute everything, log everything, drive nothing | Captures the labeled answer key |
| 2 | Assisted — detector drives the DCA, operator supervises | Not yet declared |
| 3 | Refinement from accumulated logs | |

## Why not a gate

The stadium holds ~60,000 people and level is the one dimension in which the
crowd competes directly with the band. An amplitude threshold sees crowd and
band as the same thing. Discrimination comes from structure — onset
simultaneity, harmonic coherence, inter-channel behavior. A gate and an expander
have both been tried.

The detector is **causal**, with no lookahead beyond ~15 ms: the band PA is
aligned to the acoustic band a few feet behind it, so added delay becomes
slapback and drags the band's tempo.

## Layout

```
docs/design.md      the design document — read this first
docs/gameday.md     the game-day runbook — what to start, in what order
docs/vendors/       vendor specs (Yamaha DM7 OSC, plus a text extraction)
src/tacet/          the box: OSC codec, console and Reaper clients, state
                    machine, annotation log, web UI
reaper/             ReaScript that mirrors annotations into Reaper markers
tacet.toml.example  every config key, commented; copy to tacet.toml (gitignored)
tests/
audio/              multitrack sources for offline analysis (gitignored)
```

## Configuration

`tacet-serve`, `verify_dm7` and `verify_reaper` all want the same site values —
which console, which DCA, which Reaper — so they share one **optional** TOML
file:

```
cp tacet.toml.example tacet.toml
tacet-serve --log ~/games/2026-09-13.jsonl     # the rest comes from the file
```

A `tacet.toml` in the working directory is sourced automatically. Otherwise name
one with `--config PATH` or `$TACET_CONFIG`; `~/.config/tacet/tacet.toml` is the
last place looked. Nothing requires the file — every value still has a flag, and
the flag wins:

```
built-in default  <  tacet.toml  <  explicit flag
```

Each tool prints which config it read on startup. Unknown sections and keys are
errors rather than silently ignored, so a file that says `dca_number` stops the
tool instead of leaving it on a default and driving the wrong fader. The real
`tacet.toml` is gitignored: console IP, DCA number and NAS path stay out of the
repo and live in `docs/gameday.md`.

## Development

Python 3.11+, `src/` layout, package `tacet`. The control path that moves the
fader imports only the standard library, enforced by a test; the web UI takes
`aiohttp`.

```
make install    # create .venv and install the dev tooling
make hooks      # install the pre-commit hooks
make check      # lint, type check, test - what CI runs
```

Everything is type-annotated and checked with `mypy --strict`; `ruff` handles
lint and formatting. Pre-commit runs the fast checks; CI runs the full suite on
Linux and macOS.

## Data

**Recordings never go in git.** A 14-channel 24-bit/48 kHz multitrack is roughly
7 GB/hour. They live on the home lab NAS; the repo references paths, not
contents. `audio/` is gitignored apart from its README.

## Start here

`docs/design.md` carries the reasoning behind every constraint. Several of them
contradict what would otherwise be sensible defaults — the play clock is not a
mute interlock, the fade must hold open through a diminuendo, the close is
always reactive — and they look arbitrary without it.
