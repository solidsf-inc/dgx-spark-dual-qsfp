# DGX Spark Dual-QSFP Suite: One ConnectX-7, Four Logical RoCE Devices

This is an independent solidSF, Inc. community project. It is not an NVIDIA
product, is not endorsed or supported by NVIDIA, and does not represent an
NVIDIA performance claim or supported-configuration recommendation.

The project measures one specific DGX Spark networking topology. Each system
has one ConnectX-7 adapter. With two physical QSFP cables, that adapter exposes
four logical Linux Ethernet/RoCE device paths which share two PCIe Gen5 x4
links into the GB10 SoC. The suite identifies combinations that use both cable
cages and both PCIe domains, then measures them with concurrent `ib_write_bw`
processes.

The distribution name `dgx-spark-four-hca` and command `spark-four-hca` are
retained for compatibility. In those identifiers, “four HCA” refers to the
four logical RDMA devices selectable by perftest; it does not mean four
physical NICs.

The most interesting experiment is **diagonal logical-device selection**:

```text
QSFP cage 0 / PCIe domain 0000:  rocep1s0f0
QSFP cage 1 / PCIe domain 0002:  roceP2p1s0f1
```

or the opposite diagonal:

```text
QSFP cage 1 / PCIe domain 0000:  rocep1s0f1
QSFP cage 0 / PCIe domain 0002:  roceP2p1s0f0
```

Each diagonal uses both 200G cages and both host-side x4 links with two
processes. It has the same topology ceiling as selecting all four logical RoCE
devices, without adding two extra polling processes and queue pairs.

The headline result is the completed, controlled one-QP comparison. On the
reference pair, the two diagonal modes reached 210.64 and 210.69 Gbit/s versus
195.86 Gbit/s for the same-cage control: approximately **7.5% higher aggregate
payload bandwidth when topology was the changed variable**.

A separate QP sweep was intentionally stopped at 14 of 20 planned trials. Its
four QP2 observations produced a 222.88 Gbit/s median, 5.49% above the four
QP1 diagonal-A observations in that checkpoint. QP2 is therefore the suite's
provisional operational default, but it is not a completed sweep, a universal
recommendation, or part of the controlled 7.5% headline claim. See the
[standard profile](docs/standard-profile.md), [sanitized reference
results](docs/reference-results-2026-08-13.md), and [vendor reproduction
note](docs/vendor-reproduction-note.md) for the evidence boundary.

Both observations come from one pair. The included JSON is historical schema
v1 and predates embedded postflight acceptance; its 104-check pre/post status
is operator-recorded legacy evidence, and uncaptured environment fields remain
explicitly unknown.

## The architectural limit

```text
2 x 200 Gbit/s QSFP cages
            |
        ConnectX-7
        /         \
PCIe 5.0 x4     PCIe 5.0 x4
domain 0000     domain 0002
        \         /
             GB10
```

PCIe 5.0 runs at 32 GT/s with 128b/130b encoding:

```text
32 GT/s x 4 lanes x 128/130 x 2 links = 252.06 Gbit/s
```

That is a pre-transaction, pre-RoCE ceiling per direction. PCIe TLP, DLLP,
Ethernet, RoCE, memory-system, and software overhead make it unreachable as
application payload. A 400 Gbit/s one-way result is not possible through the
two host links.

NVIDIA documents the two cages, two x4 links, and four-interface mapping in
the [DGX Spark ConnectX-7 guide][spark-topology]. NVIDIA Sync currently
supports one cable per two-node link and says a second cable does not improve
the supported configuration. Treat this project as a manual experiment
outside the Sync topology contract, not as a replacement for NVIDIA's
supported setup.

## What the suite provides

- Strict TOML validation for all four canonical DGX Spark interfaces.
- Review-only four-interface netplan rendering; it never installs networking.
- Read-only live checks for addresses, 200G carrier, MTU, RoCE state, PCIe
  Gen5 x4 width, peer reachability, and error counters.
- Concurrent, time-bounded `ib_write_bw` orchestration over SSH.
- Optional per-process CPU pinning for exploratory work; distinct configured
  cores on both nodes are required for standard-profile qualification.
- Single-logical-device, same-cage, same-domain, both diagonal, and all-four
  test modes.
- Repeatable message-size, QP, TX-depth, and direction sweeps.
- Machine-readable JSON containing parsed bandwidth but not SSH targets,
  management addresses, MAC addresses, serial numbers, or raw GIDs.
- Public-safe Markdown summaries with medians, ranges, variation, and
  descriptive relative differences.

The benchmark is host-memory RDMA verbs traffic. It is not NCCL bandwidth,
GPU payload bandwidth, or a model-serving benchmark. DGX Spark does not
support GPUDirect RDMA; the suite intentionally does not pass perftest CUDA
buffer options. See NVIDIA's [DGX Spark porting guide][spark-gdr].

Detailed material:

- [Physical/network standard and provisional raw profile](docs/standard-profile.md)
- [Architecture and resource ceilings](docs/architecture.md)
- [Benchmark methodology](docs/methodology.md)
- [Sanitized reference results and claim boundary](docs/reference-results-2026-08-13.md)
- [Concise vendor/community reproduction note](docs/vendor-reproduction-note.md)
- [Offline-only PCIe experiments](docs/offline-pcie-experiments.md)
- [Optional solidSF IDE handoff and integration boundary](docs/solidsf-integration.md)
- [Historical schema-v1 evidence shape](schemas/result-v1.schema.json)
- [Historical schema-v2 accepted-result shape](schemas/result-v2.schema.json)
- [Current schema-v3 runtime/checkpoint shape](schemas/result-v3-runtime.schema.json)
- [Current schema-v3 measurement-accepted publication shape](schemas/result-v3.schema.json)

## Requirements

- Two DGX Spark systems connected cage 0 to cage 0 and cage 1 to cage 1.
- Two compatible 200G-or-better QSFP cables, fully seated before boot. Check
  NVIDIA's current supported-cable list before treating electrical capability
  as vendor support; current Sync guidance names Amphenol NJAAKK-N911 and
  Luxshare LMTQF022-SD-R assemblies.
- A separate management network, an OpenSSH client, and key-based SSH aliases
  for both nodes. The suite does not accept passwords or key paths.
- Python 3.11 through 3.14 on the controller.
- `python3`, `perftest`, `iproute2`, `iputils` (for `ping`), `ethtool`,
  `rdma-core`, `pciutils`, `procps`, `coreutils`, and `util-linux` on both
  Sparks. Distribution package names can differ.
- TCP ports 18515–18518 reachable between the corresponding QSFP fabric
  addresses for perftest control, plus bidirectional ICMP echo for preflight.
  If `base_port` changes, permit that four-port range instead.
- The supplied 240W power adapters and a quiescent test window.

Do not benchmark a production pair while CPU, memory, or RDMA-heavy jobs are
running. A clean fabric test can still compete for unified memory bandwidth,
CPU time, and PCIe resources.

Each logical path needs its own non-overlapping RFC 1918 `/30`. Before adding
addresses, compare all four networks against management routes, site/VPN
routes, container bridges, Kubernetes networks, and the peer's existing
routes. A subnet collision can send traffic to the wrong interface even when
the TOML is internally valid. Do not add a gateway or default route to a QSFP
fabric path.

## Install from the public source repository

The package has no third-party Python runtime dependencies. Clone the public
repository and inspect it without installing:

```bash
git clone https://github.com/solidsf-inc/dgx-spark-dual-qsfp.git
cd dgx-spark-dual-qsfp
./spark-four-hca --help
```

For an isolated source installation (`setuptools>=77` is the build backend):

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
spark-four-hca --help
```

The project is not currently published to a Python package index. Install only
from a source revision or release artifact you have reviewed.

## Quick start: review before mutation

Copy and edit the example. Use SSH config aliases rather than passwords:

```bash
cp examples/pair.toml private-pair.toml
$EDITOR private-pair.toml
./spark-four-hca validate --config private-pair.toml
./spark-four-hca topology --config private-pair.toml
```

The example uses four non-overlapping `/30` networks. Every physical cage has
one interface in PCIe domain `0000` and one in domain `0002`; all four need
addresses for the complete experiment. Replace the example subnets if any of
them overlap a route on either host.

Render one netplan per node:

```bash
./spark-four-hca render-netplan \
  --config private-pair.toml --node head --output head.yaml
./spark-four-hca render-netplan \
  --config private-pair.toml --node worker --output worker.yaml
```

The suite stops there deliberately. Review the current NetworkManager and
netplan state on each node before installing either file. Do not create a
second authority for interfaces already owned by persistent NetworkManager
profiles. If netplan is appropriate for the host, use `netplan generate` and
interactive `netplan try` before a permanent apply. Never manage the node over
the QSFP address being changed.

## Validate all four paths

```bash
./spark-four-hca check --config private-pair.toml
```

The check is read-only. It fails closed if an interface is missing, not at
200G, has the wrong address/MTU, lacks active RoCE, has fallen below PCIe Gen5
x4, or cannot ping its peer. Nonzero historical physical counters are warnings
and remain visible for operator judgment.

The `benchmark` command takes fresh preflight and postflight snapshots. In a
current schema-v3 result, `trials_complete` says whether traffic completed,
while `measurement_accepted` additionally requires healthy preflight and
postflight checks, complete allowlisted physical link-error-counter coverage
across all four configured paths, and no increase or reset. The separate
`qualification` block says whether that accepted measurement also used the
exact standard profile and met its 215 Gbit/s median floor.

## Run the provisional QP2 performance profile

Use the versioned standard profile so every qualification parameter is fixed
together. Ordinary benchmark defaults are deliberately exploratory and do not
constitute a qualification run:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --profile standard \
  --output local-results/standard-profile.json
```

The profile is two concurrent paths (`p0-d0` and `p1-d2`), 64 KiB messages,
two QPs per process, TX depth 128, `worker-to-head` direction, 30-second
trials, five repeats, five-second cooldown, and deterministic interleaving.
Its QP2 choice comes from only four recorded trials in an incomplete sweep.
See [the standard profile](docs/standard-profile.md) before reproducing it;
do not carry this setting into NCCL or an application without a separate
controlled validation.

## Run the topology matrix

The first experiment changes topology only: 64 KiB messages, one QP, TX depth
128, 30 seconds, five repeats. It omits fixed GID indices and RDMA-CM so each
host can select its locally correct IPv4 RoCEv2 GID.

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode single-p0-d0 \
  --mode same-cage-p0 \
  --mode same-domain-d0 \
  --mode diagonal-a \
  --mode diagonal-b \
  --mode all-four \
  --qps 1 \
  --duration 30 \
  --repeats 5 \
  --output local-results/topology.json
```

The command refuses to start if another `ib_write_bw` process exists. Every
remote process has both a perftest duration and an outer hard timeout. The
suite never issues broad `pkill` or `killall` commands. Modes are interleaved
across repeats by default, which reduces thermal and time-order bias; use
`--schedule grouped` only when grouping is intentional.

Expected interpretation:

| Mode | Resources exercised | Architectural cap |
| --- | --- | ---: |
| Single logical RoCE device | One cage, one x4 domain | 126.03 Gbit/s |
| Same cage | One cage, both x4 domains | 200 Gbit/s |
| Same domain | Both cages, one x4 domain | 126.03 Gbit/s |
| Diagonal | Both cages, both x4 domains | 252.06 Gbit/s before overhead |
| All four | Both cages, both x4 domains | 252.06 Gbit/s before overhead |

The diagonal hypothesis is supported when its median is at least 98% of the
all-four median and the two diagonals agree within 5%. If all-four wins by more
than 5%, the extra logical functions are doing useful work beyond the simple
resource map.

## Summarize a result for review

First validate the complete result and recompute every cross-field relationship:

```bash
./spark-four-hca validate-result --input local-results/topology.json
```

Then render a concise Markdown table from a completed result or in-progress
checkpoint that has enough trials to summarize:

```bash
./spark-four-hca summarize \
  --input local-results/topology.json \
  --output local-results/topology.md
```

Before rendering, the summary command semantically validates the whole result
and recomputes aggregate rows from path observations. Its renderer emits only
completion/acceptance status, qualification, source version/digest, the raw
two-domain ceiling, and validated aggregate rows. It never copies `metadata`,
per-trial path details, pair labels, SSH targets, or fabric addresses into the
Markdown. Each row shows its median, range, population standard deviation, and
relative difference versus the lowest median in that file. This is descriptive
arithmetic, not an assertion that an uncontrolled difference is causal.

## Tune one axis at a time

After the topology run, keep the winning mode fixed. Do not launch a Cartesian
product and then guess which variable mattered.

Message size:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml --mode diagonal-a \
  --qps 1 \
  --message-size 65536 --message-size 1048576 \
  --duration 30 --repeats 5 \
  --output local-results/message-size.json
```

Queue pairs:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml --mode diagonal-a \
  --qps 1 --qps 2 --qps 4 --qps 8 \
  --duration 30 --repeats 5 \
  --output local-results/qps.json
```

TX depth:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml --mode diagonal-a \
  --qps 1 \
  --tx-depth 64 --tx-depth 128 --tx-depth 256 \
  --duration 30 --repeats 5 \
  --output local-results/tx-depth.json
```

More QPs are not automatically better on a direct cable; they are commonly
used for routing entropy in larger fabrics and can increase latency or reduce
bandwidth. Topology and control-study commands explicitly pass `--qps 1` so
the configuration's operational default cannot silently change their design.
In the incomplete reference checkpoint, two QPs per diagonal process was the
simplest observed plateau point: four and eight QPs differed by less than
0.22% while consuming more queue pairs. QP2 has four observations; QP4 and
QP8 have three each. Treat the default as a provisional reproduction target,
not a completed optimization. Reproduce it directly with:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml --profile standard \
  --output local-results/diagonal-q2.json
```

Test the reverse direction separately:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml --mode diagonal-a --qps 2 \
  --direction head-to-worker \
  --duration 30 --repeats 5 \
  --output local-results/reverse.json
```

Bidirectional perftest is intentionally absent in the initial release. Adding
opposite directions together can produce a much larger number because PCIe
and Ethernet are full-duplex, but it does not raise the per-direction ceiling
and is easy to misrepresent as a one-way speedup.

## Offline PCIe experiments

PCIe relaxed ordering was observed enabled on the reference pair, but that
state is not universal and is not encoded in the legacy public result JSON.
Check it on the live system instead of assuming it. The suite requires both
roots to negotiate Gen5 x4 but does not change relaxed ordering. Two additional
Grace/ConnectX tuning candidates exist:

- 10-bit PCIe tags, when supported by both endpoint and root port.
- A larger PCIe Max Read Request Size (MRRS), subject to platform support.

Neither is changed by this suite. Enabling 10-bit tags requires unloading the
InfiniBand/RoCE driver; both settings are volatile low-level changes that can
drop every QSFP path. They belong in a quiescent maintenance A/B with a known
rollback and console or independent management access. See
[`docs/offline-pcie-experiments.md`](docs/offline-pcie-experiments.md) and
NVIDIA's [Grace performance guide][grace-io].

## Result hygiene

Normal benchmark JSON omits SSH targets and fabric IPs. Optional
`--raw-log-dir` output contains raw perftest GIDs and addresses and is ignored
by Git; inspect and redact it before sharing. Do not publish check reports,
private TOML files, hostnames, MAC addresses, cable serials, management IPs,
or unrelated system inventory.

Write ordinary runs under the ignored `local-results/` directory or another
operator-controlled path. The tracked `results/` directory is a curated,
checksummed evidence set; do not write directly into it or replace one of its
files. Benchmark output refuses to overwrite an existing file unless the
operator explicitly passes `--force`.

Published numbers should always state:

- Host-memory `ib_write_bw`, not NCCL or GPU bandwidth.
- Unidirectional or bidirectional.
- The exact logical RoCE device/path set, message size, QP count, TX depth,
  duration, and repeats.
- Median and range, not a single peak.
- DGX OS/kernel, mlx5 driver, ConnectX firmware, netdev MTU, and active RDMA
  MTU.

The included 2026-08-13 evidence is historical schema v1. In v1, `complete`
records completion of planned trials and does not establish current
postflight integrity. Schema v2 was an unreleased intermediate contract with
overloaded `complete`/`accepted` fields. New benchmark output is schema v3:
require `trials_complete: true` and `measurement_accepted: true` for an
accepted measurement, then independently inspect `qualification.qualified`
before claiming that the 215 Gbit/s standard passed. The [runtime v3
schema](schemas/result-v3-runtime.schema.json) covers checkpoints and rejected
runs; the stricter [publication v3 schema](schemas/result-v3.schema.json)
covers measurement-accepted output. The summarizer reads v1, v2, and v3 while
preserving those distinctions.

## Optional: continue in the solidSF IDE

The benchmark suite is useful on its own. If you also want to explore the
broader engineering workspace, sign in at the [solidSF
workspace](https://app.solidsf.com/) or read about the platform at
[solidsf.com](https://solidsf.com/). Eligible signed-in accounts can use the
workspace's **Open IDE** control to see the currently approved IDE download
and activation options. Availability depends on account and platform state;
this package does not distribute or activate the IDE.

This repository has no solidSF importer, deep link, account binding,
automatic upload, or telemetry. It does not contact either site. Opening the
IDE is a separate, optional operator action; benchmark files remain local
unless the operator deliberately moves them. See the [solidSF integration
boundary](docs/solidsf-integration.md).

## Development

The package has no runtime Python dependencies:

```bash
make check
```

The entire distribution—source, documentation, tests, metadata, and included
result data—is Apache-2.0 licensed unless an individual file says otherwise.
See [LICENSE](LICENSE), [NOTICE](NOTICE), [CONTRIBUTING.md](CONTRIBUTING.md),
[REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md), and
[RELEASING.md](RELEASING.md). NVIDIA, DGX, ConnectX, CUDA, and NCCL are
trademarks of NVIDIA Corporation. Their use here is descriptive only.

## Primary references

- [NVIDIA DGX Spark ConnectX-7 topology and interface mapping][spark-topology]
- [NVIDIA one-cable raw RDMA benchmark: 189.85 Gbit/s][nvidia-benchmark]
- [NVIDIA Sync supported topology guidance][sync]
- [NVIDIA Grace I/O performance tuning][grace-io]
- [linux-rdma/perftest upstream][perftest]

[spark-topology]: https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
[spark-gdr]: https://docs.nvidia.com/dgx/dgx-spark-porting-guide/porting/cuda.html#gpudirect-rdma
[nvidia-benchmark]: https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md#step-3--run-bandwidth-test
[sync]: https://docs.nvidia.com/sync/latest/cluster-assistant.html
[grace-io]: https://docs.nvidia.com/dccpu/grace-perf-tuning-guide/optimizing-io.html
[perftest]: https://github.com/linux-rdma/perftest
