import unittest

from hfbridge.txbridge import parse_allow, source_allowed


class TxbridgeAllowTest(unittest.TestCase):
    def test_empty_allow_lets_anyone_through(self):
        self.assertTrue(source_allowed("aa" * 16, set()))

    def test_allow_list_filters_unknown_source(self):
        allowed = parse_allow(["AA" * 16, "not-a-hash", "bb" * 16])
        self.assertEqual(allowed, {"aa" * 16, "bb" * 16})
        self.assertTrue(source_allowed("aa" * 16, allowed))
        self.assertTrue(source_allowed(bytes.fromhex("aa" * 16), allowed))
        self.assertFalse(source_allowed("cc" * 16, allowed))
        self.assertFalse(source_allowed(None, allowed))


if __name__ == "__main__":
    unittest.main()
