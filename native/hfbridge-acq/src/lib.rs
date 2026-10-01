//! Acquisition search and LDPC decode for 2-CPFSK frames.
//!
//! Same algorithms as `hfbridge.fsk`: DC high-pass, Costas 7-symbol lock, then
//! a coherent correlation over the 128 known preamble + unique-word symbols.
//! Python still owns tracking and CRC.

use std::f64::consts::PI;

use num_complex::Complex64;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rustfft::{Fft, FftPlanner};

mod ldpc;

const COSTAS_TONES: [i32; 7] = [3, 1, 4, 0, 6, 5, 2];
const PREAMBLE_BYTES: [u8; 8] = [0x55; 8];
const WAKE_BYTES: [u8; 8] = [0xfd, 0x59, 0xbb, 0x49, 0xc5, 0xe5, 0x18, 0x40];
const FREQ_SPAN_HZ: f64 = 400.0;
const FREQ_STEP_HZ: f64 = 25.0;
const MIN_COSTAS_SCORE: f64 = 4.5;
const MIN_FULL_KNOWN_CORR: f64 = 0.50;

fn pm1_from_bytes(data: &[u8]) -> Vec<f64> {
    let mut out = Vec::with_capacity(data.len() * 8);
    for &byte in data {
        for i in (0..8).rev() {
            out.push(if (byte >> i) & 1 == 1 { 1.0 } else { -1.0 });
        }
    }
    out
}

fn arange(start: f64, stop: f64, step: f64) -> Vec<f64> {
    let mut out = Vec::new();
    let mut i = 0i32;
    loop {
        let x = start + f64::from(i) * step;
        if x >= stop {
            break;
        }
        out.push(x);
        i += 1;
        if i > 10_000 {
            break;
        }
    }
    out
}

fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

fn freq_candidates(centre: f64) -> Vec<f64> {
    let mut vals: Vec<f64> = arange(-FREQ_SPAN_HZ, FREQ_SPAN_HZ + 0.1, FREQ_STEP_HZ)
        .into_iter()
        .map(round3)
        .collect();
    for delta in arange(-60.0, 61.0, 10.0) {
        vals.push(round3(centre + delta));
    }
    vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    vals.dedup();
    vals
}

fn median(mut xs: Vec<f64>) -> f64 {
    if xs.is_empty() {
        return 0.0;
    }
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = xs.len();
    if n % 2 == 1 {
        xs[n / 2]
    } else {
        0.5 * (xs[n / 2 - 1] + xs[n / 2])
    }
}

fn hanning(m: usize) -> Vec<f64> {
    if m == 1 {
        return vec![1.0];
    }
    let denom = (m - 1) as f64;
    (0..m)
        .map(|n| 0.5 - 0.5 * (2.0 * PI * n as f64 / denom).cos())
        .collect()
}

fn fftfreq_shifted(n: usize, sample_rate: f64) -> Vec<f64> {
    let val = sample_rate / n as f64;
    let half = n / 2;
    let mut freqs = vec![0.0; n];
    for (k, slot) in freqs.iter_mut().enumerate() {
        *slot = if k < half {
            k as f64 * val
        } else {
            (k as i64 - n as i64) as f64 * val
        };
    }
    let mut shifted = vec![0.0; n];
    for (i, slot) in shifted.iter_mut().enumerate() {
        *slot = freqs[(i + half) % n];
    }
    shifted
}

fn samples_from_bytes(raw: &[u8]) -> Result<Vec<Complex64>, &'static str> {
    if raw.len() % 8 != 0 {
        return Err("IQ bytes must be complex64");
    }
    let mut out = Vec::with_capacity(raw.len() / 8);
    for chunk in raw.chunks_exact(8) {
        let re = f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
        let im = f32::from_le_bytes([chunk[4], chunk[5], chunk[6], chunk[7]]);
        out.push(Complex64::new(f64::from(re), f64::from(im)));
    }
    Ok(out)
}

fn estimate_offset(iq: &[Complex64], sample_rate: f64) -> f64 {
    if iq.len() < 32 {
        return 0.0;
    }
    let scale = sample_rate / (2.0 * PI);
    let mut inst = Vec::with_capacity(iq.len() - 1);
    let mut amp = Vec::with_capacity(iq.len() - 1);
    for w in iq.windows(2) {
        let delta = w[1] * w[0].conj();
        inst.push(delta.arg() * scale);
        amp.push(w[1].norm());
    }
    let max_amp = amp.iter().copied().fold(0.0_f64, f64::max);
    let threshold = 0.3 * max_amp;
    let mut loud: Vec<f64> = inst
        .iter()
        .zip(amp.iter())
        .filter_map(|(&f, &a)| if a > threshold { Some(f) } else { None })
        .collect();
    if loud.len() < 16 {
        loud = inst;
    }
    let mut hist = [0i32; 80];
    for &x in &loud {
        if !(-800.0..800.0).contains(&x) && x != 800.0 {
            continue;
        }
        let mut bin = ((x + 800.0) / 20.0).floor() as i32;
        if x == 800.0 {
            bin = 79;
        }
        if (0..80).contains(&bin) {
            hist[bin as usize] += 1;
        }
    }
    let ceiling = hist.iter().copied().max().unwrap_or(0);
    if ceiling <= 0 {
        return 0.0;
    }
    let mut peaks: Vec<(i32, f64)> = Vec::new();
    for i in 1..79 {
        if hist[i] >= hist[i - 1]
            && hist[i] >= hist[i + 1]
            && hist[i] as f64 > 0.15 * f64::from(ceiling)
        {
            let left = -800.0 + i as f64 * 20.0;
            peaks.push((hist[i], 0.5 * (left + left + 20.0)));
        }
    }
    peaks.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.partial_cmp(&a.1).unwrap()));
    if peaks.len() >= 2 && (peaks[0].1 - peaks[1].1).abs() > 60.0 {
        return 0.5 * (peaks[0].1 + peaks[1].1);
    }
    if let Some((_, freq)) = peaks.first() {
        return *freq;
    }
    loud.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    loud[loud.len() / 2]
}

struct SoftMatcher {
    fft: std::sync::Arc<dyn Fft<f64>>,
    ifft: std::sync::Arc<dyn Fft<f64>>,
    nfft: usize,
    sps: usize,
    mark_fft_conj: Vec<Complex64>,
    space_fft_conj: Vec<Complex64>,
    scratch: Vec<Complex64>,
    work: Vec<Complex64>,
    sig_fft: Vec<Complex64>,
}

impl SoftMatcher {
    fn new(n: usize, sample_rate: f64, baud: f64, deviation: f64) -> Option<Self> {
        let sps = (sample_rate / baud).round() as usize;
        if n < sps * 16 {
            return None;
        }
        let nfft = (n + sps).next_power_of_two();
        let mut planner = FftPlanner::<f64>::new();
        let fft = planner.plan_fft_forward(nfft);
        let ifft = planner.plan_fft_inverse(nfft);
        let mut mark = vec![Complex64::new(0.0, 0.0); nfft];
        let mut space = vec![Complex64::new(0.0, 0.0); nfft];
        for i in 0..sps {
            let t = i as f64 / sample_rate;
            mark[i] = Complex64::from_polar(1.0, -2.0 * PI * deviation * t);
            space[i] = Complex64::from_polar(1.0, 2.0 * PI * deviation * t);
        }
        fft.process(&mut mark);
        fft.process(&mut space);
        for slot in mark.iter_mut() {
            *slot = slot.conj();
        }
        for slot in space.iter_mut() {
            *slot = slot.conj();
        }
        Some(Self {
            fft,
            ifft,
            nfft,
            sps,
            mark_fft_conj: mark,
            space_fft_conj: space,
            scratch: vec![Complex64::new(0.0, 0.0); nfft],
            work: vec![Complex64::new(0.0, 0.0); nfft],
            sig_fft: vec![Complex64::new(0.0, 0.0); nfft],
        })
    }

    fn metric(&mut self, iq: &[Complex64], sample_rate: f64, offset_hz: f64) -> Vec<f64> {
        let n = iq.len();
        let omega = -2.0 * PI * offset_hz / sample_rate;
        self.scratch.fill(Complex64::new(0.0, 0.0));
        for (i, &z) in iq.iter().enumerate() {
            self.scratch[i] = z * Complex64::from_polar(1.0, omega * i as f64);
        }
        self.fft.process(&mut self.scratch);
        self.sig_fft.copy_from_slice(&self.scratch);
        let scale = 1.0 / self.nfft as f64;
        let valid = n - self.sps + 1;

        for i in 0..self.nfft {
            self.work[i] = self.sig_fft[i] * self.mark_fft_conj[i];
        }
        self.ifft.process(&mut self.work);
        let mut mark_out = vec![Complex64::new(0.0, 0.0); valid];
        for (i, slot) in mark_out.iter_mut().enumerate() {
            *slot = self.work[i] * scale;
        }

        for i in 0..self.nfft {
            self.work[i] = self.sig_fft[i] * self.space_fft_conj[i];
        }
        self.ifft.process(&mut self.work);
        let mut metric = vec![0.0; valid];
        for (i, slot) in metric.iter_mut().enumerate() {
            let s = self.work[i] * scale;
            *slot = mark_out[i].norm_sqr() - s.norm_sqr();
        }
        metric
    }
}

fn correlate_valid(a: &[f64], v: &[f64]) -> Vec<f64> {
    let out_len = a.len().saturating_sub(v.len()) + 1;
    if a.len() < v.len() {
        return Vec::new();
    }
    let mut out = vec![0.0; out_len];
    for (k, slot) in out.iter_mut().enumerate() {
        let mut sum = 0.0;
        for (n, &vn) in v.iter().enumerate() {
            sum += a[k + n] * vn;
        }
        *slot = sum;
    }
    out
}

fn sliding_energy(symbols: &[f64], width: usize) -> Vec<f64> {
    if symbols.len() < width {
        return Vec::new();
    }
    let mut prefix = vec![0.0; symbols.len() + 1];
    for (i, &x) in symbols.iter().enumerate() {
        prefix[i + 1] = prefix[i] + x * x;
    }
    (0..=symbols.len() - width)
        .map(|k| prefix[k + width] - prefix[k])
        .collect()
}

fn argmax(xs: &[f64]) -> usize {
    let mut best_i = 0;
    let mut best = f64::NEG_INFINITY;
    for (i, &x) in xs.iter().enumerate() {
        if x > best {
            best = x;
            best_i = i;
        }
    }
    best_i
}

fn costas_lock_inner(
    iq: &[Complex64],
    sample_rate: f64,
    baud: f64,
) -> Result<(f64, usize, f64), &'static str> {
    let sps = (sample_rate / baud).round() as usize;
    if sps < 4 || iq.len() < (COSTAS_TONES.len() + 16) * sps {
        return Err("Costas preamble not found");
    }
    let mut nfft = 1usize;
    while nfft < 4 * sps {
        nfft *= 2;
    }
    let freqs = fftfreq_shifted(nfft, sample_rate);
    let carrier_grid = arange(-FREQ_SPAN_HZ, FREQ_SPAN_HZ + 0.1, 25.0);
    let tone_offsets: Vec<f64> = COSTAS_TONES
        .iter()
        .map(|&tone| (f64::from(tone) - (COSTAS_TONES.len() - 1) as f64 / 2.0 + 0.5) * baud)
        .collect();
    let carrier_bins: Vec<Vec<usize>> = carrier_grid
        .iter()
        .map(|&carrier| {
            tone_offsets
                .iter()
                .map(|&offset| {
                    let target = carrier + offset;
                    let mut best_i = 0;
                    let mut best_d = f64::INFINITY;
                    for (i, &f) in freqs.iter().enumerate() {
                        let d = (f - target).abs();
                        if d < best_d {
                            best_d = d;
                            best_i = i;
                        }
                    }
                    best_i
                })
                .collect()
        })
        .collect();

    let max_blocks = (2.0 * baud) as usize + COSTAS_TONES.len();
    let tau_step = 1.max(sps / 20);
    let window = hanning(sps);
    let mut planner = FftPlanner::<f64>::new();
    let fft: std::sync::Arc<dyn Fft<f64>> = planner.plan_fft_forward(nfft);
    let half = nfft / 2;

    let mut best = (f64::NEG_INFINITY, 0.0, 0usize);
    let mut scores: Vec<f64> = Vec::new();

    for tau in (0..sps).step_by(tau_step) {
        let n_blocks = ((iq.len() - tau) / sps).min(max_blocks);
        if n_blocks < COSTAS_TONES.len() + 1 {
            continue;
        }
        let mut power = vec![0.0; n_blocks * nfft];
        let mut row = vec![Complex64::new(0.0, 0.0); nfft];
        for b in 0..n_blocks {
            row.fill(Complex64::new(0.0, 0.0));
            let base = tau + b * sps;
            for i in 0..sps {
                let z = iq[base + i];
                row[i] = Complex64::new(z.re * window[i], z.im * window[i]);
            }
            fft.process(&mut row);
            for i in 0..nfft {
                let src = row[(i + half) % nfft];
                power[b * nfft + i] = src.norm_sqr();
            }
        }
        for (carrier_i, &carrier) in carrier_grid.iter().enumerate() {
            let bins = &carrier_bins[carrier_i];
            for start in 0..=n_blocks - COSTAS_TONES.len() {
                let mut score = 0.0;
                for (k, &bin) in bins.iter().enumerate() {
                    score += power[(start + k) * nfft + bin];
                }
                scores.push(score);
                if score > best.0 {
                    best = (score, carrier, tau + start * sps);
                }
            }
        }
    }
    if scores.is_empty() {
        return Err("Costas preamble not found");
    }
    let ratio = best.0 / (median(scores) + 1e-12);
    if ratio < MIN_COSTAS_SCORE {
        return Err("Costas preamble not found");
    }
    Ok((best.1, best.2 + COSTAS_TONES.len() * sps, ratio))
}

fn full_known_lock_inner(
    iq: &[Complex64],
    sample_rate: f64,
    baud: f64,
    deviation: f64,
) -> Result<(f64, usize, f64), &'static str> {
    let sps = (sample_rate / baud).round() as usize;
    if sps < 2 {
        return Err("full preamble not found");
    }
    let mut known = pm1_from_bytes(&PREAMBLE_BYTES);
    known.extend(pm1_from_bytes(&WAKE_BYTES));
    let preamble_syms = PREAMBLE_BYTES.len() * 8;
    let known_energy: f64 = known.iter().map(|x| x * x).sum();
    let centre = estimate_offset(iq, sample_rate);
    let tau_step = 1.max(sps / 10);
    let mut best = (f64::NEG_INFINITY, 0.0, 0usize);
    let Some(mut matcher) = SoftMatcher::new(iq.len(), sample_rate, baud, deviation) else {
        return Err("full preamble not found");
    };

    for offset_hz in freq_candidates(centre) {
        let metric = matcher.metric(iq, sample_rate, offset_hz);
        for tau in (0..sps).step_by(tau_step) {
            let symbols: Vec<f64> = metric.iter().skip(tau).step_by(sps).copied().collect();
            if symbols.len() < known.len() + 16 {
                continue;
            }
            let corr = correlate_valid(&symbols, &known);
            let energy = sliding_energy(&symbols, known.len());
            if corr.is_empty() || energy.len() != corr.len() {
                continue;
            }
            let mut coeff = vec![0.0; corr.len()];
            for i in 0..corr.len() {
                let denom_sq = energy[i] * known_energy;
                coeff[i] = if denom_sq < 1e-18 {
                    0.0
                } else {
                    corr[i].abs() / denom_sq.sqrt()
                };
            }
            let lag = argmax(&coeff);
            let score = coeff[lag];
            if score > best.0 {
                let wake_start = tau + (lag + preamble_syms) * sps;
                best = (score, offset_hz, wake_start);
            }
        }
    }
    if best.0 < MIN_FULL_KNOWN_CORR {
        return Err("full preamble not found");
    }
    Ok((best.1, best.2, best.0))
}

fn highpass_dc_bytes(raw: &[u8], sample_rate: f64) -> Result<Vec<u8>, &'static str> {
    if raw.len() % 8 != 0 {
        return Err("IQ bytes must be complex64");
    }
    if raw.len() < 16 {
        return Ok(raw.to_vec());
    }
    let alpha = (-2.0 * PI * 12.0 / sample_rate).exp();
    let mut out = vec![0u8; raw.len()];
    let mut pxr = 0.0;
    let mut pxi = 0.0;
    let mut pyr = 0.0;
    let mut pyi = 0.0;
    for (i, chunk) in raw.chunks_exact(8).enumerate() {
        let xr = f64::from(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
        let xi = f64::from(f32::from_le_bytes([chunk[4], chunk[5], chunk[6], chunk[7]]));
        let yr = xr - pxr + alpha * pyr;
        let yi = xi - pxi + alpha * pyi;
        let base = i * 8;
        out[base..base + 4].copy_from_slice(&(yr as f32).to_le_bytes());
        out[base + 4..base + 8].copy_from_slice(&(yi as f32).to_le_bytes());
        pxr = xr;
        pxi = xi;
        pyr = yr;
        pyi = yi;
    }
    Ok(out)
}

fn py_iq(raw: &[u8]) -> PyResult<Vec<Complex64>> {
    samples_from_bytes(raw).map_err(PyValueError::new_err)
}

#[pyfunction]
fn highpass_dc(py: Python<'_>, iq: &[u8], sample_rate: f64) -> PyResult<Vec<u8>> {
    py.allow_threads(|| highpass_dc_bytes(iq, sample_rate).map_err(PyValueError::new_err))
}

#[pyfunction]
fn costas_lock(
    py: Python<'_>,
    iq: &[u8],
    sample_rate: f64,
    baud: f64,
) -> PyResult<(f64, i64, f64)> {
    let samples = py_iq(iq)?;
    py.allow_threads(|| {
        costas_lock_inner(&samples, sample_rate, baud)
            .map(|(offset, start, ratio)| (offset, start as i64, ratio))
            .map_err(PyValueError::new_err)
    })
}

#[pyfunction]
fn full_known_lock(
    py: Python<'_>,
    iq: &[u8],
    sample_rate: f64,
    baud: f64,
    deviation: f64,
) -> PyResult<(f64, i64, f64)> {
    let samples = py_iq(iq)?;
    py.allow_threads(|| {
        full_known_lock_inner(&samples, sample_rate, baud, deviation)
            .map(|(offset, start, score)| (offset, start as i64, score))
            .map_err(PyValueError::new_err)
    })
}

#[pyfunction]
fn decode_ldpc_block(py: Python<'_>, llr: &[u8], iters: usize) -> PyResult<Vec<u8>> {
    if llr.len() != ldpc::N * 8 {
        return Err(PyValueError::new_err("LDPC block must be 128 float64 LLRs"));
    }
    let mut ch = [0.0_f64; ldpc::N];
    for (i, slot) in ch.iter_mut().enumerate() {
        let base = i * 8;
        *slot = f64::from_le_bytes([
            llr[base],
            llr[base + 1],
            llr[base + 2],
            llr[base + 3],
            llr[base + 4],
            llr[base + 5],
            llr[base + 6],
            llr[base + 7],
        ]);
    }
    py.allow_threads(|| {
        ldpc::decode_block(&ch, iters)
            .map(|hard| hard.to_vec())
            .map_err(PyValueError::new_err)
    })
}

#[pymodule]
fn _acq(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("native", true)?;
    m.add_function(wrap_pyfunction!(highpass_dc, m)?)?;
    m.add_function(wrap_pyfunction!(costas_lock, m)?)?;
    m.add_function(wrap_pyfunction!(full_known_lock, m)?)?;
    m.add_function(wrap_pyfunction!(decode_ldpc_block, m)?)?;
    Ok(())
}
