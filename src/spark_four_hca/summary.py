"""Public-safe Markdown summaries for benchmark result JSON."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .benchmark import (
    STANDARD_PROFILE_ID,
    STANDARD_PROFILE_MIN_COOLDOWN_SECONDS,
    STANDARD_PROFILE_MIN_DURATION_SECONDS,
    STANDARD_PROFILE_MIN_MEDIAN_GBPS,
    STANDARD_PROFILE_MIN_REPEATS,
    STANDARD_PROFILE_VERSION,
)
from .result_validation import ResultValidationError, validate_result


class SummaryError(ValueError):
    """Raised when result JSON cannot be safely summarized."""


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUITE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_DIRECTIONS = {"worker-to-head", "head-to-worker"}
_PUBLIC_MODES = {
    "single-p0-d0",
    "single-p0-d2",
    "single-p1-d0",
    "single-p1-d2",
    "same-cage-p0",
    "same-cage-p1",
    "same-domain-d0",
    "same-domain-d2",
    "diagonal-a",
    "diagonal-b",
    "all-four",
}
_QUALIFICATION_STATUSES = {
    "awaiting-measurement-acceptance",
    "measurement-unaccepted",
    "ineligible",
    "insufficient-observations",
    "qualification-evidence-incomplete",
    "below-performance-floor",
    "qualified",
}
_QUALIFICATION_REASONS = {
    "measurement-awaiting-postflight",
    "measurement-unaccepted",
    "mode-set-mismatch",
    "direction-set-mismatch",
    "qps-set-mismatch",
    "message-size-set-mismatch",
    "tx-depth-set-mismatch",
    "duration-below-minimum",
    "repeats-below-minimum",
    "cooldown-below-minimum",
    "schedule-mismatch",
    "preflight-not-healthy",
    "standard-profile-observation-incomplete",
    "cpu-pinning-not-demonstrated",
    "median-below-minimum",
}


@dataclass(frozen=True)
class SummaryRow:
    mode: str
    direction: str
    message_size_bytes: int
    qps: int
    tx_depth: int
    repeats: int
    median_gbps: float
    min_gbps: float
    max_gbps: float
    population_stdev_gbps: float


@dataclass(frozen=True)
class QualificationSummary:
    profile_id: str
    profile_version: int
    eligible: bool
    qualified: bool
    status: str
    reasons: tuple[str, ...]
    median_aggregate_gbps: float | None
    trial_count: int
    cpu_pinning_verified: bool


@dataclass(frozen=True)
class PublicResultSummary:
    schema_version: int
    suite_version: str | None
    input_sha256: str | None
    trials_complete: bool
    measurement_accepted: bool | None
    completed_trials: int
    planned_trials: int
    two_domain_raw_ceiling_gbps: float
    rows: tuple[SummaryRow, ...]
    qualification: QualificationSummary | None

    @property
    def complete(self) -> bool:
        """Compatibility alias for callers that treated v1/v2 completion as status."""

        return self.trials_complete

    @property
    def accepted(self) -> bool | None:
        """Compatibility alias for callers reading historical v2 summaries."""

        return self.measurement_accepted


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SummaryError(f"{context} must be a JSON object")
    return value


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SummaryError(f"{context} must be an integer >= {minimum}")
    return value


def _number(value: object, context: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummaryError(f"{context} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or (positive and parsed <= 0):
        qualifier = "positive and " if positive else "non-negative and "
        raise SummaryError(f"{context} must be {qualifier}finite")
    return parsed


def _label(value: object, context: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise SummaryError(f"{context} must be a public-safe label")
    return value


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise SummaryError(f"{context} must be a boolean")
    return value


def _parse_qualification(
    raw: object,
    *,
    measurement_accepted: bool,
) -> QualificationSummary:
    block = _mapping(raw, "qualification")
    try:
        profile_id = _label(block["profile_id"], "qualification.profile_id")
        profile_version = _integer(
            block["profile_version"], "qualification.profile_version", minimum=1
        )
        eligible = _boolean(block["eligible"], "qualification.eligible")
        qualified = _boolean(block["qualified"], "qualification.qualified")
        status = block["status"]
        raw_reasons = block["reasons"]
        criteria = _mapping(block["criteria"], "qualification.criteria")
        observed = _mapping(block["observed"], "qualification.observed")
    except KeyError as exc:
        raise SummaryError(f"qualification is missing {exc.args[0]!r}") from exc

    if profile_id != STANDARD_PROFILE_ID or profile_version != STANDARD_PROFILE_VERSION:
        raise SummaryError("qualification identifies an unsupported standard profile")
    if status not in _QUALIFICATION_STATUSES:
        raise SummaryError("qualification.status is unsupported")
    if not isinstance(raw_reasons, list) or any(
        reason not in _QUALIFICATION_REASONS for reason in raw_reasons
    ):
        raise SummaryError("qualification.reasons contains an unsupported reason")
    if len(set(raw_reasons)) != len(raw_reasons):
        raise SummaryError("qualification.reasons must not contain duplicates")

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
    if criteria != expected_criteria:
        raise SummaryError("qualification.criteria does not match the supported profile")

    try:
        raw_median = observed["median_aggregate_gbps"]
        trial_count = _integer(
            observed["trial_count"], "qualification.observed.trial_count"
        )
        cpu_pinning_verified = _boolean(
            observed["cpu_pinning_verified"],
            "qualification.observed.cpu_pinning_verified",
        )
    except KeyError as exc:
        raise SummaryError(
            f"qualification.observed is missing {exc.args[0]!r}"
        ) from exc
    median = (
        None
        if raw_median is None
        else _number(
            raw_median,
            "qualification.observed.median_aggregate_gbps",
        )
    )
    reasons = tuple(raw_reasons)

    if qualified:
        if not (
            measurement_accepted
            and eligible
            and status == "qualified"
            and not reasons
            and median is not None
            and median >= STANDARD_PROFILE_MIN_MEDIAN_GBPS
            and trial_count >= STANDARD_PROFILE_MIN_REPEATS
            and cpu_pinning_verified
        ):
            raise SummaryError("qualified result has inconsistent qualification evidence")
    elif measurement_accepted:
        if not eligible:
            if status != "ineligible" or not reasons:
                raise SummaryError("ineligible result has inconsistent qualification status")
        elif median is None or trial_count < STANDARD_PROFILE_MIN_REPEATS:
            if status != "insufficient-observations" or reasons != (
                "standard-profile-observation-incomplete",
            ):
                raise SummaryError("qualification observation status is inconsistent")
        elif not cpu_pinning_verified:
            if status != "qualification-evidence-incomplete" or reasons != (
                "cpu-pinning-not-demonstrated",
            ):
                raise SummaryError("qualification evidence status is inconsistent")
        elif median < STANDARD_PROFILE_MIN_MEDIAN_GBPS:
            if status != "below-performance-floor" or reasons != (
                "median-below-minimum",
            ):
                raise SummaryError("performance qualification status is inconsistent")
        else:
            raise SummaryError("qualifying evidence must set qualified to true")
    elif status == "awaiting-measurement-acceptance":
        if not reasons or reasons[0] != "measurement-awaiting-postflight":
            raise SummaryError("pending qualification must explain postflight state")
    elif status == "measurement-unaccepted":
        if not reasons or reasons[0] != "measurement-unaccepted":
            raise SummaryError("unaccepted qualification must explain measurement state")
    else:
        raise SummaryError("unaccepted measurement has a final qualification status")

    return QualificationSummary(
        profile_id=profile_id,
        profile_version=profile_version,
        eligible=eligible,
        qualified=qualified,
        status=status,
        reasons=reasons,
        median_aggregate_gbps=median,
        trial_count=trial_count,
        cpu_pinning_verified=cpu_pinning_verified,
    )


def _parse_row(raw: object, index: int) -> SummaryRow:
    item = _mapping(raw, f"summaries[{index}]")
    try:
        mode = _label(item["mode"], f"summaries[{index}].mode")
        if mode not in _PUBLIC_MODES:
            raise SummaryError(
                f"summaries[{index}].mode is not a canonical public mode"
            )
        direction = item["direction"]
        if direction not in _DIRECTIONS:
            raise SummaryError(
                f"summaries[{index}].direction must be worker-to-head or head-to-worker"
            )
        row = SummaryRow(
            mode=mode,
            direction=direction,
            message_size_bytes=_integer(
                item["message_size_bytes"],
                f"summaries[{index}].message_size_bytes",
                minimum=1,
            ),
            qps=_integer(item["qps"], f"summaries[{index}].qps", minimum=1),
            tx_depth=_integer(
                item["tx_depth"], f"summaries[{index}].tx_depth", minimum=1
            ),
            repeats=_integer(
                item["repeats"], f"summaries[{index}].repeats", minimum=1
            ),
            median_gbps=_number(
                item["median_gbps"], f"summaries[{index}].median_gbps"
            ),
            min_gbps=_number(item["min_gbps"], f"summaries[{index}].min_gbps"),
            max_gbps=_number(item["max_gbps"], f"summaries[{index}].max_gbps"),
            population_stdev_gbps=_number(
                item["population_stdev_gbps"],
                f"summaries[{index}].population_stdev_gbps",
            ),
        )
    except KeyError as exc:
        raise SummaryError(f"summaries[{index}] is missing {exc.args[0]!r}") from exc
    if not row.min_gbps <= row.median_gbps <= row.max_gbps:
        raise SummaryError(
            f"summaries[{index}] must satisfy min_gbps <= median_gbps <= max_gbps"
        )
    return row


def parse_result(payload: object) -> PublicResultSummary:
    """Parse only the public, aggregate subset of a benchmark result."""

    root = _mapping(payload, "result")
    try:
        schema_version = _integer(root["schema_version"], "schema_version", minimum=1)
        if schema_version not in (1, 2, 3):
            raise SummaryError(f"unsupported result schema_version: {schema_version}")
        completed = _integer(root["completed_trials"], "completed_trials")
        planned = _integer(root["planned_trials"], "planned_trials", minimum=1)
        ceiling = _number(
            root["two_domain_raw_ceiling_gbps"],
            "two_domain_raw_ceiling_gbps",
            positive=True,
        )
        raw_rows = root["summaries"]
    except KeyError as exc:
        raise SummaryError(f"result is missing {exc.args[0]!r}") from exc
    if completed > planned:
        raise SummaryError("completed_trials cannot exceed planned_trials")
    raw_suite_version = root.get("suite_version")
    if raw_suite_version is None:
        suite_version = None
    elif not isinstance(raw_suite_version, str) or not _SUITE_VERSION.fullmatch(
        raw_suite_version
    ):
        raise SummaryError("suite_version must be a public semantic version")
    else:
        suite_version = raw_suite_version
    measurement_accepted: bool | None = None
    qualification: QualificationSummary | None = None
    if schema_version == 1:
        try:
            trials_complete = _boolean(root["complete"], "complete")
        except KeyError as exc:
            raise SummaryError("schema v1 result is missing 'complete'") from exc
        if trials_complete != (completed == planned):
            raise SummaryError(
                "schema v1 complete must be true exactly when completed_trials "
                "equals planned_trials"
            )
    elif schema_version == 2:
        try:
            trials_complete = _boolean(root["complete"], "complete")
            measurement_accepted = _boolean(root["accepted"], "accepted")
        except KeyError as exc:
            raise SummaryError(
                f"schema v2 result is missing {exc.args[0]!r}"
            ) from exc
        if trials_complete != measurement_accepted:
            raise SummaryError("schema v2 complete and accepted must match")
        if trials_complete and completed != planned:
            raise SummaryError(
                "an accepted schema v2 result must contain every planned trial"
            )
    else:
        legacy_fields = sorted({"accepted", "complete"} & set(root))
        if legacy_fields:
            raise SummaryError(
                "schema v3 must not contain legacy acceptance fields: "
                + ", ".join(legacy_fields)
                + "; use trials_complete and measurement_accepted"
            )
        try:
            trials_complete = _boolean(
                root["trials_complete"], "trials_complete"
            )
            measurement_accepted = _boolean(
                root["measurement_accepted"], "measurement_accepted"
            )
            raw_qualification = root["qualification"]
        except KeyError as exc:
            raise SummaryError(
                f"schema v3 result is missing {exc.args[0]!r}"
            ) from exc
        if trials_complete != (completed == planned):
            raise SummaryError(
                "schema v3 trials_complete must match completed/planned trial counts"
            )
        if measurement_accepted and not trials_complete:
            raise SummaryError("an accepted measurement must contain every planned trial")
        qualification = _parse_qualification(
            raw_qualification,
            measurement_accepted=measurement_accepted,
        )
    if not isinstance(raw_rows, list) or not raw_rows:
        raise SummaryError("summaries must be a non-empty array")
    rows = tuple(_parse_row(item, index) for index, item in enumerate(raw_rows))
    return PublicResultSummary(
        schema_version=schema_version,
        suite_version=suite_version,
        input_sha256=None,
        trials_complete=trials_complete,
        measurement_accepted=measurement_accepted,
        completed_trials=completed,
        planned_trials=planned,
        two_domain_raw_ceiling_gbps=ceiling,
        rows=rows,
        qualification=qualification,
    )


def load_result(path: str | Path) -> PublicResultSummary:
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_bytes()
        payload = json.loads(raw)
    except OSError as exc:
        raise SummaryError(f"cannot read result {source}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SummaryError(f"invalid JSON in {source}: {exc}") from exc
    try:
        validated = validate_result(payload)
    except ResultValidationError as exc:
        raise SummaryError(f"result failed semantic validation: {exc}") from exc
    safe_payload = dict(_mapping(payload, "result"))
    safe_payload["summaries"] = validated.summary_dicts()
    summary = parse_result(safe_payload)
    return replace(summary, input_sha256=hashlib.sha256(raw).hexdigest())


def _relative_difference_percent(value: float, baseline: float) -> float | None:
    if baseline == 0:
        return 0.0 if value == 0 else None
    return (value / baseline - 1.0) * 100.0


def _row_identity(row: SummaryRow) -> str:
    return (
        f"{row.mode} ({row.direction}, {row.message_size_bytes} B, "
        f"{row.qps} QP, TX {row.tx_depth})"
    )


def render_markdown(summary: PublicResultSummary) -> str:
    """Render aggregate-only Markdown; metadata, trials, paths, and labels are ignored."""

    baseline = min(summary.rows, key=lambda row: row.median_gbps)
    fastest = max(summary.rows, key=lambda row: row.median_gbps)
    if summary.schema_version == 1:
        status = "complete" if summary.complete else "incomplete"
        status_lines = [
            f"Status: **{status}** "
            f"({summary.completed_trials}/{summary.planned_trials} trials). "
            "This is legacy trial completion; current postflight measurement "
            "acceptance is unknown. Rates are unidirectional host-memory RDMA "
            "write payload bandwidth."
        ]
    elif summary.schema_version == 2:
        status = "accepted" if summary.accepted else "unaccepted"
        status_lines = [
            f"Status: **{status}** "
            f"({summary.completed_trials}/{summary.planned_trials} trials). "
            "Rates are unidirectional host-memory RDMA write payload bandwidth."
        ]
    else:
        qualification = summary.qualification
        assert qualification is not None
        traffic_status = "complete" if summary.trials_complete else "incomplete"
        measurement_status = (
            "accepted" if summary.measurement_accepted else "unaccepted"
        )
        if qualification.status == "qualified":
            qualification_status = "qualified"
        elif qualification.status == "awaiting-measurement-acceptance":
            qualification_status = "pending measurement acceptance"
        elif qualification.status == "measurement-unaccepted":
            qualification_status = "not evaluated; measurement unaccepted"
        elif qualification.status == "ineligible":
            qualification_status = "not qualified; profile ineligible"
        else:
            qualification_status = f"not qualified; {qualification.status}"
        observation = "no standard-profile observation"
        if qualification.median_aggregate_gbps is not None:
            observation = (
                f"{qualification.median_aggregate_gbps:.2f} Gbit/s median over "
                f"{qualification.trial_count} trials"
            )
        status_lines = [
            f"Traffic trials: **{traffic_status}** "
            f"({summary.completed_trials}/{summary.planned_trials}).",
            f"Measurement acceptance: **{measurement_status}**.",
            "Standard-profile qualification: "
            f"**{qualification_status}** "
            f"({qualification.profile_id} v{qualification.profile_version}; "
            f"{observation}; floor {STANDARD_PROFILE_MIN_MEDIAN_GBPS:.2f} Gbit/s).",
            "Rates are unidirectional host-memory RDMA write payload bandwidth.",
        ]
    lines = [
        "# Benchmark summary",
        "",
        *status_lines,
        "",
        (
            f"Source: schema v{summary.schema_version}, "
            + (
                f"suite v{summary.suite_version}"
                if summary.suite_version is not None
                else "suite version not present in the aggregate input"
            )
            + (
                f", input SHA-256 `{summary.input_sha256}`."
                if summary.input_sha256 is not None
                else "; in-memory input has no file digest."
            )
        ),
        "",
        "| Mode | Direction | Message (B) | QPs | TX depth | n | Median (Gbit/s) | Range (Gbit/s) | σ (Gbit/s) | Difference vs lowest median |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.rows:
        difference = _relative_difference_percent(
            row.median_gbps, baseline.median_gbps
        )
        if row is baseline:
            difference_text = "reference"
        elif difference is None:
            difference_text = "n/a"
        else:
            difference_text = f"+{difference:.2f}%"
        lines.append(
            f"| {row.mode} | {row.direction} | {row.message_size_bytes:,} | "
            f"{row.qps} | {row.tx_depth} | {row.repeats} | {row.median_gbps:.2f} | "
            f"{row.min_gbps:.2f}–{row.max_gbps:.2f} | "
            f"{row.population_stdev_gbps:.3f} | {difference_text} |"
        )

    fastest_difference = _relative_difference_percent(
        fastest.median_gbps, baseline.median_gbps
    )
    ceiling_fraction = fastest.median_gbps / summary.two_domain_raw_ceiling_gbps * 100.0
    if fastest_difference is None:
        difference_clause = "an undefined percentage over the zero-rate reference"
    else:
        difference_clause = f"{fastest_difference:+.2f}% relative to that row"
    lines.extend(
        [
            "",
            "## Relative comparison",
            "",
            f"The reference is the lowest median in this file: {_row_identity(baseline)} "
            f"at {baseline.median_gbps:.2f} Gbit/s.",
            "",
            f"The fastest row is {_row_identity(fastest)} at {fastest.median_gbps:.2f} "
            f"Gbit/s: {difference_clause}, and {ceiling_fraction:.2f}% of the "
            f"{summary.two_domain_raw_ceiling_gbps:.2f} Gbit/s raw two-domain ceiling.",
            "",
            "Relative differences compare medians within this result file. They are not "
            "called speedups because rows may differ in topology, QP count, message size, "
            "direction, or other variables, and do not by themselves establish causality.",
            "",
        ]
    )
    return "\n".join(lines)
