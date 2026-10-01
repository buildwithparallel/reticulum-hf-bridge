"""Minimal librtlsdr wrapper for the in-tree osmocom build.

`rtlsdr_read_sync` drops samples between calls — the bench capture had a
phase jump every 131072 input samples, which is exactly one sync read.
Streaming uses the async USB callback so the IQ is gapless.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import sys
import threading
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32, ctypes.c_void_p
)
# 256 KiB is librtlsdr's usual default and a multiple of 512.
_ASYNC_BYTES = 262144


def default_librtlsdr_path() -> Path | str:
    """Prefer the in-tree build, then a system librtlsdr (Linux .so or macOS .dylib)."""
    env = os.environ.get("HFBRIDGE_RTLSDR_LIB")
    if env:
        return Path(env)
    prefix = _REPO / "third_party" / "prefix" / "lib"
    for name in ("librtlsdr.dylib", "librtlsdr.so", "librtlsdr.so.0"):
        candidate = prefix / name
        if candidate.is_file():
            return candidate
    found = ctypes.util.find_library("rtlsdr")
    if found:
        return found
    return prefix / ("librtlsdr.dylib" if sys.platform == "darwin" else "librtlsdr.so")


class RtlError(RuntimeError):
    pass


def _iq_from_bytes(raw: np.ndarray) -> np.ndarray:
    iq = raw.astype(np.float32).reshape(-1, 2)
    return ((iq[:, 0] - 127.5) + 1j * (iq[:, 1] - 127.5)) / 127.5


class RtlSdr:
    def __init__(self, index: int = 0, lib_path: Path | None = None):
        self._lib = ctypes.CDLL(str(lib_path or default_librtlsdr_path()))
        self._setup_prototypes()
        self._dev = ctypes.c_void_p()
        err = self._lib.rtlsdr_open(ctypes.byref(self._dev), index)
        if err:
            raise RtlError(f"rtlsdr_open failed: {err}")
        self._index = index
        self._lib_path = lib_path
        self._cfg: dict | None = None
        # ~5.7 s of 256 KiB USB buffers. Decode can hold the GIL; extra
        # depth keeps librtlsdr from stalling the pipe.
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._leftover = np.zeros(0, dtype=np.complex64)
        self._thread: threading.Thread | None = None
        self._callback = _CALLBACK(self._on_rx)
        self.overruns = 0

    def _setup_prototypes(self) -> None:
        lib = self._lib
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        lib.rtlsdr_open.restype = ctypes.c_int
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_close.restype = ctypes.c_int
        lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
        lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_center_freq.restype = ctypes.c_int
        lib.rtlsdr_get_center_freq.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_get_center_freq.restype = ctypes.c_uint32
        lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
        lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
        lib.rtlsdr_set_agc_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_agc_mode.restype = ctypes.c_int
        lib.rtlsdr_set_direct_sampling.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_direct_sampling.restype = ctypes.c_int
        lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_reset_buffer.restype = ctypes.c_int
        lib.rtlsdr_read_async.argtypes = [
            ctypes.c_void_p,
            _CALLBACK,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        lib.rtlsdr_read_async.restype = ctypes.c_int
        lib.rtlsdr_cancel_async.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_cancel_async.restype = ctypes.c_int

    def configure(
        self,
        *,
        center_freq: int,
        sample_rate: int,
        gain_db: float | None = 20.7,
        direct_sampling: int = 0,
    ) -> None:
        if direct_sampling:
            err = self._lib.rtlsdr_set_direct_sampling(self._dev, direct_sampling)
            if err:
                raise RtlError(f"set_direct_sampling failed: {err}")
        err = self._lib.rtlsdr_set_sample_rate(self._dev, sample_rate)
        if err:
            raise RtlError(f"set_sample_rate failed: {err}")
        err = self._lib.rtlsdr_set_center_freq(self._dev, center_freq)
        if err:
            raise RtlError(f"set_center_freq failed: {err}")
        if gain_db is None:
            self._lib.rtlsdr_set_agc_mode(self._dev, 1)
        else:
            self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 1)
            tenths = int(round(gain_db * 10))
            err = self._lib.rtlsdr_set_tuner_gain(self._dev, tenths)
            if err:
                raise RtlError(f"set_tuner_gain failed: {err}")
        err = self._lib.rtlsdr_reset_buffer(self._dev)
        if err:
            raise RtlError(f"reset_buffer failed: {err}")
        self._cfg = {
            "center_freq": center_freq,
            "sample_rate": sample_rate,
            "gain_db": gain_db,
            "direct_sampling": direct_sampling,
        }
        self._start_async()

    def _on_rx(self, buf, length: int, _ctx) -> None:
        raw = np.ctypeslib.as_array(buf, shape=(length,)).copy()
        try:
            self._queue.put_nowait(_iq_from_bytes(raw).astype(np.complex64))
        except queue.Full:
            self.overruns += 1

    def _async_loop(self) -> None:
        # Four USB buffers, each 256 KiB. The call blocks until cancel_async.
        self._lib.rtlsdr_read_async(
            self._dev, self._callback, None, 4, _ASYNC_BYTES
        )

    def _start_async(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._async_loop, daemon=True)
        self._thread.start()

    @property
    def center_freq(self) -> int:
        return int(self._lib.rtlsdr_get_center_freq(self._dev))

    def set_gain(self, gain_db: float) -> None:
        """Change manual tuner gain while async capture is running."""
        self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 1)
        err = self._lib.rtlsdr_set_tuner_gain(
            self._dev, int(round(float(gain_db) * 10))
        )
        if err:
            raise RtlError(f"set_tuner_gain failed: {err}")
        if self._cfg is not None:
            self._cfg["gain_db"] = float(gain_db)

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._leftover = np.zeros(0, dtype=np.complex64)

    def _stop_async(self) -> None:
        if self._thread is None:
            return
        if self._dev:
            self._lib.rtlsdr_cancel_async(self._dev)
        self._thread.join(timeout=2.0)
        self._thread = None

    def restart_stream(self) -> None:
        """Cancel USB streaming, drop stale IQ, and start async again."""
        self._stop_async()
        self._drain()
        if not self._dev:
            raise RtlError("restart_stream: dongle is closed")
        err = self._lib.rtlsdr_reset_buffer(self._dev)
        if err:
            raise RtlError(f"reset_buffer failed: {err}")
        self._start_async()

    def recover(self) -> None:
        """Bring capture back after a stall. Reopen the dongle if USB died."""
        try:
            self.restart_stream()
            return
        except Exception:
            pass
        self.close()
        err = self._lib.rtlsdr_open(ctypes.byref(self._dev), self._index)
        if err:
            self._dev = ctypes.c_void_p()
            raise RtlError(f"rtlsdr_open failed: {err}")
        if self._cfg is None:
            raise RtlError("recover: dongle was never configured")
        self.configure(**self._cfg)

    def read_samples(self, n_samples: int) -> np.ndarray:
        if self._thread is None or not self._thread.is_alive():
            raise RtlError("RTL-SDR capture thread stopped")
        parts = []
        have = 0
        if len(self._leftover):
            take = self._leftover[:n_samples]
            parts.append(take)
            have += len(take)
            self._leftover = self._leftover[len(take) :]
        while have < n_samples:
            try:
                chunk = self._queue.get(timeout=2.0)
            except queue.Empty as exc:
                raise RtlError("timed out waiting for RTL-SDR samples") from exc
            need = n_samples - have
            if len(chunk) <= need:
                parts.append(chunk)
                have += len(chunk)
            else:
                parts.append(chunk[:need])
                self._leftover = chunk[need:]
                have += need
        return np.concatenate(parts)

    def close(self) -> None:
        if self._dev:
            if self._thread is not None:
                self._lib.rtlsdr_cancel_async(self._dev)
                self._thread.join(timeout=2.0)
                self._thread = None
            self._lib.rtlsdr_close(self._dev)
            self._dev = ctypes.c_void_p()

    def __enter__(self) -> RtlSdr:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
