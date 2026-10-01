"""Listen on Technician 10 m data and try to decode a PoC frame.

The RTL-SDR read loop never demodulates. Finished bursts go on a queue and
a worker thread decodes them, so consecutive shouts are not dropped while
the previous frame is still being decoded.
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from collections.abc import Callable

from hfbridge.channelize import Channelizer
from hfbridge.fieldreport import ChannelStatus, DecodeEvent
from hfbridge.frame import VERSION_LIGHT, Frame
from hfbridge.fsk import DEFAULT_CHANNEL_RATE, demodulate, modulate

_REPO = Path(__file__).resolve().parents[2]
_CAPTURE_DIR = _REPO / "captures"
# The frame's Costas preamble is at the very front, and cutting it costs the
# whole frame ("frame shorter than LEN") even when the payload is loud. A
# signal that fades in trips the gate late, so hold enough audio to survive
# a trigger several seconds after the real frame start.
_PREROLL_SECONDS = 5.0
# Capture still keeps 5 s in case the gate trips late. Demod first tries
# hang + this much audio before the trip so lock does not walk stale quiet.
# If that misses the unique word, decode retries the full preroll.
_PREROLL_DECODE_SECONDS = 2.0
# After a fade, stay open this long so a mobile dip does not split a
# frame the decoder could still recover. One 0.11 s quiet chunk used
# to close the burst; the demodulator already survives a 1 s hole.
_BURST_HANG_SECONDS = 1.2
# A real frame's tones live within the shift of channel centre, so its median
# tone lands near zero. Noise picks a tone anywhere in the 6 kHz channel, and
# decoding those costs seconds each — enough to push the worker minutes behind
# and make every answer stale. Judge by tone before spending a decode.
_MAX_TONE_OFFSET_HZ = 400.0
# A result that arrives minutes after the frame is worthless during a live
# test, and the backlog that produced it also delays the frames that matter.
_MAX_JOB_AGE_SECONDS = 90.0
# While a burst is open the detector cannot start another one, so anything
# parked on the channel blinds the receiver for exactly this long. A field
# frame runs about 9 s and 30 bytes about 13 s, so re-baselining here still
# admits any real frame whole while cutting the blind window that a noisy
# antenna or a stuck carrier can impose.
_RELATCH_SECONDS = 20.0
# 2-CPFSK at 100 baud / ±50 Hz occupies roughly ±150 Hz. Gating on the whole
# 6 kHz channel throws away ~13 dB of measured SNR versus gating on the bins
# the signal actually lives in.
_SIGNAL_HALF_BW_HZ = 150.0
# The shortest current frame holds the detector for about 3.2 seconds.
# Reject shorter energy excursions before they consume the expensive
# frequency/timing search.
_MIN_DECODE_BURST_SECONDS = 2.5
# Longest real frame is ~13 s. A 60 s carrier (detector re-latch) must not
# be demodulated — that decode holds the GIL long enough to kill USB.
# A 200-byte maximum frame is about 40 seconds at 100 baud. Keep a little
# detector/chunk margin while still rejecting stuck carriers.
_MAX_DECODE_BURST_SECONDS = 45.0

# Same offset as hfbridge.transmit's test tone. Frames sit near 0 Hz
# (this bench often ~-156 Hz); a 5 s carrier lands near +1000 Hz.
_TEST_TONE_HZ = 1000.0
_SNR_TOO_WEAK_DB = 10.0
_SNR_MARGINAL_DB = 20.0
_SNR_STRONG_DB = 40.0
# Driveway shouts land 50–65 dB. One of those should ease the tuner, not
# slam it. Neighborhood copies are ~20–35 dB and should not back off.
_SNR_BACKOFF_DB = _SNR_STRONG_DB
_RTL_GAIN_STEPS = (0.0, 14.4, 29.7, 36.4, 42.1)
# A car's ignition noise is impulsive: it rails a few samples and is gone.
# Treating that as overload walked the tuner down to 0 dB on the last drive
# and cost roughly 30 dB of sensitivity, so require a real, sustained overload.
_SUSTAINED_CLIP_FRACTION = 0.01
_CLIP_RUNS_BEFORE_RETREAT = 3


class _SystemdWatchdog:
    """Notify systemd only while fresh RTL samples are reaching Python."""

    def __init__(self) -> None:
        self.address = os.environ.get("NOTIFY_SOCKET", "")
        try:
            watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        except ValueError:
            watchdog_usec = 0
        # Send at one third of the deadline, but no more than once a second.
        self.interval = max(1.0, watchdog_usec / 3_000_000) if watchdog_usec else 0.0
        self.next_ping = 0.0

    def _send(self, message: str) -> None:
        if not self.address:
            return
        address = (
            "\0" + self.address[1:]
            if self.address.startswith("@")
            else self.address
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                notify.sendto(message.encode(), address)
        except OSError:
            # Capture must keep working outside systemd or if notify races a
            # service restart. A missing ping will still trigger supervision.
            pass

    def ready(self) -> None:
        self._send("READY=1\nSTATUS=RTL capture receiving samples")
        self.next_ping = time.monotonic() + self.interval

    def samples_received(self) -> None:
        if not self.interval:
            return
        now = time.monotonic()
        if now >= self.next_ping:
            self._send("WATCHDOG=1\nSTATUS=RTL capture receiving samples")
            self.next_ping = now + self.interval


def snap_rtl_gain_db(value: float) -> float:
    return min(_RTL_GAIN_STEPS, key=lambda step: abs(step - float(value)))


class _AutoGain:
    """Step the tuner with the path, one R820T click at a time.

    High SNR (driveway) eases gain down after the burst, all the way to
    0 dB if the copies stay that loud. A quiet band or a weak copy climbs
    back so the next shout from down the road is not deaf. Hardware AGC
    stays off: it would pump inside a nine-second frame and corrupt the
    decoder's per-symbol energies. This controller never moves while a
    burst is open.
    """

    def __init__(
        self,
        initial_db: float,
        *,
        max_db: float = 42.1,
        chunk_seconds: float = 0.109,
        settle_seconds: float = 1.5,
        # Climb while the quiet floor has ADC headroom; drop only when it
        # is actually hot. The gap is hysteresis so a car whip does not
        # yo-yo between two steps on noise alone.
        floor_low: float = 0.10,
        floor_high: float = 0.25,
        climb_hold_seconds: float = 12.0,
    ) -> None:
        allowed = [g for g in _RTL_GAIN_STEPS if g <= max_db + 1e-6]
        if not allowed:
            allowed = [0.0]
        self.steps = tuple(allowed)
        self.floor_low = floor_low
        self.floor_high = floor_high
        self.index = min(
            range(len(self.steps)), key=lambda i: abs(self.steps[i] - initial_db)
        )
        self.clip_runs = 0
        self.last_floor: float | None = None
        self.last_reason = ""
        self.last_snr: float | None = None
        self.snr_backoff = False
        self.snr_need_gain = False
        self.window = max(3, int(round(settle_seconds / chunk_seconds)))
        self.quiet_rms: deque[float] = deque(maxlen=self.window)
        self.climb_hold_chunks = max(
            1, int(round(climb_hold_seconds / chunk_seconds))
        )
        self.climb_hold = 0
        self.last_clip_frac = 0.0
        self.last_rms = 0.0

    @property
    def gain_db(self) -> float:
        return self.steps[self.index]

    @property
    def clipping(self) -> bool:
        return (
            self.last_clip_frac >= _SUSTAINED_CLIP_FRACTION or self.last_rms >= 0.55
        )

    def note_snr(self, snr_db: float) -> None:
        """After a finished on-frequency burst: ease off if it was huge."""
        self.last_snr = float(snr_db)
        if snr_db >= _SNR_BACKOFF_DB:
            self.snr_backoff = True
            self.snr_need_gain = False
        elif snr_db < _SNR_MARGINAL_DB:
            self.snr_need_gain = True
            self.snr_backoff = False

    def _step(self, delta: int, *, reason: str = "") -> float | None:
        target = self.index + delta
        if not 0 <= target < len(self.steps) or target == self.index:
            self.snr_backoff = False
            self.snr_need_gain = False
            return None
        self.index = target
        self.quiet_rms.clear()
        self.last_reason = reason
        self.snr_backoff = False
        self.snr_need_gain = False
        if delta < 0:
            self.climb_hold = self.climb_hold_chunks
        return self.gain_db

    def force(self, gain_db: float) -> float:
        snapped = min(self.steps, key=lambda step: abs(step - float(gain_db)))
        self.index = self.steps.index(snapped)
        self.quiet_rms.clear()
        self.snr_backoff = False
        self.snr_need_gain = False
        self.climb_hold = 0
        self.clip_runs = 0
        self.last_reason = "override"
        return self.gain_db

    def update(
        self, iq: np.ndarray, *, signal_active: bool, move: bool = True
    ) -> float | None:
        x = np.asarray(iq)
        if not len(x):
            return None
        clipped = float(np.mean((np.abs(x.real) >= 0.98) | (np.abs(x.imag) >= 0.98)))
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
        self.last_clip_frac = clipped
        self.last_rms = rms
        if not move:
            return None
        if clipped >= _SUSTAINED_CLIP_FRACTION or rms >= 0.55:
            # Never move in the middle of a frame. The first loud chunk
            # arrives before the detector latches; three quiet-period clip
            # runs are required before a step, which a real frame never
            # reaches. After a down step, climb_hold blocks another drop so
            # a driveway copy cannot walk 42 → 0 in one second.
            if signal_active:
                self.clip_runs = 0
                return None
            if self.climb_hold:
                self.climb_hold -= 1
                self.clip_runs = 0
                return None
            self.clip_runs += 1
            if self.clip_runs >= _CLIP_RUNS_BEFORE_RETREAT:
                self.clip_runs = 0
                return self._step(-1, reason="overload")
            return None
        self.clip_runs = 0
        if signal_active:
            return None
        if self.snr_backoff:
            snr = self.last_snr if self.last_snr is not None else 0.0
            return self._step(-1, reason=f"snr {snr:.0f} dB")
        if self.climb_hold:
            self.climb_hold -= 1
        if self.snr_need_gain and self.climb_hold <= 0:
            snr = self.last_snr if self.last_snr is not None else 0.0
            return self._step(1, reason=f"snr {snr:.0f} dB")
        self.quiet_rms.append(rms)
        if len(self.quiet_rms) < self.window:
            return None
        floor = float(np.median(self.quiet_rms))
        self.last_floor = floor
        if floor > self.floor_high:
            return self._step(-1, reason=f"quiet floor {floor:.3f}")
        if floor < self.floor_low and self.climb_hold <= 0:
            return self._step(1, reason=f"quiet floor {floor:.3f}")
        return None


def snr_quality(peak_db: float) -> str:
    """One word for the tail: too-weak / marginal / usable / strong."""
    if peak_db < _SNR_TOO_WEAK_DB:
        return "too-weak"
    if peak_db < _SNR_MARGINAL_DB:
        return "marginal"
    if peak_db < _SNR_STRONG_DB:
        return "usable"
    return "strong"


def looks_like_test_tone(tone_hz: float, held_s: float) -> bool:
    return abs(abs(tone_hz) - _TEST_TONE_HZ) < 300 and held_s >= 2.5


def burst_plain_english(peak_db: float, tone_hz: float, held_s: float) -> str:
    """What a human should do with this burst. SNR first, then usable or not."""
    quality = snr_quality(peak_db)
    snr = f"SNR {peak_db:.0f} dB {quality.upper()}"
    if peak_db < _SNR_TOO_WEAK_DB:
        return f"{snr} — ignore (noise or too far)"
    if looks_like_test_tone(tone_hz, held_s):
        return (
            f"{snr} — test tone: Hermes is heard, but this is not a Columba message"
        )
    if quality == "marginal":
        return f"{snr} — likely a data frame; short text might decode"
    return f"{snr} — likely a data frame"


def decode_plain_english(reason: str, *, tone_hz: float, held_s: float) -> str:
    if looks_like_test_tone(tone_hz, held_s) and "sync" in reason.lower():
        return (
            "not a message (test tone has no sync). "
            "The SNR line above is what matters."
        )
    if "sync" in reason.lower():
        return "not usable: heard energy but no frame (noise, or too weak to lock)"
    if "crc" in reason.lower():
        return "not usable: heard a frame but bits were flipped (CRC fail)"
    return f"not usable: {reason}"

# Inside 97.221(b) 10 m auto-control segment, not on the band edge.
DEFAULT_FREQ = 28_124_000
RTL_RATE = 1_200_000
IF_OFFSET = 20_000


def _self_test() -> int:
    dest = bytes(range(16))
    frame = Frame(origin="N0CALL", dest=dest, payload=b"hello hf", msg_id=7)
    iq = modulate(frame, fec=True)
    pad = np.zeros(1800, dtype=np.complex64)
    noise = (
        np.random.default_rng(0).normal(0, 0.05, len(iq) + len(pad))
        + 1j * np.random.default_rng(1).normal(0, 0.05, len(iq) + len(pad))
    )
    decoded = demodulate(np.concatenate([pad, iq]) + noise.astype(np.complex64))
    if decoded.payload != frame.payload or decoded.origin != "N0CALL":
        print("self-test failed", decoded, file=sys.stderr)
        return 1
    print("self-test ok, decoded frame:")
    for line in decoded.describe():
        print(f"  {line}")
    return 0


def _power_dbfs(iq: np.ndarray) -> float:
    p = float(np.mean(np.abs(iq) ** 2))
    return 10.0 * np.log10(p + 1e-12)


def _narrowband_dbfs(
    iq: np.ndarray,
    sample_rate: float,
    half_bw_hz: float = _SIGNAL_HALF_BW_HZ,
) -> float:
    """Power in just the bins the FSK tones occupy, as dBFS.

    Same units as _power_dbfs so the detector's floor history still works,
    but a distant frame is no longer averaged away by 6 kHz of empty noise.
    """
    if len(iq) < 256:
        return _power_dbfs(iq)
    window = np.hanning(len(iq))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(iq * window))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1.0 / sample_rate))
    band = np.abs(freqs) <= half_bw_hz
    if not band.any():
        return _power_dbfs(iq)
    # Normalise out the window and transform so this reads like mean power.
    scale = len(iq) * float(np.mean(window**2))
    return 10.0 * np.log10(float(spectrum[band].sum()) / scale + 1e-12)


def _peak_tone(iq: np.ndarray, sample_rate: float) -> tuple[float, float]:
    """Return the strongest tone as (offset_hz, dB above the median bin)."""
    if len(iq) < 256:
        return 0.0, 0.0
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(iq * np.hanning(len(iq))))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1.0 / sample_rate))
    peak = int(np.argmax(spectrum))
    floor = float(np.median(spectrum)) + 1e-20
    return float(freqs[peak]), 10.0 * np.log10((spectrum[peak] + 1e-20) / floor)


class _Detector:
    """Call out channel power excursions above the running noise floor.

    The floor is a median over a long history so that a burst lasting a few
    seconds cannot drag the reference up behind itself.
    """

    def __init__(
        self,
        chunk_seconds: float,
        history: int = 200,
        margin_db: float = 6.0,
        relatch_seconds: float = _RELATCH_SECONDS,
        hang_seconds: float = _BURST_HANG_SECONDS,
    ):
        self.chunk_seconds = chunk_seconds
        self.history: deque[float] = deque(maxlen=history)
        self.margin_db = margin_db
        self.relatch_seconds = relatch_seconds
        self.hang_chunks = max(1, int(round(hang_seconds / chunk_seconds)))
        self.active = False
        self.chunks = 0
        self.below = 0
        self.best_db = 0.0
        self.tones: list[float] = []
        self.last_held_s = 0.0
        self.last_tone_hz = 0.0
        self.last_peak_db = 0.0

    @property
    def floor_db(self) -> float | None:
        if len(self.history) < 20:
            return None
        return float(np.median(self.history))

    def _finish(self, note: str) -> str:
        held = self.chunks * self.chunk_seconds
        middle = float(np.median(self.tones)) if self.tones else 0.0
        best = self.best_db
        self.last_held_s = held
        self.last_tone_hz = middle
        self.last_peak_db = best
        self.active = False
        self.chunks = 0
        self.below = 0
        self.best_db = 0.0
        self.tones = []
        explain = burst_plain_english(best, middle, held)
        return (
            f"<<< {note} after {held:.1f} s, peak {best:.1f} dB over floor, "
            f"median tone {middle:+.0f} Hz — {explain}"
        )

    def update(self, chan_db: float, tone_hz: float) -> str | None:
        if len(self.history) < 20:
            self.history.append(chan_db)
            return None

        over = chan_db - float(np.median(self.history))
        if over <= self.margin_db:
            if not self.active:
                # A weak approaching frame is still "quiet" to the gate, but
                # mixing it into the median raises the floor and hides the
                # rest of the shout.
                if over <= 0.0:
                    self.history.append(chan_db)
                return None
            self.below += 1
            self.chunks += 1
            self.tones.append(tone_hz)
            if (
                self.below >= self.hang_chunks
                or self.chunks * self.chunk_seconds > self.relatch_seconds
            ):
                return self._finish("signal gone")
            return None

        self.below = 0
        self.chunks += 1
        self.best_db = max(self.best_db, over)
        self.tones.append(tone_hz)
        if self.chunks * self.chunk_seconds > self.relatch_seconds:
            # Something is parked on the channel. Take it as the new floor
            # rather than reporting it forever.
            note = self._finish("signal still up, re-baselining")
            self.history.clear()
            self.history.extend([chan_db] * 20)
            return note
        if self.active:
            return None
        self.active = True
        quality = snr_quality(over)
        return (
            f">>> SIGNAL {over:.1f} dB over floor at {tone_hz:+.0f} Hz — "
            f"{quality.upper()} (burst started)"
        )


class _Mixer:
    """Shift a fixed IF offset down to zero with phase continuous across reads."""

    def __init__(self, offset_hz: int, sample_rate: int) -> None:
        self.offset_hz = offset_hz
        self.sample_rate = sample_rate
        self.n = 0
        # Wrapping the sample counter at the exact tone period keeps the
        # phase exact no matter how long we listen.
        step = math.gcd(abs(offset_hz), sample_rate) or 1
        self.period = sample_rate // step

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        if self.offset_hz == 0:
            return iq
        idx = self.n + np.arange(len(iq), dtype=np.float64)
        phase = -2.0 * np.pi * self.offset_hz * idx / self.sample_rate
        self.n = int((self.n + len(iq)) % self.period)
        return (iq * np.exp(1j * phase)).astype(np.complex64)


@dataclass(frozen=True)
class _DecodeJob:
    iq: np.ndarray
    dump_frame: bool
    peak_db: float
    tone_hz: float
    held_s: float
    gain_db: float | None
    queued_at: float = 0.0


class BurstDecoder:
    """Decode bursts off the capture thread.

    The listener keeps reading the dongle. This worker may lag; it does not
    stall USB. Short noise bursts are rejected before enqueue. If the bounded
    queue fills, the new job is dropped so older likely-real frames retain
    their place.
    """

    def __init__(
        self,
        *,
        on_frame: Callable[[Frame], None] | None = None,
        on_decode_failed: Callable[[str], None] | None = None,
        on_decode_event: Callable[[DecodeEvent], None] | None = None,
        max_pending: int = 32,
    ) -> None:
        self._on_frame = on_frame
        self._on_decode_failed = on_decode_failed
        self._on_decode_event = on_decode_event
        self._queue: queue.Queue[_DecodeJob | None] = queue.Queue(maxsize=max_pending)
        self._pending = 0
        self._dropped = 0
        self._stale = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="hf-decode", daemon=True
        )
        self._thread.start()

    def submit(
        self,
        iq: np.ndarray,
        *,
        dump_frame: bool,
        peak_db: float,
        tone_hz: float,
        held_s: float,
        gain_db: float | None = None,
    ) -> str:
        if held_s < _MIN_DECODE_BURST_SECONDS:
            print(
                f"{time.strftime('%H:%M:%S')} ignored {held_s:.2f} s burst "
                f"before decode (minimum {_MIN_DECODE_BURST_SECONDS:.1f} s)",
                flush=True,
            )
            return "too_short"
        if held_s > _MAX_DECODE_BURST_SECONDS:
            print(
                f"{time.strftime('%H:%M:%S')} ignored {held_s:.2f} s burst "
                f"before decode (maximum {_MAX_DECODE_BURST_SECONDS:.1f} s; "
                f"noise or a stuck carrier)",
                flush=True,
            )
            return "too_long"
        if abs(tone_hz) > _MAX_TONE_OFFSET_HZ:
            print(
                f"{time.strftime('%H:%M:%S')} ignored burst at {tone_hz:+.0f} Hz "
                f"before decode (frames land within "
                f"{_MAX_TONE_OFFSET_HZ:.0f} Hz of centre; this is noise)",
                flush=True,
            )
            return "off_frequency"
        job = _DecodeJob(
            iq=np.asarray(iq, dtype=np.complex64).copy(),
            dump_frame=dump_frame,
            peak_db=peak_db,
            tone_hz=tone_hz,
            held_s=held_s,
            gain_db=gain_db,
            queued_at=time.monotonic(),
        )
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self._dropped += 1
            print(
                f"{time.strftime('%H:%M:%S')} decode queue full; "
                f"dropped new burst ({self._dropped} dropped, "
                f"{self._queue.qsize()} waiting)",
                flush=True,
            )
            return "queue_full"
        with self._lock:
            self._pending += 1
            waiting = self._pending
        print(
            f"{time.strftime('%H:%M:%S')} queued {len(job.iq) / DEFAULT_CHANNEL_RATE:.2f} s "
            f"burst for decode ({waiting} waiting)",
            flush=True,
        )
        return "queued"

    def close(self, timeout: float = 300.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        if self._dropped:
            print(
                f"decode worker finished; dropped {self._dropped} burst(s) "
                f"that never decoded",
                flush=True,
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            age = time.monotonic() - item.queued_at
            if item.queued_at and age > _MAX_JOB_AGE_SECONDS:
                self._stale += 1
                print(
                    f"{time.strftime('%H:%M:%S')} skipped a burst that waited "
                    f"{age:.0f} s for the decoder ({self._stale} stale); "
                    f"a late answer would be misleading",
                    flush=True,
                )
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                continue
            try:
                _try_decode(
                    item.iq,
                    on_frame=self._on_frame,
                    dump_frame=item.dump_frame,
                    on_decode_failed=self._on_decode_failed,
                    peak_db=item.peak_db,
                    tone_hz=item.tone_hz,
                    held_s=item.held_s,
                    gain_db=item.gain_db,
                    on_decode_event=self._on_decode_event,
                )
            finally:
                with self._lock:
                    self._pending = max(0, self._pending - 1)


def _iq_for_decode(iq: np.ndarray, held_s: float) -> np.ndarray:
    """Drop stale preroll; keep hang and a fat pad before the detector trip."""
    keep = int(
        round(
            (max(float(held_s), 0.0) + _PREROLL_DECODE_SECONDS)
            * DEFAULT_CHANNEL_RATE
        )
    )
    x = np.asarray(iq, dtype=np.complex64)
    if keep <= 0 or len(x) <= keep:
        return x
    return x[-keep:]


def _lock_missed(reason: str) -> bool:
    text = reason.lower()
    return "not found" in text or "sync" in text


def _demodulate_with_preroll_trim(iq: np.ndarray, held_s: float) -> Frame:
    work = _iq_for_decode(iq, held_s)
    try:
        return demodulate(work)
    except ValueError as exc:
        if len(work) < len(iq) and _lock_missed(str(exc)):
            return demodulate(iq)
        raise


def _save_failed_burst(iq: np.ndarray) -> Path | None:
    """Keep IQ only when decode fails; successes used to stall the Pi on disk."""
    try:
        _CAPTURE_DIR.mkdir(exist_ok=True)
        path = _CAPTURE_DIR / f"burst-{time.strftime('%H%M%S')}.npz"
        np.savez(path, iq=iq, sample_rate=np.float64(DEFAULT_CHANNEL_RATE))
    except OSError:
        return None
    return path


def _try_decode(
    iq: np.ndarray,
    on_frame: Callable[[Frame], None] | None = None,
    *,
    dump_frame: bool = True,
    on_decode_failed: Callable[[str], None] | None = None,
    peak_db: float = 0.0,
    tone_hz: float = 0.0,
    held_s: float = 0.0,
    gain_db: float | None = None,
    on_decode_event: Callable[[DecodeEvent], None] | None = None,
) -> Frame | None:
    """Decode one detected burst and say why it failed if it failed."""
    seconds = len(iq) / DEFAULT_CHANNEL_RATE
    try:
        frame = _demodulate_with_preroll_trim(iq, held_s)
    except ValueError as exc:
        saved = _save_failed_burst(iq)
        extra = f"; saved {saved.name}" if saved is not None else ""
        explain = decode_plain_english(str(exc), tone_hz=tone_hz, held_s=held_s)
        print(
            f"{time.strftime('%H:%M:%S')} {explain} "
            f"({exc}{extra})",
            flush=True,
        )
        print(
            f"bench-rx fail snr={peak_db:.1f} reason={exc}",
            flush=True,
        )
        if on_decode_failed is not None:
            on_decode_failed(str(exc))
        if on_decode_event is not None:
            on_decode_event(
                DecodeEvent(
                    msg_id=None,
                    success=False,
                    reason=str(exc),
                    snr_db=peak_db,
                    gain_db=gain_db,
                    tone_hz=tone_hz,
                    held_s=held_s,
                )
            )
        return None
    print(
        f"bench-rx ok msg_id={frame.msg_id} bytes={len(frame.payload)} "
        f"snr={peak_db:.1f} origin={frame.origin}",
        flush=True,
    )
    if dump_frame:
        print(f"\nDECODED at {time.strftime('%H:%M:%S')} from {seconds:.2f} s")
        for line in frame.describe():
            print(f"  {line}")
        print(f"  raw  {frame.encode().hex(' ')}\n", flush=True)
    else:
        print(
            f"{time.strftime('%H:%M:%S')} USABLE decoded {frame.origin} "
            f"msg_id={frame.msg_id} {len(frame.payload)}B "
            f"snr={peak_db:.1f}dB — this can reach Columba",
            flush=True,
        )
    if on_decode_event is not None:
        on_decode_event(
            DecodeEvent(
                msg_id=None if frame.version == VERSION_LIGHT else frame.msg_id,
                success=True,
                reason="decoded",
                snr_db=peak_db,
                gain_db=gain_db,
                text=frame.payload.decode("utf-8", "replace")[:200],
                tone_hz=tone_hz,
                held_s=held_s,
            )
        )
    if on_frame is not None:
        on_frame(frame)
    return frame


def _listen(
    args: argparse.Namespace,
    on_frame: Callable[[Frame], None] | None = None,
    on_decode_failed: Callable[[str], None] | None = None,
    on_decode_event: Callable[[DecodeEvent], None] | None = None,
    channel_status: ChannelStatus | None = None,
    gain_override: Callable[[], float | None] | None = None,
) -> int:
    from hfbridge.rtl import RtlError, RtlSdr

    # Tune below the signal and mix back in software. Sitting the FSK tones on
    # the dongle's centre hides them under its own LO leakage spike.
    tune_freq = args.freq - args.offset_hz
    sdr = RtlSdr(index=args.device)
    sdr.configure(
        center_freq=tune_freq,
        sample_rate=args.rtl_rate,
        gain_db=None if args.agc else args.gain,
        direct_sampling=args.direct_sampling,
    )
    mixer = _Mixer(args.offset_hz, args.rtl_rate)
    channelizer = Channelizer(args.rtl_rate, DEFAULT_CHANNEL_RATE)
    print(
        f"listening for {args.freq / 1e6:.6f} MHz, dongle tuned "
        f"{sdr.center_freq / 1e6:.6f} MHz (IF offset {args.offset_hz} Hz)\n"
        f"{args.rtl_rate} S/s into a {DEFAULT_CHANNEL_RATE:g} Hz channel, "
        f"gain={'agc' if args.agc else args.gain}, "
        f"margin={args.margin_db:g} dB, "
        f"direct_sampling={args.direct_sampling}",
        flush=True,
    )
    # Drop the first buffer; the tuner often needs a moment.
    settle = 131072
    sdr.read_samples(settle)
    preroll_need = int(DEFAULT_CHANNEL_RATE * _PREROLL_SECONDS)
    preroll: deque[np.ndarray] = deque()
    preroll_len = 0
    burst: list[np.ndarray] | None = None
    chunk_samples = 131072
    detector = _Detector(
        chunk_seconds=chunk_samples / args.rtl_rate,
        margin_db=args.margin_db,
    )
    auto_gain = (
        _AutoGain(
            args.gain,
            max_db=args.max_gain,
            chunk_seconds=chunk_samples / args.rtl_rate,
        )
        if args.auto_gain and not args.agc
        else None
    )
    if auto_gain is not None:
        sdr.set_gain(auto_gain.gain_db)
        print(
            f"adaptive gain starts at {auto_gain.gain_db:g} dB, max "
            f"{args.max_gain:g} dB; it will settle wherever this antenna's "
            f"noise floor needs",
            flush=True,
        )
    decoder = BurstDecoder(
        on_frame=on_frame,
        on_decode_failed=on_decode_failed,
        on_decode_event=on_decode_event,
    )
    if channel_status is not None and auto_gain is not None:
        channel_status.update(gain_db=auto_gain.gain_db)
    print("capture thread owns the RTL; decode runs on a worker queue", flush=True)
    watchdog = _SystemdWatchdog()
    watchdog.ready()
    next_status = 0.0
    override_latched = False
    consecutive_recover_failures = 0
    try:
        t0 = time.time()
        while True:
            try:
                chunk = sdr.read_samples(chunk_samples)
            except RtlError as exc:
                print(
                    f"{time.strftime('%H:%M:%S')} RTL stall ({exc}); "
                    f"overruns={sdr.overruns} — restarting capture",
                    flush=True,
                )
                burst = None
                preroll.clear()
                preroll_len = 0
                try:
                    sdr.recover()
                    sdr.read_samples(settle)
                    consecutive_recover_failures = 0
                except RtlError as recover_exc:
                    consecutive_recover_failures += 1
                    print(
                        f"{time.strftime('%H:%M:%S')} RTL recover failed "
                        f"({recover_exc}); attempt "
                        f"{consecutive_recover_failures}/3",
                        flush=True,
                    )
                    if consecutive_recover_failures >= 3:
                        raise RtlError(
                            "RTL-SDR recovery failed 3 consecutive times; "
                            "exiting for service restart"
                        ) from recover_exc
                    time.sleep(1.0)
                continue
            consecutive_recover_failures = 0
            watchdog.samples_received()
            if auto_gain is not None:
                old_gain = auto_gain.gain_db
                pinned = gain_override() if gain_override is not None else None
                if pinned is not None:
                    target = min(
                        auto_gain.steps, key=lambda step: abs(step - float(pinned))
                    )
                    # Measure clip/ADC for the phone, but a pin is a lock.
                    auto_gain.update(
                        chunk, signal_active=detector.active, move=False
                    )
                    if not detector.active and target != old_gain:
                        forced = auto_gain.force(target)
                        sdr.set_gain(forced)
                        print(
                            f"{time.strftime('%H:%M:%S')} RTL gain override: "
                            f"{old_gain:g} → {forced:g} dB",
                            flush=True,
                        )
                        if channel_status is not None:
                            channel_status.update(gain_db=forced)
                    override_latched = True
                    new_gain = None
                else:
                    if override_latched:
                        print(
                            f"{time.strftime('%H:%M:%S')} RTL gain auto "
                            f"(holding {old_gain:g} dB)",
                            flush=True,
                        )
                        override_latched = False
                    new_gain = auto_gain.update(chunk, signal_active=detector.active)
                if new_gain is not None:
                    sdr.set_gain(new_gain)
                    direction = "down" if new_gain < old_gain else "up"
                    extra = (
                        f" ({auto_gain.last_reason})"
                        if auto_gain.last_reason
                        else ""
                    )
                    print(
                        f"{time.strftime('%H:%M:%S')} RTL auto-gain {direction}: "
                        f"{old_gain:g} → {new_gain:g} dB{extra}",
                        flush=True,
                    )
                    if channel_status is not None:
                        channel_status.update(gain_db=new_gain)
                    # An upward quiet-band gain step changes the measured
                    # floor. Re-warm it instead of reporting a false burst.
                    if new_gain > old_gain and not detector.active:
                        detector.history.clear()
                        preroll.clear()
                        preroll_len = 0
            ch = channelizer(mixer(chunk))
            tone_hz, tone_db = _peak_tone(ch, DEFAULT_CHANNEL_RATE)
            chan_db = _power_dbfs(ch)
            gate_db = _narrowband_dbfs(ch, DEFAULT_CHANNEL_RATE)
            if not args.quiet:
                print(
                    f"{time.strftime('%H:%M:%S')} rx {args.freq / 1e6:.6f} MHz "
                    f"wide={_power_dbfs(chunk):6.1f} chan={chan_db:6.1f} "
                    f"nb={gate_db:6.1f} dBFS "
                    f"peak={(args.freq + tone_hz) / 1e6:.6f} MHz "
                    f"({tone_hz:+5.0f} Hz) {tone_db:5.1f} dB",
                    flush=True,
                )
            event = detector.update(gate_db, tone_hz)
            if event:
                print(f"{time.strftime('%H:%M:%S')} {event}", flush=True)
            if channel_status is not None:
                now_status = time.monotonic()
                if now_status >= next_status:
                    gain_now = (
                        None
                        if args.agc
                        else auto_gain.gain_db
                        if auto_gain is not None
                        else args.gain
                    )
                    floor = detector.floor_db
                    snr = None if floor is None else gate_db - floor
                    clip_fields: dict = {}
                    if auto_gain is not None:
                        clip_fields = {
                            "clip_frac": round(auto_gain.last_clip_frac, 4),
                            "adc_rms": round(auto_gain.last_rms, 3),
                            "clipping": bool(auto_gain.clipping),
                        }
                    channel_status.update(
                        gain_db=gain_now,
                        snr_db=None if snr is None else round(float(snr), 1),
                        tone_hz=round(float(tone_hz), 0),
                        bursting=bool(detector.active),
                        **clip_fields,
                    )
                    next_status = now_status + 1.0
            if event and event.startswith(">>>"):
                held = [np.concatenate(list(preroll))] if preroll else []
                burst = held + [ch]
            elif burst is not None:
                burst.append(ch)
            else:
                preroll.append(ch)
                preroll_len += len(ch)
                while preroll and preroll_len - len(preroll[0]) >= preroll_need:
                    preroll_len -= len(preroll.popleft())
            if event and event.startswith("<<<") and burst is not None:
                captured = np.concatenate(burst)
                burst = None
                preroll.clear()
                preroll_len = 0
                gain_db = (
                    None
                    if args.agc
                    else auto_gain.gain_db
                    if auto_gain is not None
                    else args.gain
                )
                if auto_gain is not None and abs(detector.last_tone_hz) <= _MAX_TONE_OFFSET_HZ:
                    auto_gain.note_snr(detector.last_peak_db)
                fate = decoder.submit(
                    captured,
                    dump_frame=not args.quiet,
                    peak_db=detector.last_peak_db,
                    tone_hz=detector.last_tone_hz,
                    held_s=detector.last_held_s,
                    gain_db=gain_db,
                )
                if channel_status is not None:
                    channel_status.note_burst(
                        snr_db=detector.last_peak_db,
                        tone_hz=detector.last_tone_hz,
                        held_s=detector.last_held_s,
                        fate=fate,
                        gain_db=gain_db,
                    )
            if args.seconds and (time.time() - t0) >= args.seconds:
                print("done", flush=True)
                return 0
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0
    finally:
        decoder.close()
        if getattr(sdr, "overruns", 0):
            print(f"RTL USB overruns: {sdr.overruns}", flush=True)
        sdr.close()


def main(
    argv: list[str] | None = None,
    on_frame: Callable[[Frame], None] | None = None,
    on_decode_failed: Callable[[str], None] | None = None,
    on_decode_event: Callable[[DecodeEvent], None] | None = None,
    channel_status: ChannelStatus | None = None,
    gain_override: Callable[[], float | None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="decode a generated packet, no radio")
    parser.add_argument("--freq", type=int, default=DEFAULT_FREQ, help="RTL center frequency, Hz")
    parser.add_argument("--rtl-rate", type=int, default=RTL_RATE, help="RTL sample rate")
    parser.add_argument(
        "--offset-hz",
        type=int,
        default=IF_OFFSET,
        help="tune this far below --freq and mix back, to dodge the DC spike",
    )
    parser.add_argument("--gain", type=float, default=20.7, help="tuner gain in dB")
    parser.add_argument(
        "--auto-gain",
        action="store_true",
        default=True,
        help="hold the quiet noise floor in range so any antenna works (default)",
    )
    parser.add_argument(
        "--fixed-gain",
        dest="auto_gain",
        action="store_false",
        help="pin the tuner at --gain instead of adapting to the antenna",
    )
    parser.add_argument(
        "--max-gain",
        type=float,
        default=42.1,
        help="maximum tuner gain the adaptive controller may use",
    )
    parser.add_argument(
        "--margin-db",
        type=float,
        default=6.0,
        help="dB over the quiet floor to start a burst (default 6; use ~2 for weak skip)",
    )
    parser.add_argument("--agc", action="store_true", help="enable RTL AGC")
    parser.add_argument("--direct-sampling", type=int, default=0, choices=(0, 1, 2))
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print signal detections and decodes, not every chunk",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    return _listen(
        args,
        on_frame=on_frame,
        on_decode_failed=on_decode_failed,
        on_decode_event=on_decode_event,
        channel_status=channel_status,
        gain_override=gain_override,
    )


if __name__ == "__main__":
    raise SystemExit(main())
