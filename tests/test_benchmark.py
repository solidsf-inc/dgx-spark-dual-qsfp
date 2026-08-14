import json
import os
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from spark_four_hca import __version__
from spark_four_hca.benchmark import (
    TWO_PCIE_GEN5_X4_GBPS,
    _build_schedule,
    _implementation_fingerprint_sha256,
    _terminate_processes,
    _write_private_text_atomic,
    evaluate_standard_profile_qualification,
    parse_perftest_bandwidth,
    run_matrix,
)
from spark_four_hca.checks import NodeMetadata
from spark_four_hca.model import load_config


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkParserTests(unittest.TestCase):
    @staticmethod
    def _trial(config, mode, **kwargs):
        return {
            "mode": mode,
            "direction": kwargs["direction"],
            "message_size_bytes": kwargs["message_size"],
            "qps": kwargs["qps"],
            "tx_depth": kwargs["tx_depth"],
            "aggregate_gbps": 222.5,
            "aggregate_GBps": 27.8125,
            "paths": [
                {"server_cpu": 5, "client_cpu": 5},
                {"server_cpu": 6, "client_cpu": 6},
            ],
        }

    def test_parses_average_bandwidth(self):
        text = (ROOT / "tests/fixtures/ib_write_bw.txt").read_text(encoding="utf-8")
        result = parse_perftest_bandwidth(text, expected_size=65536)
        self.assertEqual(result.bytes, 65536)
        self.assertEqual(result.iterations, 1038910)
        self.assertAlmostEqual(result.average_gbps, 109.87)

    def test_rejects_missing_result(self):
        with self.assertRaisesRegex(RuntimeError, "could not find"):
            parse_perftest_bandwidth("no result here")

    def test_rejects_result_for_wrong_message_size(self):
        text = "4096 100 0.00 99.10 0.10\n"
        with self.assertRaisesRegex(RuntimeError, "expected message size 65536"):
            parse_perftest_bandwidth(text, expected_size=65536)

    def test_private_log_write_is_atomic_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "raw.log"
            _write_private_text_atomic(target, "private endpoint\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "private endpoint\n")
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_interleaved_schedule_is_deterministic_and_rotates_positions(self):
        kwargs = dict(
            modes=["a", "b", "c"],
            directions=["worker-to-head"],
            message_sizes=[65536],
            qps_values=[2],
            tx_depths=[128],
            repeats=3,
            schedule="interleaved",
            schedule_seed=17,
        )
        first = _build_schedule(**kwargs)
        self.assertEqual(first, _build_schedule(**kwargs))
        orders = [[item[0] for item in first if item[-1] == repeat] for repeat in (1, 2, 3)]
        self.assertEqual(len({tuple(order) for order in orders}), 3)
        for position in range(3):
            self.assertEqual({order[position] for order in orders}, {"a", "b", "c"})

    def test_matrix_records_plan_preflight_and_initial_checkpoint(self):
        config = load_config(ROOT / "examples/pair.toml")
        metadata = NodeMetadata("head", "head", "kernel", "perftest", "mlx5", "fw")

        def fail_trial(*args, **kwargs):
            raise RuntimeError("synthetic early failure")

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch("spark_four_hca.benchmark.collect_node_metadata", return_value=metadata), patch(
                "spark_four_hca.benchmark.run_trial", side_effect=fail_trial
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic"):
                    run_matrix(
                        config,
                        modes=["diagonal-a", "diagonal-b"],
                        directions=["worker-to-head"],
                        message_sizes=[65536],
                        qps_values=[2],
                        tx_depths=[128],
                        duration=30,
                        repeats=2,
                        cooldown_seconds=0,
                        schedule_seed=9,
                        preflight_status={"status": "passed"},
                        checkpoint_path=checkpoint,
                    )
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertFalse(payload["trials_complete"])
            self.assertEqual(payload["completed_trials"], 0)
            self.assertEqual(payload["validation"]["preflight"]["status"], "passed")
            self.assertEqual(payload["validation"]["postflight"]["status"], "pending")
            self.assertEqual(payload["parameters"]["schedule_seed"], 9)
            self.assertEqual(len(payload["schedule_plan"]), 4)
            self.assertFalse(payload["measurement_accepted"])
            self.assertEqual(payload["run_status"], "benchmark-failed")
            self.assertEqual(payload["failure"], {"error_type": "RuntimeError"})

    def test_completed_matrix_separates_traffic_from_measurement_acceptance(self):
        config = load_config(ROOT / "examples/pair.toml")
        metadata = NodeMetadata("head", "head", "kernel", "perftest", "mlx5", "fw")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch(
                "spark_four_hca.benchmark.collect_node_metadata", return_value=metadata
            ), patch(
                "spark_four_hca.benchmark.run_trial", side_effect=self._trial
            ), redirect_stderr(StringIO()):
                result = run_matrix(
                    config,
                    modes=["diagonal-a"],
                    directions=["worker-to-head"],
                    message_sizes=[65536],
                    qps_values=[2],
                    tx_depths=[128],
                    duration=30,
                    repeats=1,
                    cooldown_seconds=0,
                    preflight_status={"status": "passed"},
                    checkpoint_path=checkpoint,
                )
            persisted = json.loads(checkpoint.read_text(encoding="utf-8"))

        for payload in (result, persisted):
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["suite_version"], __version__)
            self.assertEqual(payload["completed_trials"], payload["planned_trials"])
            self.assertTrue(payload["trials_complete"])
            self.assertFalse(payload["measurement_accepted"])
            self.assertEqual(payload["run_status"], "awaiting-postflight")
            self.assertFalse(payload["qualification"]["qualified"])
            self.assertEqual(
                payload["qualification"]["status"],
                "awaiting-measurement-acceptance",
            )

    def test_keyboard_interrupt_persists_explicit_interrupted_checkpoint(self):
        config = load_config(ROOT / "examples/pair.toml")
        metadata = NodeMetadata("head", "head", "kernel", "perftest", "mlx5", "fw")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch(
                "spark_four_hca.benchmark.collect_node_metadata", return_value=metadata
            ), patch(
                "spark_four_hca.benchmark.run_trial", side_effect=KeyboardInterrupt
            ), redirect_stderr(StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    run_matrix(
                        config,
                        modes=["diagonal-a"],
                        directions=["worker-to-head"],
                        message_sizes=[65536],
                        qps_values=[2],
                        tx_depths=[128],
                        duration=30,
                        repeats=1,
                        cooldown_seconds=0,
                        preflight_status={"status": "passed"},
                        checkpoint_path=checkpoint,
                    )
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))

        self.assertFalse(payload["trials_complete"])
        self.assertFalse(payload["measurement_accepted"])
        self.assertEqual(payload["run_status"], "interrupted")
        self.assertEqual(payload["failure"], {"error_type": "KeyboardInterrupt"})
        self.assertEqual(payload["completed_trials"], 0)

    def test_cleanup_reaps_process_after_kill(self):
        class StubbornProcess:
            def __init__(self):
                self.killed = False
                self.waits = 0

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout):
                self.waits += 1
                if not self.killed:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return -9

            def kill(self):
                self.killed = True

        process = StubbornProcess()
        _terminate_processes([process])
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 2)

    def test_two_link_ceiling(self):
        self.assertAlmostEqual(TWO_PCIE_GEN5_X4_GBPS, 252.061538, places=5)

    def test_interleaved_schedule_rotates_complete_conditions(self):
        conditions = _build_schedule(
            modes=["diagonal-a", "diagonal-b"],
            directions=["worker-to-head", "head-to-worker"],
            message_sizes=[65536],
            qps_values=[1, 2],
            tx_depths=[128],
            repeats=8,
            schedule="interleaved",
            schedule_seed=4,
        )
        per_repeat = [
            [item[:-1] for item in conditions if item[-1] == repeat]
            for repeat in range(1, 9)
        ]
        self.assertEqual(len(per_repeat[0]), 8)
        for position in range(8):
            self.assertEqual(
                {order[position] for order in per_repeat},
                set(per_repeat[0]),
            )

    def test_implementation_fingerprint_is_stable_and_source_sensitive(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            for root in (first, second):
                (root / "nested").mkdir()
                (root / "a.py").write_text("A = 1\n", encoding="utf-8")
                (root / "nested/b.py").write_text("B = 2\n", encoding="utf-8")
                (root / "ignored.pyc").write_bytes(b"ignored")
            baseline = _implementation_fingerprint_sha256(first)
            self.assertEqual(baseline, _implementation_fingerprint_sha256(second))
            (second / "nested/b.py").write_text("B = 3\n", encoding="utf-8")
            self.assertNotEqual(baseline, _implementation_fingerprint_sha256(second))

    def test_standard_qualification_criteria_and_floor_are_independent_of_acceptance(self):
        trial = self._trial(None, "diagonal-a", direction="worker-to-head", message_size=65536, qps=2, tx_depth=128)
        result = {
            "measurement_accepted": False,
            "parameters": {
                "modes": ["diagonal-a"],
                "directions": ["worker-to-head"],
                "message_sizes": [65536],
                "qps_values": [2],
                "tx_depths": [128],
                "duration_seconds": 30,
                "repeats": 5,
                "cooldown_seconds": 5.0,
                "schedule": "interleaved",
            },
            "validation": {
                "preflight": {"status": "passed", "ok": True},
            },
            "trials": [dict(trial, trial=index) for index in range(1, 6)],
            "summaries": [
                {
                    "mode": "diagonal-a",
                    "direction": "worker-to-head",
                    "message_size_bytes": 65536,
                    "qps": 2,
                    "tx_depth": 128,
                    "repeats": 5,
                    "median_gbps": 215.0,
                }
            ],
        }
        pending = evaluate_standard_profile_qualification(result)
        self.assertTrue(pending["eligible"])
        self.assertFalse(pending["qualified"])
        self.assertEqual(pending["status"], "awaiting-measurement-acceptance")

        result["measurement_accepted"] = True
        qualified = evaluate_standard_profile_qualification(result, measurement_final=True)
        self.assertTrue(qualified["qualified"])
        self.assertEqual(qualified["observed"]["trial_count"], 5)
        self.assertTrue(qualified["observed"]["cpu_pinning_verified"])

        result["summaries"][0]["median_gbps"] = 214.999
        below = evaluate_standard_profile_qualification(result, measurement_final=True)
        self.assertEqual(below["status"], "below-performance-floor")
        self.assertFalse(below["qualified"])

    def test_every_standard_eligibility_dimension_fails_closed(self):
        base = {
            "measurement_accepted": True,
            "parameters": {
                "modes": ["diagonal-a"],
                "directions": ["worker-to-head"],
                "message_sizes": [65536],
                "qps_values": [2],
                "tx_depths": [128],
                "duration_seconds": 30,
                "repeats": 5,
                "cooldown_seconds": 5.0,
                "schedule": "interleaved",
            },
            "validation": {"preflight": {"status": "passed", "ok": True}},
            "trials": [],
            "summaries": [],
        }
        mutations = (
            ("modes", ["diagonal-a", "diagonal-b"], "mode-set-mismatch"),
            ("directions", ["head-to-worker"], "direction-set-mismatch"),
            ("message_sizes", [4096], "message-size-set-mismatch"),
            ("qps_values", [1], "qps-set-mismatch"),
            ("tx_depths", [64], "tx-depth-set-mismatch"),
            ("duration_seconds", 29, "duration-below-minimum"),
            ("repeats", 4, "repeats-below-minimum"),
            ("cooldown_seconds", 4.99, "cooldown-below-minimum"),
            ("schedule", "grouped", "schedule-mismatch"),
        )
        for key, value, reason in mutations:
            with self.subTest(key=key):
                candidate = json.loads(json.dumps(base))
                candidate["parameters"][key] = value
                block = evaluate_standard_profile_qualification(
                    candidate, measurement_final=True
                )
                self.assertFalse(block["eligible"])
                self.assertIn(reason, block["reasons"])

        unhealthy = json.loads(json.dumps(base))
        unhealthy["validation"]["preflight"] = {"status": "skipped", "ok": False}
        block = evaluate_standard_profile_qualification(unhealthy, measurement_final=True)
        self.assertIn("preflight-not-healthy", block["reasons"])

    def test_cpu_pinning_is_required_for_qualification(self):
        trial = self._trial(None, "diagonal-a", direction="worker-to-head", message_size=65536, qps=2, tx_depth=128)
        trial["paths"][1]["server_cpu"] = 5
        result = {
            "measurement_accepted": True,
            "parameters": {
                "modes": ["diagonal-a"], "directions": ["worker-to-head"],
                "message_sizes": [65536], "qps_values": [2], "tx_depths": [128],
                "duration_seconds": 30, "repeats": 5, "cooldown_seconds": 5.0,
                "schedule": "interleaved",
            },
            "validation": {"preflight": {"status": "passed", "ok": True}},
            "trials": [trial] * 5,
            "summaries": [{
                "mode": "diagonal-a", "direction": "worker-to-head",
                "message_size_bytes": 65536, "qps": 2, "tx_depth": 128,
                "repeats": 5, "median_gbps": 220.0,
            }],
        }
        block = evaluate_standard_profile_qualification(result, measurement_final=True)
        self.assertEqual(block["status"], "qualification-evidence-incomplete")
        self.assertEqual(block["reasons"], ["cpu-pinning-not-demonstrated"])


if __name__ == "__main__":
    unittest.main()
