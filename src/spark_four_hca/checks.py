"""Read-only validation for the four logical DGX Spark RoCE paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Mapping

from .model import PairConfig, PathConfig
from .remote import SSHRunner


@dataclass(frozen=True)
class Check:
    node: str
    path: str
    name: str
    status: str
    observed: str
    expected: str


@dataclass(frozen=True)
class NodeMetadata:
    role: str
    label: str
    kernel: str
    perftest_version: str
    driver: str
    firmware: str


_COUNTERS = {
    "rx_crc_errors_phy",
    "rx_symbol_err_phy",
    "rx_discards_phy",
    "tx_discards_phy",
    "rx_out_of_buffer",
}


def _empty_counter_snapshot(role: str, path: str) -> dict[str, Any]:
    """Return a public-safe placeholder when a counter snapshot is unavailable."""

    return {
        "node_role": role,
        "path": path,
        "coverage_complete": False,
        "values": {},
    }


def _counter_snapshot(
    role: str,
    path: str,
    *,
    counters_readable: bool,
    counters: Mapping[str, int],
) -> dict[str, Any]:
    """Serialize only canonical roles, paths, and allowlisted link counters."""

    values = {
        name: int(counters[name])
        for name in sorted(_COUNTERS)
        if name in counters
    }
    return {
        "node_role": role,
        "path": path,
        "coverage_complete": counters_readable and set(values) == _COUNTERS,
        "values": values,
    }


def _snapshot_index(report: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, int]]:
    block = report.get("link_error_counters")
    if not isinstance(block, Mapping):
        raise ValueError("link error counter snapshot is missing")
    names = block.get("counter_names")
    if names != sorted(_COUNTERS):
        raise ValueError("link error counter names are incomplete")
    snapshots = block.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("link error counter snapshots are unavailable")

    indexed: dict[tuple[str, str], dict[str, int]] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            raise ValueError("invalid link error counter snapshot")
        role = snapshot.get("node_role")
        path = snapshot.get("path")
        if role not in ("head", "worker") or not isinstance(path, str):
            raise ValueError("invalid link error counter snapshot identity")
        key = (role, path)
        if key in indexed:
            raise ValueError("duplicate link error counter snapshot")
        if snapshot.get("coverage_complete") is not True:
            raise ValueError(f"incomplete counter coverage for {role}/{path}")
        values = snapshot.get("values")
        if not isinstance(values, Mapping) or set(values) != _COUNTERS:
            raise ValueError(f"incomplete counter values for {role}/{path}")
        parsed: dict[str, int] = {}
        for name in sorted(_COUNTERS):
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid counter value for {role}/{path}/{name}")
            parsed[name] = value
        indexed[key] = parsed
    return indexed


def compare_link_error_counters(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare sanitized snapshots and fail closed on growth, reset, or gaps."""

    base: dict[str, Any] = {
        "counter_names": sorted(_COUNTERS),
        "ok": False,
        "status": "unavailable",
        "deltas": [],
        "positive_deltas": [],
        "reset_counters": [],
    }
    try:
        before_index = _snapshot_index(before)
        after_index = _snapshot_index(after)
    except ValueError as exc:
        return {**base, "reason": str(exc)}
    if set(before_index) != set(after_index):
        return {**base, "reason": "preflight and postflight path sets differ"}

    deltas: list[dict[str, Any]] = []
    positive: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []
    for role, path in sorted(before_index):
        values = {
            name: after_index[(role, path)][name] - before_index[(role, path)][name]
            for name in sorted(_COUNTERS)
        }
        deltas.append({"node_role": role, "path": path, "values": values})
        positive_values = {name: value for name, value in values.items() if value > 0}
        reset_values = {name: value for name, value in values.items() if value < 0}
        if positive_values:
            positive.append(
                {"node_role": role, "path": path, "values": positive_values}
            )
        if reset_values:
            resets.append({"node_role": role, "path": path, "values": reset_values})

    if positive or resets:
        return {
            **base,
            "status": "failed",
            "deltas": deltas,
            "positive_deltas": positive,
            "reset_counters": resets,
        }
    return {
        **base,
        "status": "passed",
        "ok": True,
        "deltas": deltas,
    }


def _check(
    checks: list[Check],
    *,
    node: str,
    path: str,
    name: str,
    ok: bool,
    observed: Any,
    expected: Any,
    warning: bool = False,
) -> None:
    checks.append(
        Check(
            node=node,
            path=path,
            name=name,
            status="pass" if ok else ("warn" if warning else "fail"),
            observed=str(observed),
            expected=str(expected),
        )
    )


def _first_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix.lower()):
            return stripped.split(":", 1)[1].strip()
    return "unknown"


def collect_node_metadata(runner: SSHRunner, config: PairConfig, role: str) -> NodeMetadata:
    node = config.node(role)
    kernel = runner.run(node.ssh, ["uname", "-r"], check=True).stdout.strip()
    version_result = runner.run(node.ssh, ["ib_write_bw", "--version"], timeout=15)
    version_text = (version_result.stdout + "\n" + version_result.stderr).strip()
    if not version_text:
        version_text = "unavailable"
    interface = config.paths[0].netdev
    driver_result = runner.run(node.ssh, ["ethtool", "-i", interface], timeout=15)
    driver_text = driver_result.stdout if driver_result.returncode == 0 else ""
    return NodeMetadata(
        role=role,
        # Roles are stable and public-safe; labels may mirror private hostnames.
        label=role,
        kernel=kernel,
        perftest_version=version_text.splitlines()[0],
        driver=_first_value(driver_text, "driver"),
        firmware=_first_value(driver_text, "firmware-version").split()[0],
    )


def _counter_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z0-9_]+):\s*(\d+)\s*$", line)
        if match and match.group(1) in _COUNTERS:
            values[match.group(1)] = int(match.group(2))
    return values


def _pci_address(path: PathConfig) -> str:
    return f"{path.pcie_domain:04d}:01:00.{path.physical_port}"


_REMOTE_PROBE = r'''
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

payload = json.loads(sys.argv[1])

def run(argv, timeout=15):
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"}, check=False,
        )
        return {"rc": result.returncode, "out": result.stdout, "err": result.stderr}
    except Exception as exc:
        return {"rc": 255, "out": "", "err": str(exc)}

def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""

def read_optional(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None

def parse_cpu_list(text):
    if text is None or not re.fullmatch(r"[0-9,-]+", text):
        return None
    cpus = set()
    try:
        for part in text.split(","):
            bounds = part.split("-", 1)
            start = int(bounds[0])
            stop = int(bounds[-1])
            if start > stop:
                return None
            cpus.update(range(start, stop + 1))
    except ValueError:
        return None
    return cpus

def machine_identity_digest():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        value = read_optional(path)
        if value and re.fullmatch(r"[0-9a-fA-F]{32}", value):
            return hashlib.sha256(value.lower().encode("ascii")).hexdigest()
    return ""

def field(text, name):
    prefix = name.lower() + ":"
    for line in text.splitlines():
        value = line.strip()
        if value.lower().startswith(prefix):
            return value.split(":", 1)[1].strip()
    return "unknown"

first = payload["paths"][0]
driver = run(["ethtool", "-i", first["netdev"]])
version = run(["ib_write_bw", "--version"])
mapping = run(["ibdev2netdev"])
metadata = {
    "kernel": platform.release(),
    "perftest_version": (version["out"] + "\n" + version["err"]).strip().splitlines()[0] if (version["out"] + version["err"]).strip() else "unavailable",
    "driver": field(driver["out"], "driver"),
    "firmware": field(driver["out"], "firmware-version").split()[0],
}

configured_cores = payload.get("cpu_cores", [])
online_cpus = parse_cpu_list(read_optional("/sys/devices/system/cpu/online"))
cpu_affinity = {
    "configured": bool(configured_cores),
    "observable": not configured_cores or online_cpus is not None,
    "all_present": all(
        os.path.isdir(f"/sys/devices/system/cpu/cpu{core}")
        for core in configured_cores
    ),
    "all_online": (
        not configured_cores
        or (
            online_cpus is not None
            and all(core in online_cpus for core in configured_cores)
        )
    ),
}

paths = []
counter_names = {
    "rx_crc_errors_phy", "rx_symbol_err_phy", "rx_discards_phy",
    "tx_discards_phy", "rx_out_of_buffer",
}
for item in payload["paths"]:
    netdev = item["netdev"]
    hca = item["rdma_device"]
    netroot = f"/sys/class/net/{netdev}"
    rdmaroot = f"/sys/class/infiniband/{hca}/ports/1"
    exists = os.path.isdir(netroot)
    ipv4_observable = False
    expected_only_global_ipv4 = False
    addr_result = run(["ip", "-j", "-4", "address", "show", "dev", netdev]) if exists else {"rc": 1, "out": "[]", "err": "missing"}
    if addr_result["rc"] == 0:
        try:
            global_addresses = []
            for interface in json.loads(addr_result["out"]):
                for address in interface.get("addr_info", []):
                    if address.get("family") == "inet" and address.get("scope") == "global":
                        global_addresses.append(
                            (str(address.get("local")), address.get("prefixlen"))
                        )
            ipv4_observable = True
            expected_only_global_ipv4 = global_addresses == [
                (item["local_address"], item["prefixlen"])
            ]
        except Exception:
            pass
    route_result = run([
        "ip", "-j", "-4", "route", "get", item["peer_address"],
    ]) if exists else {"rc": 1, "out": "[]", "err": "missing"}
    route_observable = False
    route_dev_matches = False
    route_source_matches = False
    if route_result["rc"] == 0:
        try:
            routes = json.loads(route_result["out"])
            if isinstance(routes, list) and len(routes) == 1 and isinstance(routes[0], dict):
                route = routes[0]
                route_observable = True
                route_dev_matches = route.get("dev") == netdev
                route_source_matches = (
                    route.get("prefsrc", route.get("src")) == item["local_address"]
                )
        except Exception:
            pass
    devinfo = run(["ibv_devinfo", "-d", hca, "-i", "1"]) if exists else {"rc": 1, "out": "", "err": "missing"}
    active_mtu_match = re.search(r"^\s*active_mtu:\s*(\d+)", devinfo["out"], re.MULTILINE)
    counters_result = run(["ethtool", "-S", netdev]) if exists else {"rc": 1, "out": "", "err": "missing"}
    counters = {}
    for line in counters_result["out"].splitlines():
        match = re.match(r"\s*([A-Za-z0-9_]+):\s*(\d+)\s*$", line)
        if match and match.group(1) in counter_names:
            counters[match.group(1)] = int(match.group(2))
    pciroot = f"/sys/bus/pci/devices/{item['pci_address']}"
    netdev_device = f"{netroot}/device"
    pci_backing_observable = False
    pci_backing_matches = False
    try:
        if exists and os.path.exists(netdev_device) and os.path.exists(pciroot):
            pci_backing_observable = True
            pci_backing_matches = os.path.samefile(netdev_device, pciroot)
    except OSError:
        pass
    ping = run([
        "ping", "-I", netdev, "-c", str(payload["ping_count"]),
        "-W", "2", item["peer_address"],
    ], timeout=max(10, payload["ping_count"] * 3)) if exists else {"rc": 1, "out": "", "err": "missing"}
    paths.append({
        "name": item["name"],
        "exists": exists,
        "operstate": read(f"{netroot}/operstate"),
        "carrier": read(f"{netroot}/carrier"),
        "speed": read(f"{netroot}/speed"),
        "mtu": read(f"{netroot}/mtu"),
        "ipv4_check": {
            "observable": ipv4_observable,
            "expected_only_global": expected_only_global_ipv4,
        },
        "route_check": {
            "observable": route_observable,
            "expected_dev": route_dev_matches,
            "expected_source": route_source_matches,
        },
        "pci_backing": {
            "observable": pci_backing_observable,
            "matches": pci_backing_matches,
        },
        "mapping_up": f"{hca} port 1 ==> {netdev} (Up)" in " ".join(mapping["out"].split()),
        "rdma_state": read(f"{rdmaroot}/state"),
        "active_mtu": active_mtu_match.group(1) if active_mtu_match else "",
        "pcie_width": read(f"{pciroot}/current_link_width"),
        "pcie_speed": read(f"{pciroot}/current_link_speed"),
        "counters_readable": counters_result["rc"] == 0,
        "counters": counters,
        "ping_ok": ping["rc"] == 0,
    })

print(json.dumps({
    "metadata": metadata,
    "machine_identity_digest": machine_identity_digest(),
    "cpu_affinity": cpu_affinity,
    "paths": paths,
}, sort_keys=True))
'''


def _probe_node(
    runner: SSHRunner,
    config: PairConfig,
    role: str,
    ping_count: int,
) -> dict[str, Any]:
    node = config.node(role)
    payload = {
        "ping_count": ping_count,
        "cpu_cores": list(node.cpu_cores),
        "paths": [
            {
                "name": path.name,
                "netdev": path.netdev,
                "rdma_device": path.rdma_device,
                "pci_address": _pci_address(path),
                "local_address": str(path.address_for(role).ip),
                "prefixlen": path.address_for(role).network.prefixlen,
                "peer_address": str(path.peer_address_for(role).ip),
            }
            for path in config.paths
        ],
    }
    result = runner.run(
        node.ssh,
        ["python3", "-c", _REMOTE_PROBE, json.dumps(payload, separators=(",", ":"))],
        timeout=max(30, ping_count * len(config.paths) * 3),
    )
    if result.returncode != 0:
        raise RuntimeError("remote probe failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("remote probe returned invalid JSON") from exc


def check_pair(
    config: PairConfig,
    *,
    runner: SSHRunner | None = None,
    ping_count: int = 2,
) -> dict[str, Any]:
    runner = runner or SSHRunner()
    checks: list[Check] = []
    metadata: list[NodeMetadata] = []
    counter_snapshots: list[dict[str, Any]] = []
    machine_identities: dict[str, str | None] = {"head": None, "worker": None}

    for role in ("head", "worker"):
        node = config.node(role)
        try:
            probe = _probe_node(runner, config, role, ping_count)
            if not isinstance(probe, Mapping):
                raise ValueError("invalid probe result")
        except Exception:
            _check(
                checks,
                node=role,
                path="all",
                name="remote-probe",
                ok=False,
                observed="unavailable",
                expected="one successful read-only probe",
            )
            counter_snapshots.extend(
                _empty_counter_snapshot(role, path.name) for path in config.paths
            )
            continue

        meta = probe.get("metadata")
        if not isinstance(meta, Mapping):
            meta = {}
        metadata.append(
            NodeMetadata(
                role=role,
                label=role,
                kernel=str(meta.get("kernel", "unavailable")),
                perftest_version=str(meta.get("perftest_version", "unavailable")),
                driver=str(meta.get("driver", "unavailable")),
                firmware=str(meta.get("firmware", "unavailable")),
            )
        )

        identity_digest = probe.get("machine_identity_digest")
        if isinstance(identity_digest, str) and re.fullmatch(
            r"[0-9a-f]{64}", identity_digest
        ):
            machine_identities[role] = identity_digest

        affinity = probe.get("cpu_affinity")
        if not node.cpu_cores:
            affinity_ok = True
            affinity_observed = "no CPU affinity configured"
        elif not isinstance(affinity, Mapping) or affinity.get("observable") is not True:
            affinity_ok = False
            affinity_observed = "unavailable"
        elif affinity.get("all_present") is not True:
            affinity_ok = False
            affinity_observed = "one or more configured cores are missing"
        elif affinity.get("all_online") is not True:
            affinity_ok = False
            affinity_observed = "one or more configured cores are offline"
        else:
            affinity_ok = True
            affinity_observed = "all configured cores exist and are online"
        _check(
            checks,
            node=role,
            path="all",
            name="cpu-affinity",
            ok=affinity_ok,
            observed=affinity_observed,
            expected="all configured cores exist and are online",
        )

        path_items = probe.get("paths")
        if not isinstance(path_items, list):
            path_items = []
        observed_by_name = {
            item.get("name"): item
            for item in path_items
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        for path in config.paths:
            observed = observed_by_name.get(path.name)
            if not isinstance(observed, Mapping):
                _check(
                    checks,
                    node=role,
                    path=path.name,
                    name="path-probe",
                    ok=False,
                    observed="unavailable",
                    expected="one complete read-only path probe",
                )
                counter_snapshots.append(_empty_counter_snapshot(role, path.name))
                continue

            exists = observed.get("exists") is True
            _check(
                checks,
                node=role,
                path=path.name,
                name="netdev-exists",
                ok=exists,
                observed=path.netdev if exists else "missing",
                expected=path.netdev,
            )
            if not exists:
                counter_snapshots.append(_empty_counter_snapshot(role, path.name))
                continue
            _check(
                checks,
                node=role,
                path=path.name,
                name="operstate",
                ok=observed.get("operstate") == "up",
                observed=observed.get("operstate", "unavailable"),
                expected="up",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="carrier",
                ok=observed.get("carrier") == "1",
                observed=observed.get("carrier", "unavailable"),
                expected="1",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="link-speed-mbps",
                ok=observed.get("speed") == "200000",
                observed=observed.get("speed", "unavailable"),
                expected="200000",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="mtu",
                ok=observed.get("mtu") == str(config.mtu),
                observed=observed.get("mtu", "unavailable"),
                expected=config.mtu,
            )

            ipv4_check = observed.get("ipv4_check")
            ipv4_observable = (
                isinstance(ipv4_check, Mapping)
                and ipv4_check.get("observable") is True
            )
            ipv4_ok = (
                ipv4_observable
                and ipv4_check.get("expected_only_global") is True
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="ipv4-global-address",
                ok=ipv4_ok,
                observed=(
                    "configured /30 is the only global IPv4"
                    if ipv4_ok
                    else (
                        "unexpected global IPv4 configuration"
                        if ipv4_observable
                        else "unavailable"
                    )
                ),
                expected="only the configured /30 has global scope; link-local is permitted",
            )

            route_check = observed.get("route_check")
            route_observable = (
                isinstance(route_check, Mapping)
                and route_check.get("observable") is True
            )
            route_ok = (
                route_observable
                and route_check.get("expected_dev") is True
                and route_check.get("expected_source") is True
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="peer-route",
                ok=route_ok,
                observed=(
                    "canonical device and source selected"
                    if route_ok
                    else ("wrong device or source" if route_observable else "unavailable")
                ),
                expected="canonical device and configured source",
            )

            pci_backing = observed.get("pci_backing")
            pci_observable = (
                isinstance(pci_backing, Mapping)
                and pci_backing.get("observable") is True
            )
            pci_ok = pci_observable and pci_backing.get("matches") is True
            _check(
                checks,
                node=role,
                path=path.name,
                name="netdev-pci-backing",
                ok=pci_ok,
                observed=(
                    "canonical backing verified"
                    if pci_ok
                    else ("backing mismatch" if pci_observable else "unavailable")
                ),
                expected=_pci_address(path),
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="rdma-mapping",
                ok=observed.get("mapping_up") is True,
                observed="up" if observed.get("mapping_up") is True else "missing/down",
                expected=f"{path.rdma_device} -> {path.netdev} (Up)",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="rdma-state",
                ok="ACTIVE" in str(observed.get("rdma_state", "")),
                observed=observed.get("rdma_state", "unavailable"),
                expected="ACTIVE",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="rdma-active-mtu",
                ok=observed.get("active_mtu") == "4096",
                observed=observed.get("active_mtu", "unavailable"),
                expected="4096",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="pcie-width",
                ok=observed.get("pcie_width") == "4",
                observed=observed.get("pcie_width", "unavailable"),
                expected="4",
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="pcie-speed",
                ok=str(observed.get("pcie_speed", "")).startswith("32.0 GT/s"),
                observed=observed.get("pcie_speed", "unavailable"),
                expected="32.0 GT/s PCIe",
            )
            counters = observed.get("counters", {})
            if not isinstance(counters, Mapping):
                counters = {}
            counters_readable = bool(observed.get("counters_readable", False))
            counter_snapshots.append(
                _counter_snapshot(
                    role,
                    path.name,
                    counters_readable=counters_readable,
                    counters=counters,
                )
            )
            missing_counters = sorted(_COUNTERS - set(counters))
            coverage_ok = counters_readable and not missing_counters
            if not counters_readable:
                coverage_observed = "ethtool -S failed"
            elif missing_counters:
                coverage_observed = "missing: " + ",".join(missing_counters)
            else:
                coverage_observed = "all expected counters present"
            _check(
                checks,
                node=role,
                path=path.name,
                name="error-counter-coverage",
                ok=coverage_ok,
                observed=coverage_observed,
                expected=",".join(sorted(_COUNTERS)),
                # A successful command with driver-specific missing counters is
                # visible but non-fatal. Failure to execute ethtool is fatal.
                warning=counters_readable,
            )
            bad = {name: value for name, value in counters.items() if value != 0}
            _check(
                checks,
                node=role,
                path=path.name,
                name="error-counters",
                ok=coverage_ok and not bad,
                observed=(
                    json.dumps(bad, sort_keys=True)
                    if bad
                    else ("zero" if coverage_ok else "not fully observable")
                ),
                expected="zero",
                warning=True,
            )
            _check(
                checks,
                node=role,
                path=path.name,
                name="peer-ping",
                ok=observed.get("ping_ok") is True,
                observed="reachable" if observed.get("ping_ok") is True else "unreachable",
                expected="peer reachable on canonical path",
            )

    identities_available = all(machine_identities.values())
    identities_distinct = (
        identities_available
        and machine_identities["head"] != machine_identities["worker"]
    )
    _check(
        checks,
        node="pair",
        path="all",
        name="node-identity-distinct",
        ok=bool(identities_distinct),
        observed=(
            "distinct machine identities"
            if identities_distinct
            else ("same machine identity" if identities_available else "unavailable")
        ),
        expected="head and worker aliases resolve to distinct machines",
    )

    failed = sum(item.status == "fail" for item in checks)
    warned = sum(item.status == "warn" for item in checks)
    return {
        "schema_version": 1,
        "pair_label": config.pair_label,
        "ok": failed == 0,
        "summary": {"passed": len(checks) - failed - warned, "warned": warned, "failed": failed},
        "metadata": [asdict(item) for item in metadata],
        "checks": [asdict(item) for item in checks],
        "link_error_counters": {
            "counter_names": sorted(_COUNTERS),
            "snapshots": counter_snapshots,
        },
    }
