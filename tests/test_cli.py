from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spark_four_hca import __version__
from spark_four_hca.cli import main
from test_schemas import _current_v3_payload


ROOT = Path(__file__).resolve().parents[1]

COUNTER_NAMES = (
    "rx_crc_errors_phy",
    "rx_discards_phy",
    "rx_out_of_buffer",
    "rx_symbol_err_phy",
    "tx_discards_phy",
)
PATH_NAMES = ("p0-d0", "p0-d2", "p1-d0", "p1-d2")


def _counter_block(value: int = 0) -> dict:
    return {
        "counter_names": list(COUNTER_NAMES),
        "snapshots": [
            {
                "node_role": role,
                "path": path,
                "coverage_complete": True,
                "values": {name: value for name in COUNTER_NAMES},
            }
            for role in ("head", "worker")
            for path in PATH_NAMES
        ],
    }


def _report(counter_value: int = 0, *, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "checks": [],
        "summary": {"passed": 112 if ok else 111, "warned": 0, "failed": 0 if ok else 1},
        "link_error_counters": _counter_block(counter_value),
    }


def _matrix_result(*, standard: bool = False, median: float = 220.0) -> dict:
    repeats = 5 if standard else 1
    cooldown = 5.0 if standard else 1.0
    trial = {
        "mode": "diagonal-a",
        "direction": "worker-to-head",
        "message_size_bytes": 65536,
        "qps": 2,
        "tx_depth": 128,
        "paths": [
            {"server_cpu": 5, "client_cpu": 5},
            {"server_cpu": 6, "client_cpu": 6},
        ],
    }
    return {
        "schema_version": 3,
        "trials_complete": True,
        "measurement_accepted": False,
        "run_status": "awaiting-postflight",
        "parameters": {
            "modes": ["diagonal-a"],
            "directions": ["worker-to-head"],
            "message_sizes": [65536],
            "qps_values": [2],
            "tx_depths": [128],
            "duration_seconds": 30,
            "repeats": repeats,
            "cooldown_seconds": cooldown,
            "schedule": "interleaved",
        },
        "trials": [dict(trial, trial=index) for index in range(1, repeats + 1)],
        "summaries": [
            {
                "mode": "diagonal-a",
                "direction": "worker-to-head",
                "message_size_bytes": 65536,
                "qps": 2,
                "tx_depth": 128,
                "repeats": repeats,
                "median_gbps": median,
            }
        ],
        "validation": {
            "preflight": {
                "status": "passed",
                "ok": True,
            },
            "postflight": {"status": "pending"},
        },
    }


class CliTests(unittest.TestCase):
    def test_result_commands_report_oversized_integer_as_validation_error(self):
        payload = _current_v3_payload()
        payload["trials"][0]["aggregate_gbps"] = 10**400
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            for command in ("validate-result", "summarize"):
                with self.subTest(command=command):
                    stderr = StringIO()
                    with redirect_stderr(stderr), redirect_stdout(StringIO()):
                        code = main([command, "--input", str(path)])
                    self.assertEqual(code, 2)
                    self.assertIn("aggregate_gbps", stderr.getvalue())
                    self.assertIn("finite", stderr.getvalue())
                    self.assertNotIn(str(10**400), stderr.getvalue())

    def test_version_uses_package_single_source(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"spark-four-hca {__version__}")

    def test_validate(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["validate", "--config", str(ROOT / "examples/pair.toml")])
        self.assertEqual(code, 0)
        self.assertIn("valid:", output.getvalue())

    def test_validate_result_recomputes_historical_evidence(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "validate-result",
                    "--input",
                    str(ROOT / "results/reference-interleaved-2026-08-13.json"),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("valid semantic result: schema v1", output.getvalue())
        self.assertIn("measurement_accepted=legacy-unknown", output.getvalue())

    def test_validate_result_rejects_cross_field_corruption(self):
        source = ROOT / "results/reference-interleaved-2026-08-13.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["trials"][0]["aggregate_gbps"] += 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampered.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(["validate-result", "--input", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("aggregate_gbps", stderr.getvalue())

    def test_theory_json(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["theory", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"raw_one_way_ceiling_gbps": 252.061538', output.getvalue())

    def test_rejects_nonfinite_and_out_of_bounds_numeric_overrides(self):
        base = [
            "benchmark",
            "--config",
            str(ROOT / "examples/pair.toml"),
            "--output",
            "/tmp/result.json",
        ]
        for override in (
            ["--cooldown", "nan"],
            ["--cooldown", "inf"],
            ["--duration", "1"],
            ["--qps", "129"],
            ["--message-size", str((1 << 30) + 1)],
            ["--tx-depth", "16385"],
            ["--repeats", "1001"],
        ):
            with self.subTest(override=override), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    main(base + override)

    def test_benchmark_refuses_to_overwrite_config_before_ssh(self):
        stderr = StringIO()
        config = ROOT / "examples/pair.toml"
        with redirect_stderr(stderr):
            code = main(
                [
                    "benchmark",
                    "--config",
                    str(config),
                    "--output",
                    str(config),
                    "--skip-live-check",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("must not overwrite", stderr.getvalue())

    def test_benchmark_uses_standard_mode_and_qps_by_default(self):
        report = _report()
        result = _matrix_result()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch("spark_four_hca.cli.check_pair", return_value=report), patch(
                "spark_four_hca.cli.run_matrix", return_value=result
            ) as run_matrix, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(run_matrix.call_args.kwargs["modes"], ["diagonal-a"])
        self.assertEqual(run_matrix.call_args.kwargs["qps_values"], [2])
        self.assertTrue(payload["trials_complete"])
        self.assertTrue(payload["measurement_accepted"])
        self.assertEqual(payload["run_status"], "measurement-accepted")
        self.assertFalse(payload["qualification"]["qualified"])
        self.assertEqual(payload["qualification"]["status"], "ineligible")
        self.assertEqual(
            payload["validation"]["link_error_counter_delta"]["status"],
            "passed",
        )

    def test_positive_postflight_counter_delta_is_persisted_and_unaccepted(self):
        preflight = _report()
        postflight = _report()
        postflight["link_error_counters"]["snapshots"][0]["values"][
            "rx_crc_errors_phy"
        ] = 1
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch(
                "spark_four_hca.cli.check_pair", side_effect=[preflight, postflight]
            ), patch(
                "spark_four_hca.cli.run_matrix", return_value=_matrix_result()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 2)
        self.assertTrue(payload["trials_complete"])
        self.assertFalse(payload["measurement_accepted"])
        self.assertEqual(payload["run_status"], "link-errors-detected")
        delta = payload["validation"]["link_error_counter_delta"]
        self.assertEqual(delta["status"], "failed")
        self.assertEqual(
            delta["positive_deltas"][0]["values"], {"rx_crc_errors_phy": 1}
        )

    def test_postflight_exception_preserves_unaccepted_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch(
                "spark_four_hca.cli.check_pair",
                side_effect=[_report(), RuntimeError("synthetic postflight failure")],
            ), patch(
                "spark_four_hca.cli.run_matrix", return_value=_matrix_result()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 2)
        self.assertTrue(payload["trials_complete"])
        self.assertFalse(payload["measurement_accepted"])
        self.assertEqual(payload["run_status"], "postflight-error")
        self.assertEqual(payload["qualification"]["status"], "measurement-unaccepted")
        self.assertEqual(
            payload["validation"]["postflight"],
            {"status": "error", "error_type": "RuntimeError"},
        )

    def test_summarize_refuses_to_overwrite_input(self):
        stderr = StringIO()
        source = ROOT / "results/reference-interleaved-2026-08-13.json"
        with redirect_stderr(stderr):
            code = main(
                [
                    "summarize",
                    "--input",
                    str(source),
                    "--output",
                    str(source),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("must not overwrite", stderr.getvalue())

    def test_benchmark_refuses_existing_evidence_before_ssh_unless_forced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            output.write_text("original\n", encoding="utf-8")
            with patch("spark_four_hca.cli.check_pair") as check_pair, patch(
                "spark_four_hca.cli.run_matrix"
            ) as run_matrix, redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "original\n")
            check_pair.assert_not_called()
            run_matrix.assert_not_called()

            with patch("spark_four_hca.cli.check_pair", return_value=_report()), patch(
                "spark_four_hca.cli.run_matrix", return_value=_matrix_result()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                        "--force",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["measurement_accepted"])

    def test_standard_profile_selects_exact_qualification_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch("spark_four_hca.cli.check_pair", return_value=_report()), patch(
                "spark_four_hca.cli.run_matrix",
                return_value=_matrix_result(standard=True),
            ) as run_matrix, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--profile",
                        "standard",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        kwargs = run_matrix.call_args.kwargs
        self.assertEqual(kwargs["modes"], ["diagonal-a"])
        self.assertEqual(kwargs["directions"], ["worker-to-head"])
        self.assertEqual(kwargs["message_sizes"], [65536])
        self.assertEqual(kwargs["qps_values"], [2])
        self.assertEqual(kwargs["tx_depths"], [128])
        self.assertEqual(kwargs["duration"], 30)
        self.assertEqual(kwargs["repeats"], 5)
        self.assertEqual(kwargs["cooldown_seconds"], 5.0)
        self.assertEqual(kwargs["schedule"], "interleaved")
        self.assertTrue(payload["measurement_accepted"])
        self.assertTrue(payload["qualification"]["qualified"])

    def test_standard_profile_rejects_conflicts_and_duplicate_sweeps_before_ssh(self):
        cases = (
            ["--profile", "standard", "--qps", "1"],
            ["--profile", "standard", "--skip-live-check"],
            ["--qps", "2", "--qps", "2"],
            ["--mode", "diagonal-a", "--mode", "diagonal-a"],
        )
        for extra in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result.json"
                with patch("spark_four_hca.cli.check_pair") as check_pair, patch(
                    "spark_four_hca.cli.run_matrix"
                ) as run_matrix, redirect_stderr(StringIO()):
                    code = main(
                        [
                            "benchmark",
                            "--config",
                            str(ROOT / "examples/pair.toml"),
                            "--output",
                            str(output),
                            *extra,
                        ]
                    )
                self.assertEqual(code, 2)
                check_pair.assert_not_called()
                run_matrix.assert_not_called()

    def test_full_counter_coverage_is_required_for_measurement_acceptance(self):
        report = _report()
        report["link_error_counters"]["snapshots"].pop()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch("spark_four_hca.cli.check_pair", return_value=report), patch(
                "spark_four_hca.cli.run_matrix", return_value=_matrix_result()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    [
                        "benchmark",
                        "--config",
                        str(ROOT / "examples/pair.toml"),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertFalse(payload["measurement_accepted"])
        self.assertEqual(payload["run_status"], "validation-incomplete")


if __name__ == "__main__":
    unittest.main()
