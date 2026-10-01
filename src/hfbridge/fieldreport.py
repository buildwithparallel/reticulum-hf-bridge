"""Send decode reports with a durable JSONL retry queue.

Reports carry the decoded text so the operator can confirm the bytes that
survived the path are the bytes they sent, which a msg_id match alone cannot
prove.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class DecodeEvent:
    msg_id: int | None
    success: bool
    reason: str
    snr_db: float
    gain_db: float | None = None
    text: str = ""
    tone_hz: float | None = None
    held_s: float | None = None
    timestamp: str = ""

    def report(self) -> dict[str, Any]:
        report = asdict(self)
        if not report["timestamp"]:
            report["timestamp"] = datetime.now(timezone.utc).isoformat()
        return report


class ChannelStatus:
    """Live receiver snapshot for heartbeats: gain, last burst, why it was kept or dropped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._data.update(fields)
            self._data["updated_at"] = datetime.now(timezone.utc).isoformat()

    def note_burst(
        self,
        *,
        snr_db: float,
        tone_hz: float,
        held_s: float,
        fate: str,
        gain_db: float | None = None,
    ) -> None:
        burst = {
            "snr_db": round(float(snr_db), 1),
            "tone_hz": round(float(tone_hz), 0),
            "held_s": round(float(held_s), 2),
            "fate": fate,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        fields: dict[str, Any] = {"last_burst": burst}
        if gain_db is not None:
            fields["gain_db"] = gain_db
        self.update(**fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


def latest_gps_fix(path: Path | None) -> dict[str, Any] | None:
    """Return the last valid fix from gpslog's CSV."""
    if path is None:
        return None
    try:
        with path.open(newline="") as src:
            rows = csv.DictReader(src)
            latest = None
            for row in rows:
                if row.get("lat") and row.get("lon"):
                    latest = row
    except (OSError, csv.Error):
        return None
    if latest is None:
        return None
    try:
        fix: dict[str, Any] = {
            "lat": float(latest["lat"]),
            "lon": float(latest["lon"]),
        }
        if latest.get("speed_mph"):
            fix["speed_mph"] = float(latest["speed_mph"])
    except (KeyError, ValueError):
        return None
    if latest.get("iso_utc"):
        fix["timestamp"] = latest["iso_utc"]
    return fix


class FieldReporter:
    """Persist decode events, then POST queued reports in order."""

    def __init__(
        self,
        url: str,
        queue_path: Path,
        *,
        gps_csv: Path | None = None,
        token: str = "",
        timeout: float = 5.0,
        retry_interval: float = 2.0,
        heartbeat_interval: float = 10.0,
        status: ChannelStatus | None = None,
    ) -> None:
        self.url = url
        self.queue_path = queue_path
        self.gps_csv = gps_csv
        self.token = token
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.heartbeat_interval = heartbeat_interval
        self.status = status
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closing = False
        self._rx_gain_db: float | None = None
        self._thread = threading.Thread(
            target=self._run, name="hf-field-report", daemon=True
        )
        self._thread.start()
        self._wake.set()

    def submit(self, event: DecodeEvent) -> None:
        report = event.report()
        report["gps"] = latest_gps_fix(self.gps_csv)
        encoded = json.dumps(report, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self.queue_path.open("a", encoding="utf-8") as out:
                out.write(encoded + "\n")
        self._wake.set()

    def rx_gain_override(self) -> float | None:
        with self._lock:
            return self._rx_gain_db

    def close(self, timeout: float = 10.0) -> None:
        self._closing = True
        self._wake.set()
        self._thread.join(timeout)

    def _queued(self) -> list[str]:
        try:
            with self.queue_path.open(encoding="utf-8") as src:
                return [line.rstrip("\n") for line in src if line.strip()]
        except OSError:
            return []

    def _replace_queue(self, lines: list[str]) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.queue_path.with_suffix(self.queue_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as out:
            for line in lines:
                out.write(line + "\n")
        temporary.replace(self.queue_path)

    def _post(self, encoded: str) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Report-Token"] = self.token
        request = Request(
            self.url,
            data=encoded.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            if not 200 <= response.status < 300:
                raise OSError(f"controller returned HTTP {response.status}")
            raw = response.read() if hasattr(response, "read") else b""
        if not raw:
            return
        try:
            body = json.loads(raw.decode())
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(body, dict) or "rx_gain_db" not in body:
            return
        value = body["rx_gain_db"]
        with self._lock:
            self._rx_gain_db = None if value is None else float(value)

    def _flush(self) -> None:
        while True:
            with self._lock:
                queued = self._queued()
            if not queued:
                return
            first = queued[0]
            try:
                json.loads(first)
            except ValueError:
                with self._lock:
                    current = self._queued()
                    if current and current[0] == first:
                        self._replace_queue(current[1:])
                continue
            try:
                self._post(first)
            except OSError:
                return
            with self._lock:
                current = self._queued()
                if current and current[0] == first:
                    self._replace_queue(current[1:])

    def _run(self) -> None:
        last_heartbeat = time.monotonic()
        while True:
            self._wake.wait(min(self.retry_interval, self.heartbeat_interval))
            self._wake.clear()
            self._flush()
            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_interval:
                heartbeat = {
                    "kind": "heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "gps": latest_gps_fix(self.gps_csv),
                    "rx": self.status.snapshot() if self.status is not None else {},
                }
                try:
                    self._post(json.dumps(heartbeat, separators=(",", ":")))
                except OSError:
                    pass
                last_heartbeat = now
            if self._closing:
                return
