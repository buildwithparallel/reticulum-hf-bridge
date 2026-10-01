import unittest

from hfbridge.frame import VERSION_LIGHT, Frame


def _full(**kwargs) -> Frame:
    fields = dict(
        origin="N0CALL", dest=bytes(16), payload=b"hello", msg_id=7
    )
    fields.update(kwargs)
    return Frame(**fields)


class FrameTest(unittest.TestCase):
    def test_light_round_trip_drops_dest_and_msg_id(self):
        frame = _full(version=VERSION_LIGHT, dest=bytes(range(16)), msg_id=99)
        encoded = frame.encode()
        decoded = Frame.decode(encoded)
        self.assertEqual(decoded.version, VERSION_LIGHT)
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello")
        self.assertEqual(decoded.dest, bytes(16))
        self.assertEqual(decoded.msg_id, 0)
        self.assertLess(len(encoded), len(_full().encode()))

    def test_full_frame_still_carries_dest(self):
        dest = bytes(range(16))
        frame = _full(dest=dest, msg_id=99)
        decoded = Frame.decode(frame.encode())
        self.assertEqual(decoded.dest, dest)
        self.assertEqual(decoded.msg_id, 99)
        self.assertEqual(decoded.payload, b"hello")

    def test_wire_size_follows_the_version_nibble(self):
        full = _full().encode()
        light = _full(version=VERSION_LIGHT).encode()
        self.assertEqual(Frame.wire_size(full), len(full))
        self.assertEqual(Frame.wire_size(light), len(light))
        self.assertIsNone(Frame.wire_size(b""))
        self.assertIsNone(Frame.wire_size(b"\x30"))

    def test_inner_from_ldpc_recovers_a_flipped_version_nibble(self):
        light = _full(version=VERSION_LIGHT).encode()
        broken = bytes([(light[0] & 0x0F) | 0x30]) + light[1:]
        self.assertIsNone(Frame.wire_size(broken))
        recovered = Frame.inner_from_ldpc(broken)
        self.assertIsNotNone(recovered)
        decoded = Frame.decode(recovered)
        self.assertEqual(decoded.payload, b"hello")
        self.assertEqual(decoded.version, VERSION_LIGHT)

        full = _full().encode()
        broken_full = bytes([(full[0] & 0x0F) | 0x30]) + full[1:]
        self.assertIsNone(Frame.wire_size(broken_full))
        recovered_full = Frame.inner_from_ldpc(broken_full)
        self.assertIsNotNone(recovered_full)
        decoded_full = Frame.decode(recovered_full)
        self.assertEqual(decoded_full.payload, b"hello")
        self.assertEqual(decoded_full.msg_id, 7)
