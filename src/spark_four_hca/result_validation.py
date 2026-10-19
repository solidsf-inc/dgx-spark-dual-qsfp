"""Strict, dependency-free semantic validation for benchmark result JSON.

JSON Schema is useful for checking the shape of a result, but it cannot prove
the cross-field relationships which make the result meaningful.  This module
recomputes those relationships from the path observations and returns only
aggregate values which have survived that validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Mapping, Sequence

from .model import CANONICAL_MODES, CANONICAL_PATHS


PCIE_GEN5_X4_RAW_GBPS = round(32.0 * 4 * (128.0 / 130.0), 6)
TWO_DOMAIN_RAW_CEILING_GBPS = round(PCIE_GEN5_X4_RAW_GBPS * 2, 6)

STANDARD_PROFILE_ID = "dgx-spark-dual-qsfp-standard"
STANDARD_PROFILE_VERSION = 1
STANDARD_PROFILE_MIN_MEDIAN_GBPS = 215.0
STANDARD_PROFILE_MIN_DURATION_SECONDS = 30
STANDARD_PROFILE_MIN_REPEATS = 5
STANDARD_PROFILE_MIN_COOLDOWN_SECONDS = 5.0

_DIRECTIONS = frozenset(("worker-to-head", "head-to-worker"))
_COUNTER_NAMES = (
    "rx_crc_errors_phy",
    "rx_discards_phy",
    "rx_out_of_buffer",
    "rx_symbol_err_phy",
    "tx_discards_phy",
)
_ROLES = frozenset(("head", "worker"))
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUITE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FLOAT_TOLERANCE = 5.000001e-7
_V3_RUN_STATUSES = frozenset(
    {
        "initializing",
        "running",
        "awaiting-postflight",
        "benchmark-failed",
        "interrupted",
        "postflight-error",
        "link-errors-detected",
        "postflight-failed",
        "validation-incomplete",
        "measurement-accepted",
    }
)

ConditionKey = tuple[str, str, int, int, int]
PlanKey = tuple[str, str, int, int, int, int]


class ResultValidationError(ValueError):
    """Raised when a result fails structural or semantic validation."""


@dataclass(frozen=True)
class AggregateSummary:
    """An aggregate row recomputed exclusively from validated trials."""

    mode: str
    direction: str
    message_size_bytes: int
    qps: int
    tx_depth: int
    repeats: int
    median_gbps: float
    mean_gbps: float
    min_gbps: float
    max_gbps: float
    population_stdev_gbps: float
    median_fraction_of_two_x4_raw_ceiling: float

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical JSON representation used by result producers."""

        return asdict(self)


@dataclass(frozen=True)
class ValidatedResult:
    """Security boundary output for summary and import consumers."""

    schema_version: int
    suite_version: str
    trials_complete: bool
    measurement_accepted: bool | None
    run_status: str | None
    completed_trials: int
    planned_trials: int
    config_fingerprint_sha256: str
    implementation_fingerprint_sha256: str | None
    summaries: tuple[AggregateSummary, ...]

    @property
    def recomputed_summaries(self) -> tuple[AggregateSummary, ...]:
        """Explicit alias for integrations which must not trust input summaries."""

        return self.summaries

    def summary_dicts(self) -> list[dict[str, Any]]:
        """Return fresh canonical dictionaries suitable for JSON or rendering."""

        return [summary.as_dict() for summary in self.summaries]


def _fail(context: str, message: str) -> None:
    raise ResultValidationError(f"{context}: {message}")


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(context, "must be an object")
    return value


def _array(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(context, "must be an array")
    return value


def _required(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    try:
        return mapping[key]
    except KeyError as exc:
        raise ResultValidationError(f"{context}: missing {key!r}") from exc


def _integer(
    value: object,
    context: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(context, "must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        _fail(context, f"must be >= {minimum}{upper}")
    return value


def _number(value: object, context: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(context, "must be a finite number")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ResultValidationError(f"{context}: must be a finite number") from exc
    if not math.isfinite(parsed) or parsed < minimum:
        _fail(context, f"must be finite and >= {minimum}")
    return parsed


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        _fail(context, "must be a boolean")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(context, "must be a string")
    return value


def _finite_tree(value: object, context: str = "result") -> None:
    """Reject JSON's non-standard NaN/Infinity values before doing arithmetic."""

    if isinstance(value, float) and not math.isfinite(value):
        _fail(context, "contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _finite_tree(child, f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite_tree(child, f"{context}[{index}]")


def _timestamp(value: object, context: str) -> datetime:
    text = _string(value, context)
    if not text.endswith("Z"):
        _fail(context, "must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ResultValidationError(f"{context}: invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(context, "must identify UTC")
    return parsed


def _equal_number(actual: object, expected: float, context: str) -> None:
    parsed = _number(actual, context)
    if not math.isclose(parsed, expected, rel_tol=0.0, abs_tol=_FLOAT_TOLERANCE):
        _fail(context, f"does not match recomputed value {expected}")


def _unique_values(
    value: object,
    context: str,
    parser: Any,
) -> list[Any]:
    raw = _array(value, context)
    if not raw:
        _fail(context, "must not be empty")
    parsed = [parser(item, f"{context}[{index}]") for index, item in enumerate(raw)]
    if len(set(parsed)) != len(parsed):
        _fail(context, "must not contain duplicates")
    return parsed


def _parse_parameters(root: Mapping[str, Any], schema_version: int) -> tuple[dict[str, Any], int]:
    parameters = _mapping(_required(root, "parameters", "result"), "parameters")

    def mode(value: object, context: str) -> str:
        parsed = _string(value, context)
        if parsed not in CANONICAL_MODES:
            _fail(context, "is not a canonical mode")
        return parsed

    def direction(value: object, context: str) -> str:
        parsed = _string(value, context)
        if parsed not in _DIRECTIONS:
            _fail(context, "is not a supported direction")
        return parsed

    parsed: dict[str, Any] = {
        "modes": _unique_values(_required(parameters, "modes", "parameters"), "parameters.modes", mode),
        "directions": _unique_values(
            _required(parameters, "directions", "parameters"),
            "parameters.directions",
            direction,
        ),
        "message_sizes": _unique_values(
            _required(parameters, "message_sizes", "parameters"),
            "parameters.message_sizes",
            lambda item, context: _integer(item, context, minimum=1, maximum=1 << 30),
        ),
        "qps_values": _unique_values(
            _required(parameters, "qps_values", "parameters"),
            "parameters.qps_values",
            lambda item, context: _integer(item, context, minimum=1, maximum=128),
        ),
        "tx_depths": _unique_values(
            _required(parameters, "tx_depths", "parameters"),
            "parameters.tx_depths",
            lambda item, context: _integer(item, context, minimum=1, maximum=16384),
        ),
        "duration_seconds": _integer(
            _required(parameters, "duration_seconds", "parameters"),
            "parameters.duration_seconds",
            minimum=2,
            maximum=3600,
        ),
        "repeats": _integer(
            _required(parameters, "repeats", "parameters"),
            "parameters.repeats",
            minimum=1,
            maximum=1000,
        ),
        "cooldown_seconds": _number(
            _required(parameters, "cooldown_seconds", "parameters"),
            "parameters.cooldown_seconds",
        ),
        "schedule": _string(_required(parameters, "schedule", "parameters"), "parameters.schedule"),
    }
    if parsed["schedule"] not in ("interleaved", "grouped"):
        _fail("parameters.schedule", "must be interleaved or grouped")
    if schema_version >= 2:
        parsed["schedule_seed"] = _integer(
            _required(parameters, "schedule_seed", "parameters"),
            "parameters.schedule_seed",
            maximum=0xFFFFFFFF,
        )

    cardinality = (
        len(parsed["modes"])
        * len(parsed["directions"])
        * len(parsed["message_sizes"])
        * len(parsed["qps_values"])
        * len(parsed["tx_depths"])
        * parsed["repeats"]
    )
    return parsed, cardinality


def _identity(item: Mapping[str, Any], context: str, *, repeat_key: str) -> PlanKey:
    mode = _string(_required(item, "mode", context), f"{context}.mode")
    if mode not in CANONICAL_MODES:
        _fail(f"{context}.mode", "is not a canonical mode")
    direction = _string(_required(item, "direction", context), f"{context}.direction")
    if direction not in _DIRECTIONS:
        _fail(f"{context}.direction", "is not supported")
    return (
        mode,
        direction,
        _integer(_required(item, "message_size_bytes", context), f"{context}.message_size_bytes", minimum=1),
        _integer(_required(item, "qps", context), f"{context}.qps", minimum=1),
        _integer(_required(item, "tx_depth", context), f"{context}.tx_depth", minimum=1),
        _integer(_required(item, repeat_key, context), f"{context}.{repeat_key}", minimum=1),
    )


def _identity_is_planned(identity: PlanKey, parameters: Mapping[str, Any]) -> bool:
    mode, direction, message_size, qps, tx_depth, repeat = identity
    return bool(
        mode in parameters["modes"]
        and direction in parameters["directions"]
        and message_size in parameters["message_sizes"]
        and qps in parameters["qps_values"]
        and tx_depth in parameters["tx_depths"]
        and 1 <= repeat <= parameters["repeats"]
    )


def _validate_schedule(
    root: Mapping[str, Any],
    schema_version: int,
    parameters: Mapping[str, Any],
    cardinality: int,
) -> tuple[PlanKey, ...] | None:
    if schema_version == 1:
        return None
    raw_plan = _array(_required(root, "schedule_plan", "result"), "schedule_plan")
    if len(raw_plan) != cardinality:
        _fail("schedule_plan", f"must contain the Cartesian plan of {cardinality} entries")
    identities: list[PlanKey] = []
    positions: set[int] = set()
    for index, raw_entry in enumerate(raw_plan):
        context = f"schedule_plan[{index}]"
        entry = _mapping(raw_entry, context)
        position = _integer(_required(entry, "position", context), f"{context}.position", minimum=1)
        if position in positions:
            _fail(f"{context}.position", "is duplicated")
        positions.add(position)
        if position != index + 1:
            _fail(f"{context}.position", f"must be {index + 1} at this array position")
        identity = _identity(entry, context, repeat_key="repeat")
        if not _identity_is_planned(identity, parameters):
            _fail(context, "contains a condition outside parameters")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        _fail("schedule_plan", "contains duplicate Cartesian conditions")
    return tuple(identities)


def _validate_metadata(root: Mapping[str, Any], *, require_complete: bool) -> None:
    metadata = _array(_required(root, "metadata", "result"), "metadata")
    if require_complete and len(metadata) != 2:
        _fail("metadata", "must contain exactly head and worker")
    if len(metadata) > 2:
        _fail("metadata", "must contain at most head and worker")
    roles: list[str] = []
    for index, raw_item in enumerate(metadata):
        context = f"metadata[{index}]"
        item = _mapping(raw_item, context)
        role = _string(_required(item, "role", context), f"{context}.role")
        if role not in _ROLES:
            _fail(f"{context}.role", "must be head or worker")
        roles.append(role)
        label = _string(_required(item, "label", context), f"{context}.label")
        if not _SAFE_LABEL.fullmatch(label):
            _fail(f"{context}.label", "is not public-safe")
        for field in ("kernel", "perftest_version", "driver", "firmware"):
            if not _string(_required(item, field, context), f"{context}.{field}"):
                _fail(f"{context}.{field}", "must not be empty")
    if len(set(roles)) != len(roles):
        _fail("metadata", "roles must be unique")
    if require_complete and set(roles) != _ROLES:
        _fail("metadata", "roles must be unique and exactly head/worker")


def _validate_trial(
    raw_trial: object,
    index: int,
    parameters: Mapping[str, Any],
    ceiling: float,
    root_started: datetime,
    root_finished: datetime,
) -> tuple[PlanKey, ConditionKey, float, datetime, datetime]:
    context = f"trials[{index}]"
    trial = _mapping(raw_trial, context)
    identity = _identity(trial, context, repeat_key="trial")
    if not _identity_is_planned(identity, parameters):
        _fail(context, "does not belong to the declared Cartesian plan")
    mode, direction, message_size, qps, tx_depth, _ = identity
    if _integer(_required(trial, "duration_seconds", context), f"{context}.duration_seconds", minimum=2) != parameters["duration_seconds"]:
        _fail(f"{context}.duration_seconds", "does not match parameters")

    started = _timestamp(_required(trial, "started_at", context), f"{context}.started_at")
    finished = _timestamp(_required(trial, "finished_at", context), f"{context}.finished_at")
    if started > finished:
        _fail(context, "started_at is after finished_at")
    if started < root_started or finished > root_finished:
        _fail(context, "timestamps fall outside the result interval")

    raw_paths = _array(_required(trial, "paths", context), f"{context}.paths")
    expected_path_order = tuple(CANONICAL_MODES[mode])
    expected_path_names = frozenset(expected_path_order)
    if len(raw_paths) != len(expected_path_names):
        _fail(f"{context}.paths", "does not have the canonical mode path count")
    path_count = _integer(_required(trial, "path_count", context), f"{context}.path_count", minimum=1)
    if path_count != len(raw_paths):
        _fail(f"{context}.path_count", "does not equal len(paths)")

    observed_names: set[str] = set()
    control_ports: set[int] = set()
    averages: list[float] = []
    physical_ports: set[int] = set()
    pcie_domains: set[int] = set()
    for path_index, raw_path in enumerate(raw_paths):
        path_context = f"{context}.paths[{path_index}]"
        path = _mapping(raw_path, path_context)
        name = _string(_required(path, "path", path_context), f"{path_context}.path")
        if name in observed_names:
            _fail(f"{context}.paths", f"contains duplicate path {name!r}")
        observed_names.add(name)
        if name not in CANONICAL_PATHS:
            _fail(f"{path_context}.path", "is not canonical")
        expected_port, expected_domain, _netdev, expected_rdma = CANONICAL_PATHS[name]
        port = _integer(_required(path, "physical_port", path_context), f"{path_context}.physical_port")
        domain = _integer(_required(path, "pcie_domain", path_context), f"{path_context}.pcie_domain")
        rdma = _string(_required(path, "rdma_device", path_context), f"{path_context}.rdma_device")
        if (port, domain, rdma) != (expected_port, expected_domain, expected_rdma):
            _fail(path_context, "does not match the canonical path resource mapping")
        physical_ports.add(port)
        pcie_domains.add(domain)
        control_port = _integer(
            _required(path, "control_port", path_context),
            f"{path_context}.control_port",
            minimum=1,
            maximum=65535,
        )
        if control_port in control_ports:
            _fail(f"{context}.paths", "contains a duplicate control port")
        control_ports.add(control_port)
        for cpu_field in ("server_cpu", "client_cpu"):
            cpu = _required(path, cpu_field, path_context)
            if cpu is not None:
                _integer(cpu, f"{path_context}.{cpu_field}")
        if _integer(_required(path, "bytes", path_context), f"{path_context}.bytes", minimum=1) != message_size:
            _fail(f"{path_context}.bytes", "does not match the trial message size")
        _integer(_required(path, "iterations", path_context), f"{path_context}.iterations", minimum=1)
        _number(_required(path, "peak_gbps", path_context), f"{path_context}.peak_gbps")
        averages.append(_number(_required(path, "average_gbps", path_context), f"{path_context}.average_gbps"))
        _number(_required(path, "message_rate_mpps", path_context), f"{path_context}.message_rate_mpps")

    if observed_names != expected_path_names:
        _fail(f"{context}.paths", f"must be exactly {sorted(expected_path_names)}")
    observed_order = tuple(
        _string(
            _required(_mapping(item, f"{context}.paths"), "path", f"{context}.paths"),
            f"{context}.paths.path",
        )
        for item in raw_paths
    )
    if observed_order != expected_path_order:
        _fail(f"{context}.paths", "does not follow the canonical mode path order")

    declared_ports = _array(_required(trial, "physical_ports", context), f"{context}.physical_ports")
    if declared_ports != sorted(physical_ports):
        _fail(f"{context}.physical_ports", "does not match path resources")
    declared_domains = _array(_required(trial, "pcie_domains", context), f"{context}.pcie_domains")
    if declared_domains != sorted(pcie_domains):
        _fail(f"{context}.pcie_domains", "does not match path resources")

    aggregate = round(sum(averages), 6)
    mean_path = statistics.mean(averages)
    spread = round(max(averages) - min(averages), 6) if len(averages) > 1 else 0.0
    spread_percent = round((spread / mean_path * 100.0) if mean_path else 0.0, 6)
    _equal_number(_required(trial, "aggregate_gbps", context), aggregate, f"{context}.aggregate_gbps")
    _equal_number(_required(trial, "aggregate_GBps", context), round(aggregate / 8.0, 6), f"{context}.aggregate_GBps")
    _equal_number(_required(trial, "path_spread_gbps", context), spread, f"{context}.path_spread_gbps")
    _equal_number(
        _required(trial, "path_spread_percent_of_mean", context),
        spread_percent,
        f"{context}.path_spread_percent_of_mean",
    )
    _equal_number(
        _required(trial, "fraction_of_two_x4_raw_ceiling", context),
        round(aggregate / ceiling, 6),
        f"{context}.fraction_of_two_x4_raw_ceiling",
    )
    return identity, identity[:5], aggregate, started, finished


def _recompute_summaries(
    grouped_values: Mapping[ConditionKey, Sequence[float]],
    ceiling: float,
) -> tuple[AggregateSummary, ...]:
    rows: list[AggregateSummary] = []
    for key in sorted(grouped_values):
        values = list(grouped_values[key])
        mode, direction, message_size, qps, tx_depth = key
        median = round(statistics.median(values), 6)
        rows.append(
            AggregateSummary(
                mode=mode,
                direction=direction,
                message_size_bytes=message_size,
                qps=qps,
                tx_depth=tx_depth,
                repeats=len(values),
                median_gbps=median,
                mean_gbps=round(statistics.mean(values), 6),
                min_gbps=round(min(values), 6),
                max_gbps=round(max(values), 6),
                population_stdev_gbps=round(statistics.pstdev(values), 6),
                median_fraction_of_two_x4_raw_ceiling=round(median / ceiling, 6),
            )
        )
    return tuple(rows)


def _validate_supplied_summaries(root: Mapping[str, Any], expected: tuple[AggregateSummary, ...]) -> None:
    supplied = _array(_required(root, "summaries", "result"), "summaries")
    if len(supplied) != len(expected):
        _fail("summaries", f"must contain exactly {len(expected)} recomputed groups")
    integer_fields = ("message_size_bytes", "qps", "tx_depth", "repeats")
    number_fields = (
        "median_gbps",
        "mean_gbps",
        "min_gbps",
        "max_gbps",
        "population_stdev_gbps",
        "median_fraction_of_two_x4_raw_ceiling",
    )
    for index, (raw_row, recomputed) in enumerate(zip(supplied, expected)):
        context = f"summaries[{index}]"
        row = _mapping(raw_row, context)
        expected_dict = recomputed.as_dict()
        for field in ("mode", "direction"):
            if _required(row, field, context) != expected_dict[field]:
                _fail(f"{context}.{field}", f"does not match recomputed value {expected_dict[field]!r}")
        for field in integer_fields:
            actual = _integer(_required(row, field, context), f"{context}.{field}")
            if actual != expected_dict[field]:
                _fail(f"{context}.{field}", f"does not match recomputed value {expected_dict[field]}")
        for field in number_fields:
            _equal_number(_required(row, field, context), expected_dict[field], f"{context}.{field}")


def _validate_snapshot_block(value: object, context: str, *, require_complete: bool) -> dict[tuple[str, str], dict[str, int]]:
    block = _mapping(value, context)
    names = _array(_required(block, "counter_names", context), f"{context}.counter_names")
    snapshots = _array(_required(block, "snapshots", context), f"{context}.snapshots")
    if names != list(_COUNTER_NAMES):
        if require_complete:
            _fail(f"{context}.counter_names", "does not contain the canonical counter allowlist")
        if snapshots:
            _fail(context, "cannot contain snapshots without the canonical counter allowlist")
        return {}
    indexed: dict[tuple[str, str], dict[str, int]] = {}
    for index, raw_snapshot in enumerate(snapshots):
        snapshot_context = f"{context}.snapshots[{index}]"
        snapshot = _mapping(raw_snapshot, snapshot_context)
        role = _string(_required(snapshot, "node_role", snapshot_context), f"{snapshot_context}.node_role")
        path = _string(_required(snapshot, "path", snapshot_context), f"{snapshot_context}.path")
        if role not in _ROLES or path not in CANONICAL_PATHS:
            _fail(snapshot_context, "has a non-canonical identity")
        identity = (role, path)
        if identity in indexed:
            _fail(snapshot_context, "duplicates a counter snapshot identity")
        coverage = _boolean(_required(snapshot, "coverage_complete", snapshot_context), f"{snapshot_context}.coverage_complete")
        values = _mapping(_required(snapshot, "values", snapshot_context), f"{snapshot_context}.values")
        if require_complete and not coverage:
            _fail(snapshot_context, "must have complete counter coverage")
        if coverage and set(values) != set(_COUNTER_NAMES):
            _fail(f"{snapshot_context}.values", "does not contain exactly the counter allowlist")
        parsed_values: dict[str, int] = {}
        for name, raw_counter in values.items():
            if name not in _COUNTER_NAMES:
                _fail(f"{snapshot_context}.values", f"contains non-allowlisted counter {name!r}")
            parsed_values[name] = _integer(raw_counter, f"{snapshot_context}.values.{name}")
        indexed[identity] = parsed_values
    expected_identities = set(itertools.product(_ROLES, CANONICAL_PATHS))
    if require_complete and set(indexed) != expected_identities:
        _fail(context, "must contain exactly one snapshot for each role/path identity")
    return indexed


def _validate_check(value: object, context: str, *, require_accepted: bool) -> dict[tuple[str, str], dict[str, int]]:
    check = _mapping(value, context)
    if require_accepted:
        if _required(check, "status", context) not in ("passed", "passed-with-warnings"):
            _fail(f"{context}.status", "is not healthy")
        if _boolean(_required(check, "ok", context), f"{context}.ok") is not True:
            _fail(f"{context}.ok", "must be true for accepted evidence")
        summary = _mapping(_required(check, "summary", context), f"{context}.summary")
        for field in ("passed", "warned", "failed"):
            _integer(_required(summary, field, f"{context}.summary"), f"{context}.summary.{field}")
        if summary["failed"] != 0:
            _fail(f"{context}.summary.failed", "must be zero for accepted evidence")
    counters = check.get("link_error_counters")
    if counters is None:
        if require_accepted:
            _fail(context, "is missing link_error_counters")
        return {}
    return _validate_snapshot_block(counters, f"{context}.link_error_counters", require_complete=require_accepted)


def _validate_counter_delta(
    value: object,
    before: Mapping[tuple[str, str], Mapping[str, int]],
    after: Mapping[tuple[str, str], Mapping[str, int]],
    *,
    require_accepted: bool,
) -> None:
    context = "validation.link_error_counter_delta"
    block = _mapping(value, context)
    if not before or not after:
        if require_accepted:
            _fail(context, "cannot be verified without complete snapshots")
        return
    if set(before) != set(after):
        _fail(context, "preflight and postflight identities differ")
    if any(
        set(values) != set(_COUNTER_NAMES)
        for snapshots in (before, after)
        for values in snapshots.values()
    ):
        if require_accepted:
            _fail(context, "cannot be verified without complete counter values")
        return
    recomputed = {
        identity: {name: after[identity][name] - before[identity][name] for name in _COUNTER_NAMES}
        for identity in before
    }
    raw_deltas = _array(_required(block, "deltas", context), f"{context}.deltas")
    supplied: dict[tuple[str, str], dict[str, int]] = {}
    for index, raw_delta in enumerate(raw_deltas):
        delta_context = f"{context}.deltas[{index}]"
        delta = _mapping(raw_delta, delta_context)
        identity = (
            _string(_required(delta, "node_role", delta_context), f"{delta_context}.node_role"),
            _string(_required(delta, "path", delta_context), f"{delta_context}.path"),
        )
        if identity in supplied:
            _fail(delta_context, "duplicates a counter delta identity")
        values = _mapping(_required(delta, "values", delta_context), f"{delta_context}.values")
        if set(values) != set(_COUNTER_NAMES):
            _fail(f"{delta_context}.values", "does not contain exactly the counter allowlist")
        supplied[identity] = {
            name: _integer(values[name], f"{delta_context}.values.{name}", minimum=-(1 << 63))
            for name in _COUNTER_NAMES
        }
    expected_delta_rows = [
        {"node_role": role, "path": path, "values": recomputed[(role, path)]}
        for role, path in sorted(recomputed)
    ]
    if supplied != recomputed or raw_deltas != expected_delta_rows:
        _fail(context, "reported deltas do not match recomputed snapshot differences")
    positive_rows = [
        {
            "node_role": role,
            "path": path,
            "values": {
                name: amount
                for name, amount in recomputed[(role, path)].items()
                if amount > 0
            },
        }
        for role, path in sorted(recomputed)
        if any(amount > 0 for amount in recomputed[(role, path)].values())
    ]
    reset_rows = [
        {
            "node_role": role,
            "path": path,
            "values": {
                name: amount
                for name, amount in recomputed[(role, path)].items()
                if amount < 0
            },
        }
        for role, path in sorted(recomputed)
        if any(amount < 0 for amount in recomputed[(role, path)].values())
    ]
    if _array(_required(block, "counter_names", context), f"{context}.counter_names") != list(_COUNTER_NAMES):
        _fail(f"{context}.counter_names", "does not contain the canonical allowlist")
    if _array(_required(block, "positive_deltas", context), f"{context}.positive_deltas") != positive_rows:
        _fail(f"{context}.positive_deltas", "does not match recomputed positive deltas")
    if _array(_required(block, "reset_counters", context), f"{context}.reset_counters") != reset_rows:
        _fail(f"{context}.reset_counters", "does not match recomputed counter resets")
    expected_ok = not positive_rows and not reset_rows
    expected_status = "passed" if expected_ok else "failed"
    if _boolean(_required(block, "ok", context), f"{context}.ok") != expected_ok:
        _fail(f"{context}.ok", "does not match recomputed counter state")
    if _required(block, "status", context) != expected_status:
        _fail(f"{context}.status", f"must be {expected_status!r}")
    if require_accepted and not expected_ok:
        _fail(context, "accepted evidence must have zero counter deltas")


def _validate_evidence(root: Mapping[str, Any], *, require_accepted: bool) -> None:
    validation = _mapping(_required(root, "validation", "result"), "validation")
    before = _validate_check(_required(validation, "preflight", "validation"), "validation.preflight", require_accepted=require_accepted)
    after = _validate_check(_required(validation, "postflight", "validation"), "validation.postflight", require_accepted=require_accepted)
    delta = validation.get("link_error_counter_delta")
    if delta is None:
        if require_accepted:
            _fail("validation", "is missing link_error_counter_delta")
        return
    _validate_counter_delta(delta, before, after, require_accepted=require_accepted)


def _profile_eligibility_reasons(parameters: Mapping[str, Any], root: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field, expected, reason in (
        ("modes", ["diagonal-a"], "mode-set-mismatch"),
        ("directions", ["worker-to-head"], "direction-set-mismatch"),
        ("qps_values", [2], "qps-set-mismatch"),
        ("message_sizes", [65536], "message-size-set-mismatch"),
        ("tx_depths", [128], "tx-depth-set-mismatch"),
    ):
        if parameters[field] != expected:
            reasons.append(reason)
    for field, minimum, reason in (
        ("duration_seconds", STANDARD_PROFILE_MIN_DURATION_SECONDS, "duration-below-minimum"),
        ("repeats", STANDARD_PROFILE_MIN_REPEATS, "repeats-below-minimum"),
        ("cooldown_seconds", STANDARD_PROFILE_MIN_COOLDOWN_SECONDS, "cooldown-below-minimum"),
    ):
        if parameters[field] < minimum:
            reasons.append(reason)
    if parameters["schedule"] != "interleaved":
        reasons.append("schedule-mismatch")
    validation = root.get("validation")
    preflight = validation.get("preflight") if isinstance(validation, Mapping) else None
    if not (
        isinstance(preflight, Mapping)
        and preflight.get("ok") is True
        and preflight.get("status") in ("passed", "passed-with-warnings")
    ):
        reasons.append("preflight-not-healthy")
    return reasons


def _cpu_pinning_verified(raw_trials: Sequence[Any]) -> bool:
    matched = 0
    for raw_trial in raw_trials:
        if not isinstance(raw_trial, Mapping):
            return False
        identity = (
            raw_trial.get("mode"),
            raw_trial.get("direction"),
            raw_trial.get("message_size_bytes"),
            raw_trial.get("qps"),
            raw_trial.get("tx_depth"),
        )
        if identity != ("diagonal-a", "worker-to-head", 65536, 2, 128):
            continue
        paths = raw_trial.get("paths")
        if not isinstance(paths, list) or len(paths) != 2:
            return False
        server = [path.get("server_cpu") for path in paths if isinstance(path, Mapping)]
        client = [path.get("client_cpu") for path in paths if isinstance(path, Mapping)]
        if (
            len(server) != 2
            or len(client) != 2
            or any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in server + client)
            or len(set(server)) != 2
            or len(set(client)) != 2
        ):
            return False
        matched += 1
    return matched > 0


def _validate_qualification(
    root: Mapping[str, Any],
    parameters: Mapping[str, Any],
    summaries: Sequence[AggregateSummary],
    raw_trials: Sequence[Any],
    measurement_accepted: bool,
) -> None:
    context = "qualification"
    block = _mapping(_required(root, "qualification", "result"), context)
    expected_criteria = {
        "mode": "diagonal-a",
        "direction": "worker-to-head",
        "qps": 2,
        "message_size_bytes": 65536,
        "tx_depth": 128,
        "minimum_duration_seconds": STANDARD_PROFILE_MIN_DURATION_SECONDS,
        "minimum_repeats": STANDARD_PROFILE_MIN_REPEATS,
        "minimum_cooldown_seconds": STANDARD_PROFILE_MIN_COOLDOWN_SECONDS,
        "schedule": "interleaved",
        "minimum_median_aggregate_gbps": STANDARD_PROFILE_MIN_MEDIAN_GBPS,
        "measurement_must_be_accepted": True,
        "preflight_must_be_healthy": True,
        "link_error_counter_delta_must_be_zero": True,
        "cpu_pinning_must_be_distinct": True,
    }
    if _required(block, "profile_id", context) != STANDARD_PROFILE_ID:
        _fail(f"{context}.profile_id", "is unsupported")
    if _required(block, "profile_version", context) != STANDARD_PROFILE_VERSION:
        _fail(f"{context}.profile_version", "is unsupported")
    if _mapping(_required(block, "criteria", context), f"{context}.criteria") != expected_criteria:
        _fail(f"{context}.criteria", "does not match the standard profile")

    eligibility_reasons = _profile_eligibility_reasons(parameters, root)
    eligible = not eligibility_reasons
    if _boolean(_required(block, "eligible", context), f"{context}.eligible") != eligible:
        _fail(f"{context}.eligible", "does not match recomputed profile eligibility")

    standard = next(
        (
            row
            for row in summaries
            if (row.mode, row.direction, row.message_size_bytes, row.qps, row.tx_depth)
            == ("diagonal-a", "worker-to-head", 65536, 2, 128)
        ),
        None,
    )
    median = standard.median_gbps if standard is not None else None
    count = standard.repeats if standard is not None else 0
    cpu_verified = _cpu_pinning_verified(raw_trials)
    observed = _mapping(_required(block, "observed", context), f"{context}.observed")
    supplied_median = _required(observed, "median_aggregate_gbps", f"{context}.observed")
    if median is None:
        if supplied_median is not None:
            _fail(f"{context}.observed.median_aggregate_gbps", "must be null without observations")
    else:
        _equal_number(supplied_median, median, f"{context}.observed.median_aggregate_gbps")
    if _integer(_required(observed, "trial_count", f"{context}.observed"), f"{context}.observed.trial_count") != count:
        _fail(f"{context}.observed.trial_count", "does not match recomputed observation count")
    if _boolean(_required(observed, "cpu_pinning_verified", f"{context}.observed"), f"{context}.observed.cpu_pinning_verified") != cpu_verified:
        _fail(f"{context}.observed.cpu_pinning_verified", "does not match trial CPU evidence")

    if measurement_accepted:
        if not eligible:
            expected_status, expected_reasons, expected_qualified = "ineligible", eligibility_reasons, False
        elif median is None or count < STANDARD_PROFILE_MIN_REPEATS:
            expected_status, expected_reasons, expected_qualified = (
                "insufficient-observations",
                ["standard-profile-observation-incomplete"],
                False,
            )
        elif not cpu_verified:
            expected_status, expected_reasons, expected_qualified = (
                "qualification-evidence-incomplete",
                ["cpu-pinning-not-demonstrated"],
                False,
            )
        elif median < STANDARD_PROFILE_MIN_MEDIAN_GBPS:
            expected_status, expected_reasons, expected_qualified = (
                "below-performance-floor",
                ["median-below-minimum"],
                False,
            )
        else:
            expected_status, expected_reasons, expected_qualified = "qualified", [], True
    else:
        status = _required(block, "status", context)
        if status == "awaiting-measurement-acceptance":
            expected_status = status
            expected_reasons = ["measurement-awaiting-postflight", *eligibility_reasons]
        elif status == "measurement-unaccepted":
            expected_status = status
            expected_reasons = ["measurement-unaccepted", *eligibility_reasons]
        else:
            _fail(f"{context}.status", "cannot be final while measurement_accepted is false")
        expected_qualified = False

    if _required(block, "status", context) != expected_status:
        _fail(f"{context}.status", f"must be {expected_status!r}")
    if _array(_required(block, "reasons", context), f"{context}.reasons") != expected_reasons:
        _fail(f"{context}.reasons", f"must equal recomputed reasons {expected_reasons!r}")
    if _boolean(_required(block, "qualified", context), f"{context}.qualified") != expected_qualified:
        _fail(f"{context}.qualified", "does not match recomputed qualification")


def validate_result(payload: object) -> ValidatedResult:
    """Validate a result and return aggregates recomputed from path observations.

    Versions 1, 2, and 3 are supported.  Version 1 is historical evidence and
    has no measurement-acceptance decision.  Versions 2 and 3 additionally
    bind the recorded trials to an explicit schedule.  Accepted v2/v3 results
    must carry complete, zero-delta preflight/postflight counter evidence.
    This validates relationships JSON Schema cannot express; callers should
    also apply the matching closed JSON Schema when accepting external data.
    """

    _finite_tree(payload)
    root = _mapping(payload, "result")
    schema_version = _integer(_required(root, "schema_version", "result"), "schema_version", minimum=1, maximum=3)

    suite_version = _string(_required(root, "suite_version", "result"), "suite_version")
    if not _SUITE_VERSION.fullmatch(suite_version):
        _fail("suite_version", "must be a public semantic version")
    pair_label = _string(_required(root, "pair_label", "result"), "pair_label")
    if not _SAFE_LABEL.fullmatch(pair_label):
        _fail("pair_label", "is not public-safe")

    config_fingerprint = _string(
        _required(root, "config_fingerprint_sha256", "result"),
        "config_fingerprint_sha256",
    )
    if not _SHA256.fullmatch(config_fingerprint):
        _fail("config_fingerprint_sha256", "must be a lowercase SHA-256 digest")
    implementation_fingerprint: str | None = None
    if schema_version == 3:
        implementation_fingerprint = _string(
            _required(root, "implementation_fingerprint_sha256", "result"),
            "implementation_fingerprint_sha256",
        )
        if not _SHA256.fullmatch(implementation_fingerprint):
            _fail("implementation_fingerprint_sha256", "must be a lowercase SHA-256 digest")

    if _required(root, "metric", "result") != "unidirectional host-memory RDMA write payload bandwidth":
        _fail("metric", "is unsupported")
    if _required(root, "units", "result") != "Gbit/s (decimal)":
        _fail("units", "is unsupported")
    _equal_number(
        _required(root, "pcie_gen5_x4_raw_gbps_per_domain", "result"),
        PCIE_GEN5_X4_RAW_GBPS,
        "pcie_gen5_x4_raw_gbps_per_domain",
    )
    _equal_number(
        _required(root, "two_domain_raw_ceiling_gbps", "result"),
        TWO_DOMAIN_RAW_CEILING_GBPS,
        "two_domain_raw_ceiling_gbps",
    )
    ceiling = _number(root["two_domain_raw_ceiling_gbps"], "two_domain_raw_ceiling_gbps")

    root_started = _timestamp(_required(root, "started_at", "result"), "started_at")
    root_finished = _timestamp(_required(root, "finished_at", "result"), "finished_at")
    if root_started > root_finished:
        _fail("result", "started_at is after finished_at")
    require_complete_metadata = (
        schema_version in (1, 2) or root.get("measurement_accepted") is True
    )
    _validate_metadata(root, require_complete=require_complete_metadata)

    parameters, cardinality = _parse_parameters(root, schema_version)
    planned = _integer(_required(root, "planned_trials", "result"), "planned_trials", minimum=1)
    if planned != cardinality:
        _fail("planned_trials", f"must equal Cartesian parameter cardinality {cardinality}")
    plan = _validate_schedule(root, schema_version, parameters, cardinality)

    raw_trials = _array(_required(root, "trials", "result"), "trials")
    completed = _integer(_required(root, "completed_trials", "result"), "completed_trials")
    if completed != len(raw_trials):
        _fail("completed_trials", f"must equal len(trials), which is {len(raw_trials)}")
    if completed > planned:
        _fail("completed_trials", "cannot exceed planned_trials")

    trial_identities: list[PlanKey] = []
    grouped: dict[ConditionKey, list[float]] = {}
    previous_finished: datetime | None = None
    for index, raw_trial in enumerate(raw_trials):
        identity, condition, aggregate, started, finished = _validate_trial(
            raw_trial,
            index,
            parameters,
            ceiling,
            root_started,
            root_finished,
        )
        if previous_finished is not None and started < previous_finished:
            _fail(f"trials[{index}].started_at", "precedes the prior trial's finish")
        previous_finished = finished
        trial_identities.append(identity)
        grouped.setdefault(condition, []).append(aggregate)
    if len(set(trial_identities)) != len(trial_identities):
        _fail("trials", "contains a duplicate Cartesian trial identity")
    if plan is not None and tuple(trial_identities) != plan[:completed]:
        _fail("trials", "do not correspond position-for-position to schedule_plan")
    if schema_version == 1 and completed == planned and len(set(trial_identities)) != cardinality:
        _fail("trials", "does not cover the complete Cartesian plan")

    recomputed = _recompute_summaries(grouped, ceiling)
    _validate_supplied_summaries(root, recomputed)

    run_status: str | None = None
    measurement_accepted: bool | None = None
    if schema_version == 1:
        trials_complete = _boolean(_required(root, "complete", "result"), "complete")
        if trials_complete != (completed == planned):
            _fail("complete", "must be true exactly when all planned trials are present")
    elif schema_version == 2:
        trials_complete = _boolean(_required(root, "complete", "result"), "complete")
        measurement_accepted = _boolean(_required(root, "accepted", "result"), "accepted")
        run_status = _string(_required(root, "run_status", "result"), "run_status")
        if not (trials_complete and measurement_accepted and completed == planned and run_status == "accepted"):
            _fail("result", "schema v2 represents only complete accepted results")
        _validate_evidence(root, require_accepted=True)
    else:
        trials_complete = _boolean(_required(root, "trials_complete", "result"), "trials_complete")
        measurement_accepted = _boolean(
            _required(root, "measurement_accepted", "result"),
            "measurement_accepted",
        )
        run_status = _string(_required(root, "run_status", "result"), "run_status")
        if run_status not in _V3_RUN_STATUSES:
            _fail("run_status", "is unsupported")
        if trials_complete != (completed == planned):
            _fail("trials_complete", "must match completed/planned cardinality")
        if measurement_accepted and not trials_complete:
            _fail("measurement_accepted", "cannot be true for incomplete trials")
        if measurement_accepted != (run_status == "measurement-accepted"):
            _fail("run_status", "must identify measurement acceptance exactly")
        _validate_evidence(root, require_accepted=measurement_accepted)
        _validate_qualification(
            root,
            parameters,
            recomputed,
            raw_trials,
            measurement_accepted,
        )

    return ValidatedResult(
        schema_version=schema_version,
        suite_version=suite_version,
        trials_complete=trials_complete,
        measurement_accepted=measurement_accepted,
        run_status=run_status,
        completed_trials=completed,
        planned_trials=planned,
        config_fingerprint_sha256=config_fingerprint,
        implementation_fingerprint_sha256=implementation_fingerprint,
        summaries=recomputed,
    )


def load_and_validate_result(path: str | Path) -> ValidatedResult:
    """Load UTF-8 JSON from *path* and pass it through :func:`validate_result`."""

    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResultValidationError(f"cannot read result {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ResultValidationError(f"invalid JSON in {source}: {exc}") from exc
    return validate_result(payload)
