"""Real frames from the game 4 and game 5 captures, as bytes (#210, #235), plus a builder for synthetic ones.

The captures are gitignored, so the tests carry the few frames they need.
Every constant here was checked against `data/rtd-game-4-gt.pcap` or, for the
`GAME_5_*` ones, `data/rtd-26111401-stanford.pcap`; no test reads a pcap.
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


# --- Game 5 (2026-11-14, Stanford): the frames behind the live game clock (#235). ---
#
# Pulled from `data/rtd-26111401-stanford.pcap` by datagram number (counting RTD
# datagrams to port 21000 in capture order) and carried here so no test reads
# the pcap. Each `_AT` is seconds after the first datagram of its stretch. The
# sender was 192.168.0.25 throughout.

GAME_5_SOURCE = "192.168.0.25"

G5_14620 = b"\x1600000000\x010042100039\x02 7:34:58\x0419\x17"
G5_14620_AT = 0.000000
G5_14621 = b"\x1600000000\x010042100000\x02 0:02 0:02    0:02 0:02   \x04DE\x17"
G5_14621_AT = 0.200887
G5_14625 = b"\x1600000000\x010042100000\x02 0:00 0:00    0:00 0:00   zs\x04C3\x17"
G5_14625_AT = 2.211641
G5_14632 = (
    b"\x1600000000\x010042100000\x02 0:00 0:00    0:00 0:00   zs      "
    b"      7:35:06VT                  Stanford            Tech      G"
    b"UEST        0   0 3     3 3     3            PRE Pregame      c "
    b"                     66016001001         25         >POSS201st10"
    b"                                                                "
    b"                                            V  20BALLON        "
    b"\x04C4\x17"
)
G5_14632_AT = 8.644088
G5_14634 = b"\x1600000000\x010042100000\x0215:0015:00   15:0015:00    \x044E\x17"
G5_14634_AT = 9.248004
G5_14637 = (
    b"\x1600000000\x010042100141\x02 11ST 1ST Quarter  c              "
    b"        66016001001         25                                  "
    b"                                                                "
    b"                                    \x0468\x17"
)
G5_14637_AT = 11.757207
G5_14645 = (
    b"\x1600000000\x010042100000\x0215:0015:00   15:0015:00    s      "
    b"      7:35:16VT                  Stanford            Tech      G"
    b"UEST        0   0 3     3 3     3           11ST 1ST Quarter  c "
    b"                     66016001001         22                     "
    b"                                                                "
    b"                                                 BALLON        "
    b"\x0427\x17"
)
G5_14645_AT = 18.694015
G5_14775 = b"\x1600000000\x010042100027\x02 \x0497\x17"
G5_14775_AT = 87.760329
G5_14783 = b"\x1600000000\x010042100000\x0214:5114:51   14:5114:51   \x0442\x17"
G5_14783_AT = 90.275805
G5_25206 = b"\x1600000000\x010042100000\x02 0:00 0:00    0:00 0:00   zs\x04C3\x17"
G5_25206_AT = 0.000000
G5_25216 = b"\x1600000000\x010042100000\x0220:0020:00   20:0020:00    s            9:18:10\x04A8\x17"
G5_25216_AT = 7.140320
G5_25219 = b"\x1600000000\x010042100141\x02  HALFHalftime    \x0479\x17"
G5_25219_AT = 9.752321
G5_25223 = b"\x1600000000\x010042100027\x02 \x0497\x17"
G5_25223_AT = 11.659719

#: Kickoff, from 19:34:58 in pregame: the frames that matter, not every one.
#: 14625 the clock at zero and stopped; 14632 the first full write, `PRE`;
#: 14634 `15:00`; 14637 the first quarter; 14645 the next full write, which
#: clears the at-zero flag; 14775 the clock released; 14783 `14:51`.
GAME_5_KICKOFF: tuple[tuple[int, float, bytes], ...] = (
    (14620, G5_14620_AT, G5_14620),
    (14621, G5_14621_AT, G5_14621),
    (14625, G5_14625_AT, G5_14625),
    (14632, G5_14632_AT, G5_14632),
    (14634, G5_14634_AT, G5_14634),
    (14637, G5_14637_AT, G5_14637),
    (14645, G5_14645_AT, G5_14645),
    (14775, G5_14775_AT, G5_14775),
    (14783, G5_14783_AT, G5_14783),
)

#: Halftime, from the clock at zero at 21:18:02.
GAME_5_HALFTIME: tuple[tuple[int, float, bytes], ...] = (
    (25206, G5_25206_AT, G5_25206),
    (25216, G5_25216_AT, G5_25216),
    (25219, G5_25219_AT, G5_25219),
    (25223, G5_25223_AT, G5_25223),
)
