import tempfile
import unittest
from pathlib import Path

import RNS

from hfbridge.hashes import collect, lxmf_hash


class HashesTest(unittest.TestCase):
    def test_lxmf_hash_is_stable_for_an_identity_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity"
            identity = RNS.Identity()
            identity.to_file(str(path))
            first = lxmf_hash(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(lxmf_hash(path), first)

    def test_collect_skips_roles_without_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collect(Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()
