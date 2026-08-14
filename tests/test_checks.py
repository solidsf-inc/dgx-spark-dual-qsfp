from pathlib import Path
import unittest
from unittest.mock import patch

from spark_four_hca.checks import (
    _COUNTERS,
    check_pair,
    compare_link_error_counters,
)
from spark_four_hca.model import load_config


ROOT = Path(__file__).resolve().parents[1]


def _probe(
    config,
    role,
    *,
    counters_readable=True,
    counters=None,
    identity_digest=None,
):
    return {
        "machine_identity_digest": identity_digest or ("a" if role == "head" else "b") * 64,
        "cpu_affinity": {
            "configured": True,
            "observable": True,
            "all_present": True,
            "all_online": True,
        },
        "metadata": {
            "kernel": "6.17.0-test",
            "perftest_version": "Version: 6.20",
            "driver": "mlx5_core",
            "firmware": "28.45.4028",
        },
        "paths": [
            {
                "name": path.name,
                "exists": True,
                "operstate": "up",
                "carrier": "1",
                "speed": "200000",
                "mtu": str(config.mtu),
                "ipv4_check": {
                    "observable": True,
                    "expected_only_global": True,
                },
                "route_check": {
                    "observable": True,
                    "expected_dev": True,
                    "expected_source": True,
                },
                "pci_backing": {
                    "observable": True,
                    "matches": True,
                },
                "mapping_up": True,
                "rdma_state": "4: ACTIVE",
                "active_mtu": "4096",
                "pcie_width": "4",
                "pcie_speed": "32.0 GT/s PCIe",
                "counters_readable": counters_readable,
                "counters": (
                    dict(counters)
                    if counters is not None
                    else {name: 0 for name in _COUNTERS}
                ),
                "ping_ok": True,
            }
            for path in config.paths
        ],
    }


class CheckPairTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "examples/pair.toml")

    def _report_with_mutation(self, mutate):
        def side_effect(runner, config, role, ping_count):
            probe = _probe(config, role)
            mutate(probe, role)
            return probe

        with patch("spark_four_hca.checks._probe_node", side_effect=side_effect):
            return check_pair(self.config)

    @staticmethod
    def _failures(report, name):
        return [
            item
            for item in report["checks"]
            if item["name"] == name and item["status"] == "fail"
        ]

    def test_healthy_pair_passes_with_full_counter_coverage(self):
        with patch(
            "spark_four_hca.checks._probe_node",
            side_effect=lambda runner, config, role, ping_count: _probe(config, role),
        ):
            report = check_pair(self.config)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"], {"passed": 131, "warned": 0, "failed": 0})
        snapshots = report["link_error_counters"]["snapshots"]
        self.assertEqual(len(snapshots), 8)
        self.assertTrue(all(item["coverage_complete"] for item in snapshots))
        self.assertNotIn(self.config.head.ssh, str(snapshots))
        self.assertNotIn(self.config.worker.ssh, str(snapshots))

    def test_ipv4_global_exclusivity_fails_closed_without_leaking_addresses(self):
        for fact in (
            {"observable": True, "expected_only_global": False},
            {"observable": False, "expected_only_global": False},
        ):
            with self.subTest(fact=fact):
                report = self._report_with_mutation(
                    lambda probe, role: [
                        item.update(ipv4_check=fact) for item in probe["paths"]
                    ]
                )
                self.assertFalse(report["ok"])
                self.assertEqual(len(self._failures(report, "ipv4-global-address")), 8)
                rendered = str(report)
                for path in self.config.paths:
                    self.assertNotIn(str(path.head_address.ip), rendered)
                    self.assertNotIn(str(path.worker_address.ip), rendered)

    def test_peer_route_requires_expected_device_and_source(self):
        for fact in (
            {"observable": True, "expected_dev": False, "expected_source": True},
            {"observable": True, "expected_dev": True, "expected_source": False},
            {"observable": False, "expected_dev": False, "expected_source": False},
        ):
            with self.subTest(fact=fact):
                report = self._report_with_mutation(
                    lambda probe, role: [
                        item.update(route_check=fact) for item in probe["paths"]
                    ]
                )
                self.assertFalse(report["ok"])
                self.assertEqual(len(self._failures(report, "peer-route")), 8)

    def test_netdev_pci_backing_mismatch_or_unavailable_fails_closed(self):
        for fact in (
            {"observable": True, "matches": False},
            {"observable": False, "matches": False},
        ):
            with self.subTest(fact=fact):
                report = self._report_with_mutation(
                    lambda probe, role: [
                        item.update(pci_backing=fact) for item in probe["paths"]
                    ]
                )
                self.assertFalse(report["ok"])
                self.assertEqual(len(self._failures(report, "netdev-pci-backing")), 8)

    def test_configured_cpu_cores_must_exist_and_be_online(self):
        for fact in (
            {"observable": False, "all_present": False, "all_online": False},
            {"observable": True, "all_present": False, "all_online": False},
            {"observable": True, "all_present": True, "all_online": False},
        ):
            with self.subTest(fact=fact):
                report = self._report_with_mutation(
                    lambda probe, role: probe.update(cpu_affinity=fact)
                )
                self.assertFalse(report["ok"])
                self.assertEqual(len(self._failures(report, "cpu-affinity")), 2)

    def test_aliases_must_resolve_to_distinct_machine_identities(self):
        report = self._report_with_mutation(
            lambda probe, role: probe.update(machine_identity_digest="c" * 64)
        )
        self.assertFalse(report["ok"])
        failures = self._failures(report, "node-identity-distinct")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["observed"], "same machine identity")
        self.assertNotIn("c" * 64, str(report))

        unavailable = self._report_with_mutation(
            lambda probe, role: probe.update(machine_identity_digest="")
        )
        self.assertFalse(unavailable["ok"])
        self.assertEqual(
            self._failures(unavailable, "node-identity-distinct")[0]["observed"],
            "unavailable",
        )

    def test_report_uses_roles_and_sanitizes_remote_probe_failures(self):
        secret = "private-host 192.0.2.99 raw-machine-id"

        def side_effect(runner, config, role, ping_count):
            if role == "head":
                raise RuntimeError(secret)
            return _probe(config, role)

        with patch("spark_four_hca.checks._probe_node", side_effect=side_effect):
            report = check_pair(self.config)
        rendered = str(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(self.config.head.ssh, rendered)
        self.assertNotIn(self.config.worker.ssh, rendered)
        self.assertEqual(
            self._failures(report, "remote-probe")[0]["observed"], "unavailable"
        )

    def test_missing_driver_counter_is_visible_warning(self):
        incomplete = {name: 0 for name in _COUNTERS if name != "rx_crc_errors_phy"}
        with patch(
            "spark_four_hca.checks._probe_node",
            side_effect=lambda runner, config, role, ping_count: _probe(
                config, role, counters=incomplete
            ),
        ):
            report = check_pair(self.config)
        self.assertTrue(report["ok"])
        coverage = [
            item for item in report["checks"] if item["name"] == "error-counter-coverage"
        ]
        self.assertEqual({item["status"] for item in coverage}, {"warn"})
        self.assertTrue(all("rx_crc_errors_phy" in item["observed"] for item in coverage))

    def test_failed_ethtool_counter_probe_fails_closed(self):
        with patch(
            "spark_four_hca.checks._probe_node",
            side_effect=lambda runner, config, role, ping_count: _probe(
                config, role, counters_readable=False, counters={}
            ),
        ):
            report = check_pair(self.config)
        self.assertFalse(report["ok"])
        failures = [
            item
            for item in report["checks"]
            if item["name"] == "error-counter-coverage" and item["status"] == "fail"
        ]
        self.assertEqual(len(failures), 8)

    def test_positive_link_error_delta_fails_acceptance_comparison(self):
        baseline = {name: 7 for name in _COUNTERS}
        increased = dict(baseline)
        increased["rx_crc_errors_phy"] += 1
        with patch(
            "spark_four_hca.checks._probe_node",
            side_effect=lambda runner, config, role, ping_count: _probe(
                config, role, counters=baseline
            ),
        ):
            before = check_pair(self.config)
        with patch(
            "spark_four_hca.checks._probe_node",
            side_effect=lambda runner, config, role, ping_count: _probe(
                config, role, counters=increased
            ),
        ):
            after = check_pair(self.config)

        comparison = compare_link_error_counters(before, after)
        self.assertFalse(comparison["ok"])
        self.assertEqual(comparison["status"], "failed")
        self.assertEqual(len(comparison["positive_deltas"]), 8)
        self.assertTrue(
            all(
                item["values"] == {"rx_crc_errors_phy": 1}
                for item in comparison["positive_deltas"]
            )
        )


if __name__ == "__main__":
    unittest.main()
