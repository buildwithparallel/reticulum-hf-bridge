import time
import unittest

import numpy as np

from hfbridge.frame import Frame
from hfbridge.fsk import (
    DEFAULT_BAUD,
    DEFAULT_CHANNEL_RATE,
    DEFAULT_DEVIATION,
    _acq_native,
    _costas_lock,
    _costas_lock_py,
    _full_known_lock,
    _full_known_lock_py,
    _highpass_dc,
    _highpass_dc_py,
    modulate,
)


def _frame() -> Frame:
    return Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=1)


@unittest.skipUnless(_acq_native is not None, "optional Rust acquisition not built")
class NativeAcqTest(unittest.TestCase):
    def test_highpass_matches_python(self):
        rng = np.random.default_rng(1)
        iq = (
            rng.normal(0.4, 0.7, 12_000) + 1j * rng.normal(-0.2, 0.7, 12_000)
        ).astype(np.complex64)
        rust = _highpass_dc(iq, DEFAULT_CHANNEL_RATE)
        python = _highpass_dc_py(iq, DEFAULT_CHANNEL_RATE)
        np.testing.assert_allclose(rust, python, rtol=0, atol=1e-6)

    def test_highpass_is_faster_than_python(self):
        rng = np.random.default_rng(2)
        n = int(DEFAULT_CHANNEL_RATE * 12)
        iq = (rng.normal(0, 0.5, n) + 1j * rng.normal(0, 0.5, n)).astype(
            np.complex64
        )
        _highpass_dc(iq, DEFAULT_CHANNEL_RATE)
        _highpass_dc_py(iq, DEFAULT_CHANNEL_RATE)
        t0 = time.perf_counter()
        _highpass_dc(iq, DEFAULT_CHANNEL_RATE)
        rust_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        _highpass_dc_py(iq, DEFAULT_CHANNEL_RATE)
        python_s = time.perf_counter() - t0
        self.assertLess(rust_s, python_s)
        print(
            f"\nhighpass: rust {rust_s*1000:.1f} ms, "
            f"python {python_s*1000:.1f} ms, "
            f"{python_s / rust_s:.1f}x"
        )

    def test_costas_matches_python(self):
        clean = modulate(_frame(), fec=True)
        quiet = np.zeros(731, dtype=np.complex64)
        n = np.arange(len(clean), dtype=np.float64)
        shifted = clean * np.exp(2j * np.pi * 173.0 * n / DEFAULT_CHANNEL_RATE)
        iq = np.concatenate([quiet, shifted.astype(np.complex64)])
        rust = _costas_lock(
            iq, sample_rate=DEFAULT_CHANNEL_RATE, baud=DEFAULT_BAUD
        )
        python = _costas_lock_py(
            iq, sample_rate=DEFAULT_CHANNEL_RATE, baud=DEFAULT_BAUD
        )
        self.assertAlmostEqual(rust[0], python[0], delta=1e-9)
        self.assertEqual(rust[1], python[1])
        self.assertAlmostEqual(rust[2], python[2], delta=1e-9)

    def test_full_known_matches_python(self):
        iq = modulate(_frame(), fec=True)
        rust = _full_known_lock(
            iq,
            sample_rate=DEFAULT_CHANNEL_RATE,
            baud=DEFAULT_BAUD,
            deviation=DEFAULT_DEVIATION,
        )
        python = _full_known_lock_py(
            iq,
            sample_rate=DEFAULT_CHANNEL_RATE,
            baud=DEFAULT_BAUD,
            deviation=DEFAULT_DEVIATION,
        )
        self.assertAlmostEqual(rust[0], python[0], delta=1e-6)
        self.assertEqual(rust[1], python[1])
        self.assertGreater(rust[2], 0.5)
        self.assertAlmostEqual(rust[2], python[2], delta=1e-9)

    def test_full_known_is_faster_than_python(self):
        iq = modulate(_frame(), fec=True)
        kwargs = dict(
            sample_rate=DEFAULT_CHANNEL_RATE,
            baud=DEFAULT_BAUD,
            deviation=DEFAULT_DEVIATION,
        )
        _full_known_lock(iq, **kwargs)
        _full_known_lock_py(iq, **kwargs)
        t0 = time.perf_counter()
        _full_known_lock(iq, **kwargs)
        rust_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        _full_known_lock_py(iq, **kwargs)
        python_s = time.perf_counter() - t0
        self.assertLess(rust_s, python_s)
        print(
            f"\nfull-known lock: rust {rust_s*1000:.1f} ms, "
            f"python {python_s*1000:.1f} ms, "
            f"{python_s / rust_s:.1f}x"
        )

    def test_costas_is_faster_than_python(self):
        clean = modulate(_frame(), fec=True)
        quiet = np.zeros(731, dtype=np.complex64)
        n = np.arange(len(clean), dtype=np.float64)
        shifted = clean * np.exp(2j * np.pi * 173.0 * n / DEFAULT_CHANNEL_RATE)
        iq = np.concatenate([quiet, shifted.astype(np.complex64)])
        kwargs = dict(sample_rate=DEFAULT_CHANNEL_RATE, baud=DEFAULT_BAUD)
        _costas_lock(iq, **kwargs)
        _costas_lock_py(iq, **kwargs)
        t0 = time.perf_counter()
        _costas_lock(iq, **kwargs)
        rust_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        _costas_lock_py(iq, **kwargs)
        python_s = time.perf_counter() - t0
        self.assertLess(rust_s, python_s)
        print(
            f"\ncostas lock: rust {rust_s*1000:.1f} ms, "
            f"python {python_s*1000:.1f} ms, "
            f"{python_s / rust_s:.1f}x"
        )
