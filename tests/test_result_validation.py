from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from spark_four_hca.benchmark import evaluate_standard_profile_qualification
from spark_four_hca.result_validation import (
    ResultValidationError,
    load_and_validate_result,
    validate_result,
)
from test_schemas import _current_v3_payload


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


class ResultValidationTests(unittest.TestCase):
    def test_rejects_integer_outside_float_range(self):
        for value in (10**400, -(10**400)):
            with self.subTest(value_sign=value > 0):
                payload = _current_v3_payload()
                payload["trials"][0]["aggregate_gbps"] = value
                with self.assertRaisesRegex(ResultValidationError, "aggregate_gbps.*finite"):
                    validate_result(payload)

    def test_all_recorded_v1_results_pass_semantic_validation(self):
        sources = sorted(RESULTS.glob("*.json"))
        self.assertEqual(len(sources), 3)
        for source in sources:
            with self.subTest(source=source.name):
                result = load_and_validate_result(source)
                self.assertEqual(result.schema_version, 1)
                self.assertEqual(
                    result.trials_complete,
                    result.completed_trials == result.planned_trials,
                )

    def test_accepted_v3_is_recomputed_and_validated(self):
        payload = _current_v3_payload()
        result = validate_result(payload)
        self.assertEqual(result.schema_version, 3)
        self.assertEqual(result.suite_version, "0.2.0")
        self.assertTrue(result.trials_complete)
        self.assertTrue(result.measurement_accepted)
        self.assertEqual(result.run_status, "measurement-accepted")
        self.assertEqual(result.summaries[0].median_gbps, 222.88)

    def test_initial_runtime_checkpoint_is_semantically_valid(self):
        payload = _current_v3_payload()
        payload["metadata"] = []
        payload["trials"] = []
        payload["summaries"] = []
        payload["completed_trials"] = 0
        payload["trials_complete"] = False
        payload["measurement_accepted"] = False
        payload["run_status"] = "initializing"
        payload["validation"] = {
            "preflight": {
                "status": "not-run",
                "reason": "library caller did not provide it",
            },
            "postflight": {"status": "pending"},
        }
        payload["qualification"] = evaluate_standard_profile_qualification(
            payload, measurement_final=False
        )
        result = validate_result(payload)
        self.assertFalse(result.trials_complete)
        self.assertFalse(result.measurement_accepted)
        self.assertEqual(result.summaries, ())

    def test_cross_field_corruption_is_rejected(self):
        mutations: list[tuple[str, dict, str]] = []

        aggregate = _current_v3_payload()
        aggregate["trials"][0]["aggregate_gbps"] += 1
        mutations.append(("aggregate", aggregate, "aggregate_gbps"))

        schedule = _current_v3_payload()
        schedule["schedule_plan"][0]["position"] = 2
        mutations.append(("schedule", schedule, "position"))

        summary = _current_v3_payload()
        summary["summaries"][0]["mean_gbps"] += 1
        mutations.append(("summary", summary, "recomputed"))

        counter = _current_v3_payload()
        counter["validation"]["postflight"]["link_error_counters"]["snapshots"][0][
            "values"
        ]["rx_crc_errors_phy"] = 1
        mutations.append(("counter", counter, "deltas"))

        qualification = _current_v3_payload()
        qualification["qualification"]["observed"]["median_aggregate_gbps"] = 221.0
        mutations.append(("qualification", qualification, "median_aggregate_gbps"))

        path_order = _current_v3_payload()
        for trial in path_order["trials"]:
            trial["paths"].reverse()
        mutations.append(("path-order", path_order, "canonical mode path order"))

        nonfinite = _current_v3_payload()
        nonfinite["trials"][0]["aggregate_gbps"] = float("nan")
        mutations.append(("nonfinite", nonfinite, "non-finite"))

        status = _current_v3_payload()
        status["measurement_accepted"] = False
        status["run_status"] = "published"
        status["qualification"] = evaluate_standard_profile_qualification(
            status, measurement_final=True
        )
        mutations.append(("status", status, "unsupported"))

        for name, payload, message in mutations:
            with self.subTest(name=name), self.assertRaisesRegex(
                ResultValidationError, message
            ):
                validate_result(payload)

    def test_loader_reports_invalid_json_without_exposing_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(ResultValidationError, "invalid JSON"):
                load_and_validate_result(path)

    def test_validation_does_not_mutate_payload(self):
        payload = _current_v3_payload()
        original = deepcopy(payload)
        validate_result(payload)
        self.assertEqual(payload, original)
        self.assertEqual(json.dumps(payload, sort_keys=True), json.dumps(original, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
