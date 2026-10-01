import unittest

import numpy as np

from hfbridge.channelize import Channelizer, decimate_to
from hfbridge.frame import Frame
from hfbridge.fsk import DEFAULT_CHANNEL_RATE, demodulate, modulate
from hfbridge.listen import IF_OFFSET, RTL_RATE, _Mixer

CHUNK = 131072


def _stream(source: np.ndarray, rate: int = RTL_RATE) -> np.ndarray:
    """Push samples through in dongle-sized reads, as the listener does."""
    channelizer = Channelizer(rate, DEFAULT_CHANNEL_RATE)
    return np.concatenate(
        [channelizer(source[i : i + CHUNK]) for i in range(0, len(source), CHUNK)]
    )


def _tone(freq: float, count: int, rate: int = RTL_RATE) -> np.ndarray:
    t = np.arange(count) / rate
    return np.exp(2j * np.pi * freq * t).astype(np.complex64)


def _dbfs(iq: np.ndarray) -> float:
    return 10.0 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-30)


class ChannelizerTest(unittest.TestCase):
    def test_passband_tone_survives_intact(self):
        self.assertAlmostEqual(_dbfs(_stream(_tone(250.0, CHUNK * 4))), 0.0, delta=0.5)

    def test_rejects_the_lo_spur_the_if_offset_would_otherwise_alias_in(self):
        # Averaging blocks of samples folds the dongle's DC spike from the IF
        # offset straight onto -2 kHz, right beside the wanted tones.
        spur = _stream(_tone(-float(IF_OFFSET), CHUNK * 4))
        self.assertLess(_dbfs(spur), -55.0)

    def test_noise_folding_stays_near_the_ideal_bandwidth_ratio(self):
        rng = np.random.default_rng(0)
        count = CHUNK * 4
        noise = (rng.normal(0, 1, count) + 1j * rng.normal(0, 1, count)).astype(
            np.complex64
        )
        ideal = 10.0 * np.log10(DEFAULT_CHANNEL_RATE / RTL_RATE)
        self.assertLess(_dbfs(_stream(noise)) - ideal, 3.0)

    def test_streaming_in_chunks_matches_one_shot(self):
        source = _tone(600.0, CHUNK * 3)
        streamed = _stream(source)
        one_shot = decimate_to(source, RTL_RATE, DEFAULT_CHANNEL_RATE)
        self.assertEqual(len(streamed), len(one_shot))
        np.testing.assert_allclose(streamed, one_shot, atol=1e-5)

    def test_full_receive_chain_decodes_through_spur_and_tuner_error(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=3)
        baseband = modulate(frame, sample_rate=DEFAULT_CHANNEL_RATE)
        upsampled = np.repeat(baseband, RTL_RATE // int(DEFAULT_CHANNEL_RATE))
        # A live receiver always has samples either side of a burst; without
        # them the filter tail clips the final symbol.
        quiet = np.zeros(RTL_RATE // 10, dtype=np.complex64)
        upsampled = np.concatenate([quiet, upsampled, quiet])
        n = np.arange(len(upsampled))
        # Signal parked at the IF offset with a 400 Hz tuner error, under an LO
        # leakage spike three times its amplitude.
        rf = upsampled * np.exp(2j * np.pi * (IF_OFFSET + 400.0) * n / RTL_RATE) + 3.0
        mixer = _Mixer(IF_OFFSET, RTL_RATE)
        channelizer = Channelizer(RTL_RATE, DEFAULT_CHANNEL_RATE)
        rf = rf.astype(np.complex64)
        channel = np.concatenate(
            [channelizer(mixer(rf[i : i + CHUNK])) for i in range(0, len(rf), CHUNK)]
        )
        decoded = demodulate(channel)
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello hf")

    def test_full_receive_chain_decodes_ldpc_through_spur_and_tuner_error(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=3)
        baseband = modulate(frame, sample_rate=DEFAULT_CHANNEL_RATE, fec=True)
        upsampled = np.repeat(baseband, RTL_RATE // int(DEFAULT_CHANNEL_RATE))
        quiet = np.zeros(RTL_RATE // 10, dtype=np.complex64)
        upsampled = np.concatenate([quiet, upsampled, quiet])
        n = np.arange(len(upsampled))
        rf = upsampled * np.exp(2j * np.pi * (IF_OFFSET + 400.0) * n / RTL_RATE) + 3.0
        mixer = _Mixer(IF_OFFSET, RTL_RATE)
        channelizer = Channelizer(RTL_RATE, DEFAULT_CHANNEL_RATE)
        rf = rf.astype(np.complex64)
        channel = np.concatenate(
            [channelizer(mixer(rf[i : i + CHUNK])) for i in range(0, len(rf), CHUNK)]
        )
        decoded = demodulate(channel)
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.payload, b"hello hf")


if __name__ == "__main__":
    unittest.main()
