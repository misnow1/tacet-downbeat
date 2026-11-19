import unittest
from unittest import mock

from tacet.net import TransportError, UdpSender, is_ipv4_literal

#: Past the 16-bit port field. `sendto` refuses it before anything reaches the
#: wire, with `OverflowError` rather than an `OSError` (#72).
UNSENDABLE_PORT = 499000


class TestUdpSender(unittest.TestCase):
    def test_a_sender_error_that_is_not_an_oserror_is_a_transport_error(self):
        sender = UdpSender("127.0.0.1", UNSENDABLE_PORT)
        self.addCleanup(sender.close)
        with self.assertRaises(TransportError) as caught:
            sender.send(b"")
        self.assertIsInstance(caught.exception.__cause__, OverflowError)
        self.assertIn(f"127.0.0.1:{UNSENDABLE_PORT}", str(caught.exception))


#: What is refused, and what it is: names, an IPv6 literal, nothing, and a
#: leading zero, which some resolvers read as octal.
NOT_LITERALS = ("localhost", "dm7.local", "::1", "", "010.0.0.5", "10.0.0", "10.0.0.256", " 10.0.0.5")


class TestUdpSenderTakesALiteral(unittest.TestCase):
    """#71: a name would be looked up again by every `sendto`."""

    def test_an_ipv4_literal_is_accepted(self):
        sender = UdpSender("127.0.0.1", 49900)
        self.addCleanup(sender.close)
        self.assertEqual(sender.host, "127.0.0.1")

    def test_a_name_is_refused_before_a_socket_opens(self):
        for host in NOT_LITERALS:
            with self.subTest(host=host), mock.patch("tacet.net.socket.socket") as opened:
                with self.assertRaises(ValueError) as caught:
                    UdpSender(host, 49900)
                opened.assert_not_called()
                self.assertIn(repr(host), str(caught.exception))

    def test_is_ipv4_literal(self):
        for host in ("127.0.0.1", "10.0.0.5", "0.0.0.0"):
            with self.subTest(host=host):
                self.assertTrue(is_ipv4_literal(host))
        for host in NOT_LITERALS:
            with self.subTest(host=host):
                self.assertFalse(is_ipv4_literal(host))


if __name__ == "__main__":
    unittest.main()
