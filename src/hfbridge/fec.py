"""LDPC wrapper around the plaintext CRC frame.

After the 2-FSK sync word the air holds:
  n_blocks, n_blocks, n_blocks   (3 copies of a uint8, majority vote)
  n_blocks × 128 coded bits      (interleaved)

Each block is the (128,64) LDPC in hfbridge.ldpc. The inner bytes are the
existing CRC-16 frame. One flipped radio bit should not discard the shout.
"""

from __future__ import annotations

import numpy as np

from hfbridge.frame import Frame
from hfbridge.ldpc import K, N, decode_block, encode_block, extract_info

N_BLOCKS_MIN = 1
N_BLOCKS_MAX = 40
_HEADER_BITS = 24
_POSTAMBLE_BITS = 128


def _bits_from_bytes(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(np.uint8)


def _bytes_from_bits(bits: np.ndarray) -> bytes:
    pad = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits).tobytes()


def _interleave(bits: np.ndarray) -> np.ndarray:
    cols = 16
    pad = (-len(bits)) % cols
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    rows = len(bits) // cols
    return bits.reshape(rows, cols).T.ravel()


def _deinterleave(values: np.ndarray) -> np.ndarray:
    cols = 16
    pad = (-len(values)) % cols
    if pad:
        values = np.concatenate(
            [values, np.zeros(pad, dtype=values.dtype)]
        )
    rows = len(values) // cols
    return values.reshape(cols, rows).T.ravel()


def encode_coded(plain: bytes) -> bytes:
    info = _bits_from_bytes(plain)
    pad = (-len(info)) % K
    if pad:
        info = np.concatenate([info, np.zeros(pad, dtype=np.uint8)])
    n_blocks = len(info) // K
    if not N_BLOCKS_MIN <= n_blocks <= N_BLOCKS_MAX:
        raise ValueError("payload too large for LDPC wrapper")
    coded_parts = [encode_block(info[i * K : (i + 1) * K]) for i in range(n_blocks)]
    coded = _interleave(np.concatenate(coded_parts))
    header = bytes([n_blocks, n_blocks, n_blocks])
    return header + _bytes_from_bits(coded)


def n_blocks_from_llr(llr: np.ndarray) -> int | None:
    """Soft-combine the three copies of n_blocks. llr > 0 means bit 0."""
    if len(llr) < _HEADER_BITS:
        return None
    header = np.asarray(llr[:_HEADER_BITS], dtype=np.float64)
    bits = np.zeros(8, dtype=np.uint8)
    for i in range(8):
        score = header[i] + header[8 + i] + header[16 + i]
        bits[i] = 0 if score >= 0 else 1
    n_blocks = int(np.packbits(bits)[0])
    if not N_BLOCKS_MIN <= n_blocks <= N_BLOCKS_MAX:
        return None
    return n_blocks


def n_blocks_candidates_from_llr(llr: np.ndarray) -> tuple[int, ...]:
    """Rank block-count hypotheses using duration first, soft header second."""
    values = np.asarray(llr, dtype=np.float64)
    max_fit = min(N_BLOCKS_MAX, max(0, (len(values) - _HEADER_BITS) // N))
    if max_fit < N_BLOCKS_MIN:
        return ()
    ranked: list[int] = []

    # Current frames have a fixed 128-bit postamble. Burst duration therefore
    # carries an independent, often stronger block-count estimate even when
    # the 24 uncoded header bits are in a deep fade.
    duration_n = int(
        round((len(values) - _HEADER_BITS - _POSTAMBLE_BITS) / float(N))
    )
    if N_BLOCKS_MIN <= duration_n <= max_fit:
        ranked.append(duration_n)

    if len(values) >= _HEADER_BITS:
        header = values[:_HEADER_BITS]
        scored: list[tuple[float, int]] = []
        for n_blocks in range(N_BLOCKS_MIN, max_fit + 1):
            bits = np.unpackbits(np.array([n_blocks], dtype=np.uint8))
            expected = np.where(np.tile(bits, 3) == 0, 1.0, -1.0)
            scored.append((float(header @ expected), n_blocks))
        scored.sort(reverse=True)
        ranked.extend(n for _score, n in scored)

    return tuple(dict.fromkeys(ranked))


def decode_coded_llr(llr: np.ndarray, *, n_blocks: int | None = None) -> bytes:
    """llr > 0 means bit 0. Returns the inner CRC frame bytes."""
    if n_blocks is None:
        n_blocks = n_blocks_from_llr(llr)
        if n_blocks is None:
            candidates = n_blocks_candidates_from_llr(llr)
            n_blocks = candidates[0] if candidates else None
    if n_blocks is None:
        raise ValueError("LDPC block count not found")
    if not N_BLOCKS_MIN <= n_blocks <= N_BLOCKS_MAX:
        raise ValueError("invalid LDPC block count")
    need = _HEADER_BITS + n_blocks * N
    if len(llr) < need:
        raise ValueError("LDPC payload truncated")
    body_llr = _deinterleave(np.asarray(llr[_HEADER_BITS:need], dtype=np.float64))
    info_parts = []
    for i in range(n_blocks):
        block = decode_block(body_llr[i * N : (i + 1) * N])
        info_parts.append(extract_info(block))
    blob = _bytes_from_bits(np.concatenate(info_parts))
    inner = Frame.inner_from_ldpc(blob)
    if inner is None:
        raise ValueError("LDPC decoded frame truncated")
    return inner
