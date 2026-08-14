# Dual-QSFP reference topology results

Run date: 2026-08-13 (America/Los_Angeles; JSON timestamps are UTC)

These files are historical schema v1. In that schema, `complete: true` means
that planned trials finished; it establishes neither current schema-v3
`measurement_accepted` status nor `qualification.qualified` status. The
104-check pre/postflight statement below is an operator-recorded legacy note
because those reports and counter deltas are not embedded in the public v1
JSON.

The reference pair showed a repeatable benefit from selecting a diagonal pair
of HCAs: one logical path on each physical QSFP cage and one on each PCIe
domain. At one QP per process, the two diagonal modes sustained medians of
210.64 and 210.69 Gbit/s in the interleaved confirmation run. That is about
7.5% above the 195.86 Gbit/s same-cage control, while using half as many
processes as the all-four mode.

A later, intentionally stopped QP sweep provides four exploratory QP2
observations with a 222.88 Gbit/s median for diagonal A. That checkpoint is
incomplete (14 of 20 planned trials), so QP2 remains provisional, is reported
separately, and is not folded into the completed topology-study claim.

This is an original empirical result from this project on one pair and software
stack. It is **not** an NVIDIA performance claim or a supported-topology
recommendation.

## Test conditions

The measurement is unidirectional host-memory `ib_write_bw` payload bandwidth,
summed across independent concurrent paths. It is not a single striped flow,
GPU-direct traffic, NCCL bandwidth, or a bidirectional total.

| Parameter | Value |
| --- | --- |
| Direction | `worker-to-head` |
| Message size | 65,536 bytes |
| Queue pairs | 1 per process |
| TX depth | 128 |
| Trial duration | 30 seconds |
| Repeats | 5 per condition |
| Ethernet MTU | 9000 |
| Active RDMA MTU | 4096 bytes |
| Perftest | 6.20 |
| Kernel | `6.17.0-1029-nvidia` |
| Driver | `mlx5_core` |
| ConnectX firmware | `28.45.4028` |

The operator recorded that both nodes negotiated 200 Gbit/s on all four
logical netdev paths and PCIe Gen5 x4 on both domains, that the pair was
quiescent, and that read-only preflight and postflight checks each completed
with 104 passes, no warnings, and no failures. These facts were recorded
outside the sanitized result schema; the included v1 JSON cannot independently
verify them or prove a zero counter delta under the current acceptance rule.

Known directly from the v1 JSON are the benchmark parameters and trials plus
kernel, mlx5 driver, firmware, and perftest versions. Not captured are the
exact DGX OS image, BIOS, power mode, clocks/governor, thermal history, cable
vendor/model, Python version, tool commit, or complete PCIe capability state.
Relaxed ordering was observed enabled on the reference pair, but that state is
also absent from the JSON and must not be generalized.

## Complete grouped control matrix

The first run grouped the five repeats for each condition and used a one-second
cooldown. The `fraction of cap` column uses the resource-specific pre-overhead
cap: 126.03 Gbit/s for one PCIe domain, 200 Gbit/s for one cage, and 252.06
Gbit/s for both domains.

| Mode | Median Gbit/s | Min–max Gbit/s | Population SD | Fraction of cap |
| --- | ---: | ---: | ---: | ---: |
| Single `p0-d0` | 109.05 | 109.00–109.09 | 0.035 | 86.53% |
| Same domain `d0` | 110.43 | 110.39–110.44 | 0.017 | 87.62% |
| Same cage `p0` | 195.88 | 195.86–195.92 | 0.020 | 97.94% |
| Diagonal A | 210.60 | 210.49–210.64 | 0.054 | 83.55% |
| Diagonal B | 210.55 | 210.35–210.59 | 0.101 | 83.53% |
| All four | 208.64 | 207.43–215.29 | 3.245 | 82.77% |

Machine-readable evidence:
[`reference-topology-2026-08-13.json`](../results/reference-topology-2026-08-13.json)

The controls match the resource model. Adding the second cage while remaining
on one PCIe domain changed the median only from 109.05 to 110.43 Gbit/s. Using
both PCIe domains through one cage reached 195.88 Gbit/s, nearly the cage's
200 Gbit/s line-rate cap. Only a path set spanning both cages and both domains
could move beyond that physical-port ceiling.

## Interleaved confirmation

The second run interleaved the four performance-relevant modes across repeats
and increased cooldown to five seconds.

| Mode | Median Gbit/s | Min–max Gbit/s | Population SD | Change vs same cage |
| --- | ---: | ---: | ---: | ---: |
| Same cage `p0` | 195.86 | 195.86–195.88 | 0.008 | baseline |
| Diagonal A | 210.64 | 210.54–211.39 | 0.321 | +7.546% |
| Diagonal B | 210.69 | 210.59–210.88 | 0.112 | +7.572% |
| All four | 208.54 | 205.56–211.68 | 2.435 | +6.474% |

Machine-readable evidence:
[`reference-interleaved-2026-08-13.json`](../results/reference-interleaved-2026-08-13.json)

The two diagonals agreed within 0.05 Gbit/s at the median. Their medians were
also within 0.14 Gbit/s of the corresponding grouped results. The diagonal
modes were about 1.0% above the all-four median and substantially less variable
in both schedules. On this system and setting, the extra two polling processes
in the all-four mode did not improve the median aggregate.

## Exploratory QP checkpoint — intentionally incomplete

After the completed topology runs, the operator started an interleaved
diagonal-A sweep of 1, 2, 4, and 8 QPs per process. It held direction, message
size, TX depth, duration, cooldown, and path placement constant. The operator
intentionally stopped after trial 14 because the result was sufficient to pick
the simplest point on the observed throughput plateau without spending the
remaining benchmark time.

The checkpoint correctly records `complete: false`, `completed_trials: 14`,
and `planned_trials: 20`. It contains three full interleaved rounds plus the
1-QP and 2-QP trials from round four. The unequal sample counts and optional
stopping mean these values must remain exploratory rather than being presented
as a completed five-repeat sweep.

| QPs per process | Repeats present | Median Gbit/s | Min–max Gbit/s | Population SD |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 211.275 | 210.98–211.79 | 0.314086 |
| 2 | 4 | 222.880 | 222.79–222.90 | 0.042647 |
| 4 | 3 | 223.370 | 223.36–223.38 | 0.008165 |
| 8 | 3 | 223.220 | 223.21–223.25 | 0.016997 |

Machine-readable exploratory checkpoint:
[`checkpoint-qps-exploratory-2026-08-13.json`](../results/checkpoint-qps-exploratory-2026-08-13.json)

QP2 is the provisional simple plateau point from this checkpoint. Its four
observations have a 222.88 Gbit/s median, 5.49% above the checkpoint's four
QP1 observations, 13.79% above the completed QP1 same-cage median of 195.86
Gbit/s, and 88.42% of the 252.06 Gbit/s two-domain raw ceiling. The 4-QP and
8-QP medians are within 0.22% of the QP2 median while requiring more queue
pairs, and each has only three observations. None of these QP settings was
completed to the planned five repeats, so the result is a provisional
reproduction target rather than a completed optimization recommendation.

The stopped study was launched with this command:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode diagonal-a \
  --qps 1 --qps 2 --qps 4 --qps 8 \
  --duration 30 \
  --repeats 5 \
  --cooldown 5 \
  --schedule interleaved \
  --skip-live-check \
  --output local-results/checkpoint-qps-exploratory-2026-08-13.json
```

`--skip-live-check` was used only because the full read-only check had just
passed on the reference pair. Independent reproductions should omit that flag
and allow the benchmark command to run its live check. A future five-repeat
completion would be new evidence; this document does not imply that it was
run.

## Interpretation and claim boundary

The evidence supports this narrow statement:

> On the tested dual-cable DGX Spark pair, two concurrent host-memory
> `ib_write_bw` processes placed diagonally across both QSFP cages and both PCIe
> domains sustained about 210.6 Gbit/s at one QP per process, roughly 7.5% more
> than the analogous two-process same-cage control.

It does not establish that:

- every DGX Spark pair or software release will reproduce the gain;
- a single TCP, RoCE, NCCL, inference, or training flow reaches this aggregate;
- four logical HCAs provide 800 Gbit/s or two cages deliver 400 Gbit/s into the
  SoC in one direction;
- the result is GPUDirect RDMA bandwidth;
- the second cable improves every workload or NVIDIA-supported cluster setup;
- message size, TX depth, reverse direction, or low-level PCIe tuning is already
  optimal;
- the stopped QP checkpoint is a completed optimization sweep.

NVIDIA documents the four-interface/two-domain topology in the
[DGX Spark ConnectX-7 guide][nvidia-topology]. NVIDIA Sync separately directs
users to use one cable per supported link and states that a second cable does
not improve performance in its supported topology. This project uses a manual
four-subnet configuration and concurrent per-path processes outside that
Cluster Assistant contract; the two statements should not be conflated. See
the [NVIDIA Sync Cluster Assistant guidance][nvidia-sync].

For context, NVIDIA's public one-cable RoCE example runs two concurrent
one-QP `ib_write_bw` processes through the two functions of one cage and reports
an aggregate 189.85 Gbit/s. The reference pair here reached 195.86 Gbit/s in
the analogous same-cage control. Those values are useful context, not a direct
product comparison: the software versions, MTU, test duration, and surrounding
system state differ. See the [NVIDIA DGX Spark performance guide][nvidia-bw].

## Reproduction expectations

A credible independent reproduction should:

1. Run the complete same-domain and same-cage controls, not only a diagonal.
2. Keep message size, QPs, TX depth, duration, affinity, and direction fixed.
3. Use at least five repeats and publish median, range, and dispersion.
4. Interleave the confirmation schedule to reduce thermal and time-order bias.
5. Test both diagonals and report asymmetry rather than choosing the better one.
6. Confirm all four links before and after, including PCIe width and error
   counters.
7. Repeat the selected condition in the opposite direction.
8. Validate current schema-v3 output with `spark-four-hca validate-result` and
   report measurement acceptance separately from standard-profile
   qualification.
9. Publish sanitized per-trial JSON and retain raw logs privately for audit.

See [`methodology.md`](methodology.md) for the exact commands and result-hygiene
checklist, and [`architecture.md`](architecture.md) for the resource model.

[nvidia-topology]: https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
[nvidia-sync]: https://docs.nvidia.com/sync/latest/cluster-assistant.html
[nvidia-bw]: https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md#step-3--run-bandwidth-test
