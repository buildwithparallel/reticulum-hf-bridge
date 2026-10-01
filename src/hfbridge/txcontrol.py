"""Phone-first localhost controller for one-at-a-time Hermes HF tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hfbridge.fec import encode_coded
from hfbridge.fsk import POSTAMBLE_BYTES, PREAMBLE_BYTES, SYNC
from hfbridge.hl2 import (
    HL2Session,
    TransmissionCancelled,
    UdpTransport,
    emergency_stop,
)
from hfbridge.hpsdr import ControlBank, EP2Builder
from hfbridge.lxmfconv import via_title
from hfbridge.transmit import DEFAULT_FREQUENCY, build_plan
from hfbridge.listen import _RTL_GAIN_STEPS, snap_rtl_gain_db

MIN_UI_WATTS = 0.001
MAX_UI_WATTS = 5.0
# The Pi heartbeats every 10 s. Past this, its uplink is down rather than the
# link being weak, and saying so is the difference between "drive further" and
# "your hotspot is not up".
PI_OFFLINE_AFTER_SECONDS = 35.0
# The reporter thread can remain online while the native RTL capture is wedged.
# Receiver status normally advances every second, so ten seconds is a real stall.
RTL_STALE_AFTER_SECONDS = 10.0
# After the Hermes unkeys, the Pi still has to finish the burst and the
# decoder. Past this, with the Pi still heartbeating, it did not hear us.
WAIT_FOR_PI_SECONDS = 45.0
# After a dropout the first heartbeat must not beat the JSONL flush. The
# Pi posts queued decode events, then a heartbeat; give that handshake
# time before calling a miss.
PI_FLUSH_GRACE_SECONDS = 25.0
_OPEN_FOR_REPORT = frozenset({"transmitting", "waiting_for_pi", "not_heard"})
_FILTER_START = "<!--FILTER_START-->"
_FILTER_END = "<!--FILTER_END-->"


@dataclass
class TxRecord:
    msg_id: int
    payload: str
    watts: float
    drive: int
    frequency_hz: int
    airtime_seconds: float
    requested_at: str
    state: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    report: dict[str, Any] | None = None
    reported_at: str | None = None
    frame_hex: str | None = None
    frame_bytes: int | None = None
    on_air_bytes: int | None = None
    frame_origin: str | None = None
    frame_dest: str | None = None
    lxmf_title: str | None = None
    light: bool = False
    uplink_gap: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _frame_sizes(encoded: bytes) -> tuple[int, int]:
    """Inner CRC frame length and the FSK packet after LDPC, preamble, unique word, postamble."""
    on_air = len(PREAMBLE_BYTES) + len(SYNC) + len(encode_coded(encoded)) + len(
        POSTAMBLE_BYTES
    )
    return len(encoded), on_air


def _drive_for_watts(watts: float) -> int:
    if not MIN_UI_WATTS <= watts <= MAX_UI_WATTS:
        raise ValueError(
            f"power must be at least {MIN_UI_WATTS * 1000:g} mW and at most "
            f"{MAX_UI_WATTS:g} W"
        )
    return max(1, min(255, round(255 * math.sqrt(watts / 5.0))))


class Controller:
    def __init__(
        self,
        *,
        hl2_ip: str,
        callsign: str,
        destination: str,
        log_path: Path,
        report_token: str,
        filter_installed: bool = False,
        tx_lat: float | None = None,
        tx_lon: float | None = None,
    ):
        self.hl2_ip = hl2_ip
        self.callsign = callsign
        self.destination = destination
        self.log_path = log_path
        self.report_token = report_token
        self.filter_installed = filter_installed
        self.tx_lat = tx_lat
        self.tx_lon = tx_lon
        self.control_token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.state = "IDLE"
        self.current: TxRecord | None = None
        self.history: list[TxRecord] = []
        self.last_pi_report_at: str | None = None
        self.last_pi_contact_monotonic: float | None = None
        self.pi_online_since: float | None = None
        self.noise_reports = 0
        self.pi_rx: dict[str, Any] = {}
        self.last_pi_rx_update_monotonic: float | None = None
        self.pi_gps: dict[str, Any] | None = None
        # Field tests start at minimum tuner gain. The phone can select Auto
        # or a higher stepped gain after checking the live ADC/clipping card.
        self.rx_gain_db: float | None = 0.0
        self.active_builder: EP2Builder | None = None
        # Only a real key-up earns the right to send UDP at the radio. Without
        # this, idle stop requests and process shutdown would fire EP2 control words
        # (filters, PA relay) at an idle Hermes.
        self.keyed_since_start = False
        self.page, self.ui_version = render_page(filter_installed)
        # The on-air frame carries msg_id in 2 bytes; keep every id inside it.
        self._next_msg_id = int(time.time()) & 0xFFFF

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._expire_stale_wait()
            return {
                "state": self.state,
                "hl2_ip": self.hl2_ip,
                "pi_last_report_at": self.last_pi_report_at,
                "pi_age_seconds": (
                    None
                    if self.last_pi_contact_monotonic is None
                    else round(time.monotonic() - self.last_pi_contact_monotonic, 1)
                ),
                "pi_offline_after_seconds": PI_OFFLINE_AFTER_SECONDS,
                "pi_flushing": self._pi_flushing(),
                "pi_rx": dict(self.pi_rx),
                "pi_rx_age_seconds": (
                    None
                    if self.last_pi_rx_update_monotonic is None
                    else round(
                        time.monotonic() - self.last_pi_rx_update_monotonic, 1
                    )
                ),
                "rtl_stale_after_seconds": RTL_STALE_AFTER_SECONDS,
                "pi_miles": self._pi_miles(),
                "wait_detail": self._wait_detail(),
                "noise_reports": self.noise_reports,
                "current": asdict(self.current) if self.current else None,
                "history": [asdict(record) for record in self.history[-20:]][::-1],
                "control_token": self.control_token,
                "max_watts": MAX_UI_WATTS,
                "rx_gain_db": self.rx_gain_db,
                "rx_gain_steps": list(_RTL_GAIN_STEPS),
                "filter_installed": self.filter_installed,
                "ui_version": self.ui_version,
            }

    def disarm(self) -> None:
        self.stop()

    def set_rx_gain(self, gain_db: float | None) -> float | None:
        with self.lock:
            if gain_db is None:
                self.rx_gain_db = None
            else:
                self.rx_gain_db = snap_rtl_gain_db(float(gain_db))
            return self.rx_gain_db

    def report_ack(self) -> dict[str, Any]:
        with self.lock:
            return {"ok": True, "rx_gain_db": self.rx_gain_db}

    def send(
        self,
        payload: str,
        watts: float,
        frequency_hz: int,
        filter_confirmed: bool = False,
        light: bool = False,
    ) -> TxRecord:
        payload = str(payload)
        if not payload:
            raise ValueError("message cannot be empty")
        if not (filter_confirmed or self.filter_installed):
            raise ValueError("confirm the 10 m output filter before transmitting")
        drive = _drive_for_watts(float(watts))
        with self.lock:
            if self.state == "TRANSMITTING":
                raise ValueError("a frame is already transmitting")
            msg_id = self._next_msg_id
            self._next_msg_id = (self._next_msg_id + 1) & 0xFFFF

        plan = build_plan(
            callsign=self.callsign,
            text=payload,
            destination=self.destination,
            message_id=msg_id,
            frequency_hz=int(frequency_hz),
            amplitude=1.0,
            drive=drive,
            light=bool(light),
        )
        frame = plan.frame
        encoded = frame.encode() if frame is not None else None
        inner_bytes, on_air_bytes = (
            _frame_sizes(encoded) if encoded is not None else (None, None)
        )
        record = TxRecord(
            msg_id=msg_id,
            payload=payload,
            watts=round(plan.estimated_watts, 4),
            drive=drive,
            frequency_hz=plan.frequency_hz,
            airtime_seconds=round(plan.airtime_seconds, 3),
            requested_at=_utc_now(),
            frame_hex=encoded.hex() if encoded is not None else None,
            frame_bytes=inner_bytes,
            on_air_bytes=on_air_bytes,
            frame_origin=frame.origin if frame is not None else None,
            frame_dest=None if light else (frame.dest.hex() if frame is not None else None),
            lxmf_title=via_title(frame.origin) if frame is not None else None,
            light=bool(light),
        )
        with self.lock:
            self.cancel = threading.Event()
            self.current = record
            self.history.append(record)
            self.state = "TRANSMITTING"
        thread = threading.Thread(
            target=self._transmit, args=(record, plan), daemon=True, name="hermes-tx"
        )
        thread.start()
        return record

    def _transmit(self, record: TxRecord, plan) -> None:
        builder = EP2Builder(
            controls=ControlBank(
                frequency_hz=plan.frequency_hz,
                drive=plan.drive,
                pa_enabled=True,
            ),
            amplitude=plan.amplitude,
        )
        with self.lock:
            self.active_builder = builder
            self.keyed_since_start = True
            record.state = "transmitting"
            record.started_at = _utc_now()
            self._write_record(record)
        try:
            session = HL2Session(
                transport=UdpTransport(self.hl2_ip),
                builder=builder,
                receive_timeout=3.0,
            )
            session.transmit(plan.iq, cancel=self.cancel)
        except TransmissionCancelled:
            with self.lock:
                record.state = "stopped"
        except Exception as exc:
            with self.lock:
                record.state = "failed"
                record.error = str(exc)
                self.state = "FAULT"
        else:
            with self.lock:
                record.state = "waiting_for_pi"
                if not self._pi_online():
                    record.uplink_gap = True
                self.state = "IDLE"
        finally:
            with self.lock:
                record.finished_at = _utc_now()
                self.active_builder = None
                if record.state == "stopped":
                    self.state = "IDLE"
                self._write_record(record)

    def stop(self) -> None:
        with self.lock:
            self.cancel.set()
            builder = self.active_builder
            # Never send UDP at a radio this process has not keyed. Idle stop
            # requests and shutdown must be silent on the wire.
            needs_radio = self.keyed_since_start
            if needs_radio:
                self.state = "STOPPING"
        if needs_radio:
            if builder is None:
                builder = EP2Builder(
                    controls=ControlBank(
                        frequency_hz=DEFAULT_FREQUENCY,
                        drive=0,
                        pa_enabled=False,
                    ),
                    amplitude=0.0,
                )
            try:
                emergency_stop(self.hl2_ip, builder)
            except OSError:
                pass
        with self.lock:
            if self.current and self.current.state in {"queued", "transmitting"}:
                self.current.state = "stopped"
                self.current.finished_at = _utc_now()
                self._write_record(self.current)
            self.state = "IDLE"

    def accept_report(self, report: dict[str, Any]) -> None:
        msg_id = report.get("msg_id")
        now = _utc_now()
        with self.lock:
            self._note_pi_contact()
            if report.get("kind") == "heartbeat":
                rx = report.get("rx")
                if isinstance(rx, dict):
                    updated_at = rx.get("updated_at")
                    if (
                        updated_at is None
                        or updated_at != self.pi_rx.get("updated_at")
                    ):
                        self.last_pi_rx_update_monotonic = time.monotonic()
                    self.pi_rx = rx
                gps = report.get("gps")
                if isinstance(gps, dict):
                    self.pi_gps = gps
                return
            candidates = list(reversed(self.history))
            if msg_id is None:
                # A failed decode has no id to match on, so it can only be
                # guessed at. Guessing wrongly reports someone's car ignition
                # as the operator's frame, so only accept a failure that looks
                # like a frame at all.
                if not _looks_like_a_frame(report):
                    self.noise_reports += 1
                    return
                candidates = [
                    record
                    for record in candidates
                    if record.state in {"transmitting", "waiting_for_pi"}
                ][:1]
            for record in candidates:
                if not _report_targets_record(record, msg_id, report):
                    continue
                safe = {
                    key: report[key]
                    for key in (
                        "success",
                        "reason",
                        "snr_db",
                        "gain_db",
                        "lat",
                        "lon",
                        "distance_miles",
                        "timestamp",
                        "text",
                    )
                    if key in report
                }
                if "snr_db" in safe:
                    safe["snr_db"] = round(float(safe["snr_db"]), 1)
                if safe.get("gain_db") is not None:
                    safe["gain_db"] = round(float(safe["gain_db"]), 1)
                if "text" in safe:
                    safe["text_matches"] = safe["text"] == record.payload
                gps = report.get("gps")
                if isinstance(gps, dict):
                    for key in ("lat", "lon"):
                        if key in gps:
                            safe[key] = gps[key]
                if (
                    self.tx_lat is not None
                    and self.tx_lon is not None
                    and "lat" in safe
                    and "lon" in safe
                ):
                    safe["distance_miles"] = round(
                        _haversine_miles(
                            self.tx_lat,
                            self.tx_lon,
                            float(safe["lat"]),
                            float(safe["lon"]),
                        ),
                        3,
                    )
                if record.state == "decoded" and not safe.get("success"):
                    # A real decode already proved this frame arrived; a
                    # later anonymous failure is a different burst.
                    self.noise_reports += 1
                    return
                record.report = safe
                record.reported_at = now
                record.state = "decoded" if safe.get("success") else "decode_failed"
                if safe.get("success"):
                    record.error = None
                self._write_record(record)
                break

    def _note_pi_contact(self) -> None:
        now = time.monotonic()
        returning = (
            self.last_pi_contact_monotonic is None
            or (now - self.last_pi_contact_monotonic) > PI_OFFLINE_AFTER_SECONDS
        )
        if returning:
            self.pi_online_since = now
            for record in self.history:
                if record.state in {"transmitting", "waiting_for_pi"}:
                    record.uplink_gap = True
        self.last_pi_contact_monotonic = now
        self.last_pi_report_at = _utc_now()

    def _pi_online(self) -> bool:
        if self.last_pi_contact_monotonic is None:
            return False
        return (
            time.monotonic() - self.last_pi_contact_monotonic
        ) <= PI_OFFLINE_AFTER_SECONDS

    def _pi_flushing(self) -> bool:
        if not self._pi_online() or self.pi_online_since is None:
            return False
        return (time.monotonic() - self.pi_online_since) < PI_FLUSH_GRACE_SECONDS

    def _pi_miles(self) -> float | None:
        gps = self.pi_gps
        if (
            gps is None
            or self.tx_lat is None
            or self.tx_lon is None
            or "lat" not in gps
            or "lon" not in gps
        ):
            return None
        try:
            return round(
                _haversine_miles(
                    self.tx_lat,
                    self.tx_lon,
                    float(gps["lat"]),
                    float(gps["lon"]),
                ),
                3,
            )
        except (TypeError, ValueError):
            return None

    def _wait_detail(self) -> str:
        rx = self.pi_rx
        bits: list[str] = []
        if rx.get("gain_db") is not None:
            bits.append(f"RTL gain {rx['gain_db']:g} dB")
        if not self._pi_online():
            bits.append("Pi is not checking in")
            return " · ".join(bits)
        if self._pi_flushing():
            bits.append("Pi reconnected; applying queued results")
        record = self.current
        burst = rx.get("last_burst") if isinstance(rx.get("last_burst"), dict) else None
        started = None
        if record is not None:
            started = record.started_at or record.requested_at
        if burst and _iso_at_least(burst.get("at"), started):
            bits.append(_format_burst(burst))
            fate = burst.get("fate")
            if fate == "off_frequency":
                bits.append("that energy is not your frame")
            elif fate == "too_short":
                bits.append("too short to be a full frame")
            elif fate == "too_long":
                bits.append("interferer, not a frame")
            elif fate == "queued":
                bits.append("decode may still be running")
        elif (
            record is not None
            and record.state in {
                "waiting_for_pi",
                "transmitting",
                "not_heard",
            }
            and not record.uplink_gap
            and not self._pi_flushing()
        ):
            bits.append("Pi has not heard a burst since this send")
        elif burst:
            bits.append(_format_burst(burst))
        return " · ".join(bits)

    def _expire_stale_wait(self) -> None:
        record = self.current
        if record is None or record.state != "waiting_for_pi":
            return
        if not record.finished_at or not self._pi_online():
            return
        # Queued POSTs land just after the reconnect heartbeat. A wait
        # that spanned an uplink hole is not an RF miss; leave it queued
        # until the Pi's JSONL report arrives and overwrites the state.
        if self._pi_flushing() or record.uplink_gap:
            return
        try:
            finished = datetime.fromisoformat(record.finished_at)
        except ValueError:
            return
        now = datetime.now(timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if (now - finished).total_seconds() < WAIT_FOR_PI_SECONDS:
            return
        detail = self._wait_detail()
        record.state = "not_heard"
        record.error = detail
        record.report = {
            "success": False,
            "reason": detail or "Pi was online but did not decode this frame",
        }
        if self.pi_rx.get("gain_db") is not None:
            record.report["gain_db"] = self.pi_rx["gain_db"]
        self._write_record(record)

    def _write_record(self, record: TxRecord) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")


_PLAUSIBLE_TONE_HZ = 400.0
_PLAUSIBLE_HOLD_SECONDS = 2.5


def _iso_at_least(stamp: Any, baseline: str | None) -> bool:
    if not stamp or not baseline:
        return False
    try:
        left = datetime.fromisoformat(str(stamp))
        right = datetime.fromisoformat(baseline)
    except ValueError:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left >= right


_FATE_ENGLISH = {
    "queued": "queued for decode",
    "too_short": "ignored, too short",
    "too_long": "ignored, too long",
    "off_frequency": "ignored, off frequency",
    "queue_full": "dropped, decoder busy",
}


def _format_burst(burst: dict[str, Any]) -> str:
    bits: list[str] = []
    if burst.get("held_s") is not None:
        bits.append(f"{burst['held_s']}s")
    if burst.get("tone_hz") is not None:
        bits.append(f"{burst['tone_hz']:+.0f} Hz")
    if burst.get("snr_db") is not None:
        bits.append(f"SNR {burst['snr_db']} dB")
    fate = _FATE_ENGLISH.get(str(burst.get("fate") or ""), "")
    if fate:
        bits.append(fate)
    return "last burst " + " ".join(bits)


def _report_targets_record(
    record: TxRecord, msg_id: Any, report: dict[str, Any]
) -> bool:
    if msg_id is None:
        return True
    try:
        reported = int(msg_id)
    except (TypeError, ValueError):
        return False
    if record.msg_id == reported:
        return True
    # Light frames do not carry msg_id. The decoder reports 0; match the
    # still-open shout by the text that actually came off the air, not an
    # older decoded copy of the same range-01 string.
    if record.light and reported == 0:
        if record.state not in _OPEN_FOR_REPORT:
            return False
        text = report.get("text")
        return text is None or text == record.payload
    return False


def _looks_like_a_frame(report: dict[str, Any]) -> bool:
    """Judge whether an unidentified failure could be the operator's frame.

    Reports predating the tone/hold fields carry neither, and are trusted so
    an older Pi build still reports something rather than nothing.
    """
    tone = report.get("tone_hz")
    if tone is not None and abs(float(tone)) > _PLAUSIBLE_TONE_HZ:
        return False
    held = report.get("held_s")
    if held is not None and float(held) < _PLAUSIBLE_HOLD_SECONDS:
        return False
    return True


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    value = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_miles * math.asin(math.sqrt(value))


class Handler(BaseHTTPRequestHandler):
    controller: Controller

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return self.headers.get("X-Control-Token") == self.controller.control_token

    def do_GET(self) -> None:
        if self.path == "/":
            data = self.controller.page.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/status":
            self._send_json(self.controller.snapshot())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            body = self._json_body()
            if self.path == "/api/report":
                if self.headers.get("X-Report-Token") != self.controller.report_token:
                    self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                self.controller.accept_report(body)
                self._send_json(self.controller.report_ack())
                return
            if not self._authorized():
                self._send_json({"error": "stale or missing control token"}, HTTPStatus.FORBIDDEN)
                return
            if self.path == "/api/stop":
                self.controller.stop()
            elif self.path == "/api/rx-gain":
                raw = body.get("gain_db")
                self.controller.set_rx_gain(None if raw is None else float(raw))
            elif self.path == "/api/send":
                record = self.controller.send(
                    body.get("payload", ""),
                    float(body.get("watts", 0)),
                    int(body.get("frequency_hz", DEFAULT_FREQUENCY)),
                    bool(body.get("filter_confirmed")),
                    light=bool(body.get("light")),
                )
                self._send_json(asdict(record), HTTPStatus.ACCEPTED)
                return
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json(self.controller.snapshot())
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Field TX</title><style>
:root{color-scheme:dark;--bg:#07111d;--card:#102237;--line:#28445f;--text:#eef6ff;--muted:#9eb4c9;--ok:#35d07f;--bad:#ff5f62;--accent:#56a8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui}
main{max-width:720px;margin:auto;padding:16px}.top{display:flex;justify-content:space-between;align-items:center}.pill{padding:7px 11px;border-radius:20px;background:#25384b;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0}
label{display:block;color:var(--muted);font-size:13px;margin:12px 0 6px}textarea,input{width:100%;background:#091725;border:1px solid #36536e;color:var(--text);border-radius:10px;padding:12px;font:inherit}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.power{display:grid;grid-template-columns:1fr 95px;gap:10px;align-items:center}
button{border:0;border-radius:12px;padding:14px;font-weight:800;font-size:16px;background:var(--accent);color:#03111d}
button:disabled{opacity:.35}.row{display:flex;gap:10px}.row button{flex:1}.stop{background:var(--bad);color:white}.ghost{background:#30465a;color:white}
.result{font-size:19px;font-weight:750}.muted{color:var(--muted);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.history{font-size:13px;border-top:1px solid var(--line);padding:9px 0}.check{display:flex;gap:9px;align-items:center;margin-top:18px}.check input{width:auto}
.hex{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;color:var(--muted);margin-top:6px}
@media(max-width:520px){.grid{grid-template-columns:1fr}.row{position:sticky;bottom:8px}.stop{min-width:120px}}
.sendbtn{width:100%;padding:20px;font-size:20px;margin-top:22px}
.bigval{font-size:30px;font-weight:800;margin:4px 0 10px}
.presets{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{flex:1;min-width:70px;padding:10px 8px;font-size:14px;font-weight:700;background:#30465a;color:#fff}
.chip.on{background:var(--accent);color:#03111d}
.meters{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;text-align:center}
.meters .bigval{font-size:22px;margin:2px 0 0}
</style></head><body><main>
<div class="top"><div><b>Hermes Field TX</b><div class="muted" id="links">loading…</div></div></div>
<section class="card" id="piCard">
<div class="muted" id="piLabel">Pi receiver</div>
<div class="meters">
<div><div class="muted">gain</div><div class="bigval" id="rxGain">—</div></div>
<div><div class="muted">SNR now</div><div class="bigval" id="rxSnr">—</div></div>
<div><div class="muted">tone</div><div class="bigval" id="rxTone">—</div></div>
</div>
<div class="muted" id="rxExtra"></div>
</section>
<section class="card">
<label>Message</label><textarea id="payload" rows="2">range-01</textarea>
<label>TX power</label>
<div class="bigval" id="wattLabel">1 mW</div>
<input id="powerRange" type="range" min="1" max="5000" step="1" value="1">
<div class="presets"><button class="chip" data-mw="1">1 mW</button><button class="chip" data-mw="10">10 mW</button><button class="chip" data-mw="100">100 mW</button><button class="chip" data-mw="1000">1 W</button><button class="chip" data-mw="2000">2 W</button><button class="chip" data-mw="3000">3 W</button><button class="chip" data-mw="4000">4 W</button><button class="chip" data-mw="5000">5 W</button></div>
<!--FILTER_START--><label class="check" id="filterRow"><input id="filter" type="checkbox" checked> 10 m output filter is installed</label><!--FILTER_END-->
<button class="sendbtn" id="send">SEND</button>
<button class="sendbtn stop" id="stop" hidden>STOP TRANSMITTING</button>
<div class="muted" id="advancedToggle" style="margin-top:12px;text-decoration:underline">advanced</div>
<div id="advanced" hidden>
<label>Frequency (Hz)</label><input id="frequency" type="number" value="28124000">
<label>RTL gain</label>
<div class="presets" id="gainChips"><button class="chip" data-gain="auto">Auto</button><button class="chip" data-gain="0">0</button><button class="chip" data-gain="14.4">14.4</button><button class="chip" data-gain="29.7">29.7</button><button class="chip" data-gain="36.4">36.4</button><button class="chip" data-gain="42.1">42.1</button></div>
<div class="muted" id="gainHint">Auto — Pi walks gain from SNR and noise</div>
<label class="check"><input id="light" type="checkbox"> light mode — callsign + message only (no dest hash / msg id)</label>
<div class="muted">Test frame. Drops the 16-byte dest and other unused fields. Codec, callsign, and text stay. Shorter airtime.</div>
</div>
</section>
<section class="card"><div class="result" id="result">Ready.</div><div class="muted" id="details"></div></section>
<section class="card"><b>Recent frames</b><div id="history"></div></section>
</main><script>
const MY_UI="__UI_VERSION__";
let token=""; const $=id=>document.getElementById(id);
function num(v){return Number(v).toFixed(1).replace(/\.0$/,"")}
function piOffline(s){return s.pi_age_seconds!=null&&s.pi_age_seconds>s.pi_offline_after_seconds}
function rtlStale(s){return s.pi_rx_age_seconds!=null&&s.pi_rx_age_seconds>s.rtl_stale_after_seconds}
// A queued report can arrive minutes late once the Pi reconnects; unlabelled,
// it reads as a live answer to whatever was just sent.
function reportLag(c){if(!c.reported_at||!c.requested_at)return"";
let d=(new Date(c.reported_at)-new Date(c.requested_at))/1000;
return d>60?`reported ${Math.round(d/60)} min late`:""}
function esc(s){let d=document.createElement("div");d.textContent=s;return d.innerHTML}
function hexSpaced(h){return(h||"").replace(/../g,"$& ").trim()}
function frameBlock(x){if(!x.frame_hex)return"";
let dest=x.light?"light":(x.frame_dest||"");
let size=[x.frame_bytes!=null?`${x.frame_bytes} B inner`:"",x.on_air_bytes!=null?`${x.on_air_bytes} B on air`:""].filter(Boolean).join(" · ");
let rns=x.light?"":(x.lxmf_title?`ingress LXMF: title ${esc(x.lxmf_title)} · dest ${esc(dest)} · "${esc(x.payload||"")}"`:"");
return `<div class="hex">${esc((x.frame_origin||"")+(x.light?"  light":" → "+dest+"  msg "+x.msg_id)+(size?"  "+size:""))}</div><div class="hex">${esc(hexSpaced(x.frame_hex))}</div>`+(rns?`<div class="hex">${rns}</div>`:"")}
function fmtPower(mw){return mw<1000?`${Math.round(mw)} mW`:`${(mw/1000).toFixed(mw%1000?2:0)} W`}
function syncPower(mw){$("powerRange").value=mw;$("wattLabel").textContent=fmtPower(+mw);
document.querySelectorAll(".chip[data-mw]").forEach(b=>b.classList.toggle("on",+b.dataset.mw===+mw))}
$("powerRange").oninput=()=>syncPower($("powerRange").value);
document.querySelectorAll(".chip[data-mw]").forEach(b=>b.onclick=()=>syncPower(b.dataset.mw));
function syncGain(v){document.querySelectorAll("#gainChips .chip").forEach(b=>b.classList.toggle("on",b.dataset.gain===v))}
async function setGain(v){await post("/api/rx-gain",v==="auto"?{gain_db:null}:{gain_db:+v});syncGain(v)}
document.querySelectorAll("#gainChips .chip").forEach(b=>b.onclick=()=>setGain(b.dataset.gain).catch(e=>alert(e.message)));
$("advancedToggle").onclick=()=>{$("advanced").hidden=!$("advanced").hidden};
try{$("light").checked=localStorage.getItem("hfLight")==="1"}catch(e){}
$("light").onchange=()=>{try{localStorage.setItem("hfLight",$("light").checked?"1":"")}catch(e){}};
syncPower(1);
async function post(path,body={}){let r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":token},body:JSON.stringify(body)});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
$("send").onclick=async()=>{try{await post("/api/send",{payload:$("payload").value,watts:+$("powerRange").value/1000,frequency_hz:+$("frequency").value,filter_confirmed:$("filter")?$("filter").checked:false,light:$("light").checked});await poll()}catch(e){alert(e.message)}};
$("stop").onclick=async()=>{try{await post("/api/stop");await poll()}catch(e){alert(e.message)}};
function render(s){token=s.control_token;
// A stale tab must not keep showing an old layout; the server decides.
if(s.ui_version&&s.ui_version!==MY_UI){location.reload();return}
let sending=s.state==="TRANSMITTING";$("send").hidden=sending;$("stop").hidden=!sending;
let filt=s.filter_installed?"10 m filter fitted":"filter unconfirmed";
let off=piOffline(s);
let stale=rtlStale(s);
let pi=s.pi_last_report_at?(off?`PI UPLINK DOWN ${Math.round(s.pi_age_seconds)}s`:stale?`RTL STALLED ${Math.round(s.pi_rx_age_seconds)}s — recovering`:`Pi ok ${new Date(s.pi_last_report_at).toLocaleTimeString()}`):"Pi has not checked in";
let noise=s.noise_reports?` · ${s.noise_reports} noise ignored`:"";
$("links").innerHTML=`Hermes ${s.hl2_ip} · ${filt} · <span class="${off||stale?"bad":""}">${pi}</span>${noise}`;
let rx=s.pi_rx||{};
$("rxGain").textContent=rx.gain_db!=null?`${Number(rx.gain_db).toFixed(1).replace(/\.0$/,"")} dB`:"—";
$("rxSnr").textContent=!stale&&rx.snr_db!=null?`${num(rx.snr_db)} dB`:"—";
$("rxSnr").className="bigval"+(rx.snr_db>=20?" ok":rx.snr_db!=null&&rx.snr_db<10?" bad":"");
$("rxTone").textContent=!stale&&rx.tone_hz!=null?`${rx.tone_hz>=0?"+":""}${Math.round(rx.tone_hz)} Hz`:"—";
let extra=[];
if(stale)extra.push("RTL STALLED — AUTO-RECOVERING");
if(rx.clipping)extra.push("CLIPPING");
if(rx.bursting)extra.push("IN BURST");
if(s.pi_miles!=null)extra.push(`${num(s.pi_miles)} mi`);
if(s.wait_detail&&!off)extra.push(s.wait_detail);
$("rxExtra").textContent=extra.filter(Boolean).join(" · ")||(off?"no uplink":"listening");
$("piLabel").textContent=off?"Pi receiver (offline)":stale?"Pi receiver (RTL stalled)":"Pi receiver";
let ov=s.rx_gain_db;
syncGain(ov==null?"auto":String(ov));
$("gainHint").textContent=stale?`Requested ${ov==null?"Auto":ov+" dB"}; waiting for RTL recovery.`:ov==null?"Auto — Pi walks gain from SNR and noise. Takes ~2 s to apply.":`Pinned at ${ov} dB. Live card is what the dongle is using. CLIPPING means tap a lower chip. Auto to resume.`;
let c=s.current;if(!c){$("result").className="result";$("result").textContent="Ready.";$("details").textContent=""}
else{let p=fmtPower(c.watts*1000);let r=c.report;if(r){$("result").className="result "+(r.success?"ok":"bad");$("result").textContent=r.success?"DECODED BY PI":"PI DECODE FAILED";
let got=r.text!=null?(r.text_matches===false?`got "${r.text}" (SENT "${c.payload}")`:`"${r.text}"`):"";
let lag=reportLag(c);
$("details").innerHTML=[got?`<b>${esc(got)}</b>`:"",p,r.snr_db!=null?`SNR ${num(r.snr_db)} dB`:"",r.distance_miles!=null?`${num(r.distance_miles)} mi`:"",r.success?"":esc(r.reason||""),lag].filter(Boolean).join(" · ")}
else if(c.state==="transmitting"){$("result").className="result";$("result").textContent=`Transmitting… ${c.airtime_seconds}s`;$("details").textContent=p}
else if(c.state==="waiting_for_pi"){
if(off){$("result").className="result bad";$("result").textContent="PI UPLINK DOWN";
$("details").textContent=`${p} · frame was sent; the Pi queued the result and will post it when the hotspot comes back`}
else if(c.uplink_gap||s.pi_flushing){$("result").className="result";$("result").textContent="Sent — waiting for queued Pi result…";
$("details").textContent=[p,`${c.airtime_seconds}s air`,s.wait_detail||""].filter(Boolean).join(" · ")}
else{$("result").className="result";$("result").textContent="Sent — waiting for Pi…";
$("details").textContent=[p,`${c.airtime_seconds}s air`,s.wait_detail||""].filter(Boolean).join(" · ")}}
else if(c.state==="not_heard"){$("result").className="result bad";$("result").textContent="NOT HEARD BY PI";
$("details").textContent=[p,esc(c.error||s.wait_detail||"Pi was online but did not decode this frame")].filter(Boolean).join(" · ")}
else if(c.state==="failed"){$("result").className="result bad";$("result").textContent="TX FAILED";$("details").textContent=c.error||""}
else{$("result").className="result";$("result").textContent=c.state;$("details").textContent=p}}
if(c){let dump=frameBlock(c);if(dump)$("details").innerHTML+=dump}
$("history").innerHTML=s.history.map(x=>{let st=x.state;if(st==="waiting_for_pi")st=x.uplink_gap?"queued (Pi uplink)":"waiting for Pi";return `<div class="history"><b>${fmtPower(x.watts*1000)}</b> · ${esc(x.payload||"")} · ${st}${x.report&&x.report.snr_db!=null?` · ${num(x.report.snr_db)} dB`:""}${x.report&&x.report.distance_miles!=null?` · ${num(x.report.distance_miles)} mi`:""}${frameBlock(x)}</div>`}).join("")}
async function poll(){try{let r=await fetch("/api/status",{cache:"no-store"});render(await r.json())}catch(e){$("result").textContent="app offline: "+e.message}}
poll();setInterval(poll,1000);
</script></body></html>"""


def render_page(filter_installed: bool) -> tuple[str, str]:
    """Return the page for this configuration and its version stamp.

    A permanently fitted filter means the confirmation control is removed from
    the document, not merely hidden, so no stale styling can resurrect it.
    """
    html = HTML
    if filter_installed:
        start = html.index(_FILTER_START)
        end = html.index(_FILTER_END) + len(_FILTER_END)
        html = html[:start] + html[end:]
    version = hashlib.sha256(html.encode()).hexdigest()[:12]
    return html.replace("__UI_VERSION__", version), version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hl2-ip", required=True)
    parser.add_argument("--callsign", required=True)
    parser.add_argument("--destination", default="00" * 16)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path, default=Path("hf-tx-control.jsonl"))
    parser.add_argument("--report-token-file", type=Path, default=Path(".hf-report-token"))
    parser.add_argument(
        "--filter-installed",
        action="store_true",
        help="a 10 m transmit low-pass filter is permanently fitted (N2ADR board)",
    )
    parser.add_argument("--tx-lat", type=float)
    parser.add_argument("--tx-lon", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("txcontrol must bind to localhost; publish it with Tailscale Serve")
    if args.report_token_file.exists():
        report_token = args.report_token_file.read_text().strip()
    else:
        report_token = secrets.token_urlsafe(32)
        args.report_token_file.write_text(report_token + "\n")
        args.report_token_file.chmod(0o600)
    controller = Controller(
        hl2_ip=args.hl2_ip,
        callsign=args.callsign,
        destination=args.destination,
        log_path=args.log,
        report_token=report_token,
        filter_installed=args.filter_installed,
        tx_lat=args.tx_lat,
        tx_lon=args.tx_lon,
    )
    Handler.controller = controller
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Hermes controller: http://{args.host}:{args.port}", flush=True)
    print("TX controller ready. Tailscale Serve may publish this localhost URL.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.disarm()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
