import unittest

from hfbridge.ingress import _stats_line as ingress_stats_line
from hfbridge.txbridge import _stats_line as txbridge_stats_line


class BridgeStatsLineTest(unittest.TestCase):
    def test_ingress_stats_line_omits_payload(self):
        line = ingress_stats_line(
            heard=3,
            forwarded=2,
            decode_failed=1,
            inject_failed=0,
            last_origin="N0CALL",
        )
        self.assertIn("heard=3", line)
        self.assertIn("last=N0CALL", line)
        self.assertNotIn("content", line)
        self.assertNotIn("payload", line)

    def test_txbridge_stats_line_omits_payload(self):
        line = txbridge_stats_line(
            received=4,
            on_air=1,
            held=2,
            rejected=1,
            tx_failed=0,
            last_bytes=12,
        )
        self.assertIn("received=4", line)
        self.assertIn("on_air=1", line)
        self.assertIn("last_bytes=12", line)
        self.assertNotIn("content", line)
        self.assertNotIn("payload", line)


if __name__ == "__main__":
    unittest.main()
