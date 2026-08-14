import json
import hashlib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from spark_four_hca.summary import SummaryError, load_result, parse_result, render_markdown
from spark_four_hca.cli import main


ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict:
    return {
        "schema_version": 1,
        "complete": True,
        "completed_trials": 10,
        "planned_trials": 10,
        "two_domain_raw_ceiling_gbps": 252.061538,
        "pair_label": "private-pair",
        "metadata": [
            {
                "ssh": "operator@ssh-target.invalid",
                "management_address": "192.0.2.9",
            }
        ],
        "trials": [
            {
                "paths": [
                    {
                        "target": "198.51.100.42",
                        "gid": "DO_NOT_EMIT_RAW_GID",
                        "rdma_device": "DO_NOT_EMIT_DEVICE",
                    }
                ]
            }
        ],
        "summaries": [
            {
                "mode": "single-p0-d0",
                "direction": "worker-to-head",
                "message_size_bytes": 65536,
                "qps": 1,
                "tx_depth": 128,
                "repeats": 5,
                "median_gbps": 100.0,
                "min_gbps": 99.0,
                "max_gbps": 101.0,
                "population_stdev_gbps": 0.5,
            },
            {
                "mode": "diagonal-a",
                "direction": "worker-to-head",
                "message_size_bytes": 65536,
                "qps": 2,
                "tx_depth": 128,
                "repeats": 5,
                "median_gbps": 225.0,
                "min_gbps": 224.0,
                "max_gbps": 226.0,
                "population_stdev_gbps": 0.7,
            },
        ],
    }


def _v3_payload(*, median: float = 225.0) -> dict:
    payload = _payload()
    payload["schema_version"] = 3
    payload.pop("complete")
    payload["trials_complete"] = True
    payload["measurement_accepted"] = True
    payload["summaries"][1]["median_gbps"] = median
    payload["summaries"][1]["min_gbps"] = min(median, 224.0)
    payload["summaries"][1]["max_gbps"] = max(median, 226.0)
    qualified = median >= 215.0
    payload["qualification"] = {
        "profile_id": "dgx-spark-dual-qsfp-standard",
        "profile_version": 1,
        "eligible": True,
        "qualified": qualified,
        "status": "qualified" if qualified else "below-performance-floor",
        "reasons": [] if qualified else ["median-below-minimum"],
        "criteria": {
            "mode": "diagonal-a",
            "direction": "worker-to-head",
            "qps": 2,
            "message_size_bytes": 65536,
            "tx_depth": 128,
            "minimum_duration_seconds": 30,
            "minimum_repeats": 5,
            "minimum_cooldown_seconds": 5.0,
            "schedule": "interleaved",
            "minimum_median_aggregate_gbps": 215.0,
            "measurement_must_be_accepted": True,
            "preflight_must_be_healthy": True,
            "link_error_counter_delta_must_be_zero": True,
            "cpu_pinning_must_be_distinct": True,
        },
        "observed": {
            "median_aggregate_gbps": median,
            "trial_count": 5,
            "cpu_pinning_verified": True,
        },
    }
    return payload


class SummaryTests(unittest.TestCase):
    def test_renders_table_and_relative_difference_from_allowlisted_aggregates(self):
        markdown = render_markdown(parse_result(_payload()))
        self.assertIn("| diagonal-a | worker-to-head | 65,536 | 2 |", markdown)
        self.assertIn("+125.00%", markdown)
        self.assertIn("89.26% of the 252.06 Gbit/s", markdown)
        self.assertIn("## Relative comparison", markdown)
        self.assertNotIn("## Speedup", markdown)
        for private_value in (
            "private-pair",
            "operator@ssh-target.invalid",
            "192.0.2.9",
            "198.51.100.42",
            "DO_NOT_EMIT_RAW_GID",
            "DO_NOT_EMIT_DEVICE",
        ):
            self.assertNotIn(private_value, markdown)

    def test_marks_checkpoint_result_incomplete(self):
        payload = _payload()
        payload["complete"] = False
        payload["completed_trials"] = 2
        markdown = render_markdown(parse_result(payload))
        self.assertIn("Status: **incomplete** (2/10 trials)", markdown)
        self.assertIn(
            "legacy trial completion; current postflight measurement acceptance is unknown",
            markdown,
        )

    def test_rejects_inconsistent_range(self):
        payload = _payload()
        payload["summaries"][0]["median_gbps"] = 98.0
        with self.assertRaisesRegex(SummaryError, "min_gbps <= median_gbps"):
            parse_result(payload)

    def test_rejects_nonfinite_number(self):
        payload = _payload()
        payload["summaries"][0]["median_gbps"] = float("nan")
        with self.assertRaisesRegex(SummaryError, "finite"):
            parse_result(payload)

    def test_rejects_noncanonical_mode_in_public_summary(self):
        payload = _payload()
        payload["summaries"][0]["mode"] = "private-topology"
        with self.assertRaisesRegex(SummaryError, "canonical public mode"):
            parse_result(payload)

    def test_rejects_complete_result_with_missing_trials(self):
        payload = _payload()
        payload["completed_trials"] = 9
        with self.assertRaisesRegex(SummaryError, "schema v1 complete must be true"):
            parse_result(payload)

    def test_rejects_incomplete_result_with_all_trials(self):
        payload = _payload()
        payload["complete"] = False
        with self.assertRaisesRegex(SummaryError, "schema v1 complete must be true"):
            parse_result(payload)

    def test_schema_v2_can_render_unaccepted_completed_trials(self):
        payload = _payload()
        payload["schema_version"] = 2
        payload["complete"] = False
        payload["accepted"] = False
        markdown = render_markdown(parse_result(payload))
        self.assertIn("Status: **unaccepted** (10/10 trials)", markdown)

    def test_schema_v2_rejects_complete_without_acceptance(self):
        payload = _payload()
        payload["schema_version"] = 2
        payload["accepted"] = False
        with self.assertRaisesRegex(SummaryError, "complete and accepted must match"):
            parse_result(payload)

    def test_schema_v3_reports_traffic_acceptance_and_qualification_separately(self):
        markdown = render_markdown(parse_result(_v3_payload()))
        self.assertIn("Traffic trials: **complete** (10/10)", markdown)
        self.assertIn("Measurement acceptance: **accepted**", markdown)
        self.assertIn("Standard-profile qualification: **qualified**", markdown)
        self.assertIn("225.00 Gbit/s median over 5 trials", markdown)

        below = render_markdown(parse_result(_v3_payload(median=210.0)))
        self.assertIn("Measurement acceptance: **accepted**", below)
        self.assertIn("not qualified; below-performance-floor", below)

    def test_schema_v3_completed_traffic_can_have_unaccepted_measurement(self):
        payload = _v3_payload()
        payload["measurement_accepted"] = False
        payload["qualification"]["qualified"] = False
        payload["qualification"]["status"] = "measurement-unaccepted"
        payload["qualification"]["reasons"] = ["measurement-unaccepted"]
        markdown = render_markdown(parse_result(payload))
        self.assertIn("Traffic trials: **complete**", markdown)
        self.assertIn("Measurement acceptance: **unaccepted**", markdown)
        self.assertIn("not evaluated; measurement unaccepted", markdown)

    def test_schema_v3_rejects_inconsistent_qualification(self):
        payload = _v3_payload()
        payload["qualification"]["observed"]["median_aggregate_gbps"] = 214.0
        with self.assertRaisesRegex(SummaryError, "inconsistent qualification evidence"):
            parse_result(payload)

    def test_schema_v3_requires_new_unambiguous_acceptance_fields(self):
        payload = _v3_payload()
        payload.pop("measurement_accepted")
        payload["accepted"] = True
        with self.assertRaisesRegex(SummaryError, "measurement_accepted"):
            parse_result(payload)

    def test_loads_json_file(self):
        path = ROOT / "results/reference-interleaved-2026-08-13.json"
        summary = load_result(path)
        self.assertEqual(len(summary.rows), 4)
        self.assertTrue(summary.complete)
        self.assertEqual(summary.suite_version, "0.1.0")
        self.assertEqual(summary.input_sha256, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_file_summary_recomputes_and_rejects_tampered_aggregates(self):
        source = ROOT / "results/reference-interleaved-2026-08-13.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["summaries"][0]["median_gbps"] += 10
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SummaryError, "semantic validation"):
                load_result(path)

    def test_cli_writes_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ROOT / "results/reference-interleaved-2026-08-13.json"
            destination = Path(directory) / "summary.md"
            with redirect_stdout(StringIO()):
                code = main(
                    [
                        "summarize",
                        "--input",
                        str(source),
                        "--output",
                        str(destination),
                    ]
                )
            markdown = destination.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("# Benchmark summary", markdown)
        self.assertIn("input SHA-256", markdown)
        self.assertNotIn("operator@ssh-target.invalid", markdown)


if __name__ == "__main__":
    unittest.main()
