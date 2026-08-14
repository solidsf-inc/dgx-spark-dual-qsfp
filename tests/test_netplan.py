from pathlib import Path
import unittest

from spark_four_hca.model import load_config
from spark_four_hca.netplan import render_netplan, render_topology


ROOT = Path(__file__).resolve().parents[1]


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "examples/pair.toml")

    def test_head_netplan_has_all_four_addresses(self):
        rendered = render_netplan(self.config, "head")
        self.assertEqual(rendered.count("      addresses:"), 4)
        self.assertIn("192.168.200.1/30", rendered)
        self.assertIn("192.168.203.1/30", rendered)
        self.assertNotIn("gateway4:", rendered.lower())
        self.assertNotIn("routes:", rendered.lower())

    def test_worker_netplan_uses_peer_addresses(self):
        rendered = render_netplan(self.config, "worker")
        self.assertIn("192.168.200.2/30", rendered)
        self.assertIn("192.168.203.2/30", rendered)

    def test_topology_explains_diagonal_resources(self):
        rendered = render_topology(self.config)
        diagonal = next(line for line in rendered.splitlines() if "diagonal-a" in line)
        self.assertIn("cages=[0, 1]", diagonal)
        self.assertIn("domains=[0, 2]", diagonal)


if __name__ == "__main__":
    unittest.main()
