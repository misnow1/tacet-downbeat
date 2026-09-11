"""An optional TOML file holding the site values, so the tools stop needing them.

Three programs want the same handful of facts -- which console, which DCA, which
Reaper -- and today each one takes them as flags. `tacet-serve` alone is eight
lines of command with a quoted queue path in the middle of it, retyped in a
press box on a Saturday. The values it repeats do not change between games; only
`--log` does. See docs/gameday.md.

So: a file. It is *optional* by design. Every flag still exists, nothing is
required to use it, and a machine without one behaves exactly as before.

Precedence, lowest to highest:

    built-in default  <  config file  <  explicit flag

which falls out of `argparse.set_defaults` and is asserted in the tests rather
than trusted.

The file is found in this order, and the tools print which one they used --
guessing silently is how the wrong DCA gets driven:

    1. `--config PATH`          (must exist; a typo is an error, not a shrug)
    2. $TACET_CONFIG            (likewise)
    3. ./tacet.toml             (used if present)
    4. ~/.config/tacet/tacet.toml

Unknown sections and unknown keys are errors. A config that silently ignores
`dca_number` because the key is spelled `dca` is a config that drives the wrong
fader while looking correct.

TOML rather than anything else because `tomllib` is in the standard library at
3.11, which is the floor this project already sets. No dependency is added --
this module is importable from the control path without breaking the policy in
CLAUDE.md, though nothing there needs it today.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Environment variable naming a config file, checked after `--config`.
CONFIG_ENV_VAR = "TACET_CONFIG"

#: What the file is called when it is found rather than named.
CONFIG_FILENAME = "tacet.toml"

#: Where a per-user config lives, under the home directory.
USER_CONFIG_DIR = Path(".config") / "tacet"

#: The flag every tool grows, and its `dest`.
CONFIG_FLAG = "--config"
CONFIG_DEST = "config"


class ConfigError(Exception):
    """A config file that exists but cannot be trusted.

    Always raised with the offending path and key in the message: this surfaces
    to someone standing at a laptop 20 minutes before kickoff.
    """


#: The value kinds a config key may take. `path` is `str` in the file and a
#: `Path` afterwards, with `~` expanded, because every path here is typed by a
#: human who will write `~/games`.
Kind = Literal["str", "int", "float", "bool", "path"]


@dataclass(frozen=True)
class Option:
    """One key the config file may set, and the type it must have."""

    section: str
    key: str
    kind: Kind
    help: str

    @property
    def name(self) -> str:
        """The dotted name used everywhere outside the file itself."""
        return f"{self.section}.{self.key}"


#: Every key the file may contain. Sections are by subject, not by tool: the
#: console is the console whether `tacet-serve` or `verify_dm7` is asking.
#:
#: Deliberately absent: `verify_dm7 --fade` and `verify_reaper --listen`. Both
#: read like keys that belong here and neither does. In those two tools the flag
#: is what *selects* the probe, so giving it a value from a file would make
#: every run fire a fade at a console, or sit listening, without being asked.
SCHEMA: tuple[Option, ...] = (
    Option("console", "host", "str", "the DM7's For Mixer Control IP"),
    Option("console", "port", "int", "OSC port on the console"),
    Option("console", "dca", "int", "the band DCA number"),
    Option("console", "quantized", "bool", "snap fader values to Table 1"),
    Option("reaper", "host", "str", "host running Reaper"),
    Option("reaper", "send_port", "int", "Reaper's local listen port"),
    Option("reaper", "receive_port", "int", "the port Reaper sends feedback to"),
    Option("capture", "log", "path", "annotation log (JSONL); per game"),
    Option("capture", "queue", "path", "mirror queue the ReaScript watches"),
    Option("fader", "fade_seconds", "float", "close fade length"),
    Option("ui", "listen", "str", "address the web UI binds to"),
    Option("ui", "port", "int", "port the web UI binds to"),
)

_BY_NAME: dict[str, Option] = {option.name: option for option in SCHEMA}

_SECTIONS: dict[str, tuple[Option, ...]] = {
    section: tuple(option for option in SCHEMA if option.section == section)
    for section in dict.fromkeys(option.section for option in SCHEMA)
}


def _coerce(option: Option, value: object, *, where: str) -> object:
    """Check one value against its declared kind, and convert it.

    Pure. `where` names the file for the error message and nothing else.
    """
    # bool before int: in Python `True` is an int, and `port = true` is a typo
    # worth catching rather than quietly dialling port 1.
    if option.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: {option.name} must be true or false, got {value!r}")
        return value
    if isinstance(value, bool):
        raise ConfigError(f"{where}: {option.name} must be {option.kind}, got {value!r}")
    if option.kind == "int":
        if not isinstance(value, int):
            raise ConfigError(f"{where}: {option.name} must be a whole number, got {value!r}")
        return value
    if option.kind == "float":
        if not isinstance(value, int | float):
            raise ConfigError(f"{where}: {option.name} must be a number, got {value!r}")
        return float(value)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {option.name} must be a string, got {value!r}")
    if option.kind == "path":
        return Path(value).expanduser()
    return value


def values_from_mapping(data: Mapping[str, object], *, where: str = "config") -> dict[str, object]:
    """Validate a parsed TOML document and flatten it to dotted names.

    The pure core: no file, no argparse. Everything that can be wrong with a
    config file is decided here.
    """
    values: dict[str, object] = {}
    for section, body in data.items():
        known = _SECTIONS.get(section)
        if known is None:
            raise ConfigError(f"{where}: unknown section [{section}]. Known sections: {', '.join(_SECTIONS)}")
        if not isinstance(body, Mapping):
            raise ConfigError(f"{where}: [{section}] must be a table, got {body!r}")
        for key, value in body.items():
            option = _BY_NAME.get(f"{section}.{key}")
            if option is None:
                keys = ", ".join(item.key for item in known)
                raise ConfigError(f"{where}: unknown key {key!r} in [{section}]. Known keys: {keys}")
            values[option.name] = _coerce(option, value, where=where)
    return values


def load(path: Path) -> dict[str, object]:
    """Read and validate a config file. The I/O shell around `values_from_mapping`."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return values_from_mapping(data, where=str(path))


def default_search_paths(*, cwd: Path | None = None, home: Path | None = None) -> tuple[Path, ...]:
    """Where a config is looked for when it was not named. Injected for the tests."""
    cwd = Path.cwd() if cwd is None else cwd
    home = Path.home() if home is None else home
    return (cwd / CONFIG_FILENAME, home / USER_CONFIG_DIR / CONFIG_FILENAME)


def discover(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    search: Sequence[Path] | None = None,
) -> Path | None:
    """Find the config file, or `None` when there is honestly no config.

    A path that was *asked for* -- by flag or by environment -- and is not there
    is an error. A path that was merely looked for is not.
    """
    environ = os.environ if environ is None else environ
    for candidate, source in ((explicit, CONFIG_FLAG), (environ.get(CONFIG_ENV_VAR), CONFIG_ENV_VAR)):
        if candidate:
            path = Path(candidate).expanduser()
            if not path.is_file():
                raise ConfigError(f"{source} names {path}, which does not exist")
            return path
    for path in default_search_paths() if search is None else search:
        if path.is_file():
            return path
    return None


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    """Give a tool its `--config` flag, worded the same way in all of them."""
    parser.add_argument(
        CONFIG_FLAG,
        type=Path,
        metavar="PATH",
        help=f"TOML config file (default: {CONFIG_FILENAME} here, or ~/{USER_CONFIG_DIR}/{CONFIG_FILENAME})",
    )


def defaults_for(mapping: Mapping[str, str], values: Mapping[str, object]) -> dict[str, object]:
    """Translate dotted config names into one tool's argparse `dest`s.

    Pure, and strict about the mapping itself: a `dest` pointing at a key that
    is not in the schema is a bug in this repo, not in someone's config file.
    """
    unknown = sorted(name for name in mapping.values() if name not in _BY_NAME)
    if unknown:
        raise ConfigError(f"not config keys: {', '.join(unknown)}")
    return {dest: values[name] for dest, name in mapping.items() if name in values}


def resolve(
    parser: argparse.ArgumentParser,
    mapping: Mapping[str, str],
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    search: Sequence[Path] | None = None,
) -> tuple[argparse.Namespace, Path | None]:
    """Parse `argv` with config values standing in as defaults.

    Returns the arguments and the config that was used, so the caller can say so
    out loud. Two passes over `argv`: the first only to learn `--config`, since
    the file has to be read before the real defaults can be set.
    """
    pre = argparse.ArgumentParser(add_help=False)
    add_config_argument(pre)
    known, _ = pre.parse_known_args(argv)
    path = discover(getattr(known, CONFIG_DEST, None), environ=environ, search=search)
    values = load(path) if path is not None else {}
    parser.set_defaults(**defaults_for(mapping, values))
    return parser.parse_args(argv), path


def resolve_or_exit(
    parser: argparse.ArgumentParser,
    mapping: Mapping[str, str],
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    search: Sequence[Path] | None = None,
) -> tuple[argparse.Namespace, Path | None]:
    """`resolve`, but a bad config exits like any other command-line error.

    A traceback is the wrong way to say "you spelled dca wrong" to someone
    standing at a laptop 20 minutes before kickoff. The message is the same; the
    presentation is argparse's, so it reads like every other mistake the tool
    already knows how to report.
    """
    try:
        return resolve(parser, mapping, argv, environ=environ, search=search)
    except ConfigError as exc:
        parser.error(str(exc))


def require(parser: argparse.ArgumentParser, args: argparse.Namespace, mapping: Mapping[str, str], *dests: str) -> None:
    """Fail for a value that is still missing once the config has had its turn.

    `argparse(required=True)` cannot be used for anything a config may supply,
    because it fires before the file is read. This replaces it, and names both
    ways of providing the value -- the flag and the key -- since whoever hits
    this message has just discovered that a config file exists.
    """
    for dest in dests:
        if getattr(args, dest, None) is not None:
            continue
        flag = "--" + dest.replace("_", "-")
        key = mapping.get(dest)
        where = f" or {key} in the config file" if key else ""
        parser.error(f"{flag} is required{where}")
