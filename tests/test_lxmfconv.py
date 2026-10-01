import unittest

from hfbridge.frame import MAX_PAYLOAD, Frame, check_payload
from hfbridge.lxmfconv import (
    TITLE_DEST_PREFIX,
    bridge_title,
    frame_from_lxmf,
    parse_bridge_title,
    parse_dest_hash,
    via_title,
)


class LxmfConvTest(unittest.TestCase):
    def test_round_trip_title(self):
        dest = bytes(range(16))
        title = bridge_title(dest)
        self.assertTrue(title.startswith(TITLE_DEST_PREFIX))
        self.assertEqual(parse_bridge_title(title), dest)

    def test_rejects_a_bare_hash_title(self):
        with self.assertRaises(ValueError):
            parse_bridge_title("00" * 16)

    def test_frame_from_lxmf_uses_callsign_and_payload(self):
        dest = bytes(16)
        frame = frame_from_lxmf(
            callsign="N0CALL",
            title=bridge_title(dest),
            content="hello hf",
            msg_id=3,
        )
        self.assertEqual(frame.origin, "N0CALL")
        self.assertEqual(frame.dest, dest)
        self.assertEqual(frame.payload, b"hello hf")
        self.assertEqual(frame.msg_id, 3)
        self.assertEqual(Frame.decode(frame.encode()).payload, b"hello hf")

    def test_via_title_names_the_transmitting_station(self):
        self.assertEqual(via_title("N0CALL"), "hfvia:N0CALL")

    def test_parse_dest_hash_length(self):
        with self.assertRaises(ValueError):
            parse_dest_hash("abcd")

    def test_rejects_empty_and_oversize_payload(self):
        dest = bytes(16)
        title = bridge_title(dest)
        with self.assertRaises(ValueError):
            check_payload(b"")
        with self.assertRaises(ValueError):
            check_payload(b"x" * (MAX_PAYLOAD + 1))
        with self.assertRaises(ValueError):
            frame_from_lxmf(
                callsign="N0CALL",
                title=title,
                content="x" * (MAX_PAYLOAD + 1),
                msg_id=1,
            )
        with self.assertRaises(ValueError):
            Frame(
                origin="N0CALL",
                dest=dest,
                payload=b"x" * (MAX_PAYLOAD + 1),
            ).encode()


if __name__ == "__main__":
    unittest.main()
