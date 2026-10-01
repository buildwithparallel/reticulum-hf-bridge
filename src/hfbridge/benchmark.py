"""Modem benchmark: IQ loopback, or Hermes → RTL live, then a scored log.

Loopback (no RF):

  PYTHONPATH=src .venv/bin/python -m hfbridge.benchmark
  PYTHONPATH=src .venv/bin/python -m hfbridge.benchmark --snr-db 10 --offset-hz -156

Live (Stop Hermes in Crosstalk first; Pi ingress must be running):

  PYTHONPATH=src .venv/bin/python -m hfbridge.benchmark --on-air --count 20 \\
    --callsign YOURCALL --hl2-ip HERMES_IP --filter-confirmed --arm-tx \\
    --drive 187 --amplitude 1.0 --json benchmarks/ota-tx.json

  scp pi@raspberrypi.local:hf-logs/ingress.log /tmp/ingress.log
  PYTHONPATH=src .venv/bin/python -m hfbridge.benchmark --score \\
    --tx-json benchmarks/ota-tx.json --rx-log /tmp/ingress.log \\
    --json benchmarks/ota-result.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from hfbridge.frame import MAX_PAYLOAD, Frame, crc16
from hfbridge.fsk import (
    DEFAULT_BAUD,
    DEFAULT_CHANNEL_RATE,
    DEFAULT_DEVIATION,
    POSTAMBLE_BYTES,
    PREAMBLE_BYTES,
    SYNC,
    demodulate,
    modulate,
)
from hfbridge.transmit import (
    DEFAULT_AMPLITUDE,
    DEFAULT_DRIVE,
    DEFAULT_FREQUENCY,
    build_plan,
)

_REPO = Path(__file__).resolve().parents[2]
# Live Pi ingress. Same-room on the Mac was 20.7; this stick needs 42.1.
DEFAULT_RTL_GAIN_DB = 42.1

# 100 transmissions: short "yo"-class notes through the 81/162 byte sizes
# that CRC-failed on the Pi walk, plus a few max-size frames.
PAYLOAD_PLAN: tuple[tuple[int, int], ...] = (
    (3, 25),
    (8, 20),
    (20, 20),
    (50, 15),
    (81, 10),
    (162, 7),
    (200, 3),
)
_FILLER = "pack the short bench note "
_COUNT_WORDS = (
    "zero one two three four five six seven eight nine "
    "ten eleven twelve thirteen fourteen fifteen"
).split()


@dataclass(frozen=True)
class TrialResult:
    index: int
    payload_bytes: int
    ok: bool
    reason: str
    payload_preview: str


def architecture() -> dict:
    """What is on the air today."""
    return {
        "name": "hfbridge PoC",
        "modulation": "2-CPFSK",
        "fec": "LDPC (128,64) rate 1/2, soft normalized min-sum, CRC-16 last",
        "integrity": "CRC-16-CCITT init 0xFFFF",
        "handshake": "none",
        "baud": DEFAULT_BAUD,
        "deviation_hz": DEFAULT_DEVIATION,
        "channel_rate_hz": DEFAULT_CHANNEL_RATE,
        "mark_hz": DEFAULT_DEVIATION,
        "space_hz": -DEFAULT_DEVIATION,
        "preamble_hex": PREAMBLE_BYTES.hex(),
        "sync_hex": SYNC.hex(),
        "postamble_bytes": len(POSTAMBLE_BYTES),
        "max_payload_bytes": MAX_PAYLOAD,
        "header_bytes": 30,
        "frequency_hz": DEFAULT_FREQUENCY,
        "occupied_bandwidth": (
            f"about {2 * (DEFAULT_DEVIATION + DEFAULT_BAUD):.0f} Hz "
            f"(2-CPFSK {2 * DEFAULT_DEVIATION:.0f} Hz shift at {DEFAULT_BAUD:.0f} baud)"
        ),
        "receiver": "RTL-SDR R820T",
        "rtl_gain_db": DEFAULT_RTL_GAIN_DB,
        "git": _git_revision(),
    }


def payload_sizes(count: int) -> list[int]:
    planned: list[int] = []
    for size, n in PAYLOAD_PLAN:
        planned.extend([size] * n)
    if count == len(planned):
        return planned
    classes = [size for size, _n in PAYLOAD_PLAN]
    return [classes[i % len(classes)] for i in range(count)]


def make_payload(nbytes: int, trial: int) -> bytes:
    if nbytes < 1 or nbytes > MAX_PAYLOAD:
        raise ValueError(f"payload must be 1..{MAX_PAYLOAD} bytes")
    if nbytes <= 4:
        return ("yes ping")[:nbytes].encode("ascii")
    word = _COUNT_WORDS[trial % len(_COUNT_WORDS)]
    long = f"{word} {_FILLER * 40}"
    text = long[:nbytes]
    if len(text) < nbytes:
        text = (text + (" " * nbytes))[:nbytes]
    encoded = text.encode("ascii")
    if len(encoded) != nbytes:
        raise ValueError("payload length mismatch")
    return encoded


def add_awgn(
    iq: np.ndarray, snr_db: float, rng: np.random.Generator
) -> np.ndarray:
    power = float(np.mean(np.abs(iq) ** 2))
    noise_power = power / (10.0 ** (snr_db / 10.0))
    std = float(np.sqrt(noise_power / 2.0))
    noise = rng.normal(0.0, std, len(iq)) + 1j * rng.normal(0.0, std, len(iq))
    return (iq + noise.astype(np.complex64)).astype(np.complex64)


def apply_offset(iq: np.ndarray, offset_hz: float, sample_rate: float) -> np.ndarray:
    if offset_hz == 0:
        return iq
    n = np.arange(len(iq), dtype=np.float64)
    spun = iq * np.exp(2j * np.pi * offset_hz * n / sample_rate)
    return spun.astype(np.complex64)


def apply_mobile_fading(
    iq: np.ndarray,
    rng: np.random.Generator,
    *,
    sample_rate: float = DEFAULT_CHANNEL_RATE,
    doppler_hz: float = 1.0,
    delay_ms: float = 2.0,
) -> np.ndarray:
    """Watterson-inspired two-path Rayleigh fading for regression tests.

    This is deliberately labeled "inspired", not standards-conformant
    ITU-R F.1487 calibration. It supplies repeatable slow fades, a delayed
    path, and changing phase so AWGN-only improvements cannot masquerade as
    mobile-HF robustness.
    """
    x = np.asarray(iq, dtype=np.complex64)
    if not len(x):
        return x.copy()
    knot_samples = max(8, int(round(sample_rate / max(0.1, 4.0 * doppler_hz))))
    knot_at = np.arange(0, len(x) + knot_samples, knot_samples, dtype=np.float64)
    sample_at = np.arange(len(x), dtype=np.float64)

    def rayleigh_path() -> np.ndarray:
        knots = (
            rng.normal(0.0, 1.0, len(knot_at))
            + 1j * rng.normal(0.0, 1.0, len(knot_at))
        ) / np.sqrt(2.0)
        real = np.interp(sample_at, knot_at, knots.real)
        imag = np.interp(sample_at, knot_at, knots.imag)
        return real + 1j * imag

    direct = rayleigh_path()
    echo_gain = 10.0 ** (-6.0 / 20.0)
    echo = rayleigh_path() * echo_gain
    delay = max(1, int(round(sample_rate * delay_ms / 1000.0)))
    delayed = np.zeros(len(x), dtype=np.complex128)
    delayed[delay:] = x[:-delay]
    faded = direct * x + echo * delayed
    before = float(np.mean(np.abs(x) ** 2))
    after = float(np.mean(np.abs(faded) ** 2))
    if before > 0.0 and after > 0.0:
        faded *= np.sqrt(before / after)
    return faded.astype(np.complex64)


def run_trial(
    *,
    index: int,
    nbytes: int,
    snr_db: float | None,
    offset_hz: float,
    fading: str,
    rng: np.random.Generator,
) -> TrialResult:
    payload = make_payload(nbytes, index)
    frame = Frame(
        origin="N0CALL",
        dest=bytes(16),
        payload=payload,
        msg_id=(index % 65535) + 1,
    )
    iq = modulate(frame, fec=True)
    iq = apply_offset(iq, offset_hz, DEFAULT_CHANNEL_RATE)
    if fading == "mobile":
        iq = apply_mobile_fading(iq, rng)
    if snr_db is not None:
        quiet = int(DEFAULT_CHANNEL_RATE * 0.2)
        pad = np.zeros(quiet, dtype=np.complex64)
        burst = np.concatenate([pad, iq, pad])
        burst = add_awgn(burst, snr_db, rng)
    else:
        burst = iq
    try:
        decoded = demodulate(burst)
    except ValueError as exc:
        return TrialResult(index, nbytes, False, str(exc), payload[:24].decode("ascii"))
    if decoded.payload != payload or decoded.origin != "N0CALL":
        return TrialResult(
            index,
            nbytes,
            False,
            "decoded a different payload",
            payload[:24].decode("ascii"),
        )
    return TrialResult(index, nbytes, True, "ok", payload[:24].decode("ascii"))


def run_benchmark(
    *,
    count: int = 100,
    snr_db: float | None = None,
    offset_hz: float = 0.0,
    fading: str = "none",
    seed: int = 0,
) -> dict:
    sizes = payload_sizes(count)
    rng = np.random.default_rng(seed)
    started = time.perf_counter()
    trials = [
        run_trial(
            index=i,
            nbytes=sizes[i],
            snr_db=snr_db,
            offset_hz=offset_hz,
            fading=fading,
            rng=rng,
        )
        for i in range(count)
    ]
    elapsed = time.perf_counter() - started
    decoded = sum(1 for trial in trials if trial.ok)
    failed = count - decoded
    by_size: dict[str, dict[str, int]] = {}
    reasons: Counter[str] = Counter()
    for trial in trials:
        bucket = by_size.setdefault(
            str(trial.payload_bytes), {"sent": 0, "got_through": 0, "failed": 0}
        )
        bucket["sent"] += 1
        if trial.ok:
            bucket["got_through"] += 1
        else:
            bucket["failed"] += 1
            reasons[trial.reason] += 1
    return {
        "when": datetime.now(timezone.utc).isoformat(),
        "architecture": architecture(),
        "conditions": {
            "channel": (
                "mobile two-path fading + awgn"
                if fading == "mobile" and snr_db is not None
                else "mobile two-path fading"
                if fading == "mobile"
                else "awgn"
                if snr_db is not None
                else "clean loopback"
            ),
            "fading": fading,
            "snr_db": snr_db,
            "snr_note": (
                "signal-power / noise-power on the complex baseband burst; "
                "not the listener dB-over-floor number"
                if snr_db is not None
                else "no noise added"
            ),
            "offset_hz": offset_hz,
            "count": count,
            "seed": seed,
            "keys_rf": False,
        },
        "summary": {
            "sent": count,
            "got_through": decoded,
            "failed": failed,
            "got_through_percent": round(100.0 * decoded / count, 1) if count else 0.0,
            "packet_error_rate": round(failed / count, 4) if count else 0.0,
            "elapsed_seconds": round(elapsed, 3),
        },
        "by_payload_bytes": dict(sorted(by_size.items(), key=lambda item: int(item[0]))),
        "failure_reasons": dict(reasons),
        "failures": [asdict(trial) for trial in trials if not trial.ok],
        "crc16_self_check": f"{crc16(b'hello hf'):04x}",
    }


_GAIN_LOG = re.compile(r"gain=(agc|[0-9.]+)")


def parse_rtl_gain(text: str) -> float | str | None:
    """Last tuner gain printed by listen/ingress, or None."""
    found = None
    for match in _GAIN_LOG.finditer(text):
        raw = match.group(1)
        found = raw if raw == "agc" else float(raw)
    return found


_BENCH_OK = re.compile(
    r"bench-rx ok msg_id=(\d+) bytes=(\d+) snr=([0-9.+-]+) origin=(\S+)"
)
_BENCH_FAIL = re.compile(r"bench-rx fail snr=([0-9.+-]+) reason=(.+)$")
_LOG_TIME = re.compile(r"^(\d{2}:\d{2}:\d{2})\b")


def parse_rx_log(text: str) -> dict:
    """Pull bench-rx lines out of an ingress/listen log."""
    oks: list[dict] = []
    fails: list[dict] = []
    for raw in text.splitlines():
        stamp_match = _LOG_TIME.search(raw)
        stamp = stamp_match.group(1) if stamp_match else ""
        ok = _BENCH_OK.search(raw)
        if ok:
            oks.append(
                {
                    "t_local": stamp,
                    "msg_id": int(ok.group(1)),
                    "bytes": int(ok.group(2)),
                    "snr_db": float(ok.group(3)),
                    "origin": ok.group(4),
                }
            )
            continue
        fail = _BENCH_FAIL.search(raw)
        if fail:
            fails.append(
                {
                    "t_local": stamp,
                    "snr_db": float(fail.group(1)),
                    "reason": fail.group(2).strip(),
                }
            )
    return {"ok": oks, "fail": fails}


def score_on_air(tx: dict, rx_log: str) -> dict:
    """Match Hermes transmissions to Pi bench-rx lines by msg_id."""
    parsed = parse_rx_log(rx_log)
    sent = tx.get("transmissions") or []
    sent_ids = {int(item["msg_id"]) for item in sent}
    oks = [item for item in parsed["ok"] if item["msg_id"] in sent_ids]
    fails = parsed["fail"]
    by_id = {item["msg_id"]: item for item in oks}
    trials: list[TrialResult] = []
    snrs: list[float] = []
    reasons: Counter[str] = Counter()
    by_size: dict[str, dict[str, int]] = {}
    for item in sent:
        msg_id = int(item["msg_id"])
        nbytes = int(item["payload_bytes"])
        bucket = by_size.setdefault(
            str(nbytes), {"sent": 0, "got_through": 0, "failed": 0}
        )
        bucket["sent"] += 1
        hit = by_id.get(msg_id)
        if hit:
            bucket["got_through"] += 1
            snrs.append(float(hit["snr_db"]))
            trials.append(
                TrialResult(
                    index=int(item["index"]),
                    payload_bytes=nbytes,
                    ok=True,
                    reason="ok",
                    payload_preview=item.get("payload_preview", ""),
                )
            )
        else:
            bucket["failed"] += 1
            reason = "not decoded on the Pi"
            reasons[reason] += 1
            trials.append(
                TrialResult(
                    index=int(item["index"]),
                    payload_bytes=nbytes,
                    ok=False,
                    reason=reason,
                    payload_preview=item.get("payload_preview", ""),
                )
            )
    decoded = sum(1 for trial in trials if trial.ok)
    count = len(sent)
    failed = count - decoded
    logged_gain = parse_rtl_gain(rx_log)
    rtl_gain = (
        logged_gain
        if logged_gain is not None
        else (tx.get("conditions") or {}).get("rtl_gain_db", DEFAULT_RTL_GAIN_DB)
    )
    arch = dict(tx.get("architecture") or architecture())
    arch["rtl_gain_db"] = rtl_gain
    arch["receiver"] = "RTL-SDR R820T"
    result = {
        "when": datetime.now(timezone.utc).isoformat(),
        "architecture": arch,
        "conditions": {
            **(tx.get("conditions") or {}),
            "channel": "hermes-to-rtl over the air",
            "keys_rf": True,
            "rtl_gain_db": rtl_gain,
            "rtl_gain_source": "ingress log" if logged_gain is not None else "tx manifest",
            "rx_crc_fails_in_window": len(fails),
            "rx_snr_db": {
                "n": len(snrs),
                "mean": round(sum(snrs) / len(snrs), 1) if snrs else None,
                "min": round(min(snrs), 1) if snrs else None,
                "max": round(max(snrs), 1) if snrs else None,
            },
        },
        "summary": {
            "sent": count,
            "got_through": decoded,
            "failed": failed,
            "got_through_percent": round(100.0 * decoded / count, 1) if count else 0.0,
            "packet_error_rate": round(failed / count, 4) if count else 0.0,
        },
        "by_payload_bytes": dict(sorted(by_size.items(), key=lambda item: int(item[0]))),
        "failure_reasons": dict(reasons),
        "failures": [asdict(trial) for trial in trials if not trial.ok],
        "rx_ok": oks,
        "rx_fail": fails,
        "transmissions": sent,
    }
    return result


def plan_on_air_transmissions(
    *,
    count: int,
    callsign: str,
    drive: int,
    amplitude: float,
    frequency_hz: int,
    msg_id_base: int = 1,
) -> list[dict]:
    sizes = payload_sizes(count)
    jobs = []
    for index, nbytes in enumerate(sizes):
        payload = make_payload(nbytes, index)
        msg_id = ((msg_id_base + index - 1) % 65535) + 1
        plan = build_plan(
            callsign=callsign,
            text=payload.decode("ascii"),
            destination="00" * 16,
            message_id=msg_id,
            frequency_hz=frequency_hz,
            amplitude=amplitude,
            drive=drive,
        )
        jobs.append(
            {
                "index": index,
                "msg_id": msg_id,
                "payload_bytes": nbytes,
                "payload_preview": payload[:24].decode("ascii"),
                "airtime_seconds": round(plan.airtime_seconds, 3),
                "estimated_watts": round(plan.estimated_watts, 4),
                "plan": plan,
            }
        )
    return jobs


def run_on_air(
    *,
    count: int,
    callsign: str,
    hl2_ip: str | None,
    drive: int,
    amplitude: float,
    frequency_hz: int,
    gap_s: float,
    arm_tx: bool,
    rtl_gain_db: float = DEFAULT_RTL_GAIN_DB,
    send_fn=None,
) -> dict:
    jobs = plan_on_air_transmissions(
        count=count,
        callsign=callsign,
        drive=drive,
        amplitude=amplitude,
        frequency_hz=frequency_hz,
        msg_id_base=int(time.time()) % 60000,
    )
    watts = jobs[0]["estimated_watts"] if jobs else 0.0
    t_start = time.strftime("%H:%M:%S")
    started = datetime.now(timezone.utc).isoformat()
    sent: list[dict] = []
    if arm_tx:
        sender = send_fn
        if sender is None:
            from hfbridge.hl2 import HL2Session, UdpTransport
            from hfbridge.hpsdr import ControlBank, EP2Builder

            def _send(plan) -> None:
                controls = ControlBank(
                    frequency_hz=plan.frequency_hz,
                    drive=plan.drive,
                    pa_enabled=True,
                )
                builder = EP2Builder(controls=controls, amplitude=plan.amplitude)
                session = HL2Session(
                    transport=UdpTransport(hl2_ip),
                    builder=builder,
                )
                session.transmit(plan.iq)

            sender = _send

        for job in jobs:
            print(
                f"on-air {job['index'] + 1}/{count} msg_id={job['msg_id']} "
                f"{job['payload_bytes']}B ~{job['airtime_seconds']}s",
                flush=True,
            )
            sender(job["plan"])
            record = {k: v for k, v in job.items() if k != "plan"}
            record["t_local"] = time.strftime("%H:%M:%S")
            sent.append(record)
            if gap_s > 0 and job["index"] + 1 < count:
                time.sleep(gap_s)
    else:
        for job in jobs:
            record = {k: v for k, v in job.items() if k != "plan"}
            sent.append(record)
    t_end = time.strftime("%H:%M:%S")
    arch = architecture()
    arch["rtl_gain_db"] = rtl_gain_db
    return {
        "when": started,
        "architecture": arch,
        "conditions": {
            "channel": "hermes-to-rtl over the air",
            "keys_rf": arm_tx,
            "callsign": callsign,
            "hl2_ip": hl2_ip,
            "frequency_hz": frequency_hz,
            "drive": drive,
            "amplitude": amplitude,
            "estimated_watts": watts,
            "rtl_gain_db": rtl_gain_db,
            "receiver": "RTL-SDR R820T",
            "gap_s": gap_s,
            "count": count,
        },
        "t_local_start": t_start,
        "t_local_end": t_end,
        "transmissions": sent,
        "summary": {
            "sent": len(sent) if arm_tx else 0,
            "planned": count,
            "got_through": None,
            "note": (
                "transmitted; score against the Pi ingress log with --score"
                if arm_tx
                else "dry run; no RF. Add --arm-tx to key the Hermes."
            ),
        },
    }


def format_report(result: dict) -> str:
    arch = result["architecture"]
    cond = result["conditions"]
    summary = result["summary"]
    title = (
        "HF modem benchmark (Hermes → RTL, over the air)"
        if cond.get("keys_rf")
        else "HF modem benchmark (IQ loopback, no RF)"
    )
    lines = [
        title,
        f"architecture: {arch['modulation']} {arch['baud']} baud, "
        f"FEC={arch['fec']}, {arch['integrity']}, handshake={arch['handshake']}",
        f"sync {arch['sync_hex']}  max payload {arch['max_payload_bytes']} B  "
        f"git {arch['git'] or 'unknown'}",
        f"RTL {arch.get('receiver', 'RTL-SDR')} gain {cond.get('rtl_gain_db', arch.get('rtl_gain_db'))} dB",
        f"channel: {cond['channel']}"
        + (f"  SNR {cond['snr_db']} dB" if cond.get("snr_db") is not None else "")
        + (
            f"  ~{cond['estimated_watts']} W"
            if cond.get("estimated_watts") is not None and cond.get("keys_rf")
            else ""
        )
        + (
            f"  offset {cond['offset_hz']:+.0f} Hz"
            if cond.get("offset_hz")
            else ""
        ),
        "",
        f"got through {summary['got_through']}/{summary['sent']} "
        f"({summary['got_through_percent']}%)  "
        f"PER {summary['packet_error_rate']}",
        "",
        f"{'bytes':>6}  {'sent':>4}  {'ok':>4}  {'fail':>4}  {'pct':>6}",
    ]
    for size, bucket in result["by_payload_bytes"].items():
        pct = 100.0 * bucket["got_through"] / bucket["sent"] if bucket["sent"] else 0.0
        lines.append(
            f"{size:>6}  {bucket['sent']:>4}  {bucket['got_through']:>4}  "
            f"{bucket['failed']:>4}  {pct:5.0f}%"
        )
    rx = (cond.get("rx_snr_db") or {})
    if rx.get("n"):
        lines.append(
            f"Pi SNR on decoded frames: mean {rx['mean']} dB "
            f"(min {rx['min']}, max {rx['max']}, n={rx['n']})"
        )
        lines.append("")
    if result["failure_reasons"]:
        lines.append("")
        lines.append("failures:")
        for reason, n in result["failure_reasons"].items():
            lines.append(f"  {n}  {reason}")
    return "\n".join(lines) + "\n"


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="number of frames (default 100 loopback, 20 on-air)",
    )
    parser.add_argument(
        "--snr-db",
        type=float,
        default=None,
        help="AWGN SNR in dB for loopback (omit for a clean loopback)",
    )
    parser.add_argument(
        "--offset-hz",
        type=float,
        default=0.0,
        help="frequency error to apply before decode (bench RX is often -156 Hz)",
    )
    parser.add_argument(
        "--fading",
        choices=("none", "mobile"),
        default="none",
        help="loopback channel profile; mobile adds repeatable two-path Rayleigh fading",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--json",
        type=Path,
        help="write the full result object here",
    )
    parser.add_argument(
        "--on-air",
        action="store_true",
        help="send frames on the Hermes-Lite 2 (requires --arm-tx to key RF)",
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="score a Pi ingress log against an --on-air JSON manifest",
    )
    parser.add_argument("--tx-json", type=Path, help="manifest from --on-air")
    parser.add_argument("--rx-log", type=Path, help="ingress.log from the Pi")
    parser.add_argument("--callsign", help="required for --on-air")
    parser.add_argument("--hl2-ip", help="Hermes IPv4; required with --arm-tx")
    parser.add_argument(
        "--arm-tx",
        action="store_true",
        help="allow the socket-backed path to set MOX",
    )
    parser.add_argument(
        "--filter-confirmed",
        action="store_true",
        help="confirm a 10 m transmit low-pass filter is installed",
    )
    parser.add_argument("--drive", type=int, default=DEFAULT_DRIVE)
    parser.add_argument("--amplitude", type=float, default=DEFAULT_AMPLITUDE)
    parser.add_argument("--frequency", type=int, default=DEFAULT_FREQUENCY)
    parser.add_argument(
        "--rtl-gain",
        type=float,
        default=DEFAULT_RTL_GAIN_DB,
        help=f"RTL tuner gain in dB to record (Pi listen is {DEFAULT_RTL_GAIN_DB})",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=2.5,
        help="seconds of silence between on-air frames",
    )
    return parser


def _write_json(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.score:
        if not args.tx_json or not args.rx_log:
            print("--score needs --tx-json and --rx-log", file=sys.stderr)
            return 2
        tx = json.loads(args.tx_json.read_text())
        result = score_on_air(tx, args.rx_log.read_text())
        print(format_report(result), end="")
        out = args.json or args.tx_json.with_name(args.tx_json.stem + "-scored.json")
        _write_json(out, result)
        return 0

    if args.on_air:
        if not args.callsign:
            print("--on-air needs --callsign", file=sys.stderr)
            return 2
        if args.arm_tx:
            if not args.hl2_ip:
                print("--hl2-ip is required with --arm-tx", file=sys.stderr)
                return 2
            if not args.filter_confirmed:
                print("--filter-confirmed is required with --arm-tx", file=sys.stderr)
                return 2
        count = 20 if args.count is None else args.count
        if count < 1:
            print("--count must be at least 1", file=sys.stderr)
            return 2
        result = run_on_air(
            count=count,
            callsign=args.callsign,
            hl2_ip=args.hl2_ip,
            drive=args.drive,
            amplitude=args.amplitude,
            frequency_hz=args.frequency,
            gap_s=args.gap,
            arm_tx=args.arm_tx,
            rtl_gain_db=args.rtl_gain,
        )
        print(
            f"on-air manifest: {result['summary']['note']}\n"
            f"architecture: {result['architecture']['modulation']} "
            f"{result['architecture']['baud']} baud, "
            f"FEC={result['architecture']['fec']}, "
            f"RTL gain {result['conditions']['rtl_gain_db']} dB\n"
            f"planned {result['conditions']['count']} frames, "
            f"~{result['conditions']['estimated_watts']} W, "
            f"window {result['t_local_start']}–{result['t_local_end']}",
            flush=True,
        )
        out = args.json
        if out is None:
            out = _REPO / "benchmarks" / f"ota-{time.strftime('%Y%m%d-%H%M%S')}.json"
        _write_json(out, result)
        if args.arm_tx:
            print(
                f"Score when the Pi is done:\n"
                f"  Copy the Pi ingress log to /tmp/ingress.log, then run:\n"
                f"  PYTHONPATH=src .venv/bin/python -m hfbridge.benchmark --score "
                f"--tx-json {out} --rx-log /tmp/ingress.log",
                file=sys.stderr,
            )
        return 0

    count = 100 if args.count is None else args.count
    if count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2
    result = run_benchmark(
        count=count,
        snr_db=args.snr_db,
        offset_hz=args.offset_hz,
        fading=args.fading,
        seed=args.seed,
    )
    print(format_report(result), end="")
    if args.json:
        _write_json(args.json, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
