import struct
import unittest

import numpy as np

from hfbridge.hpsdr import (
    DISCOVERY_PACKET,
    EP2_PACKET_SIZE,
    SAMPLES_PER_PACKET,
    START_PACKET,
    STOP_PACKET,
    ControlBank,
    EP2Builder,
    is_ep6_packet,
    quantize_iq,
)


class ProtocolPacketsTest(unittest.TestCase):
    def test_command_packet_sizes_and_watchdog(self):
        self.assertEqual(len(DISCOVERY_PACKET), 63)
        self.assertEqual(len(START_PACKET), 64)
        self.assertEqual(len(STOP_PACKET), 64)
        self.assertEqual(START_PACKET[:4], b"\xef\xfe\x04\x01")
        self.assertEqual(START_PACKET[3] & 0x80, 0)

    def test_ep6_validation(self):
        packet = bytearray(EP2_PACKET_SIZE)
        packet[:4] = b"\xef\xfe\x01\x06"
        self.assertTrue(is_ep6_packet(bytes(packet)))
        packet[3] = 0x02
        self.assertFalse(is_ep6_packet(bytes(packet)))


class ControlBankTest(unittest.TestCase):
    def test_frequency_drive_pa_and_mox_bits(self):
        bank = ControlBank(
            frequency_hz=28_124_000,
            drive=0,
            pa_enabled=True,
        )
        self.assertEqual(bank.control(1, mox=False), b"\x02" + (28_124_000).to_bytes(4, "big"))
        self.assertEqual(bank.control(9, mox=False), b"\x12\x00\x08\x00\x00")
        self.assertEqual(bank.control(9, mox=True), b"\x13\x00\x08\x00\x00")
        self.assertEqual(bank.control(0, mox=False), b"\x00\x00\xc0\x00\x04")
        self.assertEqual(bank.control(0x17, mox=False), b"\x2e\x00\x00\x1e\x32")

    def test_tx_buffer_controls_validate_protocol_bit_widths(self):
        with self.assertRaises(ValueError):
            ControlBank(tx_buffer_latency_ms=128)
        with self.assertRaises(ValueError):
            ControlBank(ptt_hang_ms=32)


class IQPackingTest(unittest.TestCase):
    def test_quisk_wire_order_is_imag_then_real(self):
        wire = quantize_iq(
            np.array([0.5 + 0.25j], dtype=np.complex64),
            amplitude=1.0,
        )
        self.assertEqual(int(wire[0, 0]), 8192)
        self.assertEqual(int(wire[0, 1]), 16384)

    def test_builder_layout_sequence_rotation_and_iq(self):
        bank = ControlBank(pa_enabled=True)
        builder = EP2Builder(controls=bank, amplitude=1.0)
        iq = np.zeros(SAMPLES_PER_PACKET, dtype=np.complex64)
        iq[0] = 0.5 + 0.25j
        packet = builder.build(iq, mox=True)

        self.assertEqual(len(packet), EP2_PACKET_SIZE)
        self.assertEqual(packet[:4], b"\xef\xfe\x01\x02")
        self.assertEqual(struct.unpack_from(">I", packet, 4)[0], 0)
        self.assertEqual(packet[8:11], b"\x7f\x7f\x7f")
        self.assertEqual(packet[11] & 1, 1)
        self.assertEqual(packet[11] >> 1, 1)
        self.assertEqual(packet[520:523], b"\x7f\x7f\x7f")
        self.assertEqual(packet[523] >> 1, 1)
        self.assertEqual(struct.unpack_from(">hh", packet, 20), (8192, 16384))

        second = builder.build(mox=False)
        self.assertEqual(struct.unpack_from(">I", second, 4)[0], 1)
        self.assertEqual(second[11] >> 1, 1)
        self.assertEqual(second[523] >> 1, 2)
        self.assertEqual(second[11] & 1, 0)

    def test_idle_rotation_does_not_retoggle_n2adr_relays(self):
        builder = EP2Builder(controls=ControlBank(pa_enabled=True), amplitude=1.0)
        indices = []
        for _ in range(24):
            packet = builder.build(mox=False)
            indices.append(packet[11] >> 1)
            indices.append(packet[523] >> 1)
        self.assertNotIn(0, indices)

        seated = builder.build(mox=False, seat_filters=True)
        self.assertEqual(seated[11] >> 1, 0)
        self.assertNotEqual(seated[523] >> 1, 0)
        later = builder.build(mox=False)
        self.assertNotEqual(later[11] >> 1, 0)
        self.assertNotEqual(later[523] >> 1, 0)


if __name__ == "__main__":
    unittest.main()
