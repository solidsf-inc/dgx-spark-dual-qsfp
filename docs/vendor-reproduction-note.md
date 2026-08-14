# Vendor and community reproduction note

This note isolates the result that is ready for technical review from a
separate exploratory observation. It is provided by an independent solidSF,
Inc. community project. The project is not an NVIDIA product, is not endorsed
or supported by NVIDIA, and does not redefine NVIDIA's supported DGX Spark
topologies.

## Narrow controlled finding

On one two-node DGX Spark pair connected with two direct QSFP cables, changing
two concurrent one-QP `ib_write_bw` processes from same-cage placement to a
diagonal placement increased the measured unidirectional host-memory RDMA
WRITE payload aggregate by approximately 7.5%. Message size, QP count, TX
depth, duration, direction, process count, and CPU placement were fixed.

| Interleaved QP1 mode | Median Gbit/s | Relative difference vs same cage |
| --- | ---: | ---: |
| Same cage `p0` | 195.86 | baseline |
| Diagonal A | 210.64 | +7.546% |
| Diagonal B | 210.69 | +7.572% |
| All four | 208.54 | +6.474% |

Each condition has five 30-second observations. The diagonal modes use one
logical RoCE path from each physical cage and one from each PCIe domain. The
measurement is the sum of two independent flows; it is not one striped flow,
NCCL or GPU bandwidth, Ethernet line rate, or a bidirectional total.

Evidence:

- [`reference-interleaved-2026-08-13.json`](../results/reference-interleaved-2026-08-13.json)
- [`reference-topology-2026-08-13.json`](../results/reference-topology-2026-08-13.json)
- [`SHA256SUMS`](../results/SHA256SUMS)

## Separate exploratory QP2 observation

The later QP sweep was intentionally stopped after 14 of 20 planned trials.
It contains four QP1 and four QP2 observations, plus three each for QP4 and
QP8. The QP2 median was 222.88 Gbit/s, 5.49% above the 211.275 Gbit/s QP1
median within that checkpoint. QP4 and QP8 were within 0.22% of QP2.

Because the sweep is incomplete and optionally stopped, QP2 is only a
provisional operational default and reproduction target. It is not part of the
controlled approximately 7.5% topology claim and is not a completed tuning
recommendation.

Evidence:

- [`checkpoint-qps-exploratory-2026-08-13.json`](../results/checkpoint-qps-exploratory-2026-08-13.json)
- [Full result interpretation](reference-results-2026-08-13.md)

## Environment provenance and gaps

The schema-v1 JSON directly records kernel `6.17.0-1029-nvidia`, `mlx5_core`,
ConnectX firmware `28.45.4028`, perftest `6.20`, benchmark parameters, and
every parsed trial. The operator separately recorded two direct 200 Gbit/s
cables, four point-to-point `/30` networks, Ethernet MTU 9000, active RDMA MTU
4096, PCIe Gen5 x4 on both domains, and 104 passing checks with no warnings or
failures before and after the topology study. Those check reports and counter
deltas are not embedded in the public v1 files.

The public evidence does not capture the exact DGX OS image, BIOS, power mode,
clocks/governor, thermal history, cable vendor/model, Python version, suite
commit, or complete PCIe capability state. Relaxed ordering was observed
enabled on the reference pair but is absent from the JSON and must not be
assumed elsewhere.

Schema v1 `complete: true` records completion of planned trials only. Current
schema-v3 output adds embedded validation and counter deltas. Require both
`trials_complete: true` and `measurement_accepted: true` for a current run to
satisfy the suite's zero-new-allowlisted-link-error measurement contract across
all four configured paths. Treat `qualification.qualified` as the separate
standard-profile performance decision.

## Current reproduction commands

First configure four non-overlapping `/30`s, verify that they do not collide
with management, VPN, container, orchestration, or site routes, and allow ICMP
plus TCP 18515–18518 between fabric peers. Then run the current hardened
interleaved QP1 confirmation:

```bash
./spark-four-hca check --config private-pair.toml
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode same-cage-p0 \
  --mode diagonal-a \
  --mode diagonal-b \
  --mode all-four \
  --qps 1 \
  --message-size 65536 \
  --tx-depth 128 \
  --direction worker-to-head \
  --duration 30 \
  --repeats 5 \
  --cooldown 5 \
  --schedule interleaved \
  --output local-results/reproduction-q1.json
```

The current suite deterministically rotates interleaved mode order; the legacy
confirmation used the fixed order documented in [the
methodology](methodology.md). This is intentional hardening, so a reproduction
should report its recorded `schedule_plan` rather than claim byte-for-byte
experimental ordering.

To complete, rather than merely repeat, the unfinished QP study:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode diagonal-a \
  --qps 1 --qps 2 --qps 4 --qps 8 \
  --message-size 65536 \
  --tx-depth 128 \
  --direction worker-to-head \
  --duration 30 \
  --repeats 5 \
  --cooldown 5 \
  --schedule interleaved \
  --output local-results/reproduction-qps.json
```

Do not pass `--skip-live-check` for a new publication run. Inspect every trial,
validate the result with
`spark-four-hca validate-result --input <result.json>`, require schema v3
`trials_complete: true` and `measurement_accepted: true`, and inspect
`qualification.qualified` separately. Repeat the selected condition in the
reverse direction, and publish median, range, and dispersion rather than the
best trial.

## Questions useful to vendor review

1. Is the documented four-logical-device/two-PCIe-domain mapping expected to
   remain stable across supported DGX OS, driver, and firmware combinations?
2. Does the one-cable NVIDIA Sync support boundary apply specifically to Sync
   and supported cluster workflows, or are there additional restrictions for
   manually addressed, direct, dual-cable host-memory RoCE experiments?
3. Are there vendor-preferred counters or diagnostics beyond the selected
   mlx5 physical counters for accepting a direct-link bandwidth run?
4. Which power, thermal, PCIe capability, and cable fields would make an
   independent reproduction most actionable to NVIDIA engineering?
5. Is there a supported method for confirming relaxed ordering, 10-bit tag,
   and MRRS state without implying that operators should change them?

The supported-topology boundary is described in NVIDIA's [DGX Spark
ConnectX-7 guide][nvidia-topology] and [NVIDIA Sync Cluster Assistant
guidance][nvidia-sync]. This project asks a narrower experimental question
about concurrent host-memory RoCE flows outside the Sync topology contract.

[nvidia-topology]: https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
[nvidia-sync]: https://docs.nvidia.com/sync/latest/cluster-assistant.html
