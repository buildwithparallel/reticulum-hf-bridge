"""Rate-1/2 (128, 64) LDPC for the HF frame.

H is a sparse ~ (3,6)-regular matrix so belief propagation has something to
do. Encoding uses a systematic form of that same H (info bits + parity).
Decode is min-sum BP on the sparse H, using 2-FSK LLRs. CRC stays outside
this module and is the last check.
"""

from __future__ import annotations

import numpy as np

try:
    from hfbridge import _acq as _acq_native
except ImportError:
    _acq_native = None

N = 128
K = 64
M = N - K
DV = 3
DC = 6
BP_ITERS = 40
# Min-sum overestimates check-to-variable magnitudes. 0.75 is the usual fix.
MS_SCALE = 0.75
_H_SEED = 20260823


def _gf2_rank(matrix: np.ndarray) -> int:
    a = matrix.copy().astype(np.uint8)
    rows, cols = a.shape
    rank = 0
    used = np.zeros(rows, dtype=bool)
    for c in range(cols):
        pivot = None
        for r in range(rows):
            if not used[r] and a[r, c]:
                pivot = r
                break
        if pivot is None:
            continue
        used[pivot] = True
        rank += 1
        for r in range(rows):
            if r != pivot and a[r, c]:
                a[r] ^= a[pivot]
    return rank


def _choose_h() -> np.ndarray:
    """Column weight 3, row weight ~6, full rank. Seed is part of the air spec."""
    rng = np.random.default_rng(_H_SEED)
    for _ in range(2000):
        h = np.zeros((M, N), dtype=np.uint8)
        row_w = np.zeros(M, dtype=int)
        ok = True
        for v in range(N):
            order = np.argsort(row_w.astype(np.float64) + rng.random(M) * 0.01)
            picked: list[int] = []
            for r in order:
                r = int(r)
                if row_w[r] >= DC:
                    continue
                picked.append(r)
                if len(picked) == DV:
                    break
            if len(picked) < DV:
                ok = False
                break
            h[picked, v] = 1
            row_w[picked] += 1
        if ok and _gf2_rank(h) == M:
            return h
    raise RuntimeError("could not build a full-rank sparse LDPC H")


def _systematic_form(h: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """info_cols, parity_cols, P with x[parity] = P x[info] (mod 2)."""
    a = h.copy().astype(np.uint8)
    pivot_col = [-1] * M
    used_cols: set[int] = set()
    row = 0
    for c in range(N - 1, -1, -1):
        if row >= M:
            break
        piv = None
        for r in range(row, M):
            if a[r, c]:
                piv = r
                break
        if piv is None:
            continue
        if piv != row:
            a[[row, piv]] = a[[piv, row]]
        for r in range(M):
            if r != row and a[r, c]:
                a[r] ^= a[row]
        pivot_col[row] = c
        used_cols.add(c)
        row += 1
    if row != M or -1 in pivot_col:
        raise RuntimeError("H lost rank while building the encoder")
    parity_cols = np.array(pivot_col, dtype=int)
    info_cols = np.array([c for c in range(N) if c not in used_cols], dtype=int)
    if len(info_cols) != K:
        raise RuntimeError("unexpected info length for LDPC encoder")
    return info_cols, parity_cols, a[:, info_cols].copy()


H = _choose_h()
INFO_COLS, PARITY_COLS, P = _systematic_form(H)
ROW_IDX = [np.flatnonzero(H[r]).tolist() for r in range(M)]
COL_IDX = [np.flatnonzero(H[:, c]).tolist() for c in range(N)]


def encode_block(info: np.ndarray) -> np.ndarray:
    bits = np.asarray(info, dtype=np.uint8).reshape(K)
    coded = np.zeros(N, dtype=np.uint8)
    coded[INFO_COLS] = bits
    coded[PARITY_COLS] = (P @ bits) % 2
    return coded


def extract_info(codeword: np.ndarray) -> np.ndarray:
    return np.asarray(codeword, dtype=np.uint8).reshape(N)[INFO_COLS]


def syndrome(codeword: np.ndarray) -> np.ndarray:
    return (H @ np.asarray(codeword, dtype=np.uint8).reshape(N)) % 2


def decode_block(llr: np.ndarray, *, iters: int = BP_ITERS) -> np.ndarray:
    """Min-sum BP on the sparse H. llr > 0 means bit 0 is more likely."""
    if _acq_native is not None:
        raw = np.ascontiguousarray(llr, dtype=np.float64).reshape(N)
        hard = _acq_native.decode_ldpc_block(raw.tobytes(), int(iters))
        return np.frombuffer(hard, dtype=np.uint8).copy()
    return decode_block_py(llr, iters=iters)


def decode_block_py(llr: np.ndarray, *, iters: int = BP_ITERS) -> np.ndarray:
    """Python min-sum BP. Same math as the Rust path."""
    ch = np.asarray(llr, dtype=np.float64).reshape(N)
    msg_c2v = np.zeros((M, N), dtype=np.float64)
    hard = (ch < 0).astype(np.uint8)
    for _ in range(iters):
        msg_v2c = np.zeros((M, N), dtype=np.float64)
        for v in range(N):
            total = ch[v] + msg_c2v[:, v].sum()
            for c in COL_IDX[v]:
                msg_v2c[c, v] = total - msg_c2v[c, v]
        for c in range(M):
            idxs = ROW_IDX[c]
            incoming = msg_v2c[c, idxs]
            signs = np.sign(incoming)
            signs[signs == 0] = 1.0
            mag = np.abs(incoming)
            for i, v in enumerate(idxs):
                mask = np.ones(len(idxs), dtype=bool)
                mask[i] = False
                msg_c2v[c, v] = MS_SCALE * float(
                    np.prod(signs[mask]) * np.min(mag[mask])
                )
        posterior = ch + msg_c2v.sum(axis=0)
        hard = (posterior < 0).astype(np.uint8)
        if not np.any(syndrome(hard)):
            return hard
    return hard
