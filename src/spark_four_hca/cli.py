"""Command-line interface for the DGX Spark dual-QSFP fabric suite."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from ._version import VERSION
from .benchmark import (
    STANDARD_PROFILE_MIN_COOLDOWN_SECONDS,
    STANDARD_PROFILE_MIN_DURATION_SECONDS,
    STANDARD_PROFILE_MIN_REPEATS,
    TWO_PCIE_GEN5_X4_GBPS,
    evaluate_standard_profile_qualification,
    run_matrix,
)
from .checks import check_pair, compare_link_error_counters
from .model import ConfigError, load_config
from .netplan import render_netplan, render_topology
from .result_validation import load_and_validate_result
from .summary import load_result, render_markdown


DEFAULT_BENCHMARK_MODE = "diagonal-a"


def _bounded_int(value: str, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _bounded_float(value: str, *, minimum: float, maximum: float, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a number") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be finite and between {minimum:g} and {maximum:g}"
        )
    return parsed


def _message_size(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=1 << 30, label="message size")


def _qps(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=128, label="QPs")


def _tx_depth(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=16384, label="TX depth")


def _duration(value: str) -> int:
    return _bounded_int(value, minimum=2, maximum=3600, label="duration")


def _repeats(value: str) -> int:
    return _bounded_int(value, minimum=1, maximum=1000, label="repeats")


def _cooldown(value: str) -> float:
    return _bounded_float(value, minimum=0.0, maximum=3600.0, label="cooldown")


def _schedule_seed(value: str) -> int:
    return _bounded_int(value, minimum=0, maximum=(1 << 32) - 1, label="schedule seed")


def _write_atomic(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _refuse_input_overwrite(output: Path | None, *inputs: Path) -> None:
    """Reject an output path that would destroy one of the command inputs."""

    if output is None:
        return
    resolved_output = output.expanduser().resolve()
    if any(resolved_output == item.expanduser().resolve() for item in inputs):
        raise ConfigError("output must not overwrite an input file")


def _refuse_existing_output(output: Path, *, force: bool) -> None:
    if output.expanduser().resolve().exists() and not force:
        raise ConfigError("output already exists; pass --force to replace it")


def _reject_duplicate_values(values: list | None, label: str) -> None:
    if values is not None and len(values) != len(set(values)):
        raise ConfigError(f"duplicate {label} values are not allowed")


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _print_check_table(report: dict) -> None:
    for item in report["checks"]:
        if item["status"] != "pass":
            print(
                f"{item['status'].upper():4} {item['node']}/{item['path']} "
                f"{item['name']}: observed={item['observed']} expected={item['expected']}"
            )
    summary = report["summary"]
    print(
        f"check summary: {summary['passed']} passed, {summary['warned']} warned, "
        f"{summary['failed']} failed"
    )


def _check_status(report: dict) -> dict:
    summary = {
        "passed": int(report["summary"]["passed"]),
        "warned": int(report["summary"]["warned"]),
        "failed": int(report["summary"]["failed"]),
    }
    if not report["ok"]:
        status = "failed"
    elif summary["warned"]:
        status = "passed-with-warnings"
    else:
        status = "passed"
    return {
        "status": status,
        "ok": bool(report["ok"]),
        "summary": summary,
        "link_error_counters": report.get(
            "link_error_counters",
            {"counter_names": [], "snapshots": []},
        ),
    }


def _measurement_acceptance_ok(
    *,
    expected_path_count: int,
    preflight_status: dict,
    postflight_status: dict,
    counter_delta: dict,
) -> bool:
    names = counter_delta.get("counter_names")
    deltas = counter_delta.get("deltas")
    full_zero_delta = (
        isinstance(names, list)
        and bool(names)
        and isinstance(deltas, list)
        and len(deltas) == expected_path_count * 2
    )
    if full_zero_delta:
        for row in deltas:
            values = row.get("values") if isinstance(row, dict) else None
            if (
                not isinstance(values, dict)
                or set(values) != set(names)
                or any(value != 0 for value in values.values())
            ):
                full_zero_delta = False
                break
    return bool(
        preflight_status.get("ok") is True
        and postflight_status.get("ok") is True
        and counter_delta.get("ok") is True
        and counter_delta.get("status") == "passed"
        and not counter_delta.get("positive_deltas")
        and not counter_delta.get("reset_counters")
        and full_zero_delta
    )
def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path, help="pair TOML configuration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spark-four-hca",
        description=(
            "Safely inspect and benchmark dual-QSFP/four-RoCE-device DGX Spark pairs."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a pair TOML without SSH")
    _add_config(validate)

    topology = subparsers.add_parser("topology", help="show paths, resources, and modes")
    _add_config(topology)

    render = subparsers.add_parser("render-netplan", help="render review-only netplan YAML")
    _add_config(render)
    render.add_argument("--node", choices=("head", "worker"), required=True)
    render.add_argument("--output", type=Path)

    check = subparsers.add_parser("check", help="perform read-only live link validation")
    _add_config(check)
    check.add_argument("--json", action="store_true", help="emit the complete JSON report")
    check.add_argument("--output", type=Path, help="write complete JSON report atomically")

    theory = subparsers.add_parser("theory", help="print the two-PCIe-domain raw ceiling")
    theory.add_argument("--json", action="store_true")

    summarize = subparsers.add_parser(
        "summarize", help="render public-safe Markdown from benchmark result JSON"
    )
    summarize.add_argument("--input", type=Path, required=True, help="benchmark result JSON")
    summarize.add_argument("--output", type=Path, help="write Markdown atomically")

    validate_result = subparsers.add_parser(
        "validate-result",
        help="validate result cross-fields and recompute aggregate summaries",
    )
    validate_result.add_argument(
        "--input", type=Path, required=True, help="benchmark result JSON"
    )

    benchmark = subparsers.add_parser(
        "benchmark",
        help="run concurrent host-memory ib_write_bw topology tests",
        description=(
            "Run topology measurements. Defaults are exploratory; use "
            "--profile standard for the qualification profile."
        ),
    )
    _add_config(benchmark)
    benchmark.add_argument(
        "--profile",
        choices=("standard",),
        help="select and enforce the versioned standard qualification profile",
    )
    benchmark.add_argument(
        "--mode",
        action="append",
        help=(
            "mode from the config; repeat to select multiple "
            f"(default: {DEFAULT_BENCHMARK_MODE})"
        ),
    )
    benchmark.add_argument(
        "--direction",
        action="append",
        choices=("worker-to-head", "head-to-worker"),
        help="repeat to test both directions (default: worker-to-head)",
    )
    benchmark.add_argument(
        "--message-size",
        action="append",
        type=_message_size,
        help="bytes; repeat for a sweep (default: config value)",
    )
    benchmark.add_argument(
        "--qps",
        action="append",
        type=_qps,
        help="queue pairs per connection; repeat for a sweep (default: config value)",
    )
    benchmark.add_argument(
        "--tx-depth",
        action="append",
        type=_tx_depth,
        help="TX queue depth; repeat for a sweep (default: 128)",
    )
    benchmark.add_argument(
        "--duration", type=_duration, help="seconds per trial (default: config value)"
    )
    benchmark.add_argument(
        "--repeats", type=_repeats, help="trials per condition (exploratory default: 1)"
    )
    benchmark.add_argument(
        "--cooldown", type=_cooldown, help="seconds between trials (exploratory default: 1)"
    )
    benchmark.add_argument(
        "--schedule",
        choices=("interleaved", "grouped"),
        default=None,
        help=(
            "rotate full swept conditions across repeats to reduce time/thermal bias "
            "(default: interleaved)"
        ),
    )
    benchmark.add_argument(
        "--schedule-seed",
        type=_schedule_seed,
        default=0,
        help="deterministic initial mode order seed (default: 0)",
    )
    benchmark.add_argument("--output", type=Path, required=True, help="result JSON path")
    benchmark.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file (default: refuse to overwrite evidence)",
    )
    benchmark.add_argument(
        "--raw-log-dir",
        type=Path,
        help="optional raw perftest logs; these may contain private IP addresses",
    )
    benchmark.add_argument(
        "--skip-live-check",
        action="store_true",
        help="skip the read-only preflight link check (postflight still runs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "theory":
            payload = {
                "pcie_generation": 5,
                "links": 2,
                "lanes_per_link": 4,
                "transfer_rate_gtps": 32.0,
                "encoding": "128b/130b",
                "raw_one_way_ceiling_gbps": round(TWO_PCIE_GEN5_X4_GBPS, 6),
                "note": "PCIe transaction, RoCE, Ethernet, and software overhead reduce payload throughput.",
            }
            if args.json:
                print(_json_text(payload), end="")
            else:
                print(
                    "2 x PCIe Gen5 x4 = "
                    f"{TWO_PCIE_GEN5_X4_GBPS:.3f} Gbit/s raw per direction before overhead"
                )
            return 0

        if args.command == "summarize":
            _refuse_input_overwrite(args.output, args.input)
            rendered = render_markdown(load_result(args.input))
            if args.output:
                _write_atomic(args.output, rendered)
                print(args.output.expanduser().resolve())
            else:
                print(rendered, end="")
            return 0

        if args.command == "validate-result":
            result = load_and_validate_result(args.input)
            acceptance = (
                "legacy-unknown"
                if result.measurement_accepted is None
                else str(result.measurement_accepted).lower()
            )
            print(
                f"valid semantic result: schema v{result.schema_version}, "
                f"{result.completed_trials}/{result.planned_trials} trials, "
                f"measurement_accepted={acceptance}"
            )
            return 0

        config = load_config(args.config)
        if args.command == "validate":
            print(f"valid: {config.pair_label} ({len(config.paths)} paths, {len(config.modes)} modes)")
            return 0
        if args.command == "topology":
            print(render_topology(config), end="")
            return 0
        if args.command == "render-netplan":
            _refuse_input_overwrite(args.output, config.source)
            rendered = render_netplan(config, args.node)
            if args.output:
                _write_atomic(args.output, rendered)
                print(args.output.expanduser().resolve())
            else:
                print(rendered, end="")
            return 0
        if args.command == "check":
            _refuse_input_overwrite(args.output, config.source)
            report = check_pair(config)
            if args.output:
                _write_atomic(args.output, _json_text(report))
            if args.json:
                print(_json_text(report), end="")
            else:
                _print_check_table(report)
            return 0 if report["ok"] else 2
        if args.command == "benchmark":
            _refuse_input_overwrite(args.output, config.source)
            _refuse_existing_output(args.output, force=args.force)
            for values, label in (
                (args.mode, "mode"),
                (args.direction, "direction"),
                (args.message_size, "message size"),
                (args.qps, "QPs"),
                (args.tx_depth, "TX depth"),
            ):
                _reject_duplicate_values(values, label)

            if args.profile == "standard":
                conflicts: list[str] = []
                for values, expected, flag in (
                    (args.mode, ["diagonal-a"], "--mode"),
                    (args.direction, ["worker-to-head"], "--direction"),
                    (args.message_size, [65536], "--message-size"),
                    (args.qps, [2], "--qps"),
                    (args.tx_depth, [128], "--tx-depth"),
                ):
                    if values is not None and values != expected:
                        conflicts.append(flag)
                for value, expected, flag in (
                    (args.duration, STANDARD_PROFILE_MIN_DURATION_SECONDS, "--duration"),
                    (args.repeats, STANDARD_PROFILE_MIN_REPEATS, "--repeats"),
                    (
                        args.cooldown,
                        STANDARD_PROFILE_MIN_COOLDOWN_SECONDS,
                        "--cooldown",
                    ),
                    (args.schedule, "interleaved", "--schedule"),
                ):
                    if value is not None and value != expected:
                        conflicts.append(flag)
                if args.skip_live_check:
                    conflicts.append("--skip-live-check")
                if conflicts:
                    raise ConfigError(
                        "--profile standard conflicts with: "
                        + ", ".join(sorted(conflicts))
                    )
                modes = ["diagonal-a"]
                directions = ["worker-to-head"]
                message_sizes = [65536]
                qps_values = [2]
                tx_depths = [128]
                duration = STANDARD_PROFILE_MIN_DURATION_SECONDS
                repeats = STANDARD_PROFILE_MIN_REPEATS
                cooldown_seconds = STANDARD_PROFILE_MIN_COOLDOWN_SECONDS
                schedule = "interleaved"
            else:
                modes = args.mode or [DEFAULT_BENCHMARK_MODE]
                directions = args.direction or ["worker-to-head"]
                message_sizes = args.message_size or [config.message_size_bytes]
                qps_values = args.qps or [config.qps]
                tx_depths = args.tx_depth or [128]
                duration = (
                    args.duration
                    if args.duration is not None
                    else config.duration_seconds
                )
                repeats = args.repeats if args.repeats is not None else 1
                cooldown_seconds = args.cooldown if args.cooldown is not None else 1.0
                schedule = args.schedule or "interleaved"

            missing_modes = sorted(set(modes) - set(config.modes))
            if missing_modes:
                raise ConfigError(
                    f"benchmark modes are missing from the config: {missing_modes}"
                )
            preflight_status: dict
            if not args.skip_live_check:
                print("running read-only live link checks", file=sys.stderr)
                report = check_pair(config)
                _print_check_table(report)
                preflight_status = _check_status(report)
                if not report["ok"]:
                    raise ConfigError("live checks failed; benchmark refused")
            else:
                preflight_status = {
                    "status": "skipped",
                    "reason": "operator requested --skip-live-check",
                    "link_error_counters": {
                        "counter_names": [],
                        "snapshots": [],
                    },
                }
            result = run_matrix(
                config,
                modes=modes,
                directions=directions,
                message_sizes=message_sizes,
                qps_values=qps_values,
                tx_depths=tx_depths,
                duration=duration,
                repeats=repeats,
                cooldown_seconds=cooldown_seconds,
                schedule=schedule,
                schedule_seed=args.schedule_seed,
                preflight_status=preflight_status,
                raw_log_dir=args.raw_log_dir,
                checkpoint_path=args.output,
            )
            print("running read-only postflight link checks", file=sys.stderr)
            try:
                postflight_report = check_pair(config)
                _print_check_table(postflight_report)
                postflight_status = _check_status(postflight_report)
            except KeyboardInterrupt:
                result["validation"]["postflight"] = {"status": "interrupted"}
                result["validation"]["link_error_counter_delta"] = {
                    "status": "unavailable",
                    "ok": False,
                    "reason": "postflight was interrupted",
                }
                result["measurement_accepted"] = False
                result["run_status"] = "interrupted"
                result["qualification"] = evaluate_standard_profile_qualification(
                    result,
                    measurement_final=True,
                )
                _write_atomic(args.output, _json_text(result))
                raise
            except Exception as exc:
                postflight_status = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
                result["validation"]["postflight"] = postflight_status
                result["validation"]["link_error_counter_delta"] = {
                    "status": "unavailable",
                    "ok": False,
                    "reason": "postflight did not complete",
                }
                result["measurement_accepted"] = False
                result["run_status"] = "postflight-error"
                result["qualification"] = evaluate_standard_profile_qualification(
                    result,
                    measurement_final=True,
                )
                _write_atomic(args.output, _json_text(result))
                raise ConfigError(f"postflight link check could not complete: {exc}") from exc
            result["validation"]["postflight"] = postflight_status
            counter_delta = compare_link_error_counters(
                preflight_status,
                postflight_status,
            )
            result["validation"]["link_error_counter_delta"] = counter_delta
            accepted = _measurement_acceptance_ok(
                expected_path_count=len(config.paths),
                preflight_status=preflight_status,
                postflight_status=postflight_status,
                counter_delta=counter_delta,
            )
            result["measurement_accepted"] = accepted
            if accepted:
                result["run_status"] = "measurement-accepted"
            elif counter_delta["positive_deltas"]:
                result["run_status"] = "link-errors-detected"
            elif not postflight_report["ok"]:
                result["run_status"] = "postflight-failed"
            else:
                result["run_status"] = "validation-incomplete"
            result["qualification"] = evaluate_standard_profile_qualification(
                result,
                measurement_final=True,
            )
            _write_atomic(args.output, _json_text(result))
            print(args.output.expanduser().resolve())
            if not accepted:
                print(
                    f"benchmark measurement is unaccepted: {result['run_status']}",
                    file=sys.stderr,
                )
            return 0 if accepted else 2
    except (ConfigError, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unhandled command")
    return 2
