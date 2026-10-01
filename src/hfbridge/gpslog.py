"""Log GPS fixes to a CSV so later we can score range vs msg_id time.

USB GPS dongles usually show up as /dev/ttyACM0 or /dev/ttyUSB0 and speak
NMEA at 9600 baud. No extra Python packages.

Run on the Pi next to ingress (GPS does not use the RTL):

  PYTHONPATH=src .venv/bin/python -m hfbridge.gpslog \\
    --device /dev/ttyACM0 --csv ~/hf-logs/gps.csv

Join after the test: each bench-rx line has local HH:MM:SS; pick the GPS
row with the nearest timestamp.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _nmea_deg(raw: str, hemi: str) -> float | None:
    if not raw or not hemi:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    deg = int(val // 100)
    minutes = val - deg * 100
    dec = deg + minutes / 60.0
    if hemi in "SW":
        dec = -dec
    return dec


def _parse_rmc(line: str) -> dict | None:
    if not (line.startswith("$GPRMC") or line.startswith("$GNRMC")):
        return None
    p = line.split(",")
    if len(p) < 10 or p[2] != "A":
        return None
    lat = _nmea_deg(p[3], p[4])
    lon = _nmea_deg(p[5], p[6])
    if lat is None or lon is None:
        return None
    try:
        knots = float(p[7]) if p[7] else 0.0
    except ValueError:
        knots = 0.0
    return {
        "lat": lat,
        "lon": lon,
        "speed_mph": knots * 1.15078,
        "nmea_time": p[1],
        "nmea_date": p[9] if len(p) > 9 else "",
    }


def _open_serial(path: str, baud: int):
    try:
        import serial
    except ImportError:
        serial = None
    if serial is not None:
        return serial.Serial(path, baud, timeout=1.5)
    fh = open(path, "rb", buffering=0)
    try:
        import termios

        attrs = termios.tcgetattr(fh.fileno())
        speed = {
            4800: termios.B4800,
            9600: termios.B9600,
            38400: termios.B38400,
            115200: termios.B115200,
        }.get(baud)
        if speed is None:
            raise ValueError(f"unsupported baud {baud} without pyserial")
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fh.fileno(), termios.TCSANOW, attrs)
    except (ImportError, OSError):
        pass
    return fh


def _readline(port) -> str:
    if hasattr(port, "readline"):
        raw = port.readline()
    else:
        buf = b""
        while not buf.endswith(b"\n"):
            ch = port.read(1)
            if not ch:
                break
            buf += ch
        raw = buf
    if isinstance(raw, bytes):
        return raw.decode("ascii", errors="replace").strip()
    return str(raw).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("gps.csv"),
        help="append timestamped fixes here",
    )
    args = parser.parse_args(argv)

    try:
        port = _open_serial(args.device, args.baud)
    except OSError as exc:
        print(f"cannot open {args.device}: {exc}", file=sys.stderr)
        print("try ls /dev/ttyACM* /dev/ttyUSB*", file=sys.stderr)
        return 1

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    new = not args.csv.exists() or args.csv.stat().st_size == 0
    out = args.csv.open("a", newline="")
    writer = csv.DictWriter(
        out,
        fieldnames=(
            "local_time",
            "unix",
            "iso_utc",
            "lat",
            "lon",
            "speed_mph",
            "nmea_time",
            "nmea_date",
        ),
    )
    if new:
        writer.writeheader()
        out.flush()
    print(
        f"gpslog {args.device} {args.baud} baud → {args.csv}  (Ctrl-C to stop)",
        flush=True,
    )
    last_print = 0.0
    try:
        while True:
            line = _readline(port)
            fix = _parse_rmc(line)
            if fix is None:
                continue
            now = time.time()
            row = {
                "local_time": time.strftime("%H:%M:%S"),
                "unix": f"{now:.3f}",
                "iso_utc": datetime.now(timezone.utc).isoformat(),
                "lat": f"{fix['lat']:.6f}",
                "lon": f"{fix['lon']:.6f}",
                "speed_mph": f"{fix['speed_mph']:.1f}",
                "nmea_time": fix["nmea_time"],
                "nmea_date": fix["nmea_date"],
            }
            writer.writerow(row)
            out.flush()
            if now - last_print >= 1.0:
                print(
                    f"{row['local_time']}  {row['lat']}  {row['lon']}  "
                    f"{row['speed_mph']} mph",
                    flush=True,
                )
                last_print = now
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0
    finally:
        out.close()
        close = getattr(port, "close", None)
        if close:
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
