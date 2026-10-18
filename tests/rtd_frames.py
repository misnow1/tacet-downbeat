"""Real frames from the game 4 capture, as bytes (#210), plus a builder for synthetic ones.

The capture itself is gitignored, so the tests carry the few frames they need.
Every constant here was checked against `data/rtd-game-4-gt.pcap`; the pcap is
never read by a test.
"""

#: One frame is SYN, header, SOH, prefix, offset, STX, text, EOT, checksum, ETB.
TIME_OF_DAY = b"\x1600000000\x010042100039\x02 2:24:54\x040F\x17"
PLAY_CLOCK_39 = b"\x1600000000\x010042100200\x02   39   \x049C\x17"
CLOCK_RUNNING = b"\x1600000000\x010042100027\x02 \x0497\x17"
DOWN_2ND = b"\x1600000000\x010042100221\x022nd\x0477\x17"
TIMEOUT_CLOCK = b"\x1600000000\x010042100031\x02 0:59   \x04CA\x17"
COUNTDOWN = b"\x1600000000\x010042100000\x0270:0670:06   70:0670:06   \x044A\x17"
CLOCK_AT_ZERO = b"\x1600000000\x010042100000\x02 0:00 0:00    0:00 0:00   zs\x04C3\x17"
QUARTER_1 = b"\x1600000000\x010042100141\x02 11ST 1ST Quarter \x04B9\x17"
HOME_SCORE_7 = b"\x1600000000\x010042100107\x02   7\x040D\x17"
GUEST_SCORE_10 = b"\x1600000000\x010042100111\x02  10\x0412\x17"

#: Datagram 1: the whole 353-byte buffer, 378 bytes on the wire.
PREGAME_FULL = (
    b"\x1600000000\x010042100000\x0270:0770:07   70:0770:07                "
    b" 2:24:53VT                  GT                  Tech      GU"
    b"EST        0   0 3     3 3     3            PRE Pregame     "
    b" c                      66016001001         40              "
    b"                                                            "
    b"                                                            "
    b"BALLON        \x046B\x17"
)

#: Datagram 27,347, the last full write of the game.
FINAL_FULL = (
    b"\x1600000000\x010042100000\x02 0:00 0:00    0:00 0:00   zs           "
    b" 6:42:23VT                  GT                  Tech      GU"
    b"EST       34  24 2     2 0     0           44TH 4TH Quarter "
    b" c                      66016001001         40    <POSS     "
    b"254th15                                                     "
    b"                                                       H  25"
    b"BALLON        \x0483\x17"
)

#: Ethernet, IPv4 and UDP in front of datagram 1 (.30 to 255.255.255.255, DF set).
REAL_HEADERS = bytes.fromhex("ffffffffffff92ed636c905a080045000196f51f400040118371c0a8001effffffff520852080182b2ae")

HEADER = b"00000000"
DATA_PREFIX = b"004210"
CHECKSUM_MODULUS = 256


def frame(offset: int, text: str, *, prefix: bytes = DATA_PREFIX, checksum: bytes | None = None) -> bytes:
    """A well-formed frame; the checksum is worked out here, not by `rtd.checksum`."""
    body = HEADER + b"\x01" + prefix + b"%04d" % offset + b"\x02" + text.encode("ascii") + b"\x04"
    digits = b"%02X" % (sum(body) % CHECKSUM_MODULUS) if checksum is None else checksum
    return b"\x16" + body + digits + b"\x17"
