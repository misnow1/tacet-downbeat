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
#: Meter updates a second on one address: about 1.2 with the Pilot on (1.4
#: rolling), none at all with no signal (bench, 2026-11-16). #163's "about 11
#: packets a second" counted messages across several addresses; the cadence the
#: box sees on any one is this, about 0.83 s between updates at worst.
METERS_PER_SECOND = 1.2
#: The denser cadence #163's game-rig capture reported (about 11 packets a
#: second). The field rate with live inputs is unknown, and `/record` is the
#: irreversible path, so the safety tests run at both.
DENSE_METERS_PER_SECOND = 11
#: The `/time` family: one reading arrives as these five, together.
CLOCK_FAMILY = ("/time", "/time/str", "/beat/str", "/samples", "/frames/str")


def rolling_tick(address: str, tick: int) -> bytes:
    value: float | str = "0:00.0" if address.endswith("/str") else float(tick)
    return osc.encode_message(address, value)


def mid_take_stream(
    lead: float,
    *,
    clock: bool = True,
    seconds: float = MIX_SECONDS,
    start: float = 100.0,
    meters_per_second: float = METERS_PER_SECOND,
):
    """A rolling Reaper heard from the middle, one packet at a time.

    Meters at `meters_per_second` and the `/time` family at 12, on independent
    cadences, the first meter leading the first `/time` by `lead` seconds. No
    `/record` or `/play` was ever announced to this box. Yields `(time, packet)`
    in arrival order.
    """
    events: list[tuple[float, int, bytes]] = []
    order = 0
    for k in range(int(seconds * meters_per_second)):
        events.append((start + k / meters_per_second, order, rolling_tick(METER, k)))
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


def refresh_dump(
    sent_at: float,
    *,
    recording: bool,
    playing: bool,
    transport_first: bool = True,
    stall: float = REFRESH_STALL_SECONDS,
):
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
    last = sent_at + stall
    for k, packet in enumerate(packets):
        yield first + (last - first) * k / (len(packets) - 1), packet


def rolling_with_refresh(
    sent_at: float,
    *,
    recording: bool = True,
    transport_first: bool = True,
    seconds: float = MIX_SECONDS,
    lead: float = 0.05,
    stall: float = REFRESH_STALL_SECONDS,
):
    """A rolling Reaper that is asked for its state at `sent_at`, one packet at
    a time, in arrival order: the mix with its `/time` family silent for the
    stall, and the dump interleaved. Meters keep flowing, as they did on the
    bench. The mix starts at `sent_at`."""
    stall_end = sent_at + stall
    events: list[tuple[float, int, bytes]] = []
    for at, packet in mid_take_stream(lead, seconds=seconds, start=sent_at):
        decoded = osc.decode_packet(packet)
        assert isinstance(decoded, osc.Message)
        if decoded.address in CLOCK_FAMILY and at < stall_end:
            continue
        events.append((at, len(events), packet))
    for at, packet in refresh_dump(
        sent_at, recording=recording, playing=recording, transport_first=transport_first, stall=stall
    ):
        events.append((at, len(events), packet))
    events.sort()
    for at, _, packet in events:
        yield at, packet


#: One `/time` interval, in seconds: how far apart two readings of a moving
#: playhead are, and how far the playhead moves between them.
TIME_INTERVAL = 1 / TICKS_PER_SECOND


def time_packet(position: float) -> bytes:
    return osc.encode_message("/time", position)


def rolled(position: float, now: float) -> list[tuple[float, bytes]]:
    """Two `/time` readings one interval apart, the second reading `position`
    at `now`: a playhead seen to move (#233). A lone `/time` is not enough to
    be stamped, so a test that wants an entry stamped feeds these."""
    return [(now - TIME_INTERVAL, time_packet(position - TIME_INTERVAL)), (now, time_packet(position))]


#: The Game 5 log (2026-11-14): the take's last playhead, which every refresh
#: answer after the audio device went carried again, 291 times.
GAME_5_TAKE_END = 5087.6728515625
#: Seconds from the last streamed `/time` to the first refresh answer.
GAME_5_FIRST_ANSWER_SECONDS = 13.5
#: Seconds between each answer and the next, from the log.
GAME_5_ANSWER_GAPS = (21.7, 13.0, 20.2, 18.3, 20.5, 18.1, 16.2, 32.4, 5.2)
#: Tracks in the template, one `vu` each in a refresh dump.
GAME_5_TRACKS = 34
#: From the burst's trigger to its repeated `/time`. In the log each found entry
#: lands about 2.0 s after its `commanded` entry; less the mirror's 0.25 s poll
#: that is about 1.75 s. It must stay inside one feedback run (under the 2 s
#: timeout), or the burst would start a new run of its own.
GAME_5_DUMP_SECONDS = 1.75


GAME_5_START = 100.0
GAME_5_ROLLING_SECONDS = 6.0
#: When the default stream's healthy roll has ended: the parked listening, then
#: the roll. Everything after is the silence and the bursts.
GAME_5_ROLL_ENDS = GAME_5_START + reaper.DEFAULT_FEEDBACK_TIMEOUT + LISTEN_MARGIN + GAME_5_ROLLING_SECONDS


def game_5_stream(
    *,
    dump_meters: bool,
    rolling_seconds: float = GAME_5_ROLLING_SECONDS,
    start: float = GAME_5_START,
    meters_per_second: float = METERS_PER_SECOND,
):
    """The Game 5 pattern, one packet at a time as `(time, packet)`.

    The stream starts as the field did at 18:09:45 and 19:03:07: the box is
    already hearing a metered, parked Reaper, and its first refresh is answered
    (`/record 0`). The take then starts inside that same run of feedback
    (`/record 1, /stop 0, /play 1`), so it is a start with no refresh to answer
    and writes one anchor and no found entry. A healthy roll follows, with
    meters and `/time` ending at `GAME_5_TAKE_END`; then the audio device goes,
    and Reaper is silent until a burst: a `/lastmarker/name` that triggers it,
    the reply to the refresh it draws (`/record 1`, `/play 1`), a short dump -
    one `/track/N/vu` per track when `dump_meters` - and the `/time` that has
    not moved. `meters_per_second=0` is silent inputs: no signal, no meters.
    """
    events: list[tuple[float, int, bytes]] = []

    def add(at: float, packet: bytes) -> None:
        events.append((at, len(events), packet))

    for packet in refresh_reply(recording=False, playing=False):
        add(start + REFRESH_REPLY_SECONDS, packet)
    roll_start = start + reaper.DEFAULT_FEEDBACK_TIMEOUT + LISTEN_MARGIN
    for address, value in (("/record", 1.0), ("/stop", 0.0), ("/play", 1.0)):
        add(roll_start, osc.encode_message(address, value))
    ticks = int(rolling_seconds * TICKS_PER_SECOND)
    for k in range(ticks):
        at = roll_start + k * TIME_INTERVAL
        add(at, time_packet(GAME_5_TAKE_END - (ticks - 1 - k) * TIME_INTERVAL))
    parked = roll_start - start
    if meters_per_second > 0:
        for k in range(int((parked + rolling_seconds) * meters_per_second)):
            add(start + k / meters_per_second, meter_packet())
    last_roll = roll_start + (ticks - 1) * TIME_INTERVAL
    burst_at = last_roll + GAME_5_FIRST_ANSWER_SECONDS
    for gap in (0.0, *GAME_5_ANSWER_GAPS):
        burst_at += gap
        add(burst_at, osc.encode_message("/lastmarker/name", "marker"))
        reply_at = burst_at + REFRESH_REPLY_SECONDS
        for packet in refresh_reply(recording=True, playing=True):
            add(reply_at, packet)
        if dump_meters:
            for n in range(GAME_5_TRACKS):
                add(reply_at + 0.01, osc.encode_message(f"/track/{n + 1}/vu", 0.0))
        add(burst_at + GAME_5_DUMP_SECONDS, time_packet(GAME_5_TAKE_END))
    events.sort()
    for at, _, packet in events:
        yield at, packet
