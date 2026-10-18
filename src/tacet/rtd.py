"""Decode the Daktronics RTD stream: framing, checksum, display buffer, football field map (#210).

Pure and standard library only. No sockets, files, clock or timestamps: the
offline replay (`tacet.rtd_replay`) and the live reader (#95) both feed it bytes
and keep their own time. Whatever the decoder says is context for the operator,
never an interlock (design.md 5.4).

One frame is

    SYN "00000000" SOH "004210" NNNN STX <text> EOT <2 hex digits> ETB

A write puts `<text>` into the display buffer starting at cell NNNN. Two things
the published notes and design.md 8 get wrong, both settled against Game 4's
27,356 frames (#206):

- The checksum is the sum of every byte after SYN through EOT inclusive, mod
  256, as two uppercase hex digits. It is not payload-only.
- Offsets are zero-based. The published field offsets are one-based, so the
  play clock the manual puts at 201 is written at 200.

A field reads `None` until every one of its cells has been written once, so a
blank (written spaces) is told apart from a field the stream never set. Values
are the raw characters: never stripped, never parsed.

The field map is what Game 4 showed, and its `unmapped` tripwire is quiet for
that whole game: a write that changes a cell outside every field is reported,
so a field this map does not know about shows up rather than being lost.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

SYN = 0x16
SOH = 0x01
STX = 0x02
EOT = 0x04
ETB = 0x17

HEADER_LENGTH = 8
PREFIX_LENGTH = 6
OFFSET_LENGTH = 4
CHECKSUM_LENGTH = 2
CHECKSUM_MODULUS = 256

#: Positions in a frame, each derived from the one before.
SOH_AT = 1 + HEADER_LENGTH
PREFIX_AT = SOH_AT + 1
OFFSET_AT = PREFIX_AT + PREFIX_LENGTH
STX_AT = OFFSET_AT + OFFSET_LENGTH
TEXT_AT = STX_AT + 1
#: EOT, the checksum digits and ETB, counted back from the end of the frame.
TRAILER_LENGTH = 1 + CHECKSUM_LENGTH + 1
MIN_FRAME_LENGTH = TEXT_AT + TRAILER_LENGTH

DATA_PREFIX = b"004210"
#: A full write at offset 0 is this long; Game 4 never wrote past it.
BUFFER_SIZE = 353
PRINTABLE_FIRST = 0x20
PRINTABLE_LAST = 0x7E
#: The published field offsets count from 1; the wire counts from 0.
PUBLISHED_OFFSET_BASE = 1

_SYN_BYTE = bytes((SYN,))
_ETB_BYTE = bytes((ETB,))
_HEX_DIGITS = "%02X"


class Fault(enum.Enum):
    #: SYN with no ETB before the datagram ended or the next SYN began.
    TRUNCATED = "truncated"
    #: Too short, control bytes misplaced, non-digit header, prefix or offset, non-printable text.
    MALFORMED = "malformed"
    CHECKSUM = "checksum"
    #: offset + len(text) runs past the display; the write is not applied, not even partly.
    BEYOND_BUFFER = "beyond-buffer"
    #: Bytes outside any SYN..ETB, one fault per contiguous run.
    GARBAGE = "garbage"


@dataclass(frozen=True)
class Frame:
    prefix: str
    offset: int
    text: str

    @property
    def is_write(self) -> bool:
        return self.prefix.encode("ascii") == DATA_PREFIX


@dataclass(frozen=True)
class FrameFault:
    fault: Fault
    raw: bytes


def checksum(body: bytes) -> bytes:
    """The two uppercase hex digits for `body`: the bytes after SYN through EOT."""
    return (_HEX_DIGITS % (sum(body) % CHECKSUM_MODULUS)).encode("ascii")


def _control_bytes_in_place(raw: bytes) -> bool:
    eot = len(raw) - TRAILER_LENGTH
    expected = ((0, SYN), (SOH_AT, SOH), (STX_AT, STX), (eot, EOT), (len(raw) - 1, ETB))
    return all(raw[at] == byte for at, byte in expected)


def _digits_where_digits_belong(raw: bytes) -> bool:
    return all(raw[start:stop].isdigit() for start, stop in ((1, SOH_AT), (PREFIX_AT, OFFSET_AT), (OFFSET_AT, STX_AT)))


def _printable(text: bytes) -> bool:
    return all(PRINTABLE_FIRST <= byte <= PRINTABLE_LAST for byte in text)


def parse_frame(raw: bytes) -> Frame | FrameFault:
    """Decode one frame, `raw` being SYN through ETB inclusive."""
    if len(raw) < MIN_FRAME_LENGTH or not _control_bytes_in_place(raw) or not _digits_where_digits_belong(raw):
        return FrameFault(Fault.MALFORMED, raw)
    eot = len(raw) - TRAILER_LENGTH
    text = raw[TEXT_AT:eot]
    if not _printable(text):
        return FrameFault(Fault.MALFORMED, raw)
    if checksum(raw[1 : eot + 1]) != raw[eot + 1 : eot + 1 + CHECKSUM_LENGTH]:
        return FrameFault(Fault.CHECKSUM, raw)
    return Frame(
        prefix=raw[PREFIX_AT:OFFSET_AT].decode("ascii"),
        offset=int(raw[OFFSET_AT:STX_AT]),
        text=text.decode("ascii"),
    )


def parse_datagram(payload: bytes) -> list[Frame | FrameFault]:
    """Every frame in one datagram, and a fault for each run of bytes that is not one."""
    found: list[Frame | FrameFault] = []
    pos = 0
    while pos < len(payload):
        syn = payload.find(_SYN_BYTE, pos)
        if syn < 0:
            found.append(FrameFault(Fault.GARBAGE, payload[pos:]))
            break
        if syn > pos:
            found.append(FrameFault(Fault.GARBAGE, payload[pos:syn]))
        etb = payload.find(_ETB_BYTE, syn + 1)
        next_syn = payload.find(_SYN_BYTE, syn + 1)
        if etb < 0 or 0 <= next_syn < etb:
            end = next_syn if next_syn >= 0 else len(payload)
            found.append(FrameFault(Fault.TRUNCATED, payload[syn:end]))
            pos = end
            continue
        found.append(parse_frame(payload[syn : etb + 1]))
        pos = etb + 1
    return found


@dataclass(frozen=True)
class Field:
    name: str
    #: Zero-based, as written on the wire.
    offset: int
    width: int

    @property
    def end(self) -> int:
        return self.offset + self.width


# The football field map, as Game 4 showed it. Each comment is what was seen.
#: `70:07` as the pregame countdown, ` 2:00` in play; right-aligned m:ss.
MAIN_CLOCK = Field("main_clock", 0, 5)
#: Identical to the main clock all game; what they are for is unknown.
MAIN_CLOCK_COPY_1 = Field("main_clock_copy_1", 5, 5)
MAIN_CLOCK_COPY_2 = Field("main_clock_copy_2", 13, 5)
MAIN_CLOCK_COPY_3 = Field("main_clock_copy_3", 18, 5)
#: `z` at the end of each period and of the pregame countdown, else blank.
CLOCK_AT_ZERO = Field("clock_at_zero", 26, 1)
#: `s` or blank.
MAIN_CLOCK_STOPPED = Field("main_clock_stopped", 27, 1)
#: ` 1:00`, then ` 0:59` for a second or three at each team timeout.
TIMEOUT_CLOCK = Field("timeout_clock", 31, 8)
#: ` 6:42:32`: twelve-hour, leading space.
TIME_OF_DAY = Field("time_of_day", 39, 8)
HOME_NAME = Field("home_name", 47, 20)
GUEST_NAME = Field("guest_name", 67, 20)
#: `Tech`.
HOME_SHORT_NAME = Field("home_short_name", 87, 10)
#: `GUEST`.
GUEST_SHORT_NAME = Field("guest_short_name", 97, 10)
#: Right-aligned.
HOME_SCORE = Field("home_score", 107, 4)
GUEST_SCORE = Field("guest_score", 111, 4)
#: Timeouts left; each moves together with its copy.
HOME_TIMEOUTS = Field("home_timeouts", 116, 1)
HOME_TIMEOUTS_COPY = Field("home_timeouts_copy", 122, 1)
GUEST_TIMEOUTS = Field("guest_timeouts", 124, 1)
GUEST_TIMEOUTS_COPY = Field("guest_timeouts_copy", 130, 1)
#: The timeout indicator, `<TIME`.
HOME_TIMEOUT_TEXT = Field("home_timeout_text", 131, 5)
#: `>TIME`.
GUEST_TIMEOUT_TEXT = Field("guest_timeout_text", 136, 5)
#: ` 1` to ` 4`; blank before the game and at halftime.
QUARTER = Field("quarter", 141, 2)
#: `PRE `, `1ST `, `HALF`.
QUARTER_TEXT = Field("quarter_text", 143, 4)
#: `Pregame`, `1ST Quarter`, `Halftime`.
QUARTER_DESCRIPTION = Field("quarter_description", 147, 12)
#: `c`, constant all game; meaning unknown.
UNKNOWN_160 = Field("unknown_160", 160, 1)
#: `66016001001`, constant all game; meaning unknown.
UNKNOWN_183 = Field("unknown_183", 183, 11)
#: `   40   `; the two digits sit at 203 and 204.
PLAY_CLOCK = Field("play_clock", 200, 8)
#: `<POSS`.
HOME_POSSESSION = Field("home_possession", 209, 5)
#: `>POSS`.
GUEST_POSSESSION = Field("guest_possession", 214, 5)
#: `25`; unset in the morning, populated in the game.
BALL_ON = Field("ball_on", 219, 2)
#: `1st` to `4th`.
DOWN = Field("down", 221, 3)
#: `10`.
TO_GO = Field("to_go", 224, 2)
#: `H` or `V`.
BALL_ON_SIDE = Field("ball_on_side", 334, 1)
#: A second copy of the ball-on yard line.
BALL_ON_COPY = Field("ball_on_copy", 337, 2)
#: `BALLON`; the label sits at 339, not 340.
BALL_ON_LABEL = Field("ball_on_label", 339, 6)

FIELDS: tuple[Field, ...] = (
    MAIN_CLOCK,
    MAIN_CLOCK_COPY_1,
    MAIN_CLOCK_COPY_2,
    MAIN_CLOCK_COPY_3,
    CLOCK_AT_ZERO,
    MAIN_CLOCK_STOPPED,
    TIMEOUT_CLOCK,
    TIME_OF_DAY,
    HOME_NAME,
    GUEST_NAME,
    HOME_SHORT_NAME,
    GUEST_SHORT_NAME,
    HOME_SCORE,
    GUEST_SCORE,
    HOME_TIMEOUTS,
    HOME_TIMEOUTS_COPY,
    GUEST_TIMEOUTS,
    GUEST_TIMEOUTS_COPY,
    HOME_TIMEOUT_TEXT,
    GUEST_TIMEOUT_TEXT,
    QUARTER,
    QUARTER_TEXT,
    QUARTER_DESCRIPTION,
    UNKNOWN_160,
    UNKNOWN_183,
    PLAY_CLOCK,
    HOME_POSSESSION,
    GUEST_POSSESSION,
    BALL_ON,
    DOWN,
    TO_GO,
    BALL_ON_SIDE,
    BALL_ON_COPY,
    BALL_ON_LABEL,
)


@dataclass(frozen=True)
class FieldChange:
    field: Field
    #: None when the field had never been completely written.
    old: str | None
    new: str


@dataclass(frozen=True)
class Applied:
    #: In FIELDS order.
    changes: tuple[FieldChange, ...]
    #: Cells outside every field that held a written character and now hold a different one.
    unmapped: tuple[int, ...]


class Display:
    """The receiver's copy of the scoreboard's character buffer."""

    def __init__(self, size: int = BUFFER_SIZE, fields: Sequence[Field] = FIELDS) -> None:
        for f in fields:
            if f.offset < 0 or f.width <= 0 or f.end > size:
                raise ValueError(f"field {f.name} ({f.offset}+{f.width}) does not fit a {size}-cell display")
        self._cells: list[str | None] = [None] * size
        self._fields = tuple(fields)
        self._mapped = {cell for f in fields for cell in range(f.offset, f.end)}

    def value(self, field: Field) -> str | None:
        cells = self._cells[field.offset : field.end]
        return None if None in cells else "".join(c for c in cells if c is not None)

    def snapshot(self) -> dict[str, str | None]:
        return {f.name: self.value(f) for f in self._fields}

    def apply(self, frame: Frame) -> Applied | FrameFault:
        if not frame.is_write:
            raise ValueError(f"prefix {frame.prefix!r} is not a write")
        end = frame.offset + len(frame.text)
        if end > len(self._cells):
            return FrameFault(Fault.BEYOND_BUFFER, b"")
        touched = [f for f in self._fields if f.offset < end and f.end > frame.offset]
        before = {f.name: self.value(f) for f in touched}
        unmapped: list[int] = []
        for cell, char in enumerate(frame.text, start=frame.offset):
            was = self._cells[cell]
            if was is not None and was != char and cell not in self._mapped:
                unmapped.append(cell)
            self._cells[cell] = char
        changes = tuple(
            FieldChange(f, before[f.name], now)
            for f in touched
            if (now := self.value(f)) is not None and now != before[f.name]
        )
        return Applied(changes, tuple(unmapped))
