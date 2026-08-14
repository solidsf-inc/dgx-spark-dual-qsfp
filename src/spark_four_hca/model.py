"""Configuration model and strict topology validation."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Interface, IPv4Network, ip_interface, ip_network
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    """Raised when a pair configuration is unsafe or internally inconsistent."""


_SAFE_SSH_TARGET = re.compile(r"^[A-Za-z0-9_.:@%+\-\[\]]+$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_RFC1918_NETWORKS = tuple(
    ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

_ROOT_KEYS = {
    "schema_version",
    "pair_label",
    "mtu",
    "duration_seconds",
    "message_size_bytes",
    "qps",
    "base_port",
    "nodes",
    "paths",
    "modes",
}
_NODE_KEYS = {"ssh", "label", "cpu_cores"}
_PATH_KEYS = {
    "name",
    "physical_port",
    "pcie_domain",
    "netdev",
    "rdma_device",
    "head_address",
    "worker_address",
}

CANONICAL_PATHS: dict[str, tuple[int, int, str, str]] = {
    "p0-d0": (0, 0, "enp1s0f0np0", "rocep1s0f0"),
    "p0-d2": (0, 2, "enP2p1s0f0np0", "roceP2p1s0f0"),
    "p1-d0": (1, 0, "enp1s0f1np1", "rocep1s0f1"),
    "p1-d2": (1, 2, "enP2p1s0f1np1", "roceP2p1s0f1"),
}

CANONICAL_INTERFACES: dict[tuple[int, int], tuple[str, str]] = {
    (physical_port, pcie_domain): (netdev, rdma_device)
    for physical_port, pcie_domain, netdev, rdma_device in CANONICAL_PATHS.values()
}

CANONICAL_MODES: dict[str, tuple[str, ...]] = {
    "single-p0-d0": ("p0-d0",),
    "single-p0-d2": ("p0-d2",),
    "single-p1-d0": ("p1-d0",),
    "single-p1-d2": ("p1-d2",),
    "same-cage-p0": ("p0-d0", "p0-d2"),
    "same-cage-p1": ("p1-d0", "p1-d2"),
    "same-domain-d0": ("p0-d0", "p1-d0"),
    "same-domain-d2": ("p0-d2", "p1-d2"),
    "diagonal-a": ("p0-d0", "p1-d2"),
    "diagonal-b": ("p1-d0", "p0-d2"),
    "all-four": ("p0-d0", "p0-d2", "p1-d0", "p1-d2"),
}


@dataclass(frozen=True)
class Node:
    role: str
    ssh: str
    label: str
    cpu_cores: tuple[int, ...]


@dataclass(frozen=True)
class PathConfig:
    name: str
    physical_port: int
    pcie_domain: int
    netdev: str
    rdma_device: str
    head_address: IPv4Interface
    worker_address: IPv4Interface

    @property
    def network(self) -> IPv4Network:
        return self.head_address.network

    def address_for(self, role: str) -> IPv4Interface:
        if role == "head":
            return self.head_address
        if role == "worker":
            return self.worker_address
        raise ConfigError(f"unknown node role: {role!r}")

    def peer_address_for(self, role: str) -> IPv4Interface:
        return self.address_for("worker" if role == "head" else "head")


@dataclass(frozen=True)
class PairConfig:
    source: Path
    schema_version: int
    pair_label: str
    mtu: int
    duration_seconds: int
    message_size_bytes: int
    qps: int
    base_port: int
    head: Node
    worker: Node
    paths: tuple[PathConfig, ...]
    modes: dict[str, tuple[str, ...]]

    @property
    def paths_by_name(self) -> dict[str, PathConfig]:
        return {path.name: path for path in self.paths}

    def paths_for_mode(self, mode: str) -> tuple[PathConfig, ...]:
        try:
            names = self.modes[mode]
        except KeyError as exc:
            available = ", ".join(sorted(self.modes))
            raise ConfigError(f"unknown mode {mode!r}; available: {available}") from exc
        by_name = self.paths_by_name
        return tuple(by_name[name] for name in names)

    def node(self, role: str) -> Node:
        if role == "head":
            return self.head
        if role == "worker":
            return self.worker
        raise ConfigError(f"unknown node role: {role!r}")


def _required(table: dict, key: str, context: str):
    if key not in table:
        raise ConfigError(f"{context} is missing required key {key!r}")
    return table[key]


def _reject_unknown_keys(table: dict, allowed: set[str], context: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{context} contains unknown keys: {sorted(unknown)}")


def _positive_int(value, context: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{context} must be >= {minimum}{upper}")
    return value


def _parse_node(role: str, raw: dict) -> Node:
    if not isinstance(raw, dict):
        raise ConfigError(f"nodes.{role} must be a table")
    _reject_unknown_keys(raw, _NODE_KEYS, f"nodes.{role}")
    ssh = str(_required(raw, "ssh", f"nodes.{role}"))
    label = str(raw.get("label", role))
    if ssh.startswith("-") or not _SAFE_SSH_TARGET.fullmatch(ssh):
        raise ConfigError(
            f"nodes.{role}.ssh is not a safe SSH target; use an SSH config alias "
            "that does not begin with '-'"
        )
    if not _SAFE_LABEL.fullmatch(label):
        raise ConfigError(f"nodes.{role}.label must match {_SAFE_LABEL.pattern}")
    raw_cores = raw.get("cpu_cores", [])
    if not isinstance(raw_cores, list):
        raise ConfigError(f"nodes.{role}.cpu_cores must be an array")
    cpu_cores = tuple(
        _positive_int(core, f"nodes.{role}.cpu_cores", minimum=0, maximum=4095)
        for core in raw_cores
    )
    if cpu_cores and len(cpu_cores) < 4:
        raise ConfigError(f"nodes.{role}.cpu_cores must contain at least four cores or be empty")
    if len(set(cpu_cores)) != len(cpu_cores):
        raise ConfigError(f"nodes.{role}.cpu_cores must not contain duplicates")
    return Node(role=role, ssh=ssh, label=label, cpu_cores=cpu_cores)


def _parse_address(value, context: str) -> IPv4Interface:
    try:
        parsed = ip_interface(str(value))
    except ValueError as exc:
        raise ConfigError(f"{context} is not a valid CIDR address: {value!r}") from exc
    if not isinstance(parsed, IPv4Interface):
        raise ConfigError(f"{context} must be IPv4")
    if parsed.network.prefixlen != 30:
        raise ConfigError(f"{context} must use a /30 point-to-point subnet")
    if parsed.ip in (parsed.network.network_address, parsed.network.broadcast_address):
        raise ConfigError(f"{context} must be a usable host address")
    if not any(parsed.network.subnet_of(network) for network in _RFC1918_NETWORKS):
        raise ConfigError(f"{context} must use RFC 1918 private address space")
    return parsed


def _parse_path(raw: dict, index: int) -> PathConfig:
    context = f"paths[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a table")
    _reject_unknown_keys(raw, _PATH_KEYS, context)
    name = str(_required(raw, "name", context))
    if not _SAFE_LABEL.fullmatch(name):
        raise ConfigError(f"{context}.name must match {_SAFE_LABEL.pattern}")
    physical_port = _positive_int(
        _required(raw, "physical_port", context),
        f"{context}.physical_port",
        minimum=0,
        maximum=1,
    )
    pcie_domain = _positive_int(
        _required(raw, "pcie_domain", context),
        f"{context}.pcie_domain",
        minimum=0,
        maximum=2,
    )
    key = (physical_port, pcie_domain)
    if key not in CANONICAL_INTERFACES:
        raise ConfigError(
            f"{context} has invalid DGX Spark port/domain combination {key}; "
            "domains are 0 and 2"
        )
    netdev = str(_required(raw, "netdev", context))
    rdma_device = str(_required(raw, "rdma_device", context))
    expected = CANONICAL_INTERFACES[key]
    if (netdev, rdma_device) != expected:
        raise ConfigError(
            f"{context} maps port/domain {key} to {(netdev, rdma_device)!r}; "
            f"expected {expected!r}"
        )
    head_address = _parse_address(
        _required(raw, "head_address", context), f"{context}.head_address"
    )
    worker_address = _parse_address(
        _required(raw, "worker_address", context), f"{context}.worker_address"
    )
    if head_address.network != worker_address.network:
        raise ConfigError(f"{context} head and worker addresses are not in the same /30")
    if head_address.ip == worker_address.ip:
        raise ConfigError(f"{context} head and worker addresses must differ")
    return PathConfig(
        name=name,
        physical_port=physical_port,
        pcie_domain=pcie_domain,
        netdev=netdev,
        rdma_device=rdma_device,
        head_address=head_address,
        worker_address=worker_address,
    )


def load_config(path: str | Path) -> PairConfig:
    source = Path(path).expanduser().resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {source}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {source}: {exc}") from exc

    _reject_unknown_keys(raw, _ROOT_KEYS, "root")

    schema_version = _positive_int(
        raw.get("schema_version", 0), "schema_version", minimum=1, maximum=1
    )
    pair_label = str(_required(raw, "pair_label", "root"))
    if not _SAFE_LABEL.fullmatch(pair_label):
        raise ConfigError(f"pair_label must match {_SAFE_LABEL.pattern}")
    mtu = _positive_int(raw.get("mtu", 9000), "mtu", minimum=1500, maximum=9216)
    duration = _positive_int(
        raw.get("duration_seconds", 10), "duration_seconds", minimum=2, maximum=3600
    )
    message_size = _positive_int(
        raw.get("message_size_bytes", 65536),
        "message_size_bytes",
        minimum=1,
        maximum=1 << 30,
    )
    # The operational profile uses the simplest observed throughput plateau.
    # Topology studies override this with --qps 1 so topology remains the only
    # independent variable.
    qps = _positive_int(raw.get("qps", 2), "qps", minimum=1, maximum=128)
    base_port = _positive_int(
        raw.get("base_port", 18515), "base_port", minimum=1024, maximum=65531
    )

    nodes = _required(raw, "nodes", "root")
    if not isinstance(nodes, dict):
        raise ConfigError("nodes must be a table")
    _reject_unknown_keys(nodes, {"head", "worker"}, "nodes")
    head = _parse_node("head", _required(nodes, "head", "nodes"))
    worker = _parse_node("worker", _required(nodes, "worker", "nodes"))
    if head.ssh == worker.ssh:
        raise ConfigError("head and worker SSH targets must differ")

    raw_paths = _required(raw, "paths", "root")
    if not isinstance(raw_paths, list) or len(raw_paths) != 4:
        raise ConfigError(
            "paths must contain exactly four DGX Spark logical RoCE mappings"
        )
    paths = tuple(_parse_path(item, index) for index, item in enumerate(raw_paths))
    names = [item.name for item in paths]
    if len(set(names)) != len(names):
        raise ConfigError("path names must be unique")
    configured_names = set(names)
    canonical_names = set(CANONICAL_PATHS)
    if configured_names != canonical_names:
        missing = sorted(canonical_names - configured_names)
        unexpected = sorted(configured_names - canonical_names)
        raise ConfigError(
            "paths must use exactly the canonical DGX Spark path names; "
            f"missing={missing}, unexpected={unexpected}"
        )
    for item in paths:
        configured_mapping = (
            item.physical_port,
            item.pcie_domain,
            item.netdev,
            item.rdma_device,
        )
        expected_mapping = CANONICAL_PATHS[item.name]
        if configured_mapping != expected_mapping:
            raise ConfigError(
                f"path {item.name!r} maps to {configured_mapping!r}; "
                f"expected canonical mapping {expected_mapping!r}"
            )
    port_domains = [(item.physical_port, item.pcie_domain) for item in paths]
    if set(port_domains) != set(CANONICAL_INTERFACES):
        raise ConfigError("paths must cover both physical ports and both PCIe domains")
    networks = [item.network for item in paths]
    for index, network in enumerate(networks):
        for other in networks[index + 1 :]:
            if network.overlaps(other):
                raise ConfigError(f"path networks overlap: {network} and {other}")

    raw_modes = _required(raw, "modes", "root")
    if not isinstance(raw_modes, dict) or not raw_modes:
        raise ConfigError("modes must be a non-empty table")
    known = set(names)
    modes: dict[str, tuple[str, ...]] = {}
    for mode_name, members in raw_modes.items():
        if not _SAFE_LABEL.fullmatch(mode_name):
            raise ConfigError(f"mode name {mode_name!r} must match {_SAFE_LABEL.pattern}")
        if not isinstance(members, list) or not members:
            raise ConfigError(f"mode {mode_name!r} must be a non-empty array")
        normalized = tuple(str(member) for member in members)
        if len(set(normalized)) != len(normalized):
            raise ConfigError(f"mode {mode_name!r} contains a duplicate path")
        unknown = set(normalized) - known
        if unknown:
            raise ConfigError(f"mode {mode_name!r} references unknown paths: {sorted(unknown)}")
        modes[mode_name] = normalized

    configured_mode_names = set(modes)
    canonical_mode_names = set(CANONICAL_MODES)
    if configured_mode_names != canonical_mode_names:
        missing = sorted(canonical_mode_names - configured_mode_names)
        unexpected = sorted(configured_mode_names - canonical_mode_names)
        raise ConfigError(
            "modes must define exactly the canonical benchmark mode names; "
            f"missing={missing}, unexpected={unexpected}"
        )
    for mode_name, expected_members in CANONICAL_MODES.items():
        if modes[mode_name] != expected_members:
            raise ConfigError(
                f"mode {mode_name!r} must use the canonical ordered paths "
                f"{list(expected_members)!r}; got {list(modes[mode_name])!r}"
            )

    return PairConfig(
        source=source,
        schema_version=schema_version,
        pair_label=pair_label,
        mtu=mtu,
        duration_seconds=duration,
        message_size_bytes=message_size,
        qps=qps,
        base_port=base_port,
        head=head,
        worker=worker,
        paths=paths,
        modes=modes,
    )
