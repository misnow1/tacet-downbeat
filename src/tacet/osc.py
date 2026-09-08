"""Minimal OSC 1.0 encoding and decoding.

Hand-rolled rather than pulled from PyPI: the wire format is small, and this
has to run in a press box on a Saturday with nobody available to fix it.
See CLAUDE.md, "Ask before adding dependencies".

Covers what this project speaks: int32, float32, string, blob, the no-argument
types, and bundles (Reaper sends feedback in bundles).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any, NamedTuple

#: OSC time tag meaning "right now".
IMMEDIATELY = 1

#: OSC pads every variable-length field out to a four-byte boundary.
ALIGNMENT = 4

_ADDRESS_PREFIX = "/"
_TYPE_TAG_PREFIX = ","
_BUNDLE_MARKER = "#bundle"
_BUNDLE_PREFIX = b"#bundle\x00"
_TERMINATOR = b"\x00"

_INT32 = struct.Struct(">i")
_UINT64 = struct.Struct(">Q")
_FLOAT32 = struct.Struct(">f")


class TypeTag:
    """OSC type tag characters, from the OSC 1.0 spec."""

    INT32 = "i"
    FLOAT32 = "f"
    STRING = "s"
    BLOB = "b"
    TRUE = "T"
    FALSE = "F"
    NULL = "N"
    INFINITUM = "I"


#: Tags whose whole value is carried by the tag itself, with no payload.
_NO_PAYLOAD = frozenset({TypeTag.TRUE, TypeTag.FALSE, TypeTag.NULL, TypeTag.INFINITUM})


class OscError(ValueError):
    """A malformed OSC packet, or a value that cannot be represented."""


class Message(NamedTuple):
    address: str
    args: tuple[Any, ...]


class Bundle(NamedTuple):
    timetag: int
    elements: tuple[Message | Bundle, ...]


def _padding(length: int) -> int:
    """Bytes needed to round `length` up to the next alignment boundary."""
    return (ALIGNMENT - length % ALIGNMENT) % ALIGNMENT


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def _encode_string(value: str) -> bytes:
    raw = value.encode("utf-8") + _TERMINATOR
    return raw + _TERMINATOR * _padding(len(raw))


def _encode_blob(value: bytes) -> bytes:
    return _INT32.pack(len(value)) + value + _TERMINATOR * _padding(len(value))


def _infer_tag(value: Any) -> str:
    # bool before int: bool is an int subclass and would otherwise encode as 'i'.
    if isinstance(value, bool):
        return TypeTag.TRUE if value else TypeTag.FALSE
    if value is None:
        return TypeTag.NULL
    if isinstance(value, int):
        return TypeTag.INT32
    if isinstance(value, float):
        return TypeTag.FLOAT32
    if isinstance(value, str):
        return TypeTag.STRING
    if isinstance(value, bytes | bytearray):
        return TypeTag.BLOB
    raise OscError(f"cannot infer an OSC type for {type(value).__name__}")


def encode_message(address: str, *args: Any, tags: str | None = None) -> bytes:
    """Encode one OSC message.

    Types are inferred from the Python values unless `tags` is given, which is
    a type tag string without its leading comma. Pass it when the receiver is
    fussy about int-versus-float: the DM7 wants `i` for fader levels.
    """
    if not address.startswith(_ADDRESS_PREFIX):
        raise OscError(f"OSC address must start with {_ADDRESS_PREFIX!r}: {address!r}")

    if tags is None:
        tags = "".join(_infer_tag(a) for a in args)
    elif len(tags) != len(args):
        raise OscError(f"{len(tags)} type tags for {len(args)} arguments")

    out = [_encode_string(address), _encode_string(_TYPE_TAG_PREFIX + tags)]
    for tag, value in zip(tags, args, strict=True):
        if tag == TypeTag.INT32:
            try:
                out.append(_INT32.pack(int(value)))
            except struct.error as exc:
                raise OscError(f"{value!r} does not fit in an OSC int32") from exc
        elif tag == TypeTag.FLOAT32:
            out.append(_FLOAT32.pack(float(value)))
        elif tag == TypeTag.STRING:
            out.append(_encode_string(str(value)))
        elif tag == TypeTag.BLOB:
            out.append(_encode_blob(bytes(value)))
        elif tag in _NO_PAYLOAD:
            pass
        else:
            raise OscError(f"unsupported OSC type tag {tag!r}")
    return b"".join(out)


def encode_bundle(elements: Sequence[bytes], timetag: int = IMMEDIATELY) -> bytes:
    out = [_encode_string(_BUNDLE_MARKER), _UINT64.pack(timetag)]
    for element in elements:
        out.append(_INT32.pack(len(element)))
        out.append(element)
    return b"".join(out)


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def _decode_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(_TERMINATOR, offset)
    if end == -1:
        raise OscError("unterminated OSC string")
    value = data[offset:end].decode("utf-8", errors="replace")
    length = end - offset + 1
    return value, offset + length + _padding(length)


def decode_packet(data: bytes) -> Message | Bundle:
    """Decode one OSC packet, which is either a message or a bundle."""
    if data.startswith(_BUNDLE_PREFIX):
        return _decode_bundle(data)
    return _decode_message(data)


def _decode_bundle(data: bytes) -> Bundle:
    _, offset = _decode_string(data, 0)
    (timetag,) = _UINT64.unpack_from(data, offset)
    offset += _UINT64.size
    elements: list[Message | Bundle] = []
    while offset < len(data):
        (size,) = _INT32.unpack_from(data, offset)
        offset += _INT32.size
        if size < 0 or offset + size > len(data):
            raise OscError("bundle element runs past the end of the packet")
        elements.append(decode_packet(data[offset : offset + size]))
        offset += size
    return Bundle(timetag, tuple(elements))


def _decode_message(data: bytes) -> Message:
    address, offset = _decode_string(data, 0)
    if offset >= len(data):
        return Message(address, ())  # no type tag string: legal, means no args

    tags, offset = _decode_string(data, offset)
    if not tags.startswith(_TYPE_TAG_PREFIX):
        raise OscError(f"type tag string must start with {_TYPE_TAG_PREFIX!r}: {tags!r}")

    args: list[Any] = []
    for tag in tags[1:]:
        if tag == TypeTag.INT32:
            args.append(_INT32.unpack_from(data, offset)[0])
            offset += _INT32.size
        elif tag == TypeTag.FLOAT32:
            args.append(_FLOAT32.unpack_from(data, offset)[0])
            offset += _FLOAT32.size
        elif tag == TypeTag.STRING:
            value, offset = _decode_string(data, offset)
            args.append(value)
        elif tag == TypeTag.BLOB:
            (size,) = _INT32.unpack_from(data, offset)
            offset += _INT32.size
            args.append(bytes(data[offset : offset + size]))
            offset += size + _padding(size)
        elif tag == TypeTag.TRUE:
            args.append(True)
        elif tag == TypeTag.FALSE:
            args.append(False)
        elif tag == TypeTag.NULL:
            args.append(None)
        elif tag == TypeTag.INFINITUM:
            args.append(...)
        else:
            raise OscError(f"unsupported OSC type tag {tag!r}")
    return Message(address, tuple(args))
