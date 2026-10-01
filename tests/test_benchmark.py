import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from hfbridge.airtext import air_text_error
from hfbridge.benchmark import (
    apply_mobile_fading,
    architecture,
    format_report,
    make_payload,
    parse_rx_log,
    payload_sizes,
    run_benchmark,
    run_on_air,
    score_on_air,
)
from hfbridge.frame import check_payload


class BenchmarkTest(unittest.TestCase):
    def test_architecture_names_the_poc_modem(self):
        arch = architecture()
        self.assertEqual(arch["modulation"], "2-CPFSK")
        self.assertIn("LDPC", arch["fec"])
        self.assertEqual(arch["baud"], 100)
        self.assertIn("CRC-16", arch["integrity"])
        self.assertEqual(arch["handshake"], "none")
        self.assertEqual(arch["rtl_gain_db"], 42.1)
        self.assertEqual(arch["receiver"], "RTL-SDR R820T")

    def test_default_plan_is_one_hundred_mixed_payloads(self):
        sizes = payload_sizes(100)
        self.assertEqual(len(sizes), 100)
        self.assertEqual(sizes.count(3), 25)
        self.assertEqual(sizes.count(200), 3)

    def test_payloads_are_legal_air_text(self):
        for size in (3, 8, 20, 50, 81, 162, 200):
            payload = make_payload(size, trial=7)
            self.assertEqual(len(payload), size)
            self.assertIsNone(air_text_error(payload.decode("ascii")))
            check_payload(payload)

    def test_clean_loopback_gets_everything_through(self):
        result = run_benchmark(count=8, seed=0)
        self.assertEqual(result["summary"]["got_through"], 8)
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertIn("LDPC", result["architecture"]["fec"])
        self.assertFalse(result["conditions"]["keys_rf"])
        report = format_report(result)
        self.assertIn("got through 8/8", report)
        self.assertIn("LDPC", report)

    def test_noise_benchmark_records_requested_snr(self):
        result = run_benchmark(count=8, snr_db=-8.0, seed=1)
        self.assertEqual(result["summary"]["sent"], 8)
        self.assertEqual(result["conditions"]["channel"], "awgn")
        self.assertEqual(result["conditions"]["snr_db"], -8.0)

    def test_mobile_fading_is_repeatable_and_power_normalized(self):
        iq = np.ones(6000, dtype=np.complex64)
        first = apply_mobile_fading(iq, np.random.default_rng(7))
        second = apply_mobile_fading(iq, np.random.default_rng(7))
        np.testing.assert_allclose(first, second)
        self.assertAlmostEqual(
            float(np.mean(np.abs(first) ** 2)),
            float(np.mean(np.abs(iq) ** 2)),
            places=5,
        )
        self.assertGreater(float(np.std(np.abs(first))), 0.05)

    def test_benchmark_records_mobile_fading_profile(self):
        result = run_benchmark(count=1, fading="mobile", seed=3)
        self.assertEqual(result["conditions"]["fading"], "mobile")
        self.assertIn("mobile", result["conditions"]["channel"])

    def test_json_round_trip(self):
        result = run_benchmark(count=4, seed=0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(json.dumps(result))
            loaded = json.loads(path.read_text())
        self.assertEqual(loaded["summary"]["got_through"], 4)

    def test_on_air_count_spreads_payload_sizes(self):
        sizes = payload_sizes(20)
        self.assertEqual(len(sizes), 20)
        self.assertGreater(len(set(sizes)), 4)

    def test_scores_pi_log_against_on_air_manifest(self):
        sent = run_on_air(
            count=3,
            callsign="N0CALL",
            hl2_ip=None,
            drive=187,
            amplitude=1.0,
            frequency_hz=28_124_000,
            gap_s=0,
            arm_tx=False,
        )
        ids = [item["msg_id"] for item in sent["transmissions"]]
        log = (
            "1200000 S/s into a 6000 Hz channel, gain=42.1, direct_sampling=0\n"
            f"16:00:01 bench-rx ok msg_id={ids[0]} bytes={sent['transmissions'][0]['payload_bytes']} "
            f"snr=18.4 origin=N0CALL\n"
            f"16:00:04 bench-rx fail snr=17.1 reason=crc mismatch: got 1 expected 2\n"
            f"16:00:08 bench-rx ok msg_id={ids[2]} bytes={sent['transmissions'][2]['payload_bytes']} "
            f"snr=20.1 origin=N0CALL\n"
        )
        scored = score_on_air(sent, log)
        self.assertEqual(scored["summary"]["sent"], 3)
        self.assertEqual(scored["summary"]["got_through"], 2)
        self.assertEqual(scored["conditions"]["rx_crc_fails_in_window"], 1)
        self.assertEqual(scored["conditions"]["rtl_gain_db"], 42.1)
        self.assertEqual(scored["architecture"]["rtl_gain_db"], 42.1)
        self.assertIn("42.1", format_report(scored))
        parsed = parse_rx_log(log)
        self.assertEqual(len(parsed["ok"]), 2)


if __name__ == "__main__":
    unittest.main()
