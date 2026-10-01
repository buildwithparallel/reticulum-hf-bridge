import unittest

import numpy as np

from hfbridge.frame import VERSION_LIGHT, Frame
from hfbridge.fsk import (
    DEFAULT_BAUD,
    DEFAULT_CHANNEL_RATE,
    DEFAULT_DEVIATION,
    LEGACY_BAUD,
    LEGACY_DEVIATION,
    LEGACY_WIDE_DEVIATION,
    _costas_lock,
    _estimate_offset,
    demodulate,
    modulate,
)


def _frame() -> Frame:
    return Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=1)


def _noise(count: int, std: float, rng: np.random.Generator) -> np.ndarray:
    return (rng.normal(0, std, count) + 1j * rng.normal(0, std, count)).astype(
        np.complex64
    )


class FskTest(unittest.TestCase):
    def test_clean_round_trip(self):
        frame = _frame()
        decoded = demodulate(modulate(frame))
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello hf")

    def test_survives_frequency_error_and_inversion(self):
        frame = _frame()
        clean = modulate(frame)
        n = np.arange(len(clean))
        shifted = (
            np.conj(clean) * np.exp(2j * np.pi * 400.0 * n / DEFAULT_CHANNEL_RATE)
        ).astype(np.complex64)
        decoded = demodulate(shifted)
        self.assertEqual(decoded.payload, frame.payload)

    def test_decodes_a_burst_with_quiet_edges(self):
        rng = np.random.default_rng(0)
        frame = _frame()
        burst = modulate(frame)
        quiet = _noise(int(DEFAULT_CHANNEL_RATE * 0.4), 0.05, rng)
        decoded = demodulate(np.concatenate([quiet, burst + _noise(len(burst), 0.05, rng), quiet]))
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello hf")

    def test_offset_estimate_is_not_pulled_by_unbalanced_data(self):
        frame = _frame()
        clean = modulate(frame)
        n = np.arange(len(clean))
        shifted = (
            clean * np.exp(2j * np.pi * (-8.0) * n / DEFAULT_CHANNEL_RATE)
        ).astype(np.complex64)
        # This payload is mostly zeros, so a mean-frequency estimate would
        # report something near the space tone instead of the carrier.
        self.assertAlmostEqual(_estimate_offset(shifted, DEFAULT_CHANNEL_RATE), -8.0, delta=20)

    def test_postamble_keeps_crc_off_the_trailing_edge(self):
        frame = _frame()
        burst = modulate(frame)
        # Drop the last 200 ms. That would eat the CRC if it were last.
        trimmed = burst[: -int(DEFAULT_CHANNEL_RATE * 0.2)]
        decoded = demodulate(trimmed)
        self.assertEqual(decoded.payload, b"hello hf")

    def test_48_khz_waveform_round_trips(self):
        frame = _frame()
        decoded = demodulate(modulate(frame, sample_rate=48_000), sample_rate=48_000)
        self.assertEqual(decoded.payload, b"hello hf")

    def test_finds_sync_when_trailing_55s_are_louder(self):
        frame = _frame()
        burst = modulate(frame)
        sps = int(round(DEFAULT_CHANNEL_RATE / DEFAULT_BAUD))
        fifty_fives = np.tile(np.array([0, 1], dtype=np.int8), 32 * 8)
        freqs = np.where(fifty_fives == 1, DEFAULT_DEVIATION, -DEFAULT_DEVIATION)
        phase = np.cumsum(np.repeat(2.0 * np.pi * freqs / DEFAULT_CHANNEL_RATE, sps))
        tail = (20.0 * np.exp(1j * phase)).astype(np.complex64)
        decoded = demodulate(np.concatenate([burst, tail]))
        self.assertEqual(decoded.payload, b"hello hf")

    def test_ldpc_round_trip(self):
        frame = _frame()
        decoded = demodulate(modulate(frame, fec=True))
        self.assertEqual(decoded.payload, b"hello hf")

    def test_ldpc_survives_symbol_flips_that_break_crc_only(self):
        frame = _frame()
        sps = int(round(DEFAULT_CHANNEL_RATE / DEFAULT_BAUD))
        # After preamble+wake. Skip the 3-byte LDPC header; hit the body.
        # Spread flips so they do not land in one (128,64) block after deinterleave.
        flips = tuple(8 * 16 + 40 + 48 * i for i in range(4))

        def flip_symbols(iq: np.ndarray) -> np.ndarray:
            out = iq.copy()
            for bit in flips:
                sl = slice(bit * sps, (bit + 1) * sps)
                out[sl] = np.conj(out[sl])
            return out

        with self.assertRaises(ValueError):
            demodulate(flip_symbols(modulate(frame, fec=False)))
        decoded = demodulate(flip_symbols(modulate(frame, fec=True)))
        self.assertEqual(decoded.payload, frame.payload)

    def test_correlator_survives_wakeup_bit_errors(self):
        frame = _frame()
        sps = int(round(DEFAULT_CHANNEL_RATE / DEFAULT_BAUD))
        iq = modulate(frame, fec=True).copy()
        for bit in (8 * 8 + 3, 8 * 8 + 11, 8 * 8 + 19, 8 * 8 + 27, 8 * 8 + 41):
            sl = slice(bit * sps, (bit + 1) * sps)
            iq[sl] = np.conj(iq[sl])
        decoded = demodulate(iq)
        self.assertEqual(decoded.payload, frame.payload)

    def test_finds_frame_when_frequency_estimate_is_pulled(self):
        rng = np.random.default_rng(3)
        frame = _frame()
        clean = modulate(frame, fec=True)
        n = np.arange(len(clean))
        shifted = (
            clean * np.exp(2j * np.pi * 180.0 * n / DEFAULT_CHANNEL_RATE)
        ).astype(np.complex64)
        pulled = shifted + 3.0 + _noise(len(shifted), 0.02, rng)
        decoded = demodulate(pulled)
        self.assertEqual(decoded.payload, frame.payload)

    def test_tracks_a_slow_clock_error(self):
        frame = _frame()
        clean = modulate(frame, fec=True)
        ratio = 1.00008
        t = np.linspace(0, len(clean) - 1, int(round(len(clean) * ratio)))
        i0 = np.floor(t).astype(int)
        i0 = np.clip(i0, 0, len(clean) - 2)
        frac = (t - i0).astype(np.float64)
        stretched = (clean[i0] * (1.0 - frac) + clean[i0 + 1] * frac).astype(
            np.complex64
        )
        decoded = demodulate(stretched)
        self.assertEqual(decoded.payload, frame.payload)

    def test_tracks_clock_error_across_a_long_frame(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"robust hf payload length 30!!!",
            msg_id=2,
        )
        clean = modulate(frame, fec=True)
        ratio = 1.00025
        t = np.linspace(0, len(clean) - 1, int(round(len(clean) * ratio)))
        i0 = np.floor(t).astype(int)
        i0 = np.clip(i0, 0, len(clean) - 2)
        frac = (t - i0).astype(np.float64)
        stretched = (clean[i0] * (1.0 - frac) + clean[i0 + 1] * frac).astype(
            np.complex64
        )
        decoded = demodulate(stretched)
        self.assertEqual(decoded.payload, frame.payload)

    def test_tracks_linear_carrier_drift_across_a_long_frame(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"robust hf payload length 30!!!",
            msg_id=3,
        )
        clean = modulate(frame, fec=True)
        t = np.arange(len(clean), dtype=np.float64) / DEFAULT_CHANNEL_RATE
        phase = 2.0 * np.pi * (120.0 * t + 0.5 * 1.5 * t * t)
        drifted = (clean * np.exp(1j * phase)).astype(np.complex64)
        decoded = demodulate(drifted)
        self.assertEqual(decoded.payload, frame.payload)

    def test_preamble_correlator_finds_frame_when_wake_is_damaged(self):
        frame = _frame()
        sps = int(round(DEFAULT_CHANNEL_RATE / DEFAULT_BAUD))
        iq = modulate(frame, fec=True).copy()
        # Trash a dozen unique-word symbols; preamble lock + LDPC should recover.
        for bit in range(8 * 8, 8 * 8 + 12):
            sl = slice(bit * sps, (bit + 1) * sps)
            iq[sl] = np.conj(iq[sl])
        decoded = demodulate(iq)
        self.assertEqual(decoded.payload, frame.payload)

    def test_narrow_cpfsk_round_trip(self):
        frame = _frame()
        decoded = demodulate(modulate(frame, fec=True))
        self.assertEqual(decoded.payload, b"hello hf")

    def test_costas_preamble_finds_time_and_frequency(self):
        frame = _frame()
        clean = modulate(frame, fec=True)
        quiet = np.zeros(731, dtype=np.complex64)
        n = np.arange(len(clean), dtype=np.float64)
        shifted = clean * np.exp(
            2j * np.pi * 173.0 * n / DEFAULT_CHANNEL_RATE
        )
        iq = np.concatenate([quiet, shifted.astype(np.complex64)])
        offset, data_start, score = _costas_lock(
            iq,
            sample_rate=DEFAULT_CHANNEL_RATE,
            baud=DEFAULT_BAUD,
        )
        self.assertAlmostEqual(offset, 175.0, delta=25.0)
        expected = len(quiet) + 7 * int(DEFAULT_CHANNEL_RATE / DEFAULT_BAUD)
        self.assertAlmostEqual(data_start, expected, delta=4)
        self.assertGreater(score, 4.5)

    def test_full_known_pattern_recovers_very_noisy_fec_frame(self):
        frame = _frame()
        clean = modulate(frame, fec=True)
        rng = np.random.default_rng(2500)
        noise = _noise(len(clean), 2.5, rng)
        decoded = demodulate(clean + noise)
        self.assertEqual(decoded.payload, frame.payload)

    def test_acquires_through_a_slow_fade_in(self):
        frame = _frame()
        clean = modulate(frame, fec=True)
        ramp_samples = int(1.5 * DEFAULT_CHANNEL_RATE)
        envelope = np.ones(len(clean), dtype=np.float64)
        envelope[:ramp_samples] = np.linspace(0.02, 1.0, ramp_samples)
        rng = np.random.default_rng(1015)
        decoded = demodulate(clean * envelope + _noise(len(clean), 0.8, rng))
        self.assertEqual(decoded.payload, frame.payload)

    def test_long_frame_survives_one_second_midframe_fade(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"robust hf payload length 30!!!",
            msg_id=4,
        )
        clean = modulate(frame, fec=True)
        middle = len(clean) // 2
        fade_samples = int(DEFAULT_CHANNEL_RATE)
        faded = clean.copy()
        faded[
            middle - fade_samples // 2 : middle + fade_samples // 2
        ] = 0
        rng = np.random.default_rng(300)
        decoded = demodulate(faded + _noise(len(faded), 0.15, rng))
        self.assertEqual(decoded.payload, frame.payload)

    def test_light_fec_round_trip(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"zz",
            version=VERSION_LIGHT,
        )
        decoded = demodulate(modulate(frame, fec=True))
        self.assertEqual(decoded.payload, b"zz")
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.version, VERSION_LIGHT)

    def test_truncated_current_frame_is_not_reported_as_legacy_v1(self):
        frame = _frame()
        clean = modulate(frame, fec=True)
        cut = clean[: int(3.0 * DEFAULT_CHANNEL_RATE)]
        with self.assertRaises(ValueError) as ctx:
            demodulate(cut)
        self.assertNotEqual(str(ctx.exception), "frame shorter than LEN")

    def test_still_hears_previous_300_baud_format(self):
        frame = _frame()
        iq = modulate(
            frame,
            baud=LEGACY_BAUD,
            deviation=LEGACY_DEVIATION,
            fec=True,
            costas=False,
        )
        decoded = demodulate(iq)
        self.assertEqual(decoded.payload, frame.payload)

    def test_still_hears_legacy_32bit_sync(self):
        from hfbridge.fsk import LEGACY_SYNC, POSTAMBLE_BYTES, PREAMBLE_BYTES

        frame = _frame()
        packet = PREAMBLE_BYTES + LEGACY_SYNC + frame.encode() + POSTAMBLE_BYTES
        bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        sps = int(round(DEFAULT_CHANNEL_RATE / LEGACY_BAUD))
        freqs = np.where(
            bits == 1,
            LEGACY_WIDE_DEVIATION,
            -LEGACY_WIDE_DEVIATION,
        )
        phase = np.cumsum(np.repeat(2.0 * np.pi * freqs / DEFAULT_CHANNEL_RATE, sps))
        decoded = demodulate(np.exp(1j * phase).astype(np.complex64))
        self.assertEqual(decoded.payload, frame.payload)


if __name__ == "__main__":
    unittest.main()
