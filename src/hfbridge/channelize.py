"""Bring RTL-SDR captures down to the FSK channel rate.

Decimating by simply averaging blocks of samples folds every one of the
Nyquist zones between the dongle rate and the channel rate on top of the
channel. Going from 1.2 MS/s to 6 kS/s that is 200 zones, which buries the
channel under about 23 dB of extra noise and drops the dongle's own LO spur
into the passband. So each decimation step gets a real anti-alias filter.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# Taps per unit of normalised transition width for a Blackman window, which
# buys roughly 74 dB of stopband rejection.
_BLACKMAN_TAPS = 5.5
_MAX_TAPS = 4097


def _lowpass(cutoff_hz: float, transition_hz: float, rate: float) -> np.ndarray:
    count = int(np.ceil(_BLACKMAN_TAPS * rate / transition_hz)) | 1
    count = min(count, _MAX_TAPS)
    offsets = np.arange(count) - (count - 1) / 2
    taps = np.sinc(2.0 * cutoff_hz * offsets / rate) * np.blackman(count)
    return (taps / taps.sum()).astype(np.float64)


def _split(factor: int) -> tuple[int, int]:
    """Take most of the decimation in the first, cheapest-per-sample stage."""
    for first in range(min(factor, 25), 0, -1):
        if factor % first == 0:
            return first, factor // first
    return 1, factor


class _Stage:
    """One decimating FIR that remembers its tail between reads."""

    def __init__(self, taps: np.ndarray, factor: int) -> None:
        self.taps = taps[::-1].copy()
        self.factor = factor
        self.tail = np.zeros(len(taps) - 1, dtype=np.complex64)
        self.phase = 0

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.tail, np.asarray(iq, dtype=np.complex64)])
        count = len(self.taps)
        avail = len(buf) - count + 1
        if avail <= self.phase:
            self.tail = buf
            return np.zeros(0, dtype=np.complex64)
        starts = np.arange(self.phase, avail, self.factor)
        out = sliding_window_view(buf, count)[starts] @ self.taps
        keep = count - 1
        self.phase = int(starts[-1] + self.factor - (len(buf) - keep))
        self.tail = buf[len(buf) - keep :]
        return out.astype(np.complex64)


class Channelizer:
    """Anti-aliased decimation from the dongle rate to the modem rate."""

    def __init__(self, source_rate: float, dest_rate: float) -> None:
        ratio = source_rate / dest_rate
        factor = int(round(ratio))
        if abs(ratio - factor) > 1e-6 or factor < 1:
            raise ValueError(f"cannot integer-decimate {source_rate} -> {dest_rate}")
        first, second = _split(factor)
        inter_rate = source_rate / first
        self.stages: list[_Stage] = []
        if first > 1:
            # Only what folds onto the final channel matters at this stage, so
            # the transition can be lazy and the filter stays cheap.
            self.stages.append(
                _Stage(
                    _lowpass(dest_rate, inter_rate - 2 * dest_rate, source_rate),
                    first,
                )
            )
        if second > 1:
            self.stages.append(
                _Stage(
                    _lowpass(dest_rate * 0.4, dest_rate * 0.2, inter_rate),
                    second,
                )
            )

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        for stage in self.stages:
            iq = stage(iq)
            if len(iq) == 0:
                break
        return np.asarray(iq, dtype=np.complex64)


def decimate_to(iq: np.ndarray, source_rate: float, dest_rate: float) -> np.ndarray:
    """One-shot decimation. Streaming callers should hold a Channelizer."""
    return Channelizer(source_rate, dest_rate)(iq)
