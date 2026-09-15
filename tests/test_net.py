import unittest

from tacet.net import TransportError, UdpSender

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


if __name__ == "__main__":
    unittest.main()
