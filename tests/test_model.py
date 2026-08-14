from pathlib import Path
import tempfile
import unittest

from spark_four_hca.model import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/pair.toml"


class ConfigTests(unittest.TestCase):
    def test_example_is_valid_and_complete(self):
        config = load_config(EXAMPLE)
        self.assertEqual(len(config.paths), 4)
        self.assertEqual(
            [path.name for path in config.paths_for_mode("diagonal-a")],
            ["p0-d0", "p1-d2"],
        )
        self.assertEqual(config.qps, 2)
        self.assertEqual(len(config.paths_for_mode("all-four")), 4)

    def test_missing_qps_uses_operational_default(self):
        config = self._load_modified("qps = 2\n", "")
        self.assertEqual(config.qps, 2)

    def _load_modified(self, old: str, new: str):
        source = EXAMPLE.read_text(encoding="utf-8").replace(old, new, 1)
        return self._load_source(source)

    def _load_source(self, source: str):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", encoding="utf-8") as handle:
            handle.write(source)
            handle.flush()
            return load_config(handle.name)

    def test_pair_identity_and_addresses_remain_configurable(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        source = source.replace('pair_label = "example-pair"', 'pair_label = "lab-pair"')
        source = source.replace('ssh = "spark-head"', 'ssh = "alice@head.example"')
        source = source.replace('ssh = "spark-worker"', 'ssh = "bob@worker.example"')
        source = source.replace('label = "head"', 'label = "alpha"')
        source = source.replace('label = "worker"', 'label = "beta"')
        source = source.replace("192.168.200.1/30", "10.42.0.1/30")
        source = source.replace("192.168.200.2/30", "10.42.0.2/30")

        config = self._load_source(source)

        self.assertEqual(config.pair_label, "lab-pair")
        self.assertEqual(config.head.ssh, "alice@head.example")
        self.assertEqual(config.worker.ssh, "bob@worker.example")
        self.assertEqual(str(config.paths_by_name["p0-d0"].head_address), "10.42.0.1/30")

    def test_rejects_wrong_interface_mapping(self):
        with self.assertRaisesRegex(ConfigError, "expected"):
            self._load_modified('netdev = "enp1s0f0np0"', 'netdev = "enp1s0f1np1"')

    def test_rejects_renamed_canonical_path_even_when_modes_are_updated(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace("p0-d0", "left-domain-zero")
        with self.assertRaisesRegex(ConfigError, "canonical DGX Spark path names"):
            self._load_source(source)

    def test_rejects_canonical_names_swapped_between_port_domain_mappings(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        source = source.replace('name = "p0-d0"', 'name = "swap-placeholder"', 1)
        source = source.replace('name = "p1-d2"', 'name = "p0-d0"', 1)
        source = source.replace('name = "swap-placeholder"', 'name = "p1-d2"', 1)
        with self.assertRaisesRegex(ConfigError, "expected canonical mapping"):
            self._load_source(source)

    def test_rejects_missing_canonical_mode(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace(
            '"same-domain-d2" = ["p0-d2", "p1-d2"]\n', ""
        )
        with self.assertRaisesRegex(ConfigError, "canonical benchmark mode names"):
            self._load_source(source)

    def test_rejects_extra_mode(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace(
            "[modes]\n", '[modes]\n"custom" = ["p0-d0"]\n'
        )
        with self.assertRaisesRegex(ConfigError, "canonical benchmark mode names"):
            self._load_source(source)

    def test_rejects_renamed_mode(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace(
            '"diagonal-a" =', '"preferred" ='
        )
        with self.assertRaisesRegex(ConfigError, "canonical benchmark mode names"):
            self._load_source(source)

    def test_rejects_altered_mode_membership(self):
        with self.assertRaisesRegex(ConfigError, "canonical ordered paths"):
            self._load_modified(
                '"diagonal-a" = ["p0-d0", "p1-d2"]',
                '"diagonal-a" = ["p0-d0", "p1-d0"]',
            )

    def test_rejects_reordered_mode_membership(self):
        with self.assertRaisesRegex(ConfigError, "canonical ordered paths"):
            self._load_modified(
                '"diagonal-a" = ["p0-d0", "p1-d2"]',
                '"diagonal-a" = ["p1-d2", "p0-d0"]',
            )

    def test_rejects_overlapping_subnets(self):
        with self.assertRaisesRegex(ConfigError, "not in the same /30|overlap"):
            self._load_modified(
                'head_address = "192.168.201.1/30"',
                'head_address = "192.168.200.1/30"',
            )

    def test_rejects_public_addresses(self):
        with self.assertRaisesRegex(ConfigError, "private"):
            self._load_modified(
                'head_address = "192.168.200.1/30"',
                'head_address = "8.8.8.9/30"',
            )

    def test_rejects_ssh_target_that_begins_with_option_prefix(self):
        with self.assertRaisesRegex(ConfigError, "safe SSH target"):
            self._load_modified('ssh = "spark-head"', 'ssh = "-V"')

    def test_rejects_non_rfc1918_address_that_ipaddress_calls_private(self):
        with self.assertRaisesRegex(ConfigError, "RFC 1918"):
            self._load_modified(
                'head_address = "192.168.200.1/30"',
                'head_address = "192.0.2.1/30"',
            )

    def test_rejects_unknown_root_key(self):
        with self.assertRaisesRegex(ConfigError, "root contains unknown keys"):
            self._load_modified("schema_version = 1", "schema_version = 1\nfuture = true")

    def test_rejects_unknown_node_key(self):
        with self.assertRaisesRegex(ConfigError, "nodes.head contains unknown keys"):
            self._load_modified('label = "head"', 'label = "head"\nfuture = true')

    def test_rejects_unknown_path_key(self):
        with self.assertRaisesRegex(ConfigError, r"paths\[0\] contains unknown keys"):
            self._load_modified(
                'name = "p0-d0"',
                'name = "p0-d0"\nfuture = true',
            )


if __name__ == "__main__":
    unittest.main()
