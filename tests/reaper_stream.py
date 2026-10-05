"""What Reaper sounds like to the box, shared by the tests (#163).

On the press box rig Reaper streams meters whenever its audio device runs,
parked or rolling, and `/time` only while the transport moves. The box will not
call a transport parked until it has listened for a full timeout without
hearing `/time`, so a test that wants a startable Reaper has to have listened.
"""

from __future__ import annotations

from typing import Any

from tacet import osc, reaper

#: A meter address from the bench capture.
METER = "/master/vu"
#: A little more than the timeout, so float rounding in `now - start` cannot
#: leave the listening a hair short of it.
LISTEN_MARGIN = 0.5


def meter_packet() -> bytes:
    return osc.encode_message(METER, 0.0)


def listened_parked(client: Any, now: float) -> None:
    """Make `client` (a `ReaperClient`) have listened to a parked, open Reaper
    for a full timeout, ending just before `now`: meters, and no `/time`."""
    timeout = reaper.DEFAULT_FEEDBACK_TIMEOUT
    for at in (now - timeout - LISTEN_MARGIN, now - timeout / 2):
        client.state = reaper.apply_feedback(meter_packet(), client.state, now=at)


#: The packets of a rolling Reaper on the bench, one tick's worth (#163).
ROLLING_MIX = (METER, "/track/1/vu", "/time", "/time/str", "/beat/str", "/samples", "/frames/str")
TICKS_PER_SECOND = 12
MIX_SECONDS = 5
METERS_PER_SECOND = 11
#: The `/time` family: one reading arrives as these five, together.
CLOCK_FAMILY = ("/time", "/time/str", "/beat/str", "/samples", "/frames/str")


def rolling_tick(address: str, tick: int) -> bytes:
    value: float | str = "0:00.0" if address.endswith("/str") else float(tick)
    return osc.encode_message(address, value)


def mid_take_stream(lead: float, *, clock: bool = True, seconds: float = MIX_SECONDS, start: float = 100.0):
    """A rolling Reaper heard from the middle, one packet at a time.

    Meters at 11 a second and the `/time` family at 12, on independent
    cadences, the first meter leading the first `/time` by `lead` seconds. No
    `/record` or `/play` was ever announced to this box. Yields `(time, packet)`
    in arrival order.
    """
    events: list[tuple[float, int, bytes]] = []
    order = 0
    for k in range(int(seconds * METERS_PER_SECOND)):
        events.append((start + k / METERS_PER_SECOND, order, rolling_tick(METER, k)))
        order += 1
    if clock:
        for k in range(int(seconds * TICKS_PER_SECOND)):
            for address in CLOCK_FAMILY:
                events.append((start + lead + k / TICKS_PER_SECOND, order, rolling_tick(address, k)))
                order += 1
    events.sort()
    for at, _, packet in events:
        yield at, packet


#: The refresh all surfaces action, answered (bench, 2026-10-04, #172).
#: Worst reply latency measured.
REFRESH_REPLY_SECONDS = 0.04
#: How long `/time` stalled while the reply's dump went out.
REFRESH_STALL_SECONDS = 1.5
#: The dump on the 30-track template was 3,300 to 3,600 messages; its middle.
REFRESH_DUMP_MESSAGES = 3400
#: Messages in the transport report, in bench order: record, stop, pause, play.
REFRESH_TRANSPORT_MESSAGES = 4
#: Addresses the dump is made of. None of them is `/time`.
DUMP_ADDRESSES = ("/track/{n}/name", "/track/{n}/recarm", "/tempo/raw", "/lastmarker/name")
DUMP_TRACKS = 30


def refresh_reply(*, recording: bool, playing: bool) -> list[bytes]:
    """The transport report a refresh draws, in bench order, one packet each."""
    stopped = not recording and not playing
    return [
        osc.encode_message("/record", float(recording)),
        osc.encode_message("/stop", float(stopped)),
        osc.encode_message("/pause", 0.0),
        osc.encode_message("/play", float(playing)),
    ]


def refresh_dump(sent_at: float, *, recording: bool, playing: bool, transport_first: bool = True):
    """The reply to a refresh sent at `sent_at`, one message per packet (the
    worst case for prefixes). Yields `(time, packet)`.

    Spread evenly from the reply latency to the end of the stall, with the four
    transport messages at the start of it, or at the end when
    `transport_first` is False.
    """
    transport = refresh_reply(recording=recording, playing=playing)
    others = [
        osc.encode_message(DUMP_ADDRESSES[k % len(DUMP_ADDRESSES)].format(n=k % DUMP_TRACKS), float(k))
        for k in range(REFRESH_DUMP_MESSAGES - REFRESH_TRANSPORT_MESSAGES)
    ]
    packets = transport + others if transport_first else others + transport
    first = sent_at + REFRESH_REPLY_SECONDS
    last = sent_at + REFRESH_STALL_SECONDS
    for k, packet in enumerate(packets):
        yield first + (last - first) * k / (len(packets) - 1), packet


def rolling_with_refresh(
    sent_at: float,
    *,
    recording: bool = True,
    transport_first: bool = True,
    seconds: float = MIX_SECONDS,
    lead: float = 0.05,
):
    """A rolling Reaper that is asked for its state at `sent_at`, one packet at
    a time, in arrival order: the mix with its `/time` family silent for the
    stall, and the dump interleaved. Meters keep flowing, as they did on the
    bench. The mix starts at `sent_at`."""
    stall_end = sent_at + REFRESH_STALL_SECONDS
    events: list[tuple[float, int, bytes]] = []
    for at, packet in mid_take_stream(lead, seconds=seconds, start=sent_at):
        decoded = osc.decode_packet(packet)
        assert isinstance(decoded, osc.Message)
        if decoded.address in CLOCK_FAMILY and at < stall_end:
            continue
        events.append((at, len(events), packet))
    for at, packet in refresh_dump(sent_at, recording=recording, playing=recording, transport_first=transport_first):
        events.append((at, len(events), packet))
    events.sort()
    for at, _, packet in events:
        yield at, packet
