"""Live noise floor / signal / SNR for the RTL 6 kHz channel. No decode.

The dongle is exclusive — stop hfbridge.ingress first.

On the Pi:

  PYTHONPATH=src .venv/bin/python -m hfbridge.meter --gain 14.4
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from hfbridge.channelize import Channelizer
from hfbridge.fsk import DEFAULT_CHANNEL_RATE
from hfbridge.listen import (
    DEFAULT_FREQ,
    IF_OFFSET,
    RTL_RATE,
    _Detector,
    _Mixer,
    _peak_tone,
    _power_dbfs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freq", type=int, default=DEFAULT_FREQ)
    parser.add_argument("--rtl-rate", type=int, default=RTL_RATE)
    parser.add_argument("--offset-hz", type=int, default=IF_OFFSET)
    parser.add_argument("--gain", type=float, default=14.4)
    parser.add_argument("--agc", action="store_true")
    parser.add_argument("--direct-sampling", type=int, default=0, choices=(0, 1, 2))
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between printed lines; the loudest chunk in each wins",
    )
    parser.add_argument(
        "--header-every",
        type=int,
        default=20,
        help="reprint the column header every N lines (0 to print it once)",
    )
    parser.add_argument(
        "--margin-db",
        type=float,
        default=6.0,
        help="dB over the quiet floor to flag SIGNAL (default 6; use ~2 for weak skip)",
    )
    args = parser.parse_args(argv)

    from hfbridge.rtl import RtlError, RtlSdr

    tune = args.freq - args.offset_hz
    sdr = RtlSdr(index=args.device)
    sdr.configure(
        center_freq=tune,
        sample_rate=args.rtl_rate,
        gain_db=None if args.agc else args.gain,
        direct_sampling=args.direct_sampling,
    )
    mixer = _Mixer(args.offset_hz, args.rtl_rate)
    channelizer = Channelizer(args.rtl_rate, DEFAULT_CHANNEL_RATE)
    chunk = 131072
    detector = _Detector(
        chunk_seconds=chunk / args.rtl_rate,
        margin_db=args.margin_db,
    )
    sdr.read_samples(chunk)
    print(
        f"meter {args.freq / 1e6:.6f} MHz  6 kHz channel  "
        f"gain={'agc' if args.agc else args.gain}  "
        f"margin={args.margin_db:g} dB  "
        f"(stop ingress first; Ctrl-C to quit)",
        flush=True,
    )
    header = (
        f"{'time':8}  {'floor':>7}  {'chan':>7}  {'snr':>6}  {'tone':>6}  state"
    )
    print(header, flush=True)
    rows = 0
    loudest = None
    saw_signal = False
    next_print = time.monotonic() + args.interval
    try:
        while True:
            try:
                samples = sdr.read_samples(chunk)
            except RtlError as exc:
                print(f"{time.strftime('%H:%M:%S')} RTL stall ({exc}); restarting", flush=True)
                try:
                    sdr.recover()
                    sdr.read_samples(chunk)
                except RtlError:
                    time.sleep(1.0)
                continue
            ch = channelizer(mixer(samples))
            if len(ch) == 0:
                continue
            tone_hz, _tone_db = _peak_tone(ch, DEFAULT_CHANNEL_RATE)
            chan = _power_dbfs(ch)
            detector.update(chan, tone_hz)
            saw_signal = saw_signal or detector.active
            if loudest is None or chan > loudest[0]:
                loudest = (chan, tone_hz)
            if time.monotonic() < next_print:
                continue
            next_print = time.monotonic() + args.interval
            peak_chan, peak_tone = loudest
            loudest = None
            state = "SIGNAL" if saw_signal else "quiet"
            saw_signal = False
            hist = list(detector.history)
            if len(hist) < 20:
                line = (
                    f"{time.strftime('%H:%M:%S')}  {'—':>7}  {peak_chan:7.1f}  "
                    f"{'—':>6}  {peak_tone:+6.0f}  warmup"
                )
            else:
                floor = float(np.median(hist))
                line = (
                    f"{time.strftime('%H:%M:%S')}  {floor:7.1f}  {peak_chan:7.1f}  "
                    f"{peak_chan - floor:6.1f}  {peak_tone:+6.0f}  {state}"
                )
            if args.header_every and rows and rows % args.header_every == 0:
                print(header, flush=True)
            print(line, flush=True)
            rows += 1
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0
    finally:
        sdr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
