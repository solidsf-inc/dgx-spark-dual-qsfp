from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import unittest

from spark_four_hca.benchmark import (
    _result_payload,
    evaluate_standard_profile_qualification,
)
from spark_four_hca.model import load_config


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
RESULTS_DIR = ROOT / "results"
V1_SCHEMA = SCHEMA_DIR / "result-v1.schema.json"
V2_SCHEMA = SCHEMA_DIR / "result-v2.schema.json"
V3_SCHEMA = SCHEMA_DIR / "result-v3.schema.json"
V3_RUNTIME_SCHEMA = SCHEMA_DIR / "result-v3-runtime.schema.json"

COUNTER_NAMES = [
    "rx_crc_errors_phy",
    "rx_discards_phy",
    "rx_out_of_buffer",
    "rx_symbol_err_phy",
    "tx_discards_phy",
]
PATH_NAMES = ("p0-d0", "p0-d2", "p1-d0", "p1-d2")


class SchemaValidationError(AssertionError):
    pass


class Draft202012SubsetValidator:
    """Small stdlib-only validator for the keywords used by these schemas."""

    def __init__(self, root_schema: Path):
        self.root_schema = root_schema.resolve()
        self.documents: dict[Path, object] = {}

    def _document(self, path: Path) -> object:
        resolved = path.resolve()
        if resolved not in self.documents:
            self.documents[resolved] = json.loads(resolved.read_text(encoding="utf-8"))
        return self.documents[resolved]

    def _resolve(self, ref: str, current_document: Path) -> tuple[object, Path]:
        source, marker, fragment = ref.partition("#")
        target_document = (
            (current_document.parent / source).resolve() if source else current_document
        )
        node = self._document(target_document)
        if marker:
            if fragment and not fragment.startswith("/"):
                raise SchemaValidationError(f"unsupported reference fragment: {ref}")
            for raw_part in fragment.split("/")[1:] if fragment else ():
                part = raw_part.replace("~1", "/").replace("~0", "~")
                try:
                    node = node[part]
                except (KeyError, TypeError) as exc:
                    raise SchemaValidationError(f"unresolved reference: {ref}") from exc
        return node, target_document

    @staticmethod
    def _matches_type(instance: object, expected: str) -> bool:
        if expected == "null":
            return instance is None
        if expected == "boolean":
            return isinstance(instance, bool)
        if expected == "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        if expected == "number":
            return isinstance(instance, (int, float)) and not isinstance(instance, bool)
        if expected == "string":
            return isinstance(instance, str)
        if expected == "array":
            return isinstance(instance, list)
        if expected == "object":
            return isinstance(instance, dict)
        raise SchemaValidationError(f"test validator does not implement type {expected!r}")

    def validate(self, instance: object) -> None:
        self._validate(instance, self._document(self.root_schema), self.root_schema, "$")

    def _validate(
        self,
        instance: object,
        schema: object,
        current_document: Path,
        location: str,
    ) -> None:
        if schema is True:
            return
        if schema is False:
            raise SchemaValidationError(f"{location}: false schema rejected value")
        if not isinstance(schema, dict):
            raise SchemaValidationError(f"{location}: schema must be an object or boolean")

        if "$ref" in schema:
            target, target_document = self._resolve(schema["$ref"], current_document)
            self._validate(instance, target, target_document, location)

        if "oneOf" in schema:
            matches = 0
            for option in schema["oneOf"]:
                try:
                    self._validate(instance, option, current_document, location)
                except SchemaValidationError:
                    continue
                matches += 1
            if matches != 1:
                raise SchemaValidationError(
                    f"{location}: expected exactly one oneOf match, got {matches}"
                )

        if "const" in schema and instance != schema["const"]:
            raise SchemaValidationError(
                f"{location}: expected constant {schema['const']!r}, got {instance!r}"
            )
        if "enum" in schema and instance not in schema["enum"]:
            raise SchemaValidationError(f"{location}: value is outside enum")

        if "type" in schema:
            expected_types = schema["type"]
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            if not any(self._matches_type(instance, item) for item in expected_types):
                raise SchemaValidationError(
                    f"{location}: expected type {schema['type']!r}, got {type(instance).__name__}"
                )

        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if "minimum" in schema and instance < schema["minimum"]:
                raise SchemaValidationError(f"{location}: below minimum")
            if "maximum" in schema and instance > schema["maximum"]:
                raise SchemaValidationError(f"{location}: above maximum")
            if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
                raise SchemaValidationError(f"{location}: below exclusive minimum")
            if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
                raise SchemaValidationError(f"{location}: above exclusive maximum")

        if isinstance(instance, str):
            if "minLength" in schema and len(instance) < schema["minLength"]:
                raise SchemaValidationError(f"{location}: shorter than minLength")
            if "pattern" in schema and re.search(schema["pattern"], instance) is None:
                raise SchemaValidationError(f"{location}: does not match pattern")
            if schema.get("format") == "date-time":
                try:
                    datetime.fromisoformat(instance.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise SchemaValidationError(f"{location}: invalid date-time") from exc

        if isinstance(instance, dict):
            required = schema.get("required", [])
            missing = sorted(set(required) - set(instance))
            if missing:
                raise SchemaValidationError(f"{location}: missing required fields {missing}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extras = sorted(set(instance) - set(properties))
                if extras:
                    raise SchemaValidationError(f"{location}: unexpected fields {extras}")
            for name, child_schema in properties.items():
                if name in instance:
                    self._validate(
                        instance[name], child_schema, current_document, f"{location}.{name}"
                    )

        if isinstance(instance, list):
            if "minItems" in schema and len(instance) < schema["minItems"]:
                raise SchemaValidationError(f"{location}: fewer than minItems")
            if "maxItems" in schema and len(instance) > schema["maxItems"]:
                raise SchemaValidationError(f"{location}: more than maxItems")
            if schema.get("uniqueItems"):
                canonical = [json.dumps(item, sort_keys=True) for item in instance]
                if len(set(canonical)) != len(canonical):
                    raise SchemaValidationError(f"{location}: duplicate array entries")
            prefix_items = schema.get("prefixItems", [])
            for index, child_schema in enumerate(prefix_items[: len(instance)]):
                self._validate(
                    instance[index], child_schema, current_document, f"{location}[{index}]"
                )
            if "items" in schema:
                start = len(prefix_items) if prefix_items else 0
                for index in range(start, len(instance)):
                    self._validate(
                        instance[index],
                        schema["items"],
                        current_document,
                        f"{location}[{index}]",
                    )


def _counter_snapshots(value: int = 0) -> list[dict]:
    return [
        {
            "node_role": role,
            "path": path,
            "coverage_complete": True,
            "values": {name: value for name in COUNTER_NAMES},
        }
        for role in ("head", "worker")
        for path in PATH_NAMES
    ]


def _accepted_check() -> dict:
    return {
        "status": "passed",
        "ok": True,
        "summary": {"passed": 112, "warned": 0, "failed": 0},
        "link_error_counters": {
            "counter_names": list(COUNTER_NAMES),
            "snapshots": _counter_snapshots(),
        },
    }


def _accepted_counter_delta() -> dict:
    return {
        "counter_names": list(COUNTER_NAMES),
        "ok": True,
        "status": "passed",
        "deltas": [
            {
                "node_role": role,
                "path": path,
                "values": {name: 0 for name in COUNTER_NAMES},
            }
            for role in ("head", "worker")
            for path in PATH_NAMES
        ],
        "positive_deltas": [],
        "reset_counters": [],
    }


def _current_v3_payload(*, median_gbps: float = 222.88) -> dict:
    config = load_config(ROOT / "examples/pair.toml")
    started = "2026-08-14T03:29:00.000000Z"
    finished = "2026-08-14T03:32:00.000000Z"
    path_rows = [
        {
            "path": path,
            "physical_port": physical_port,
            "pcie_domain": pcie_domain,
            "rdma_device": rdma_device,
            "control_port": 18515 + index,
            "server_cpu": 5 + index,
            "client_cpu": 5 + index,
            "bytes": 65536,
            "iterations": 3400000 + index,
            "peak_gbps": 0.0,
            "average_gbps": median_gbps / 2.0,
            "message_rate_mpps": 0.2125,
        }
        for index, (path, physical_port, pcie_domain, rdma_device) in enumerate(
            (
                ("p0-d0", 0, 0, "rocep1s0f0"),
                ("p1-d2", 1, 2, "roceP2p1s0f1"),
            )
        )
    ]
    trial = {
        "started_at": started,
        "finished_at": finished,
        "trial": 1,
        "mode": "diagonal-a",
        "direction": "worker-to-head",
        "duration_seconds": 30,
        "message_size_bytes": 65536,
        "qps": 2,
        "tx_depth": 128,
        "path_count": 2,
        "physical_ports": [0, 1],
        "pcie_domains": [0, 2],
        "paths": path_rows,
        "aggregate_gbps": median_gbps,
        "aggregate_GBps": median_gbps / 8.0,
        "path_spread_gbps": 0.0,
        "path_spread_percent_of_mean": 0.0,
        "fraction_of_two_x4_raw_ceiling": round(median_gbps / 252.061538, 6),
    }
    check = _accepted_check()
    windows = (
        ("2026-08-14T03:29:00.000000Z", "2026-08-14T03:29:30.000000Z"),
        ("2026-08-14T03:29:31.000000Z", "2026-08-14T03:30:01.000000Z"),
        ("2026-08-14T03:30:02.000000Z", "2026-08-14T03:30:32.000000Z"),
        ("2026-08-14T03:30:33.000000Z", "2026-08-14T03:31:03.000000Z"),
        ("2026-08-14T03:31:04.000000Z", "2026-08-14T03:31:34.000000Z"),
    )
    trials = [deepcopy(trial) for _ in windows]
    schedule_plan = []
    for index, item in enumerate(trials, start=1):
        item["trial"] = index
        item["started_at"], item["finished_at"] = windows[index - 1]
        schedule_plan.append(
            {
                "position": index,
                "mode": "diagonal-a",
                "direction": "worker-to-head",
                "message_size_bytes": 65536,
                "qps": 2,
                "tx_depth": 128,
                "repeat": index,
            }
        )
    payload = _result_payload(
        config=config,
        metadata=[
            {
                "role": role,
                "label": role,
                "kernel": "6.17.0-1029-nvidia",
                "perftest_version": "Version: 6.20",
                "driver": "mlx5_core",
                "firmware": "28.45.4028",
            }
            for role in ("head", "worker")
        ],
        started_at=started,
        finished_at=finished,
        modes=["diagonal-a"],
        directions=["worker-to-head"],
        message_sizes=[65536],
        qps_values=[2],
        tx_depths=[128],
        duration=30,
        repeats=5,
        cooldown_seconds=5.0,
        schedule="interleaved",
        schedule_seed=0,
        schedule_plan=schedule_plan,
        preflight_status=check,
        trials=trials,
        planned_trials=5,
        run_status="measurement-accepted",
        measurement_accepted=True,
    )
    payload["validation"] = {
        "preflight": check,
        "postflight": deepcopy(check),
        "link_error_counter_delta": _accepted_counter_delta(),
    }
    return payload


def _legacy_v2_payload() -> dict:
    payload = _current_v3_payload()
    payload["schema_version"] = 2
    payload["complete"] = payload.pop("trials_complete")
    payload["accepted"] = payload.pop("measurement_accepted")
    payload["run_status"] = "accepted"
    payload.pop("implementation_fingerprint_sha256")
    payload.pop("qualification")
    return payload


class ResultSchemaTests(unittest.TestCase):
    def test_schemas_are_draft_2020_12_json_and_all_refs_resolve(self):
        for path in (V1_SCHEMA, V2_SCHEMA, V3_SCHEMA, V3_RUNTIME_SCHEMA):
            with self.subTest(path=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
                )
                self.assertEqual(schema["$id"], path.name)
                validator = Draft202012SubsetValidator(path)

                def walk(node: object, document: Path) -> None:
                    if isinstance(node, dict):
                        if "$ref" in node:
                            target, target_document = validator._resolve(node["$ref"], document)
                            self.assertIsInstance(target, (dict, bool))
                            self.assertTrue(target_document.is_file())
                        if node.get("type") == "object":
                            self.assertIs(
                                node.get("additionalProperties"),
                                False,
                                f"object schema is not closed in {path.name}",
                            )
                        for child in node.values():
                            walk(child, document)
                    elif isinstance(node, list):
                        for child in node:
                            walk(child, document)

                walk(schema, path)

    def test_top_level_contracts_require_every_declared_field(self):
        v1 = json.loads(V1_SCHEMA.read_text(encoding="utf-8"))
        v2 = json.loads(V2_SCHEMA.read_text(encoding="utf-8"))
        v3 = json.loads(V3_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(set(v1["required"]), set(v1["properties"]))
        self.assertEqual(set(v2["required"]), set(v2["properties"]))
        self.assertEqual(set(v3["required"]), set(v3["properties"]))
        self.assertEqual(v1["properties"]["schema_version"]["const"], 1)
        self.assertEqual(v2["properties"]["schema_version"]["const"], 2)
        self.assertEqual(v3["properties"]["schema_version"]["const"], 3)
        self.assertNotIn("accepted", v1["properties"])
        self.assertIs(v2["properties"]["accepted"]["const"], True)
        self.assertIs(v2["properties"]["complete"]["const"], True)
        self.assertEqual(v2["properties"]["run_status"]["const"], "accepted")
        self.assertNotIn("accepted", v3["properties"])
        self.assertNotIn("complete", v3["properties"])
        self.assertIs(v3["properties"]["trials_complete"]["const"], True)
        self.assertIs(v3["properties"]["measurement_accepted"]["const"], True)
        self.assertEqual(
            v3["properties"]["run_status"]["const"], "measurement-accepted"
        )

    def test_all_recorded_v1_results_validate_and_obey_completion_semantics(self):
        validator = Draft202012SubsetValidator(V1_SCHEMA)
        sources = sorted(RESULTS_DIR.glob("*.json"))
        self.assertGreaterEqual(len(sources), 3)
        for source in sources:
            with self.subTest(source=source.name):
                payload = json.loads(source.read_text(encoding="utf-8"))
                validator.validate(payload)
                self.assertEqual(
                    payload["complete"],
                    payload["completed_trials"] == payload["planned_trials"],
                )

    def test_current_accepted_payload_shape_validates_as_v2(self):
        payload = _legacy_v2_payload()
        Draft202012SubsetValidator(V2_SCHEMA).validate(payload)
        self.assertEqual(payload["completed_trials"], payload["planned_trials"])
        self.assertEqual(len(payload["trials"]), payload["planned_trials"])
        self.assertEqual(len(payload["schedule_plan"]), payload["planned_trials"])

    def test_v2_rejects_unaccepted_or_link_error_payloads(self):
        validator = Draft202012SubsetValidator(V2_SCHEMA)
        mutations = []

        unaccepted = _legacy_v2_payload()
        unaccepted["accepted"] = False
        mutations.append(unaccepted)

        incomplete = _legacy_v2_payload()
        incomplete["complete"] = False
        mutations.append(incomplete)

        wrong_status = _legacy_v2_payload()
        wrong_status["run_status"] = "awaiting-postflight"
        mutations.append(wrong_status)

        link_error = _legacy_v2_payload()
        link_error["validation"]["link_error_counter_delta"]["deltas"][0]["values"][
            "rx_crc_errors_phy"
        ] = 1
        mutations.append(link_error)

        unknown_field = _legacy_v2_payload()
        unknown_field["private_endpoint"] = "192.0.2.1"
        mutations.append(unknown_field)

        for index, payload in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(SchemaValidationError):
                validator.validate(payload)

    def test_versions_are_not_structurally_interchangeable(self):
        v1_payload = json.loads(
            (RESULTS_DIR / "reference-interleaved-2026-08-13.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaises(SchemaValidationError):
            Draft202012SubsetValidator(V2_SCHEMA).validate(v1_payload)
        with self.assertRaises(SchemaValidationError):
            Draft202012SubsetValidator(V1_SCHEMA).validate(_legacy_v2_payload())
        with self.assertRaises(SchemaValidationError):
            Draft202012SubsetValidator(V2_SCHEMA).validate(_current_v3_payload())
        with self.assertRaises(SchemaValidationError):
            Draft202012SubsetValidator(V3_SCHEMA).validate(_legacy_v2_payload())

    def test_v3_accepts_qualified_below_floor_ineligible_and_unpinned_measurements(self):
        validator = Draft202012SubsetValidator(V3_SCHEMA)

        qualified = _current_v3_payload()
        validator.validate(qualified)
        self.assertTrue(qualified["measurement_accepted"])
        self.assertTrue(qualified["qualification"]["qualified"])

        below = _current_v3_payload(median_gbps=214.0)
        validator.validate(below)
        self.assertEqual(
            below["qualification"]["status"], "below-performance-floor"
        )

        ineligible = _current_v3_payload()
        ineligible["parameters"]["cooldown_seconds"] = 1.0
        ineligible["qualification"] = evaluate_standard_profile_qualification(
            ineligible, measurement_final=True
        )
        validator.validate(ineligible)
        self.assertEqual(ineligible["qualification"]["status"], "ineligible")

        unpinned = _current_v3_payload()
        for trial in unpinned["trials"]:
            trial["paths"][1]["server_cpu"] = trial["paths"][0]["server_cpu"]
        unpinned["qualification"] = evaluate_standard_profile_qualification(
            unpinned, measurement_final=True
        )
        validator.validate(unpinned)
        self.assertEqual(
            unpinned["qualification"]["status"],
            "qualification-evidence-incomplete",
        )

    def test_v3_rejects_ambiguous_or_inconsistent_acceptance_and_qualification(self):
        validator = Draft202012SubsetValidator(V3_SCHEMA)
        mutations = []

        generic_accepted = _current_v3_payload()
        generic_accepted["accepted"] = True
        mutations.append(generic_accepted)

        unaccepted = _current_v3_payload()
        unaccepted["measurement_accepted"] = False
        mutations.append(unaccepted)

        wrong_status = _current_v3_payload()
        wrong_status["run_status"] = "accepted"
        mutations.append(wrong_status)

        false_qualification = _current_v3_payload()
        false_qualification["qualification"]["qualified"] = False
        mutations.append(false_qualification)

        low_qualified = _current_v3_payload()
        low_qualified["qualification"]["observed"]["median_aggregate_gbps"] = 214.0
        mutations.append(low_qualified)

        private_field = _current_v3_payload()
        private_field["qualification"]["host"] = "192.0.2.1"
        mutations.append(private_field)

        for index, payload in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(SchemaValidationError):
                validator.validate(payload)

    def test_v3_runtime_schema_covers_checkpoints_failures_and_rejections(self):
        validator = Draft202012SubsetValidator(V3_RUNTIME_SCHEMA)
        config = load_config(ROOT / "examples/pair.toml")
        accepted = _current_v3_payload()
        plan = deepcopy(accepted["schedule_plan"])

        checkpoint = _result_payload(
            config=config,
            metadata=[],
            started_at="2026-08-14T03:29:36.734043Z",
            finished_at="2026-08-14T03:29:37.000000Z",
            modes=["diagonal-a"],
            directions=["worker-to-head"],
            message_sizes=[65536],
            qps_values=[2],
            tx_depths=[128],
            duration=30,
            repeats=5,
            cooldown_seconds=5.0,
            schedule="interleaved",
            schedule_seed=0,
            schedule_plan=plan,
            preflight_status={
                "status": "not-run",
                "reason": "library caller did not provide it",
            },
            trials=[],
            planned_trials=5,
            run_status="initializing",
            measurement_accepted=False,
        )
        validator.validate(checkpoint)
        self.assertFalse(checkpoint["trials_complete"])

        failed = deepcopy(checkpoint)
        failed["run_status"] = "benchmark-failed"
        failed["failure"] = {"error_type": "RuntimeError"}
        validator.validate(failed)

        postflight_error = _current_v3_payload()
        postflight_error["measurement_accepted"] = False
        postflight_error["run_status"] = "postflight-error"
        postflight_error["validation"]["postflight"] = {
            "status": "error",
            "error_type": "RuntimeError",
        }
        postflight_error["validation"]["link_error_counter_delta"] = {
            "status": "unavailable",
            "ok": False,
            "reason": "postflight did not complete",
        }
        postflight_error["qualification"] = evaluate_standard_profile_qualification(
            postflight_error, measurement_final=True
        )
        validator.validate(postflight_error)
        with self.assertRaises(SchemaValidationError):
            Draft202012SubsetValidator(V3_SCHEMA).validate(postflight_error)

        validator.validate(accepted)

    def test_v3_runtime_schema_is_closed_and_rejects_unknown_states(self):
        validator = Draft202012SubsetValidator(V3_RUNTIME_SCHEMA)
        unknown_field = _current_v3_payload()
        unknown_field["accepted"] = True
        with self.assertRaises(SchemaValidationError):
            validator.validate(unknown_field)

        unknown_status = _current_v3_payload()
        unknown_status["run_status"] = "published"
        with self.assertRaises(SchemaValidationError):
            validator.validate(unknown_status)


if __name__ == "__main__":
    unittest.main()
