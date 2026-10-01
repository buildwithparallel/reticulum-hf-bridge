import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hfbridge.frame import VERSION_LIGHT, Frame
from hfbridge.txcontrol import Controller, TxRecord, _drive_for_watts


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = Controller(
            hl2_ip="192.0.2.1",
            callsign="N0CALL",
            destination="00" * 16,
            log_path=Path(self.temp.name) / "tx.jsonl",
            report_token="secret",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_starts_idle_and_send_requires_filter_confirmation(self):
        self.assertEqual(self.controller.snapshot()["state"], "IDLE")
        with self.assertRaises(ValueError):
            self.controller.send("hello", 0.1, 28_124_000, False)
        self.assertEqual(self.controller.snapshot()["state"], "IDLE")

    def test_filter_confirmation_is_checked_by_default_in_ui(self):
        self.assertIn('id="filter" type="checkbox" checked', self.controller.page)

    def test_permanently_fitted_filter_satisfies_the_interlock(self):
        self.controller.filter_installed = True
        with patch.object(self.controller, "_transmit"):
            record = self.controller.send("hello", 0.1, 28_124_000, False)

        self.assertEqual(record.payload, "hello")
        self.assertEqual(record.frame_origin, "N0CALL")
        self.assertEqual(record.frame_dest, "00" * 16)
        self.assertEqual(record.lxmf_title, "hfvia:N0CALL")
        decoded = Frame.decode(bytes.fromhex(record.frame_hex))
        self.assertEqual(decoded.payload, b"hello")
        self.assertEqual(decoded.origin, "N0CALL")
        self.assertEqual(decoded.msg_id, record.msg_id)
        self.assertEqual(record.frame_bytes, len(bytes.fromhex(record.frame_hex)))
        self.assertGreater(record.on_air_bytes, record.frame_bytes)
        self.assertTrue(self.controller.snapshot()["filter_installed"])
        # The on-air frame only has two bytes for msg_id.
        self.assertLessEqual(record.msg_id, 0xFFFF)

    def test_light_mode_omits_dest_from_the_air(self):
        self.controller.filter_installed = True
        with patch.object(self.controller, "_transmit"):
            record = self.controller.send(
                "zz", 0.01, 28_124_000, False, light=True
            )
        self.controller.state = "IDLE"
        with patch.object(self.controller, "_transmit"):
            full = self.controller.send(
                "zz", 0.01, 28_124_000, False, light=False
            )

        self.assertTrue(record.light)
        self.assertIsNone(record.frame_dest)
        decoded = Frame.decode(bytes.fromhex(record.frame_hex))
        self.assertEqual(decoded.version, VERSION_LIGHT)
        self.assertEqual(decoded.payload, b"zz")
        self.assertLess(len(record.frame_hex), len(full.frame_hex))
        self.assertLess(record.frame_bytes, full.frame_bytes)
        self.assertLess(record.on_air_bytes, full.on_air_bytes)
        self.assertLess(record.airtime_seconds, full.airtime_seconds)

    def test_stop_never_touches_an_unkeyed_radio(self):
        with patch("hfbridge.txcontrol.emergency_stop") as stop:
            self.controller.stop()
            self.controller.disarm()

        stop.assert_not_called()
        self.assertEqual(self.controller.snapshot()["state"], "IDLE")

    def test_stop_reaches_the_radio_once_a_frame_has_keyed(self):
        self.controller.keyed_since_start = True
        with patch("hfbridge.txcontrol.emergency_stop") as stop:
            self.controller.stop()

        stop.assert_called_once()

    def test_rx_gain_override_snaps_to_tuner_steps(self):
        self.assertEqual(self.controller.snapshot()["rx_gain_db"], 0.0)
        self.assertEqual(self.controller.report_ack()["rx_gain_db"], 0.0)
        self.assertIsNone(self.controller.set_rx_gain(None))
        self.assertEqual(self.controller.set_rx_gain(20), 14.4)
        self.assertEqual(self.controller.snapshot()["rx_gain_db"], 14.4)
        self.assertEqual(self.controller.report_ack()["rx_gain_db"], 14.4)
        self.assertIsNone(self.controller.set_rx_gain(None))
        self.assertIsNone(self.controller.snapshot()["rx_gain_db"])

    def test_ui_power_spans_one_milliwatt_to_five_watts(self):
        self.assertGreaterEqual(_drive_for_watts(0.001), 1)
        self.assertEqual(_drive_for_watts(5.0), 255)
        with self.assertRaises(ValueError):
            _drive_for_watts(5.01)
        with self.assertRaises(ValueError):
            _drive_for_watts(0.0005)

    def _waiting_record(self, payload: str = "range-08") -> TxRecord:
        record = TxRecord(
            msg_id=42,
            payload=payload,
            watts=0.1,
            drive=36,
            frequency_hz=28_124_000,
            airtime_seconds=9.0,
            requested_at="now",
            state="waiting_for_pi",
        )
        self.controller.history.append(record)
        self.controller.current = record
        return record

    def test_light_decode_with_zero_msg_id_still_matches(self):
        record = self._waiting_record("range-01")
        record.light = True
        record.msg_id = 36361

        self.controller.accept_report(
            {
                "msg_id": 0,
                "success": True,
                "snr_db": 33.7,
                "text": "range-01",
                "tone_hz": -55.0,
                "held_s": 8.08,
            }
        )

        self.assertEqual(record.state, "decoded")
        self.assertTrue(record.report["text_matches"])
        self.assertEqual(record.report["snr_db"], 33.7)

    def test_pi_report_is_correlated_and_keeps_only_known_fields(self):
        record = self._waiting_record()

        self.controller.accept_report(
            {
                "msg_id": 42,
                "success": True,
                "snr_db": 8.5,
                "text": "range-08",
                "unexpected": "must not cross the report boundary",
            }
        )

        self.assertEqual(record.state, "decoded")
        self.assertNotIn("unexpected", record.report)
        self.assertTrue(record.report["text_matches"])

    def test_decoded_text_is_reported_and_compared_to_what_was_sent(self):
        record = self._waiting_record("range-08")

        self.controller.accept_report(
            {"msg_id": 42, "success": True, "snr_db": 8.5, "text": "range-0X"}
        )

        self.assertEqual(record.report["text"], "range-0X")
        self.assertFalse(record.report["text_matches"])

    def test_noise_burst_is_not_reported_as_the_operators_frame(self):
        record = self._waiting_record()

        # Car ignition noise: off-frequency and far too short to be a frame.
        self.controller.accept_report(
            {
                "msg_id": None,
                "success": False,
                "reason": "frame shorter than LEN",
                "snr_db": 7.1,
                "tone_hz": -944.0,
                "held_s": 5.8,
            }
        )

        self.assertEqual(record.state, "waiting_for_pi")
        self.assertIsNone(record.report)
        self.assertEqual(self.controller.noise_reports, 1)

    def test_on_frequency_failure_is_still_attributed(self):
        record = self._waiting_record()

        self.controller.accept_report(
            {
                "msg_id": None,
                "success": False,
                "reason": "crc mismatch",
                "snr_db": 9.0,
                "tone_hz": -55.0,
                "held_s": 9.4,
            }
        )

        self.assertEqual(record.state, "decode_failed")

    def test_a_proven_decode_is_not_undone_by_a_later_noise_failure(self):
        record = self._waiting_record()
        self.controller.accept_report(
            {"msg_id": 42, "success": True, "snr_db": 40.0, "text": "range-08"}
        )
        self.assertEqual(record.state, "decoded")

        self.controller.accept_report(
            {
                "msg_id": None,
                "success": False,
                "reason": "payload truncated after sync",
                "snr_db": 6.0,
                "tone_hz": -55.0,
                "held_s": 9.0,
            }
        )

        self.assertEqual(record.state, "decoded")
        self.assertTrue(record.report["success"])

    def test_reports_without_tone_fields_are_still_accepted(self):
        record = self._waiting_record()

        self.controller.accept_report(
            {"msg_id": None, "success": False, "reason": "crc mismatch", "snr_db": 9.0}
        )

        self.assertEqual(record.state, "decode_failed")

    def test_pi_contact_age_is_reported_so_offline_is_distinguishable(self):
        self.assertIsNone(self.controller.snapshot()["pi_age_seconds"])

        self.controller.accept_report({"kind": "heartbeat"})

        self.assertLess(self.controller.snapshot()["pi_age_seconds"], 5.0)

    def test_report_arrival_time_is_recorded_separately(self):
        record = self._waiting_record()

        self.controller.accept_report(
            {
                "msg_id": 42,
                "success": True,
                "snr_db": 40.0,
                "timestamp": "2026-08-26T23:58:37+00:00",
            }
        )

        self.assertIsNotNone(record.reported_at)
        self.assertNotEqual(record.reported_at, record.report["timestamp"])

    def test_snr_is_rounded_for_display(self):
        record = self._waiting_record()

        self.controller.accept_report(
            {
                "msg_id": 42,
                "success": True,
                "snr_db": 18.83333333333333,
                "gain_db": 42.10000000000001,
            }
        )

        self.assertEqual(record.report["snr_db"], 18.8)
        self.assertEqual(record.report["gain_db"], 42.1)

    def test_heartbeat_exposes_receiver_status(self):
        self.controller.accept_report(
            {
                "kind": "heartbeat",
                "rx": {
                    "gain_db": 14.4,
                    "last_burst": {
                        "at": "2026-08-27T12:00:05+00:00",
                        "snr_db": 9.0,
                        "tone_hz": -55.0,
                        "held_s": 4.2,
                        "fate": "too_short",
                    },
                },
            }
        )
        snap = self.controller.snapshot()
        self.assertEqual(snap["pi_rx"]["gain_db"], 14.4)
        self.assertLess(snap["pi_rx_age_seconds"], 5.0)

        self.controller.last_pi_rx_update_monotonic = time.monotonic() - 11.0
        snap = self.controller.snapshot()
        self.assertGreater(
            snap["pi_rx_age_seconds"], snap["rtl_stale_after_seconds"]
        )

    def test_heartbeat_gps_becomes_live_distance(self):
        self.controller.tx_lat = 10.0
        self.controller.tx_lon = 20.0
        self.controller.accept_report(
            {
                "kind": "heartbeat",
                "rx": {"gain_db": 14.4, "snr_db": 3.2, "tone_hz": -55.0},
                "gps": {"lat": 10.0, "lon": 20.0},
            }
        )
        snap = self.controller.snapshot()
        self.assertEqual(snap["pi_miles"], 0.0)
        self.assertEqual(snap["pi_rx"]["snr_db"], 3.2)

    def test_waiting_expires_as_not_heard_with_receiver_detail(self):
        record = self._waiting_record()
        record.started_at = "2026-08-27T12:00:00+00:00"
        record.finished_at = (
            datetime.now(timezone.utc) - timedelta(seconds=50)
        ).isoformat()
        self.controller.last_pi_contact_monotonic = time.monotonic()
        self.controller.pi_rx = {
            "gain_db": 0.0,
            "last_burst": {
                "at": "2026-08-27T12:00:05+00:00",
                "snr_db": 9.0,
                "tone_hz": -55.0,
                "held_s": 4.2,
                "fate": "too_short",
            },
        }

        snap = self.controller.snapshot()

        self.assertEqual(record.state, "not_heard")
        self.assertIn("RTL gain 0", snap["wait_detail"])
        self.assertIn("too short", snap["wait_detail"])

    def test_a_dropout_does_not_expire_as_not_heard(self):
        record = self._waiting_record()
        record.finished_at = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        self.controller.last_pi_contact_monotonic = time.monotonic() - 120.0

        snap = self.controller.snapshot()
        self.assertEqual(record.state, "waiting_for_pi")
        self.assertGreater(snap["pi_age_seconds"], 35.0)

        self.controller.accept_report({"kind": "heartbeat"})
        snap = self.controller.snapshot()

        self.assertEqual(record.state, "waiting_for_pi")
        self.assertTrue(record.uplink_gap)
        self.assertTrue(snap["pi_flushing"])

        self.controller.pi_online_since = time.monotonic() - 40.0
        snap = self.controller.snapshot()
        self.assertEqual(record.state, "waiting_for_pi")
        self.assertFalse(snap["pi_flushing"])

    def test_late_pi_report_overwrites_not_heard(self):
        record = self._waiting_record()
        record.state = "not_heard"
        record.uplink_gap = True
        record.error = "Pi was online but did not decode this frame"

        self.controller.accept_report(
            {
                "msg_id": 42,
                "success": True,
                "snr_db": 12.4,
                "text": "range-08",
            }
        )

        self.assertEqual(record.state, "decoded")
        self.assertTrue(record.report["success"])
        self.assertIsNone(record.error)

    def test_light_late_report_overwrites_not_heard(self):
        record = self._waiting_record("range-01")
        record.light = True
        record.msg_id = 36361
        record.state = "not_heard"

        self.controller.accept_report(
            {
                "msg_id": 0,
                "success": True,
                "snr_db": 11.0,
                "text": "range-01",
            }
        )

        self.assertEqual(record.state, "decoded")


if __name__ == "__main__":
    unittest.main()
