import time
import unittest

import numpy as np

from hfbridge.fec import (
    decode_coded_llr,
    encode_coded,
    n_blocks_candidates_from_llr,
)
from hfbridge.frame import VERSION_LIGHT, Frame
from hfbridge.ldpc import (
    K,
    N,
    _acq_native,
    decode_block,
    decode_block_py,
    encode_block,
    extract_info,
    syndrome,
)


class LdpcTest(unittest.TestCase):
    def test_encode_is_a_codeword(self):
        rng = np.random.default_rng(0)
        info = rng.integers(0, 2, K, dtype=np.uint8)
        coded = encode_block(info)
        self.assertEqual(len(coded), N)
        self.assertEqual(int(syndrome(coded).sum()), 0)
        np.testing.assert_array_equal(extract_info(coded), info)

    def test_corrects_several_hard_flips(self):
        rng = np.random.default_rng(1)
        info = rng.integers(0, 2, K, dtype=np.uint8)
        coded = encode_block(info)
        noisy = coded.copy()
        noisy[rng.choice(N, size=4, replace=False)] ^= 1
        llr = np.where(noisy == 0, 4.0, -4.0)
        decoded = decode_block(llr)
        np.testing.assert_array_equal(extract_info(decoded), info)

    def test_one_flip_is_not_enough_to_kill_a_coded_frame(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=1)
        plain = bytearray(frame.encode())
        plain[10] ^= 0x01
        with self.assertRaises(ValueError):
            Frame.decode(bytes(plain))
        coded = encode_coded(frame.encode())
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8)).astype(np.uint8)
        bits[40] ^= 1
        llr = np.where(bits == 0, 5.0, -5.0)
        recovered = decode_coded_llr(llr)
        self.assertEqual(Frame.decode(recovered).payload, b"hello hf")


@unittest.skipUnless(_acq_native is not None, "optional Rust acquisition not built")
class NativeLdpcTest(unittest.TestCase):
    def test_matches_python_on_flipped_block(self):
        rng = np.random.default_rng(1)
        info = rng.integers(0, 2, K, dtype=np.uint8)
        coded = encode_block(info)
        noisy = coded.copy()
        noisy[rng.choice(N, size=4, replace=False)] ^= 1
        llr = np.where(noisy == 0, 4.0, -4.0)
        rust = decode_block(llr)
        python = decode_block_py(llr)
        np.testing.assert_array_equal(rust, python)
        np.testing.assert_array_equal(extract_info(rust), info)

    def test_is_faster_than_python(self):
        rng = np.random.default_rng(3)
        info = rng.integers(0, 2, K, dtype=np.uint8)
        coded = encode_block(info)
        noisy = coded.copy()
        noisy[rng.choice(N, size=6, replace=False)] ^= 1
        llr = np.where(noisy == 0, 2.5, -2.5)
        decode_block(llr)
        decode_block_py(llr)
        t0 = time.perf_counter()
        for _ in range(80):
            decode_block(llr)
        rust_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(80):
            decode_block_py(llr)
        python_s = time.perf_counter() - t0
        self.assertLess(rust_s, python_s)
        print(
            f"\nldpc block: rust {rust_s*1000:.1f} ms, "
            f"python {python_s*1000:.1f} ms, "
            f"{python_s / rust_s:.1f}x"
        )


class FecFrameTest(unittest.TestCase):
    def test_coded_round_trip(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"hello hf", msg_id=1)
        plain = frame.encode()
        coded = encode_coded(plain)
        self.assertGreater(len(coded), len(plain))
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8))
        llr = np.where(bits == 0, 5.0, -5.0)
        recovered = decode_coded_llr(llr)
        self.assertEqual(Frame.decode(recovered), frame)

    def test_survives_scattered_bit_flips(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"yes", msg_id=9)
        plain = frame.encode()
        coded = encode_coded(plain)
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8)).astype(np.uint8)
        rng = np.random.default_rng(2)
        flips = rng.choice(len(bits), size=8, replace=False)
        bits[flips] ^= 1
        llr = np.where(bits == 0, 3.0, -3.0)
        recovered = decode_coded_llr(llr)
        self.assertEqual(Frame.decode(recovered).payload, b"yes")

    def test_soft_header_survives_one_smashed_copy(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"yes", msg_id=9)
        coded = bytearray(encode_coded(frame.encode()))
        coded[0] ^= 0xFF
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8))
        llr = np.where(bits == 0, 5.0, -5.0)
        recovered = decode_coded_llr(llr)
        self.assertEqual(Frame.decode(recovered).payload, b"yes")

    def test_duration_recovers_block_count_when_header_is_lost(self):
        frame = Frame(origin="N0CALL", dest=bytes(16), payload=b"yes", msg_id=9)
        coded = encode_coded(frame.encode())
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8))
        llr = np.where(bits == 0, 5.0, -5.0)
        llr[:24] = 0.0
        # The demodulator includes the fixed 128-bit postamble in its soft
        # stream, making frame duration an independent count channel.
        llr = np.concatenate([llr, np.zeros(128)])
        expected = coded[0]
        self.assertEqual(n_blocks_candidates_from_llr(llr)[0], expected)
        recovered = decode_coded_llr(llr, n_blocks=expected)
        self.assertEqual(Frame.decode(recovered).payload, b"yes")

    def test_light_frame_survives_ldpc(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"zz",
            version=VERSION_LIGHT,
        )
        coded = encode_coded(frame.encode())
        bits = np.unpackbits(np.frombuffer(coded, dtype=np.uint8))
        llr = np.where(bits == 0, 5.0, -5.0)
        recovered = decode_coded_llr(llr)
        decoded = Frame.decode(recovered)
        self.assertEqual(decoded.payload, b"zz")
        self.assertEqual(decoded.version, VERSION_LIGHT)

    def test_flipped_inner_version_nibble_is_not_truncated(self):
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"range-01",
            version=VERSION_LIGHT,
        )
        inner = bytearray(frame.encode())
        inner[0] = (inner[0] & 0x0F) | 0x30
        self.assertIsNone(Frame.wire_size(bytes(inner)))
        recovered = Frame.inner_from_ldpc(bytes(inner))
        self.assertEqual(Frame.decode(recovered).payload, b"range-01")


if __name__ == "__main__":
    unittest.main()
