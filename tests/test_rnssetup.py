import tempfile
import unittest
from pathlib import Path

from hfbridge.rnssetup import write_configs


class RnsSetupTest(unittest.TestCase):
    def test_writes_four_isolated_islands(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_configs(Path(tmp))
            names = {path.name for path in paths}
            self.assertEqual(names, {"origin", "txbridge", "ingress", "dest"})
            tx = (Path(tmp) / "txbridge" / "config").read_text()
            origin = (Path(tmp) / "origin" / "config").read_text()
            ingress = (Path(tmp) / "ingress" / "config").read_text()
            dest = (Path(tmp) / "dest" / "config").read_text()
            self.assertIn("share_instance = No", tx)
            self.assertIn("listen_port = 3742", tx)
            self.assertIn("target_port = 3742", origin)
            self.assertIn("listen_port = 3743", ingress)
            self.assertIn("target_port = 3743", dest)
            self.assertNotIn("3743", origin)
            self.assertNotIn("3742", dest)


if __name__ == "__main__":
    unittest.main()
