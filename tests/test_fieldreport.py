import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from hfbridge.fieldreport import DecodeEvent, FieldReporter, latest_gps_fix
from hfbridge.frame import Frame
from hfbridge.listen import _try_decode


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok":true}'


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


class FieldReporterTest(unittest.TestCase):
    def test_listener_emits_success_and_failure_metadata(self):
        events = []
        frame = Frame(
            origin="N0CALL",
            dest=bytes(16),
            payload=b"private message",
            msg_id=73,
        )
        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
                patch("hfbridge.listen.demodulate", return_value=frame),
            ):
                _try_decode(
                    np.ones(10, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=21.5,
                    gain_db=29.7,
                    on_decode_event=events.append,
                )
            with (
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
                patch(
                    "hfbridge.listen.demodulate",
                    side_effect=ValueError("CRC mismatch"),
                ),
            ):
                _try_decode(
                    np.ones(10, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=7.0,
                    gain_db=None,
                    on_decode_event=events.append,
                )

        self.assertEqual(events[0].msg_id, 73)
        self.assertTrue(events[0].success)
        self.assertEqual(events[0].snr_db, 21.5)
        self.assertEqual(events[0].gain_db, 29.7)
        self.assertEqual(events[0].text, "private message")
        self.assertIsNone(events[1].msg_id)
        self.assertFalse(events[1].success)
        self.assertEqual(events[1].reason, "CRC mismatch")

    def test_latest_gps_fix_reads_last_valid_row(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gps.csv"
            path.write_text(
                "iso_utc,lat,lon,speed_mph,payload\n"
                "2026-08-26T12:00:00+00:00,10.1,20.1,4.0,secret\n"
                "2026-08-26T12:00:01+00:00,10.2,20.2,5.0,secret2\n"
            )

            fix = latest_gps_fix(path)

        self.assertEqual(
            fix,
            {
                "lat": 10.2,
                "lon": 20.2,
                "speed_mph": 5.0,
                "timestamp": "2026-08-26T12:00:01+00:00",
            },
        )
        self.assertNotIn("payload", fix)

    def test_posts_allowlisted_report_without_payload_content(self):
        requests = []

        def send(request, timeout):
            requests.append((request, timeout))
            return _Response()

        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "reports.jsonl"
            with patch("hfbridge.fieldreport.urlopen", side_effect=send):
                reporter = FieldReporter(
                    "https://controller.invalid/decode",
                    queue,
                    timeout=1.25,
                    retry_interval=0.01,
                )
                reporter.submit(
                    DecodeEvent(
                        msg_id=42,
                        success=True,
                        reason="decoded",
                        snr_db=18.5,
                        gain_db=29.7,
                        timestamp="2026-08-26T12:00:00+00:00",
                    )
                )
                _wait_for(lambda: len(requests) == 1)
                reporter.close()

            body = json.loads(requests[0][0].data)

        self.assertEqual(
            set(body),
            {
                "gain_db",
                "gps",
                "held_s",
                "msg_id",
                "reason",
                "snr_db",
                "success",
                "text",
                "timestamp",
                "tone_hz",
            },
        )
        self.assertEqual(body["msg_id"], 42)
        self.assertIsNone(body["gps"])
        self.assertNotIn("payload", body)
        self.assertEqual(requests[0][1], 1.25)

    def test_heartbeat_includes_receiver_status(self):
        from hfbridge.fieldreport import ChannelStatus

        requests = []

        def send(request, timeout):
            requests.append(json.loads(request.data))
            return _Response()

        status = ChannelStatus()
        status.update(gain_db=14.4)
        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "reports.jsonl"
            with patch("hfbridge.fieldreport.urlopen", side_effect=send):
                reporter = FieldReporter(
                    "https://controller.invalid/decode",
                    queue,
                    heartbeat_interval=0.05,
                    retry_interval=0.05,
                    status=status,
                )
                _wait_for(lambda: any(r.get("kind") == "heartbeat" for r in requests))
                reporter.close()

        beats = [r for r in requests if r.get("kind") == "heartbeat"]
        self.assertTrue(beats)
        self.assertEqual(beats[0]["rx"]["gain_db"], 14.4)

    def test_controller_reply_pins_rtl_gain(self):
        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok":true,"rx_gain_db":14.4}'

        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "reports.jsonl"
            with patch("hfbridge.fieldreport.urlopen", return_value=Reply()):
                reporter = FieldReporter(
                    "https://controller.invalid/decode",
                    queue,
                    heartbeat_interval=0.05,
                    retry_interval=0.05,
                )
                _wait_for(lambda: reporter.rx_gain_override() == 14.4)
                reporter.close()

        self.assertEqual(reporter.rx_gain_override(), 14.4)

    def test_failed_post_stays_queued_and_retries(self):
        retry_started = threading.Event()
        retry_release = threading.Event()
        attempts = 0

        def send(_request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("offline")
            retry_started.set()
            retry_release.wait(timeout)
            return _Response()

        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "reports.jsonl"
            with patch("hfbridge.fieldreport.urlopen", side_effect=send) as post:
                reporter = FieldReporter(
                    "https://controller.invalid/decode",
                    queue,
                    retry_interval=10.0,
                )
                reporter.submit(
                    DecodeEvent(
                        msg_id=None,
                        success=False,
                        reason="CRC mismatch",
                        snr_db=8.0,
                    )
                )
                _wait_for(lambda: post.call_count >= 1)
                self.assertTrue(queue.read_text().strip())
                reporter._wake.set()
                self.assertTrue(retry_started.wait(1.0))
                retry_release.set()
                _wait_for(lambda: post.call_count == 2 and not queue.read_text())
                reporter.close()


if __name__ == "__main__":
    unittest.main()
