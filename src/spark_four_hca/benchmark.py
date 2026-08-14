"""Concurrent, topology-aware ib_write_bw benchmark orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable

from ._version import VERSION
from .checks import collect_node_metadata
from .model import PairConfig, PathConfig
from .remote import SSHRunner


PCIE_GEN5_X4_GBPS = 32.0 * 4 * (128.0 / 130.0)
TWO_PCIE_GEN5_X4_GBPS = PCIE_GEN5_X4_GBPS * 2

STANDARD_PROFILE_ID = "dgx-spark-dual-qsfp-standard"
STANDARD_PROFILE_VERSION = 1
STANDARD_PROFILE_MIN_MEDIAN_GBPS = 215.0
STANDARD_PROFILE_MIN_DURATION_SECONDS = 30
STANDARD_PROFILE_MIN_REPEATS = 5
STANDARD_PROFILE_MIN_COOLDOWN_SECONDS = 5.0

_RESULT_LINE = re.compile(
    r"^\s*(?P<bytes>\d+)\s+(?P<iterations>\d+)\s+"
    r"(?P<peak>\d+(?:\.\d+)?)\s+(?P<average>\d+(?:\.\d+)?)\s+"
    r"(?P<message_rate>\d+(?:\.\d+)?)\s*$"
)


class BenchmarkError(RuntimeError):
    """Raised when a benchmark cannot safely complete or be parsed."""


@dataclass(frozen=True)
class ParsedBandwidth:
    bytes: int
    iterations: int
    peak_gbps: float
    average_gbps: float
    message_rate_mpps: float


@dataclass(frozen=True)
class ProcessOutput:
    stdout: str
    stderr: str
    returncode: int


def parse_perftest_bandwidth(text: str, expected_size: int | None = None) -> ParsedBandwidth:
    candidates: list[ParsedBandwidth] = []
    for line in text.splitlines():
        match = _RESULT_LINE.match(line)
        if not match:
            continue
        candidates.append(
            ParsedBandwidth(
                bytes=int(match.group("bytes")),
                iterations=int(match.group("iterations")),
                peak_gbps=float(match.group("peak")),
                average_gbps=float(match.group("average")),
                message_rate_mpps=float(match.group("message_rate")),
            )
        )
    if expected_size is not None:
        matching = [item for item in candidates if item.bytes == expected_size]
        if matching:
            return matching[-1]
        if candidates:
            observed = ", ".join(str(item.bytes) for item in candidates)
            raise BenchmarkError(
                f"perftest did not report the expected message size {expected_size}; "
                f"observed result sizes: {observed}"
            )
    if candidates:
        return candidates[-1]
    raise BenchmarkError("could not find a perftest bandwidth result line")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _config_fingerprint(config: PairConfig) -> str:
    public_shape = {
        "schema_version": config.schema_version,
        "mtu": config.mtu,
        "paths": [
            {
                "name": path.name,
                "physical_port": path.physical_port,
                "pcie_domain": path.pcie_domain,
                "netdev": path.netdev,
                "rdma_device": path.rdma_device,
                "network_prefix": path.network.prefixlen,
            }
            for path in config.paths
        ],
        "modes": config.modes,
    }
    encoded = json.dumps(public_shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _implementation_fingerprint_sha256(package_root: Path | None = None) -> str:
    """Hash installed Python sources without including their absolute location."""

    root = (package_root or Path(__file__).resolve().parent).resolve()
    sources = sorted(
        (
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.relative_to(root).parts
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not sources:
        raise BenchmarkError("cannot fingerprint implementation: no Python sources found")
    digest = hashlib.sha256()
    for source in sources:
        relative = source.relative_to(root).as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _wait_process(process: subprocess.Popen[str], timeout: float) -> ProcessOutput:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise BenchmarkError("remote benchmark process exceeded its safety timeout") from exc
    return ProcessOutput(stdout=stdout, stderr=stderr, returncode=process.returncode)


def _terminate_processes(processes: Iterable[subprocess.Popen[str]]) -> None:
    pending = [process for process in processes if process.poll() is None]
    for process in pending:
        process.terminate()
    deadline = time.monotonic() + 5
    killed: list[subprocess.Popen[str]] = []
    for process in pending:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            killed.append(process)
    for process in killed:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkError("could not reap a killed remote benchmark process") from exc


def _role_for_direction(direction: str) -> tuple[str, str]:
    if direction == "worker-to-head":
        return "head", "worker"
    if direction == "head-to-worker":
        return "worker", "head"
    raise BenchmarkError(f"unsupported direction: {direction}")


def _write_private_text_atomic(path: Path, text: str) -> None:
    """Atomically write a raw log with owner-only permissions."""

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
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


def _perftest_argv(
    *,
    path: PathConfig,
    port: int,
    duration: int,
    message_size: int,
    qps: int,
    tx_depth: int,
    cpu_core: int | None,
    target: str | None,
) -> list[str]:
    hard_timeout = duration + 20
    command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=2s",
        f"{hard_timeout}s",
    ]
    if cpu_core is not None:
        command.extend(["taskset", "-c", str(cpu_core)])
    command.extend(
        [
            "ib_write_bw",
            "-d",
            path.rdma_device,
            "-i",
            "1",
            "-p",
            str(port),
            "-F",
            "--report_gbits",
            "-s",
            str(message_size),
            "-q",
            str(qps),
            "-t",
            str(tx_depth),
            "-D",
            str(duration),
        ]
    )
    if target is not None:
        command.append(target)
    return command


def _ensure_idle(runner: SSHRunner, config: PairConfig) -> None:
    for role in ("head", "worker"):
        node = config.node(role)
        result = runner.run(node.ssh, ["pgrep", "-x", "ib_write_bw"], timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            raise BenchmarkError(
                f"{node.label} already has ib_write_bw running; refusing to collide with it"
            )
        if result.returncode not in (0, 1):
            raise BenchmarkError(f"could not check existing ib_write_bw processes on {node.label}")


def run_trial(
    config: PairConfig,
    mode: str,
    *,
    direction: str,
    duration: int,
    message_size: int,
    qps: int,
    tx_depth: int,
    trial_index: int,
    runner: SSHRunner,
    startup_seconds: float = 1.5,
    raw_log_dir: Path | None = None,
) -> dict[str, Any]:
    paths = config.paths_for_mode(mode)
    server_role, client_role = _role_for_direction(direction)
    server = config.node(server_role)
    client = config.node(client_role)
    _ensure_idle(runner, config)

    servers: list[tuple[PathConfig, int, subprocess.Popen[str]]] = []
    clients: list[tuple[PathConfig, int, subprocess.Popen[str]]] = []
    all_processes: list[subprocess.Popen[str]] = []
    started_at = _utc_now()

    try:
        for offset, path in enumerate(paths):
            port = config.base_port + offset
            core = server.cpu_cores[offset] if server.cpu_cores else None
            argv = _perftest_argv(
                path=path,
                port=port,
                duration=duration,
                message_size=message_size,
                qps=qps,
                tx_depth=tx_depth,
                cpu_core=core,
                target=None,
            )
            process = runner.popen(server.ssh, argv)
            servers.append((path, port, process))
            all_processes.append(process)

        time.sleep(startup_seconds)
        early = [(path.name, process.returncode) for path, _, process in servers if process.poll() is not None]
        if early:
            details = []
            for path, _, process in servers:
                if process.poll() is not None:
                    output = _wait_process(process, timeout=1)
                    details.append(f"{path.name}: {output.stderr.strip() or output.stdout.strip()}")
            raise BenchmarkError(f"one or more servers exited before clients started: {early}; {'; '.join(details)}")

        for offset, path in enumerate(paths):
            port = config.base_port + offset
            core = client.cpu_cores[offset] if client.cpu_cores else None
            target = str(path.address_for(server_role).ip)
            argv = _perftest_argv(
                path=path,
                port=port,
                duration=duration,
                message_size=message_size,
                qps=qps,
                tx_depth=tx_depth,
                cpu_core=core,
                target=target,
            )
            process = runner.popen(client.ssh, argv)
            clients.append((path, port, process))
            all_processes.append(process)

        process_timeout = duration + 30
        client_outputs: dict[str, ProcessOutput] = {}
        for path, _, process in clients:
            client_outputs[path.name] = _wait_process(process, process_timeout)
        server_outputs: dict[str, ProcessOutput] = {}
        for path, _, process in servers:
            server_outputs[path.name] = _wait_process(process, process_timeout)
    finally:
        _terminate_processes(all_processes)

    path_results: list[dict[str, Any]] = []
    for path, port, _ in clients:
        client_output = client_outputs[path.name]
        server_output = server_outputs[path.name]
        if client_output.returncode != 0 or server_output.returncode != 0:
            diagnostic = client_output.stderr.strip() or server_output.stderr.strip()
            raise BenchmarkError(
                f"perftest failed for {path.name}: client={client_output.returncode}, "
                f"server={server_output.returncode}: {diagnostic}"
            )
        combined = client_output.stdout + "\n" + client_output.stderr
        parsed = parse_perftest_bandwidth(combined, expected_size=message_size)
        path_results.append(
            {
                "path": path.name,
                "physical_port": path.physical_port,
                "pcie_domain": path.pcie_domain,
                "rdma_device": path.rdma_device,
                "control_port": port,
                "server_cpu": server.cpu_cores[len(path_results)] if server.cpu_cores else None,
                "client_cpu": client.cpu_cores[len(path_results)] if client.cpu_cores else None,
                **asdict(parsed),
            }
        )
        if raw_log_dir is not None:
            stem = f"{mode}-{direction}-q{qps}-s{message_size}-t{tx_depth}-r{trial_index}-{path.name}"
            _write_private_text_atomic(
                raw_log_dir / f"{stem}-client.log",
                client_output.stdout + client_output.stderr,
            )
            _write_private_text_atomic(
                raw_log_dir / f"{stem}-server.log",
                server_output.stdout + server_output.stderr,
            )

    averages = [item["average_gbps"] for item in path_results]
    aggregate = sum(averages)
    mean_path = statistics.mean(averages)
    spread = max(averages) - min(averages) if len(averages) > 1 else 0.0
    return {
        "started_at": started_at,
        "finished_at": _utc_now(),
        "trial": trial_index,
        "mode": mode,
        "direction": direction,
        "duration_seconds": duration,
        "message_size_bytes": message_size,
        "qps": qps,
        "tx_depth": tx_depth,
        "path_count": len(paths),
        "physical_ports": sorted({path.physical_port for path in paths}),
        "pcie_domains": sorted({path.pcie_domain for path in paths}),
        "paths": path_results,
        "aggregate_gbps": round(aggregate, 6),
        "aggregate_GBps": round(aggregate / 8.0, 6),
        "path_spread_gbps": round(spread, 6),
        "path_spread_percent_of_mean": round((spread / mean_path * 100.0) if mean_path else 0.0, 6),
        "fraction_of_two_x4_raw_ceiling": round(aggregate / TWO_PCIE_GEN5_X4_GBPS, 6),
    }


def _summaries(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[float]] = {}
    for trial in trials:
        key = (
            trial["mode"],
            trial["direction"],
            trial["message_size_bytes"],
            trial["qps"],
            trial["tx_depth"],
        )
        groups.setdefault(key, []).append(trial["aggregate_gbps"])
    summaries: list[dict[str, Any]] = []
    for key, values in groups.items():
        mode, direction, message_size, qps, tx_depth = key
        summaries.append(
            {
                "mode": mode,
                "direction": direction,
                "message_size_bytes": message_size,
                "qps": qps,
                "tx_depth": tx_depth,
                "repeats": len(values),
                "median_gbps": round(statistics.median(values), 6),
                "mean_gbps": round(statistics.mean(values), 6),
                "min_gbps": round(min(values), 6),
                "max_gbps": round(max(values), 6),
                "population_stdev_gbps": round(statistics.pstdev(values), 6),
                "median_fraction_of_two_x4_raw_ceiling": round(
                    statistics.median(values) / TWO_PCIE_GEN5_X4_GBPS, 6
                ),
            }
        )
    return sorted(
        summaries,
        key=lambda item: (
            item["mode"],
            item["direction"],
            item["message_size_bytes"],
            item["qps"],
            item["tx_depth"],
        ),
    )


def _standard_profile_criteria() -> dict[str, Any]:
    return {
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


def _standard_profile_eligibility_reasons(result: dict[str, Any]) -> list[str]:
    parameters = result.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    reasons: list[str] = []
    if parameters.get("modes") != ["diagonal-a"]:
        reasons.append("mode-set-mismatch")
    if parameters.get("directions") != ["worker-to-head"]:
        reasons.append("direction-set-mismatch")
    if parameters.get("qps_values") != [2]:
        reasons.append("qps-set-mismatch")
    if parameters.get("message_sizes") != [65536]:
        reasons.append("message-size-set-mismatch")
    if parameters.get("tx_depths") != [128]:
        reasons.append("tx-depth-set-mismatch")
    if parameters.get("duration_seconds", 0) < STANDARD_PROFILE_MIN_DURATION_SECONDS:
        reasons.append("duration-below-minimum")
    if parameters.get("repeats", 0) < STANDARD_PROFILE_MIN_REPEATS:
        reasons.append("repeats-below-minimum")
    if parameters.get("cooldown_seconds", 0) < STANDARD_PROFILE_MIN_COOLDOWN_SECONDS:
        reasons.append("cooldown-below-minimum")
    if parameters.get("schedule") != "interleaved":
        reasons.append("schedule-mismatch")
    validation = result.get("validation")
    preflight = validation.get("preflight") if isinstance(validation, dict) else None
    if not (
        isinstance(preflight, dict)
        and preflight.get("ok") is True
        and preflight.get("status") in ("passed", "passed-with-warnings")
    ):
        reasons.append("preflight-not-healthy")
    return reasons


def _standard_profile_observation(
    summaries: object,
) -> tuple[float | None, int]:
    if not isinstance(summaries, list):
        return None, 0
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        if (
            summary.get("mode") == "diagonal-a"
            and summary.get("direction") == "worker-to-head"
            and summary.get("qps") == 2
            and summary.get("message_size_bytes") == 65536
            and summary.get("tx_depth") == 128
        ):
            median = summary.get("median_gbps")
            count = summary.get("repeats")
            if (
                not isinstance(median, bool)
                and isinstance(median, (int, float))
                and not isinstance(count, bool)
                and isinstance(count, int)
                and count >= 0
            ):
                return float(median), count
    return None, 0


def _standard_profile_cpu_pinning_verified(trials: object) -> bool:
    if not isinstance(trials, list) or not trials:
        return False
    matched = 0
    for trial in trials:
        if not isinstance(trial, dict):
            return False
        if not (
            trial.get("mode") == "diagonal-a"
            and trial.get("direction") == "worker-to-head"
            and trial.get("qps") == 2
            and trial.get("message_size_bytes") == 65536
            and trial.get("tx_depth") == 128
        ):
            continue
        paths = trial.get("paths")
        if not isinstance(paths, list) or len(paths) != 2:
            return False
        server_cores: list[int] = []
        client_cores: list[int] = []
        for path in paths:
            if not isinstance(path, dict):
                return False
            server_core = path.get("server_cpu")
            client_core = path.get("client_cpu")
            if (
                isinstance(server_core, bool)
                or not isinstance(server_core, int)
                or isinstance(client_core, bool)
                or not isinstance(client_core, int)
            ):
                return False
            server_cores.append(server_core)
            client_cores.append(client_core)
        if len(set(server_cores)) != len(server_cores) or len(set(client_cores)) != len(
            client_cores
        ):
            return False
        matched += 1
    return matched > 0


def evaluate_standard_profile_qualification(
    result: dict[str, Any],
    *,
    measurement_final: bool = False,
) -> dict[str, Any]:
    """Return a public-safe qualification independent of measurement acceptance."""

    eligibility_reasons = _standard_profile_eligibility_reasons(result)
    eligible = not eligibility_reasons
    median, count = _standard_profile_observation(result.get("summaries"))
    cpu_pinning_verified = _standard_profile_cpu_pinning_verified(result.get("trials"))
    accepted = result.get("measurement_accepted") is True

    qualified = False
    if not measurement_final and not accepted:
        status = "awaiting-measurement-acceptance"
        reasons = ["measurement-awaiting-postflight", *eligibility_reasons]
    elif not accepted:
        status = "measurement-unaccepted"
        reasons = ["measurement-unaccepted", *eligibility_reasons]
    elif not eligible:
        status = "ineligible"
        reasons = eligibility_reasons
    elif median is None or count < STANDARD_PROFILE_MIN_REPEATS:
        status = "insufficient-observations"
        reasons = ["standard-profile-observation-incomplete"]
    elif not cpu_pinning_verified:
        status = "qualification-evidence-incomplete"
        reasons = ["cpu-pinning-not-demonstrated"]
    elif median < STANDARD_PROFILE_MIN_MEDIAN_GBPS:
        status = "below-performance-floor"
        reasons = ["median-below-minimum"]
    else:
        qualified = True
        status = "qualified"
        reasons = []

    return {
        "profile_id": STANDARD_PROFILE_ID,
        "profile_version": STANDARD_PROFILE_VERSION,
        "eligible": eligible,
        "qualified": qualified,
        "status": status,
        "reasons": reasons,
        "criteria": _standard_profile_criteria(),
        "observed": {
            "median_aggregate_gbps": median,
            "trial_count": count,
            "cpu_pinning_verified": cpu_pinning_verified,
        },
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _build_schedule(
    *,
    modes: list[str],
    directions: list[str],
    message_sizes: list[int],
    qps_values: list[int],
    tx_depths: list[int],
    repeats: int,
    schedule: str,
    schedule_seed: int,
) -> list[tuple[str, str, int, int, int, int]]:
    """Build a deterministic plan; interleaving rotates full conditions."""

    if schedule == "grouped":
        return [
            (mode, direction, message_size, qps, tx_depth, repeat)
            for mode in modes
            for direction in directions
            for message_size in message_sizes
            for qps in qps_values
            for tx_depth in tx_depths
            for repeat in range(1, repeats + 1)
        ]
    if schedule != "interleaved":
        raise BenchmarkError(f"unsupported schedule: {schedule}")

    # Rank and rotate complete conditions, not only modes. This distributes
    # every swept condition across ordinal positions when enough repeats run.
    conditions = [
        (mode, direction, message_size, qps, tx_depth)
        for direction in directions
        for message_size in message_sizes
        for qps in qps_values
        for tx_depth in tx_depths
        for mode in modes
    ]
    ranked_conditions = [
        condition
        for _, condition in sorted(
            enumerate(conditions),
            key=lambda item: hashlib.sha256(
                (
                    f"{schedule_seed}\0{item[0]}\0"
                    + "\0".join(str(value) for value in item[1])
                ).encode("utf-8")
            ).digest(),
        )
    ]
    combinations: list[tuple[str, str, int, int, int, int]] = []
    for repeat in range(1, repeats + 1):
        offset = (repeat - 1) % len(ranked_conditions)
        condition_order = ranked_conditions[offset:] + ranked_conditions[:offset]
        combinations.extend((*condition, repeat) for condition in condition_order)
    return combinations


def _serialized_plan(
    combinations: list[tuple[str, str, int, int, int, int]],
) -> list[dict[str, Any]]:
    return [
        {
            "position": position,
            "mode": mode,
            "direction": direction,
            "message_size_bytes": message_size,
            "qps": qps,
            "tx_depth": tx_depth,
            "repeat": repeat,
        }
        for position, (mode, direction, message_size, qps, tx_depth, repeat) in enumerate(
            combinations, start=1
        )
    ]


def _result_payload(
    *,
    config: PairConfig,
    metadata: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
    modes: list[str],
    directions: list[str],
    message_sizes: list[int],
    qps_values: list[int],
    tx_depths: list[int],
    duration: int,
    repeats: int,
    cooldown_seconds: float,
    schedule: str,
    schedule_seed: int,
    schedule_plan: list[dict[str, Any]],
    preflight_status: dict[str, Any],
    trials: list[dict[str, Any]],
    planned_trials: int,
    run_status: str = "running",
    measurement_accepted: bool = False,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 3,
        "suite_version": VERSION,
        "pair_label": config.pair_label,
        "config_fingerprint_sha256": _config_fingerprint(config),
        "implementation_fingerprint_sha256": _implementation_fingerprint_sha256(),
        "started_at": started_at,
        "finished_at": finished_at,
        "trials_complete": len(trials) == planned_trials,
        "measurement_accepted": measurement_accepted,
        "run_status": run_status,
        "completed_trials": len(trials),
        "planned_trials": planned_trials,
        "metric": "unidirectional host-memory RDMA write payload bandwidth",
        "units": "Gbit/s (decimal)",
        "pcie_gen5_x4_raw_gbps_per_domain": round(PCIE_GEN5_X4_GBPS, 6),
        "two_domain_raw_ceiling_gbps": round(TWO_PCIE_GEN5_X4_GBPS, 6),
        "metadata": metadata,
        "validation": {
            "preflight": preflight_status,
            "postflight": {"status": "pending"},
        },
        "parameters": {
            "modes": modes,
            "directions": directions,
            "message_sizes": message_sizes,
            "qps_values": qps_values,
            "tx_depths": tx_depths,
            "duration_seconds": duration,
            "repeats": repeats,
            "cooldown_seconds": cooldown_seconds,
            "schedule": schedule,
            "schedule_seed": schedule_seed,
        },
        "schedule_plan": schedule_plan,
        "trials": trials,
        "summaries": _summaries(trials),
    }
    payload["qualification"] = evaluate_standard_profile_qualification(
        payload,
        measurement_final=measurement_accepted,
    )
    if failure is not None:
        payload["failure"] = failure
    return payload


def run_matrix(
    config: PairConfig,
    *,
    modes: list[str],
    directions: list[str],
    message_sizes: list[int],
    qps_values: list[int],
    tx_depths: list[int],
    duration: int,
    repeats: int,
    cooldown_seconds: float,
    schedule: str = "interleaved",
    schedule_seed: int = 0,
    preflight_status: dict[str, Any] | None = None,
    runner: SSHRunner | None = None,
    raw_log_dir: Path | None = None,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    runner = runner or SSHRunner()
    for mode in modes:
        config.paths_for_mode(mode)
    started_at = _utc_now()
    metadata: list[dict[str, Any]] = []
    trials: list[dict[str, Any]] = []
    if not modes:
        raise BenchmarkError("at least one benchmark mode is required")
    if schedule_seed < 0:
        raise BenchmarkError("schedule seed must be non-negative")
    combinations = _build_schedule(
        modes=modes,
        directions=directions,
        message_sizes=message_sizes,
        qps_values=qps_values,
        tx_depths=tx_depths,
        repeats=repeats,
        schedule=schedule,
        schedule_seed=schedule_seed,
    )
    schedule_plan = _serialized_plan(combinations)
    recorded_preflight = dict(
        preflight_status or {"status": "not-run", "reason": "library caller did not provide it"}
    )
    total = len(combinations)

    def build_result(
        run_status: str,
        *,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _result_payload(
            config=config,
            metadata=metadata,
            started_at=started_at,
            finished_at=_utc_now(),
            modes=modes,
            directions=directions,
            message_sizes=message_sizes,
            qps_values=qps_values,
            tx_depths=tx_depths,
            duration=duration,
            repeats=repeats,
            cooldown_seconds=cooldown_seconds,
            schedule=schedule,
            schedule_seed=schedule_seed,
            schedule_plan=schedule_plan,
            preflight_status=recorded_preflight,
            trials=trials,
            planned_trials=total,
            measurement_accepted=False,
            run_status=run_status,
            failure=failure,
        )

    if checkpoint_path is not None:
        _write_json_atomic(checkpoint_path, build_result("initializing"))
    try:
        for role in ("head", "worker"):
            metadata.append(asdict(collect_node_metadata(runner, config, role)))
        if checkpoint_path is not None:
            _write_json_atomic(checkpoint_path, build_result("running"))
        for index, (mode, direction, message_size, qps, tx_depth, repeat) in enumerate(
            combinations, start=1
        ):
            print(
                f"[{index}/{total}] {mode} {direction} size={message_size} "
                f"qps={qps} tx-depth={tx_depth} repeat={repeat}",
                file=sys.stderr,
                flush=True,
            )
            trial = run_trial(
                config,
                mode,
                direction=direction,
                duration=duration,
                message_size=message_size,
                qps=qps,
                tx_depth=tx_depth,
                trial_index=repeat,
                runner=runner,
                raw_log_dir=raw_log_dir,
            )
            trials.append(trial)
            if checkpoint_path is not None:
                _write_json_atomic(checkpoint_path, build_result("running"))
            print(
                f"  aggregate={trial['aggregate_gbps']:.2f} Gbit/s "
                f"({trial['aggregate_GBps']:.2f} GB/s)",
                file=sys.stderr,
                flush=True,
            )
            if index != total and cooldown_seconds:
                time.sleep(cooldown_seconds)
    except BaseException as exc:
        run_status = (
            "interrupted"
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "benchmark-failed"
        )
        if checkpoint_path is not None:
            _write_json_atomic(
                checkpoint_path,
                build_result(run_status, failure={"error_type": type(exc).__name__}),
            )
        raise

    # Traffic completion is recorded separately from evidence acceptance. The
    # CLI must execute postflight validation before accepting the measurement.
    result = build_result("awaiting-postflight")
    if checkpoint_path is not None:
        _write_json_atomic(checkpoint_path, result)
    return result
