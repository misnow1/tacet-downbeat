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
