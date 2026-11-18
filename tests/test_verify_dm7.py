"""#242: the nudge probe, the pre-game check that a one dB trim does not zipper.

Nothing here can confirm what the console does: the OSC is write-only and the
console is the only display. What is held is what the tool puts on the wire,
and that it only ever does so when asked.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tacet import dm7, osc, verify_dm7


class RecordingSender:
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)

    def messages(self) -> list[osc.Message]:
        decoded = [osc.decode_packet(p) for p in self.packets]
        assert all(isinstance(m, osc.Message) for m in decoded)
        return cast("list[osc.Message]", decoded)

    def levels(self) -> list[int]:
        return [cast("int", m.args[0]) for m in self.messages()]


class TestTheNudgeProbe(unittest.IsolatedAsyncioTestCase):
    async def probe(self) -> tuple[RecordingSender, str]:
        sender = RecordingSender()
        client = dm7.Dm7Client("192.0.2.1", dca=3, sender=sender)
        out = io.StringIO()
        with redirect_stdout(out):
            await verify_dm7._nudge(client, settle=0)
        return sender, out.getvalue()

    async def test_it_sends_only_fader_level_writes_on_the_dca_address(self):
        sender, _ = await self.probe()
        self.assertGreater(len(sender.packets), 4)
        for message in sender.messages():
            self.assertEqual(message.address, "/yosc:req/set/MIXER:Current/DCA/Fader/Level/3")
            self.assertEqual(len(message.args), 1)
            self.assertIsInstance(message.args[0], int)

    async def test_it_goes_unity_then_minus_one_minus_two_minus_one_and_unity(self):
        sender, _ = await self.probe()
        whole_db = [level for level in sender.levels() if level % 100 == 0]
        self.assertEqual(whole_db, [0, -100, -200, -100, 0])
        self.assertEqual(sender.levels()[0], dm7.UNITY)
        self.assertEqual(sender.levels()[-1], dm7.UNITY)

    async def test_it_never_goes_near_silence(self):
        sender, _ = await self.probe()
        self.assertGreaterEqual(min(sender.levels()), -200)
        self.assertNotIn(dm7.MINUS_INF, sender.levels())

    async def test_it_tells_the_operator_what_to_read_and_listen_for(self):
        _, text = await self.probe()
        for words in ("-1.00 dB", "-2.00 dB", "+0.00 dB", "zipper"):
            with self.subTest(words=words):
                self.assertIn(words, text)


class TestTheFlag(unittest.TestCase):
    def test_nudge_is_a_flag_that_defaults_off(self):
        self.assertFalse(verify_dm7.parser().parse_args([]).nudge)
        self.assertTrue(verify_dm7.parser().parse_args(["--nudge"]).nudge)

    def test_none_of_the_four_probes_is_a_usage_error_naming_all_four(self):
        err = io.StringIO()
        with TemporaryDirectory() as tmp:
            # An explicit, empty file, so a tacet.toml in the working directory
            # cannot change what this test sees.
            empty = Path(tmp) / "empty.toml"
            empty.write_text("", encoding="utf-8")
            with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                verify_dm7.main(["--config", str(empty), "--host", "192.0.2.1", "--dca", "3"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("choose --granularity, --fade, --level or --nudge", err.getvalue())
