import unittest
import threading
from unittest.mock import patch

import numpy as np

from hfbridge.fsk import demodulate
from hfbridge.hl2 import HL2Session, TransmissionCancelled, emergency_stop
from hfbridge.hpsdr import (
    EP2_PACKET_SIZE,
    SAMPLES_PER_PACKET,
    START_PACKET,
    STOP_PACKET,
    ControlBank,
    EP2Builder,
)
from hfbridge.transmit import build_plan, build_tone_plan, main


def ep6_packet() -> bytes:
    packet = bytearray(EP2_PACKET_SIZE)
    packet[:4] = b"\xef\xfe\x01\x06"
    return bytes(packet)


class FakeTransport:
    def __init__(self, receive_packets):
        self.receive_packets = list(receive_packets)
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout):
        if not self.receive_packets:
            raise TimeoutError("empty fake receive queue")
        item = self.receive_packets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class SessionTest(unittest.TestCase):
    def test_finite_burst_is_radio_paced_and_cleans_up(self):
        # This short burst fits entirely in the FIFO preload, so only the
        # initial EP6 clock packet is needed.
        transport = FakeTransport([ep6_packet()])
        builder = EP2Builder(
            controls=ControlBank(pa_enabled=True),
            amplitude=0.002,
        )
        session = HL2Session(transport=transport, builder=builder)
        session.transmit(np.ones(127, dtype=np.complex64))

        self.assertTrue(transport.closed)
        self.assertIn(START_PACKET, transport.sent)
        self.assertEqual(transport.sent[:2], [STOP_PACKET, STOP_PACKET])
        self.assertEqual(transport.sent[-2:], [STOP_PACKET, STOP_PACKET])

        ep2 = [packet for packet in transport.sent if len(packet) == EP2_PACKET_SIZE]
        keyed = [packet for packet in ep2 if packet[11] & 1]
        self.assertEqual(len(keyed), 2)
        self.assertTrue(all(packet[523] & 1 for packet in keyed))
        self.assertTrue(all((packet[11] & 1) == 0 for packet in ep2[-3:]))

        idle = [packet for packet in ep2 if (packet[11] & 1) == 0]
        keyed_at = next(i for i, packet in enumerate(ep2) if packet[11] & 1)
        filter_hits = [
            i
            for i, packet in enumerate(ep2)
            if (packet[11] >> 1) == 0 or (packet[523] >> 1) == 0
        ]
        self.assertEqual(filter_hits, [keyed_at - 1])
        pre_key_indices = [
            packet[base] >> 1
            for packet in ep2[:keyed_at]
            for base in (11, 523)
        ]
        self.assertIn(0x17, pre_key_indices)

    def test_all_tx_packets_are_built_before_streaming_starts(self):
        transport = FakeTransport([ep6_packet()])

        class ObservedBuilder(EP2Builder):
            def build(self, *args, **kwargs):
                self.assert_not_started()
                return super().build(*args, **kwargs)

            def assert_not_started(self):
                if START_PACKET in transport.sent:
                    raise AssertionError("built an EP2 packet after START")

        builder = ObservedBuilder(
            controls=ControlBank(pa_enabled=True),
            amplitude=0.002,
        )
        session = HL2Session(transport=transport, builder=builder)
        session.transmit(np.ones(127, dtype=np.complex64))

    def test_packets_after_fifo_preload_follow_radio_clock(self):
        # 10 pre-roll + 7 data + 4 post-roll = 21 packets. The first 20 are
        # preloaded and the last needs a second EP6 after the initial start.
        transport = FakeTransport([ep6_packet(), ep6_packet()])
        builder = EP2Builder(
            controls=ControlBank(pa_enabled=True),
            amplitude=0.002,
        )
        session = HL2Session(transport=transport, builder=builder)
        session.transmit(np.ones(SAMPLES_PER_PACKET * 7, dtype=np.complex64))
        ep2 = [packet for packet in transport.sent if len(packet) == EP2_PACKET_SIZE]
        keyed = [packet for packet in ep2 if packet[11] & 1]
        self.assertEqual(len(keyed), 7)
        self.assertEqual(transport.receive_packets, [])

    def test_failure_still_sends_mox_off_stop_and_closes(self):
        transport = FakeTransport([TimeoutError("simulated loss")])
        builder = EP2Builder(
            controls=ControlBank(pa_enabled=True),
            amplitude=0.002,
        )
        session = HL2Session(
            transport=transport, builder=builder, receive_timeout=0.05
        )

        with self.assertRaises(TimeoutError):
            session.transmit(np.ones(10, dtype=np.complex64))

        self.assertTrue(transport.closed)
        self.assertEqual(transport.sent[-2:], [STOP_PACKET, STOP_PACKET])
        for packet in transport.sent[-5:-2]:
            self.assertEqual(packet[11] & 1, 0)
            self.assertEqual(packet[523] & 1, 0)

    def test_cancel_before_keying_stops_without_mox(self):
        cancel = threading.Event()

        class CancellingTransport(FakeTransport):
            def recv(self, timeout):
                cancel.set()
                raise TimeoutError("poll")

        transport = CancellingTransport([])
        builder = EP2Builder(controls=ControlBank(pa_enabled=True), amplitude=0.002)
        session = HL2Session(transport=transport, builder=builder)

        with self.assertRaises(TransmissionCancelled):
            session.transmit(np.ones(127, dtype=np.complex64), cancel=cancel)

        ep2 = [packet for packet in transport.sent if len(packet) == EP2_PACKET_SIZE]
        self.assertFalse(any(packet[11] & 1 for packet in ep2))
        self.assertEqual(transport.sent[-2:], [STOP_PACKET, STOP_PACKET])
        self.assertTrue(transport.closed)

    @patch("hfbridge.hl2.UdpTransport")
    def test_emergency_stop_sends_mox_low_and_stop(self, transport_type):
        transport = FakeTransport([])
        transport_type.return_value = transport
        builder = EP2Builder(controls=ControlBank(pa_enabled=True), amplitude=0.5)

        emergency_stop("192.0.2.1", builder)

        self.assertEqual(transport.sent[-2:], [STOP_PACKET, STOP_PACKET])
        self.assertTrue(all((packet[11] & 1) == 0 for packet in transport.sent[:3]))
        self.assertTrue(transport.closed)


class TransmitCliTest(unittest.TestCase):
    def test_dry_run_never_constructs_udp_transport(self):
        with patch("hfbridge.transmit.UdpTransport") as transport:
            result = main(["--callsign", "N0CALL", "--text", "hello"])
        self.assertEqual(result, 0)
        transport.assert_not_called()

    def test_plan_rejects_unsafe_amplitude_and_frequency(self):
        common = {
            "callsign": "N0CALL",
            "text": "hello",
            "destination": "00" * 16,
            "message_id": 1,
        }
        with self.assertRaises(ValueError):
            build_plan(**common, frequency_hz=28_120_000, amplitude=0.002)
        with self.assertRaises(ValueError):
            build_plan(**common, frequency_hz=28_124_000, amplitude=1.1)
        with self.assertRaises(ValueError):
            build_plan(**common, frequency_hz=28_124_000, amplitude=0.02, drive=256)

    def test_drive_reaches_the_control_bank(self):
        plan = build_plan(
            callsign="N0CALL",
            text="hi",
            destination="00" * 16,
            message_id=1,
            frequency_hz=28_124_000,
            amplitude=0.02,
            drive=127,
        )
        # C0 index 9 carries drive in C1; confirm it is not left at the
        # hardware minimum, which produces no usable output.
        self.assertEqual(plan.drive, 127)
        control = ControlBank(frequency_hz=28_124_000, drive=127, pa_enabled=True)
        self.assertEqual(control.control(9, mox=True)[1], 127)

    def test_tone_plan_is_a_steady_offset_carrier(self):
        plan = build_tone_plan(
            seconds=2.0, frequency_hz=28_124_000, amplitude=0.02, drive=127
        )
        self.assertIsNone(plan.frame)
        self.assertAlmostEqual(plan.airtime_seconds, 2.0, places=3)
        spectrum = np.abs(np.fft.fft(plan.iq))
        peak_hz = np.fft.fftfreq(len(plan.iq), 1 / 48_000)[int(np.argmax(spectrum))]
        self.assertAlmostEqual(peak_hz, 1000.0, delta=1.0)
        with self.assertRaises(ValueError):
            build_tone_plan(seconds=120, frequency_hz=28_124_000, amplitude=0.02)

    def test_48khz_plan_round_trips_through_existing_demodulator(self):
        plan = build_plan(
            callsign="N0CALL",
            text="hello hf",
            destination="00" * 16,
            message_id=7,
            frequency_hz=28_124_000,
            amplitude=0.002,
        )
        self.assertGreater(len(plan.iq), 0)
        self.assertGreater(len(plan.packets), 0)
        decoded = demodulate(plan.iq, sample_rate=48_000)
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello hf")


if __name__ == "__main__":
    unittest.main()
