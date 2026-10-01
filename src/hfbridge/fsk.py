"""2-CPFSK physical layer. Soft-decision LDPC around the CRC frame.

Default on-air: 100 baud, ±50 Hz (orthogonal). New shouts prepend a 7-symbol
Costas array, then the 0x55 click-track and 63-bit m-sequence unique word.
Acquisition tries Costas first, then a correlator grid. Legacy 300 baud
±125 Hz and ±250 Hz still demodulate. After lock, carrier and symbol timing
are tracked for the rest of the burst. CRC is still the last check.
"""

from __future__ import annotations

import numpy as np

from hfbridge.fec import (
    decode_coded_llr,
    encode_coded,
    n_blocks_candidates_from_llr,
)
from hfbridge.frame import Frame

# Optional; build with scripts/build-acq.sh. Python lock stays as fallback.
try:
    from hfbridge import _acq as _acq_native
except ImportError:
    _acq_native = None

PREAMBLE_BYTES = bytes([0x55] * 8)
# FT8's order-7 Costas permutation, shifted by half a tone so no preamble
# symbol sits on receiver DC. At 100 baud this occupies -250..+350 Hz.
COSTAS_TONES = (3, 1, 4, 0, 6, 5, 2)
# Keep the CRC off the trailing edge. MOX-off and symbol-timing drift
# flip the last bits; today's failed Crosstalk shouts had a perfect body
# and a damaged CRC field.
POSTAMBLE_BYTES = bytes([0x55] * 16)
# 63-bit m-sequence (x^6+x+1, all-ones init) padded with one 0.
# Thumbtack autocorrelation: +63 on peak, -1 elsewhere. Detected by a
# correlator, not an exact bit match.
WAKE_BYTES = bytes.fromhex("fd59bb49c5e51840")
SYNC = WAKE_BYTES
LEGACY_SYNC = bytes([0x2E, 0xFC, 0x37, 0x49])
DEFAULT_BAUD = 100
# Orthogonal binary CPFSK: tone spacing equals the symbol rate.
DEFAULT_DEVIATION = 50.0
# Receive both earlier 300-baud formats.
LEGACY_BAUD = 300
LEGACY_DEVIATION = 125.0
LEGACY_WIDE_DEVIATION = 250.0
DEFAULT_CHANNEL_RATE = 6000.0
_FREQ_SPAN_HZ = 400.0
_FREQ_STEP_HZ = 25.0
_MIN_CORR_SNR = 4.5
_FREQ_LOOP = 0.08
_FREQ_LOOP_BODY = 0.01
_TED_LOOP = 0.025


def _bits_from_bytes(data: bytes) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    return bits.astype(np.int8)


def _pm1(data: bytes) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(np.float64)
    return np.where(bits == 1, 1.0, -1.0)


_WAKE_PM1 = _pm1(WAKE_BYTES)
_LEGACY_PM1 = _pm1(LEGACY_SYNC)
_PREAMBLE_PM1 = _pm1(PREAMBLE_BYTES)
_PREAMBLE_SYMS = len(_PREAMBLE_PM1)
_TEMPLATES = (
    ("wake", _WAKE_PM1, len(WAKE_BYTES) * 8),
    ("legacy", _LEGACY_PM1, len(LEGACY_SYNC) * 8),
)
_MIN_PREAMBLE_SNR = 1.5
_MIN_WAKE_SNR = 3.0
_MIN_TEMPLATE_AGREE = 0.75
_CANONICAL_LAG_BOOST = 1.35
_TOP_LOCKS = 8
_FREQ_FOCUS_HZ = 400.0
_MIN_COSTAS_SCORE = 4.5
_MIN_FULL_KNOWN_CORR = 0.50


def _highpass_dc(iq: np.ndarray, sample_rate: float) -> np.ndarray:
    """Track out slow DC / LO leakage before acquisition."""
    x = np.asarray(iq, dtype=np.complex64)
    if _acq_native is not None:
        raw = _acq_native.highpass_dc(_iq_bytes(x), float(sample_rate))
        return np.frombuffer(raw, dtype=np.complex64).copy()
    return _highpass_dc_py(x, sample_rate)


def _highpass_dc_py(iq: np.ndarray, sample_rate: float) -> np.ndarray:
    """Python one-pole high-pass. Same math as the Rust path.

    Per-symbol nulling is not enough when mark and space sit only ±125 Hz
    from center; a one-pole high-pass keeps narrow CPFSK discriminators
    working under RTL spur without the global mean subtraction that loud
    postambles break.
    """
    x = np.asarray(iq, dtype=np.complex64)
    n = len(x)
    if n < 2:
        return x
    alpha = float(np.exp(-2.0 * np.pi * 12.0 / sample_rate))
    y = np.empty(n, dtype=np.complex64)
    pxr = pxi = pyr = pyi = 0.0
    for i in range(n):
        xr = float(x[i].real)
        xi = float(x[i].imag)
        yr = xr - pxr + alpha * pyr
        yi = xi - pxi + alpha * pyi
        y[i] = yr + 1j * yi
        pxr, pxi, pyr, pyi = xr, xi, yr, yi
    return y


def modulate(
    frame: Frame,
    *,
    sample_rate: float = DEFAULT_CHANNEL_RATE,
    baud: float = DEFAULT_BAUD,
    deviation: float = DEFAULT_DEVIATION,
    fec: bool = False,
    costas: bool = True,
) -> np.ndarray:
    sps = sample_rate / baud
    if abs(sps - round(sps)) > 1e-6:
        raise ValueError("sample_rate must be an integer multiple of baud")
    sps_i = int(round(sps))
    body = encode_coded(frame.encode()) if fec else frame.encode()
    packet = PREAMBLE_BYTES + SYNC + body + POSTAMBLE_BYTES
    bits = _bits_from_bytes(packet)
    data_freqs = np.where(bits == 1, deviation, -deviation)
    if costas:
        costas_freqs = (
            np.asarray(COSTAS_TONES, dtype=np.float64)
            - (len(COSTAS_TONES) - 1) / 2.0
            + 0.5
        ) * baud
        freqs = np.concatenate([costas_freqs, data_freqs])
    else:
        freqs = data_freqs
    # CPFSK: integrate frequency into phase (constant envelope).
    phase = np.cumsum(np.repeat(2.0 * np.pi * freqs / sample_rate, sps_i))
    return np.exp(1j * phase).astype(np.complex64)


def _estimate_offset(iq: np.ndarray, sample_rate: float) -> float:
    """Midpoint of the two FSK tones.

    A mean-frequency estimate is pulled toward whichever tone the data uses
    more. This frame is mostly zeros, so that lands halfway to the space tone
    and the matched filters miss both marks and spaces.
    """
    if len(iq) < 32:
        return 0.0
    inst = np.angle(iq[1:] * np.conj(iq[:-1])) * sample_rate / (2.0 * np.pi)
    amp = np.abs(iq[1:])
    loud = inst[amp > (0.3 * np.max(amp))]
    if len(loud) < 16:
        loud = inst
    hist, edges = np.histogram(loud, bins=80, range=(-800.0, 800.0))
    peaks: list[tuple[int, float]] = []
    ceiling = hist.max()
    if ceiling <= 0:
        return 0.0
    for i in range(1, len(hist) - 1):
        if hist[i] >= hist[i - 1] and hist[i] >= hist[i + 1] and hist[i] > 0.15 * ceiling:
            peaks.append((int(hist[i]), float(0.5 * (edges[i] + edges[i + 1]))))
    peaks.sort(reverse=True)
    # The orthogonal 100-baud mode has tones only 100 Hz apart. Histogram
    # bins are 20 Hz wide, so accept a resolved pair above 60 Hz.
    if len(peaks) >= 2 and abs(peaks[0][1] - peaks[1][1]) > 60:
        return 0.5 * (peaks[0][1] + peaks[1][1])
    if peaks:
        return peaks[0][1]
    return float(np.median(loud))


def _decimate_for_acq(
    iq: np.ndarray, sample_rate: float, baud: float
) -> tuple[np.ndarray, float, int]:
    sps = sample_rate / baud
    if sps <= 24:
        return np.asarray(iq, dtype=np.complex64), sample_rate, 1
    stride = int(round(sps / 20.0))
    return np.asarray(iq, dtype=np.complex64)[::stride], sample_rate / stride, stride


def _iq_bytes(iq: np.ndarray) -> bytes:
    return np.ascontiguousarray(iq, dtype=np.complex64).tobytes()


def _costas_lock(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
) -> tuple[float, int, float]:
    if _acq_native is not None:
        offset, data_start, ratio = _acq_native.costas_lock(
            _iq_bytes(iq), float(sample_rate), float(baud)
        )
        return float(offset), int(data_start), float(ratio)
    return _costas_lock_py(iq, sample_rate=sample_rate, baud=baud)


def _costas_lock_py(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
) -> tuple[float, int, float]:
    """Find the order-7 Costas preamble in time and frequency.

    Returns carrier offset, first sample after the Costas symbols, and a
    peak-to-median score. Symbol FFTs turn the joint time/frequency search
    into a small energy lookup rather than the full bit-demodulation grid.
    """
    sps = int(round(sample_rate / baud))
    if sps < 4 or len(iq) < (len(COSTAS_TONES) + 16) * sps:
        raise ValueError("Costas preamble not found")
    nfft = 1
    while nfft < 4 * sps:
        nfft *= 2
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate))
    carrier_grid = np.arange(-_FREQ_SPAN_HZ, _FREQ_SPAN_HZ + 0.1, 25.0)
    tone_offsets = (
        np.asarray(COSTAS_TONES, dtype=np.float64)
        - (len(COSTAS_TONES) - 1) / 2.0
        + 0.5
    ) * baud
    max_blocks = int(2.0 * baud) + len(COSTAS_TONES)
    best = (-np.inf, 0.0, 0)
    scores: list[float] = []
    tau_step = max(1, sps // 20)
    window = np.hanning(sps)
    for tau in range(0, sps, tau_step):
        n_blocks = min((len(iq) - tau) // sps, max_blocks)
        if n_blocks < len(COSTAS_TONES) + 1:
            continue
        blocks = np.asarray(iq[tau : tau + n_blocks * sps]).reshape(n_blocks, sps)
        power = np.abs(
            np.fft.fftshift(np.fft.fft(blocks * window, n=nfft, axis=1), axes=1)
        ) ** 2
        for carrier in carrier_grid:
            bins = [
                int(np.argmin(np.abs(freqs - (carrier + offset))))
                for offset in tone_offsets
            ]
            for start in range(n_blocks - len(COSTAS_TONES) + 1):
                score = float(
                    sum(power[start + k, bins[k]] for k in range(len(bins)))
                )
                scores.append(score)
                if score > best[0]:
                    best = (score, float(carrier), tau + start * sps)
    if not scores:
        raise ValueError("Costas preamble not found")
    median = float(np.median(scores)) + 1e-12
    ratio = best[0] / median
    if ratio < _MIN_COSTAS_SCORE:
        raise ValueError("Costas preamble not found")
    return best[1], best[2] + len(COSTAS_TONES) * sps, ratio


def _freq_candidates(centre: float) -> tuple[float, ...]:
    grid = np.arange(-_FREQ_SPAN_HZ, _FREQ_SPAN_HZ + 0.1, _FREQ_STEP_HZ)
    around = centre + np.arange(-60.0, 61.0, 10.0)
    merged = np.round(np.concatenate([grid, around]), 3)
    return tuple(sorted(set(float(x) for x in merged)))


def _soft_stream(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
) -> np.ndarray | None:
    """Matched-filter soft metric at every sample offset."""
    sps = int(round(sample_rate / baud))
    if len(iq) < sps * 16:
        return None
    n = np.arange(len(iq), dtype=np.float64)
    shifted = np.asarray(iq, dtype=np.complex128) * np.exp(
        -2j * np.pi * offset_hz * n / sample_rate
    )
    n_sym = len(shifted) // sps
    t = np.arange(sps, dtype=np.float64) / sample_rate
    mark = np.exp(-2j * np.pi * deviation * t)
    space = np.exp(2j * np.pi * deviation * t)
    mf_m = np.correlate(shifted, mark, mode="valid")
    mf_s = np.correlate(shifted, space, mode="valid")
    return np.abs(mf_m) ** 2 - np.abs(mf_s) ** 2


def _full_known_lock(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
) -> tuple[float, int, float]:
    if _acq_native is not None:
        offset, wake_start, score = _acq_native.full_known_lock(
            _iq_bytes(iq), float(sample_rate), float(baud), float(deviation)
        )
        return float(offset), int(wake_start), float(score)
    return _full_known_lock_py(
        iq, sample_rate=sample_rate, baud=baud, deviation=deviation
    )


def _full_known_lock_py(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
) -> tuple[float, int, float]:
    """Jointly lock on all 128 known preamble + wake symbols.

    FT8-style acquisition integrates a long known pattern instead of first
    requiring individual symbols to agree with hard decisions.  At weak
    signal the old 75%-agreement gate discarded packets whose LDPC bodies
    were still decodable.  A normalized correlation also has a stable
    threshold across gain and fading.

    Returns carrier offset, wake start sample, and correlation coefficient.
    """
    sps = int(round(sample_rate / baud))
    if sps < 2:
        raise ValueError("full preamble not found")
    known = np.concatenate([_PREAMBLE_PM1, _WAKE_PM1])
    known_energy = float(known @ known)
    centre = _estimate_offset(iq, sample_rate)
    tau_step = max(1, sps // 10)
    best = (-np.inf, 0.0, 0)
    ones = np.ones(len(known), dtype=np.float64)

    for offset_hz in _freq_candidates(centre):
        metric = _soft_stream(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=offset_hz,
        )
        if metric is None:
            continue
        for tau in range(0, sps, tau_step):
            symbols = metric[tau::sps]
            if len(symbols) < len(known) + 16:
                continue
            corr = np.correlate(symbols, known, mode="valid")
            energy = np.convolve(symbols * symbols, ones, mode="valid")
            coeff = np.abs(corr) / np.sqrt(energy * known_energy + 1e-30)
            lag = int(np.argmax(coeff))
            score = float(coeff[lag])
            if score > best[0]:
                wake_start = tau + (lag + _PREAMBLE_SYMS) * sps
                best = (score, float(offset_hz), int(wake_start))

    if best[0] < _MIN_FULL_KNOWN_CORR:
        raise ValueError("full preamble not found")
    return best[1], best[2], best[0]


def _peak_corr(soft: np.ndarray, template: np.ndarray) -> tuple[int, float, float]:
    centered = template - template.mean()
    corr = np.correlate(soft, centered, mode="valid")
    if len(corr) == 0:
        return 0, 0.0, 0.0
    lag = int(np.argmax(np.abs(corr)))
    peak = float(corr[lag])
    med = float(np.median(np.abs(corr))) + 1e-12
    return lag, peak, abs(peak) / med


def _corr_at(soft: np.ndarray, lag: int, template: np.ndarray) -> float:
    if lag < 0 or lag + len(template) > len(soft):
        return 0.0
    centered = template - template.mean()
    corr = np.correlate(soft, centered, mode="valid")
    peak = float(corr[lag])
    lo = max(0, lag - len(template))
    hi = min(len(corr), lag + len(template))
    local = np.abs(corr[lo:hi])
    med = float(np.median(local)) + 1e-12
    return abs(peak) / med


def _local_peak_snr(corr: np.ndarray, lag: int) -> float:
    lo = max(0, lag - 32)
    hi = min(len(corr), lag + 32)
    med = float(np.median(np.abs(corr[lo:hi]))) + 1e-12
    return abs(float(corr[lag])) / med


def _template_agreement(
    soft: np.ndarray, lag: int, tmpl: np.ndarray, invert: bool
) -> float:
    end = lag + len(tmpl)
    if lag < 0 or end > len(soft):
        return 0.0
    seg = soft[lag:end]
    if invert:
        seg = -seg
    hard = (seg > 0).astype(np.uint8)
    expect = (tmpl > 0).astype(np.uint8)
    return float(np.mean(hard == expect))


def _freq_weight(offset_hz: float, centre: float) -> float:
    delta = abs(offset_hz - centre)
    return 1.0 / (1.0 + (delta / _FREQ_FOCUS_HZ) ** 2)


def _score_at_lag(
    soft: np.ndarray,
    corr: np.ndarray,
    lag: int,
    tmpl: np.ndarray,
    *,
    offset_hz: float,
    centre: float,
) -> tuple[float, int, bool, bool] | None:
    if lag + len(tmpl) + 16 > len(soft):
        return None
    for invert in (False, True):
        if _template_agreement(soft, lag, tmpl, invert) < _MIN_TEMPLATE_AGREE:
            continue
        wake_snr = _local_peak_snr(corr, lag)
        if wake_snr < _MIN_WAKE_SNR:
            continue
        pre_snr = _corr_at(soft, lag - _PREAMBLE_SYMS, _PREAMBLE_PM1)
        used_pre = pre_snr >= _MIN_PREAMBLE_SNR
        if len(tmpl) == len(_WAKE_PM1):
            if not used_pre:
                continue
            score = wake_snr * pre_snr
        elif used_pre:
            score = wake_snr * pre_snr
        elif wake_snr >= _MIN_CORR_SNR:
            score = wake_snr
        else:
            continue
        score *= _freq_weight(offset_hz, centre)
        if lag == _PREAMBLE_SYMS:
            score *= _CANONICAL_LAG_BOOST
        return score, lag, invert, used_pre
    return None


def _score_lock(
    soft: np.ndarray,
    tmpl: np.ndarray,
    *,
    offset_hz: float = 0.0,
    centre: float = 0.0,
) -> tuple[float, int, bool, bool] | None:
    """Unique-word correlator, then confirm the preamble sits 64 symbols earlier."""
    if len(soft) < len(tmpl) + 16:
        return None
    centered = tmpl - tmpl.mean()
    corr = np.correlate(soft, centered, mode="valid")
    if len(corr) == 0:
        return None
    best: tuple[float, int, bool, bool] | None = None

    scan_limit = min(len(soft) - len(tmpl) - 16, 4 * _PREAMBLE_SYMS)
    for wake_lag in range(_PREAMBLE_SYMS, max(_PREAMBLE_SYMS + 1, scan_limit)):
        cand = _score_at_lag(
            soft, corr, wake_lag, tmpl, offset_hz=offset_hz, centre=centre
        )
        if cand is not None and (best is None or cand[0] > best[0]):
            best = cand

    pre_centered = _PREAMBLE_PM1 - _PREAMBLE_PM1.mean()
    pre_corr = np.correlate(soft, pre_centered, mode="valid")
    for pre_lag in np.argsort(np.abs(pre_corr))[::-1][:10]:
        wake_lag = int(pre_lag) + _PREAMBLE_SYMS
        cand = _score_at_lag(
            soft, corr, wake_lag, tmpl, offset_hz=offset_hz, centre=centre
        )
        if cand is not None and (best is None or cand[0] > best[0]):
            best = cand

    for idx in np.argsort(np.abs(corr))[::-1][:12]:
        cand = _score_at_lag(
            soft, corr, int(idx), tmpl, offset_hz=offset_hz, centre=centre
        )
        if cand is not None and (best is None or cand[0] > best[0]):
            best = cand
    return best


def _interp_c(iq: np.ndarray, start: float, length: int) -> np.ndarray | None:
    idx = start + np.arange(length, dtype=np.float64)
    if idx[0] < 0 or idx[-1] >= len(iq) - 1:
        return None
    i0 = np.floor(idx).astype(np.intp)
    frac = idx - i0
    return iq[i0] * (1.0 - frac) + iq[i0 + 1] * frac


def _symbol_soft(
    iq: np.ndarray,
    start: float,
    *,
    sps_i: int,
    sample_rate: float,
    deviation: float,
    offset_hz: float,
) -> tuple[float, float] | None:
    sl = _interp_c(iq, start, sps_i)
    if sl is None:
        return None
    n = start + np.arange(sps_i, dtype=np.float64)
    shifted = sl * np.exp(-2j * np.pi * offset_hz * n / sample_rate)
    t = np.arange(sps_i, dtype=np.float64) / sample_rate
    mark = np.exp(-2j * np.pi * deviation * t)
    space = np.exp(2j * np.pi * deviation * t)
    e_mark = float(np.abs(shifted @ mark) ** 2)
    e_space = float(np.abs(shifted @ space) ** 2)
    # Raw energy differences make one loud (or clipped) symbol dominate
    # min-sum BP while a faded symbol contributes almost nothing.  For an
    # unknown, time-varying HF channel, the energy ratio is the useful
    # reliability: bounded to [-1, 1], sign-preserving, and insensitive to
    # slow amplitude fading. Acquisition keeps its unnormalised metric.
    soft = (e_mark - e_space) / (e_mark + e_space + 1e-12)
    inst = np.angle(shifted[1:] * np.conj(shifted[:-1]))
    inst_hz = float(np.mean(inst)) * sample_rate / (2.0 * np.pi)
    return soft, inst_hz


def _costas_known(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    start: float,
    invert: bool,
    known: np.ndarray,
) -> tuple[float, float]:
    """Lock carrier and symbol timing on a known ±1 bit sequence."""
    sps = sample_rate / baud
    sps_i = int(round(sps))
    t = float(start)
    freq = float(offset_hz)
    freq_lo = offset_hz - _FREQ_SPAN_HZ
    freq_hi = offset_hz + _FREQ_SPAN_HZ
    for k in range(len(known)):
        bit = 1 if known[k] > 0 else 0
        if invert:
            bit ^= 1
        pair = _symbol_soft(
            iq,
            t,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        if pair is None:
            break
        soft, inst_hz = pair
        expected = deviation if bit else -deviation
        if invert:
            expected = -expected
        freq = float(np.clip(freq + _FREQ_LOOP * (inst_hz - expected), freq_lo, freq_hi))
        early = _symbol_soft(
            iq,
            t - 0.7,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        late = _symbol_soft(
            iq,
            t + 0.7,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        if early is not None and late is not None:
            s_e, s_l = early[0], late[0]
            if invert:
                s_e, s_l = -s_e, -s_l
            ted = abs(s_l) - abs(s_e)
            t += _TED_LOOP * np.tanh(ted / (abs(soft) + 1e-6))
        t += sps
    return freq, t


def _prefer_lock(
    old: tuple[float, float, int, int, bool, bool],
    new: tuple[float, float, int, int, bool, bool],
) -> bool:
    old_snr, _, _, old_sym, _, _ = old
    new_snr, _, _, new_sym, _, _ = new
    if new_snr > old_snr * 1.05:
        return True
    if new_sym <= _PREAMBLE_SYMS * 2 and old_sym > _PREAMBLE_SYMS * 2:
        return new_snr > old_snr * 0.4
    if new_snr >= old_snr * 0.9 and new_sym < old_sym:
        return True
    return False


def _collect_locks(
    samples: np.ndarray,
    *,
    acq_rate: float,
    baud: float,
    deviation: float,
    tmpl: np.ndarray,
    centre: float,
) -> list[tuple[float, float, int, int, bool, bool]]:
    acq_sps = int(round(acq_rate / baud))
    tau_step = max(1, acq_sps // 10)
    candidates: list[tuple[float, float, int, int, bool, bool]] = []
    seen: set[tuple[float, int, int]] = set()
    for offset_hz in _freq_candidates(centre):
        metric = _soft_stream(
            samples,
            sample_rate=acq_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=offset_hz,
        )
        if metric is None:
            continue
        for tau in range(0, acq_sps, tau_step):
            soft = metric[tau::acq_sps]
            scored = _score_lock(soft, tmpl, offset_hz=offset_hz, centre=centre)
            if scored is None:
                continue
            snr, wake_sym, invert, used_pre = scored
            key = (round(offset_hz, 1), tau, wake_sym)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((snr, offset_hz, tau, wake_sym, invert, used_pre))
    candidates.sort(reverse=True)
    return candidates[:_TOP_LOCKS]


def _search_template(
    samples: np.ndarray,
    *,
    acq_rate: float,
    baud: float,
    deviation: float,
    tmpl: np.ndarray,
    centre: float,
) -> tuple[float, float, int, int, bool, bool]:
    acq_sps = int(round(acq_rate / baud))
    tau_step = max(1, acq_sps // 10)
    best = (-np.inf, 0.0, 0, 0, False, False)
    for offset_hz in _freq_candidates(centre):
        metric = _soft_stream(
            samples,
            sample_rate=acq_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=offset_hz,
        )
        if metric is None:
            continue
        for tau in range(0, acq_sps, tau_step):
            soft = metric[tau::acq_sps]
            scored = _score_lock(soft, tmpl, offset_hz=offset_hz, centre=centre)
            if scored is None:
                continue
            snr, wake_sym, invert, used_pre = scored
            if _prefer_lock(best, (snr, offset_hz, tau, wake_sym, invert, used_pre)):
                best = (snr, offset_hz, tau, wake_sym, invert, used_pre)
    return best


def _refine_lock(
    samples: np.ndarray,
    *,
    acq_rate: float,
    baud: float,
    deviation: float,
    tmpl: np.ndarray,
    centre: float,
    snr: float,
    offset_hz: float,
    tau: int,
    wake_sym: int,
    invert: bool,
    used_pre: bool,
) -> tuple[float, float, int, int, bool, bool]:
    acq_sps = int(round(acq_rate / baud))
    tau_step = max(1, acq_sps // 10)
    fine = (snr, offset_hz, tau, wake_sym, invert, used_pre)
    tau_lo = max(0, tau - tau_step)
    tau_hi = min(acq_sps, tau + tau_step + 1)
    for extra in (-_FREQ_STEP_HZ, 0.0, _FREQ_STEP_HZ):
        cand_f = offset_hz + extra
        metric = _soft_stream(
            samples,
            sample_rate=acq_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=cand_f,
        )
        if metric is None:
            continue
        for cand_tau in range(tau_lo, tau_hi):
            soft = metric[cand_tau::acq_sps]
            scored = _score_lock(soft, tmpl, offset_hz=cand_f, centre=centre)
            if scored is None:
                continue
            cand_snr, cand_wake, cand_inv, cand_pre = scored
            if _prefer_lock(fine, (cand_snr, cand_f, cand_tau, cand_wake, cand_inv, cand_pre)):
                fine = (cand_snr, cand_f, cand_tau, cand_wake, cand_inv, cand_pre)
    return fine


def _acquire_locks(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
) -> list[tuple[float, int, np.ndarray, bool, float]]:
    """Return ranked (offset_hz, wake_start, tmpl, used_pre, snr) locks."""
    samples, acq_rate, stride = _decimate_for_acq(iq, sample_rate, baud)
    centre = _estimate_offset(samples, acq_rate)
    candidates = _collect_locks(
        samples,
        acq_rate=acq_rate,
        baud=baud,
        deviation=deviation,
        tmpl=_WAKE_PM1,
        centre=centre,
    )
    tmpl = _WAKE_PM1
    if not candidates or candidates[0][0] < _MIN_CORR_SNR:
        candidates = _collect_locks(
            samples,
            acq_rate=acq_rate,
            baud=baud,
            deviation=deviation,
            tmpl=_LEGACY_PM1,
            centre=centre,
        )
        tmpl = _LEGACY_PM1
    if not candidates or candidates[0][0] < _MIN_CORR_SNR:
        raise ValueError("sync word not found")
    acq_sps = int(round(acq_rate / baud))
    locks: list[tuple[float, int, np.ndarray, bool, float]] = []
    seen_start: set[int] = set()
    for snr, offset_hz, tau, wake_sym, _invert, used_pre in candidates:
        snr, offset_hz, tau, wake_sym, _invert, used_pre = _refine_lock(
            samples,
            acq_rate=acq_rate,
            baud=baud,
            deviation=deviation,
            tmpl=tmpl,
            centre=centre,
            snr=snr,
            offset_hz=offset_hz,
            tau=tau,
            wake_sym=wake_sym,
            invert=_invert,
            used_pre=used_pre,
        )
        wake_start = int((tau + wake_sym * acq_sps) * stride)
        if wake_start in seen_start:
            continue
        seen_start.add(wake_start)
        locks.append((offset_hz, wake_start, tmpl, used_pre, snr))
    if not locks:
        raise ValueError("sync word not found")
    return locks


def _acquire(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
) -> tuple[float, int, bool, np.ndarray, float]:
    """Return the best (offset_hz, wake sample start, invert, unique-word ±1, corr_snr)."""
    offset_hz, wake_start, tmpl, _used_pre, snr = _acquire_locks(
        iq, sample_rate=sample_rate, baud=baud, deviation=deviation
    )[0]
    return offset_hz, wake_start, False, tmpl, snr


def _costas_refine(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    wake_start: int,
    invert: bool,
    tmpl: np.ndarray,
    used_pre: bool,
) -> float:
    sps_i = int(round(sample_rate / baud))
    if used_pre:
        preamble_start = wake_start - _PREAMBLE_SYMS * sps_i
        lock_known = np.concatenate([_PREAMBLE_PM1, tmpl])
        start = float(preamble_start)
        known = lock_known
    else:
        start = float(wake_start)
        known = tmpl
    freq = float(offset_hz)
    for _ in range(2):
        freq, _ = _costas_known(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=freq,
            start=start,
            invert=invert,
            known=known,
        )
    return freq


def _decode_at_lock(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    wake_start: int,
    tmpl: np.ndarray,
    used_pre: bool,
) -> Frame:
    wake_len = len(tmpl)
    preamble_start = wake_start - _PREAMBLE_SYMS * int(round(sample_rate / baud))
    known_full = np.concatenate([_PREAMBLE_PM1, tmpl])
    last_error: ValueError | None = None
    for invert in (False, True):
        freq = _costas_refine(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=offset_hz,
            wake_start=wake_start,
            invert=invert,
            tmpl=tmpl,
            used_pre=used_pre,
        )
        untracked = _untracked_body(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=freq,
            start=wake_start,
            invert=invert,
            wake_len=wake_len,
        )
        tracked = _track_body(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=freq,
            start=preamble_start,
            invert=invert,
            known=known_full,
        )
        for bits, llr in (tracked, untracked) if tracked is not None else (untracked,):
            try:
                return _try_body(bits, llr)
            except ValueError as exc:
                last_error = exc
    raise last_error or ValueError("sync word not found")


def _decode_full_known_at_lock(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    wake_start: int,
) -> Frame:
    """Decode a current FEC frame around a strong full-pattern lock.

    The long correlation estimates time to about a tenth of a symbol and
    carrier to one frequency-grid cell. Decision-directed refinement was
    less reliable near threshold: one bad tentative bit could pull the loop
    toward the opposite FSK tone. Try a small deterministic local grid and
    let LDPC+CRC select the right hypothesis.
    """
    last_error: ValueError | None = None
    known = np.concatenate([_PREAMBLE_PM1, _WAKE_PM1])
    preamble_start = wake_start - _PREAMBLE_SYMS * int(
        round(sample_rate / baud)
    )

    # Prefer the timing-tracked path for long/mobile frames. Its payload
    # carrier loop only accepts high-confidence decisions and tracks slowly,
    # while the early/late detector follows sample-clock error.
    for invert in (False, True):
        tracked = _track_body(
            iq,
            sample_rate=sample_rate,
            baud=baud,
            deviation=deviation,
            offset_hz=offset_hz,
            start=preamble_start,
            invert=invert,
            known=known,
        )
        if tracked is None:
            continue
        _bits, llr = tracked
        for n_blocks in n_blocks_candidates_from_llr(llr)[:4]:
            try:
                return Frame.decode(decode_coded_llr(llr, n_blocks=n_blocks))
            except ValueError as exc:
                last_error = exc

    # Most locks are exact. Keep that path first and expand only on CRC fail.
    freq_delta = (0.0, -5.0, 5.0, -10.0, 10.0)
    sample_delta = (0, -2, 2, -4, 4, -6, 6)
    hypotheses = sorted(
        ((df, ds) for df in freq_delta for ds in sample_delta),
        key=lambda item: abs(item[0]) + 2.5 * abs(item[1]),
    )
    # Bound failure latency so one damaged burst cannot monopolize the Pi's
    # single decode worker. Correct locks normally succeed in the first few.
    for df, ds in hypotheses[:20]:
        for invert in (True, False):
            _bits, llr = _untracked_body(
                iq,
                sample_rate=sample_rate,
                baud=baud,
                deviation=deviation,
                offset_hz=offset_hz + df,
                start=wake_start + ds,
                invert=invert,
                wake_len=len(_WAKE_PM1),
            )
            # Duration is first, then the soft header. Try both: a long
            # capture tail (detector hang, preroll) can throw duration off
            # by a block, and that used to be the only hypothesis.
            for n_blocks in n_blocks_candidates_from_llr(llr)[:4]:
                try:
                    return Frame.decode(
                        decode_coded_llr(llr, n_blocks=n_blocks)
                    )
                except ValueError as exc:
                    last_error = exc
            # Same preamble is still used by CRC-only bench frames. Recover
            # those, but do not let a failed v1 parse hide the LDPC reason
            # as "frame shorter than LEN".
            try:
                return _decode_v1(_bits)
            except ValueError:
                pass
    raise last_error or ValueError("LDPC frame not found near full preamble lock")


def _untracked_body(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    start: int,
    invert: bool,
    wake_len: int,
) -> tuple[np.ndarray, np.ndarray]:
    sps = int(round(sample_rate / baud))
    metric = _soft_stream(
        iq,
        sample_rate=sample_rate,
        baud=baud,
        deviation=deviation,
        offset_hz=offset_hz,
    )
    if metric is None or start >= len(metric):
        raise ValueError("sync word not found")
    signed = metric[start::sps]
    if invert:
        signed = -signed
    body = signed[wake_len:]
    bits = (body > 0).astype(np.uint8)
    llr = (-body).astype(np.float64)
    return bits, llr


def _track_body(
    iq: np.ndarray,
    *,
    sample_rate: float,
    baud: float,
    deviation: float,
    offset_hz: float,
    start: int,
    invert: bool,
    known: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    sps = sample_rate / baud
    sps_i = int(round(sps))
    t = float(start)
    freq = float(offset_hz)
    n_symbols = int((len(iq) - start) / sps) - 1
    if n_symbols <= len(known) + 16:
        return None
    known_bits = (known > 0).astype(np.uint8)
    body: list[float] = []
    freq_lo = offset_hz - _FREQ_SPAN_HZ
    freq_hi = offset_hz + _FREQ_SPAN_HZ
    for k in range(n_symbols):
        pair = _symbol_soft(
            iq,
            t,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        if pair is None:
            break
        soft, inst_hz = pair
        if invert:
            soft = -soft
        if k < len(known_bits):
            bit = int(known_bits[k])
            expected = deviation if bit else -deviation
            if invert:
                expected = -expected
            freq = float(
                np.clip(
                    freq + _FREQ_LOOP * (inst_hz - expected),
                    freq_lo,
                    freq_hi,
                )
            )
        else:
            body.append(soft)
            # Track only from high-confidence payload decisions, and much
            # more slowly than on known symbols. This follows drift on long
            # frames without allowing one noisy decision to pull the loop
            # toward the opposite FSK tone.
            confidence = abs(soft)
            if confidence >= 0.5:
                bit = 1 if soft > 0 else 0
                expected = deviation if bit else -deviation
                if invert:
                    expected = -expected
                freq = float(
                    np.clip(
                        freq
                        + _FREQ_LOOP_BODY
                        * confidence
                        * (inst_hz - expected),
                        freq_lo,
                        freq_hi,
                    )
                )
        early = _symbol_soft(
            iq,
            t - 0.7,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        late = _symbol_soft(
            iq,
            t + 0.7,
            sps_i=sps_i,
            sample_rate=sample_rate,
            deviation=deviation,
            offset_hz=freq,
        )
        if early is not None and late is not None:
            s_e, s_l = early[0], late[0]
            if invert:
                s_e, s_l = -s_e, -s_l
            ted = abs(s_l) - abs(s_e)
            t += _TED_LOOP * np.tanh(ted / (abs(soft) + 1e-6))
        t += sps
    if len(body) < 16:
        return None
    signed = np.asarray(body, dtype=np.float64)
    bits = (signed > 0).astype(np.uint8)
    llr = (-signed).astype(np.float64)
    return bits, llr


def _decode_v1(bits: np.ndarray) -> Frame:
    framed = np.packbits(bits).tobytes()
    total = Frame.wire_size(framed)
    if total is None:
        raise ValueError("payload truncated after sync")
    if len(framed) < total:
        raise ValueError("frame shorter than LEN")
    return Frame.decode(framed[:total])


def _try_body(bits: np.ndarray, llr: np.ndarray) -> Frame:
    last_error: ValueError | None = None
    for n_blocks in n_blocks_candidates_from_llr(llr)[:4]:
        try:
            return Frame.decode(decode_coded_llr(llr, n_blocks=n_blocks))
        except ValueError as exc:
            last_error = exc
    try:
        return _decode_v1(bits)
    except ValueError:
        if last_error is not None:
            raise last_error
        raise


def demodulate(
    iq: np.ndarray,
    *,
    sample_rate: float = DEFAULT_CHANNEL_RATE,
    baud: float | None = None,
    deviation: float | None = None,
) -> Frame:
    iq = _highpass_dc(np.asarray(iq, dtype=np.complex64), sample_rate)
    modes = (
        ((DEFAULT_BAUD, DEFAULT_DEVIATION),)
        if baud is None and deviation is not None
        else ((float(baud), DEFAULT_DEVIATION if deviation is None else deviation),)
        if baud is not None
        else (
            (DEFAULT_BAUD, DEFAULT_DEVIATION),
            (LEGACY_BAUD, LEGACY_DEVIATION),
            (LEGACY_BAUD, LEGACY_WIDE_DEVIATION),
        )
    )
    if baud is None and deviation is not None:
        modes = ((DEFAULT_BAUD, deviation),)
    last_error: ValueError | None = None
    for mode_baud, dev in modes:
        if mode_baud == DEFAULT_BAUD and dev == DEFAULT_DEVIATION:
            # First integrate all 128 known symbols. This is substantially
            # more sensitive than hard per-symbol agreement or the 7-symbol
            # Costas marker, while a normalized threshold rejects noise.
            try:
                offset_hz, wake_start, _score = _full_known_lock(
                    iq,
                    sample_rate=sample_rate,
                    baud=mode_baud,
                    deviation=dev,
                )
            except ValueError as exc:
                last_error = exc
            else:
                # A strong 128-symbol match uniquely identifies the current
                # air format. If its bounded timing/frequency search cannot
                # satisfy LDPC+CRC, probing 300-baud legacy modes only ties
                # up the Pi decode worker and cannot recover this frame.
                return _decode_full_known_at_lock(
                    iq,
                    sample_rate=sample_rate,
                    baud=mode_baud,
                    deviation=dev,
                    offset_hz=offset_hz,
                    wake_start=wake_start,
                )
            try:
                offset_hz, data_start, _score = _costas_lock(
                    iq,
                    sample_rate=sample_rate,
                    baud=mode_baud,
                )
                wake_start = data_start + _PREAMBLE_SYMS * int(
                    round(sample_rate / mode_baud)
                )
                return _decode_at_lock(
                    iq,
                    sample_rate=sample_rate,
                    baud=mode_baud,
                    deviation=dev,
                    offset_hz=offset_hz,
                    wake_start=wake_start,
                    tmpl=_WAKE_PM1,
                    used_pre=True,
                )
            except ValueError as exc:
                last_error = exc
        try:
            locks = _acquire_locks(
                iq, sample_rate=sample_rate, baud=mode_baud, deviation=dev
            )
        except ValueError as exc:
            last_error = exc
            continue
        for offset_hz, wake_start, tmpl, used_pre, _snr in locks:
            try:
                return _decode_at_lock(
                    iq,
                    sample_rate=sample_rate,
                    baud=mode_baud,
                    deviation=dev,
                    offset_hz=offset_hz,
                    wake_start=wake_start,
                    tmpl=tmpl,
                    used_pre=used_pre,
                )
            except ValueError as exc:
                last_error = exc
    raise last_error or ValueError("sync word not found")
