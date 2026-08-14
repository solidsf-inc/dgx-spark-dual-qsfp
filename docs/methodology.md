# Benchmark methodology

This document defines the reference procedure for measuring the dual-QSFP
topology. The goal is a reproducible one-way host-memory RDMA payload result,
not a peak selected from an unconstrained sweep.

These historical topology controls use one QP per process. The package's
provisional operational default is separately defined as diagonal A with two
QPs per process in [`standard-profile.md`](standard-profile.md). QP2 has only
four observations from an incomplete sweep. Every control command below
passes `--qps 1` explicitly so changing an operational default cannot change
the experiment.

## Measurement scope

The suite runs [`ib_write_bw` from linux-rdma/perftest][perftest] over RoCE.
Each selected HCA gets a separate process, point-to-point IPv4 subnet, and
control port. When CPU cores are configured, each process is pinned to its
assigned core. Distinct configured cores on both nodes are mandatory for the
standard qualification profile; an exploratory run may omit affinity but must
label it as an uncontrolled variable. Processes run concurrently, and their
reported average payload rates are summed only after every path completes
successfully.

The resulting aggregate is:

- unidirectional, decimal Gbit/s;
- host-memory RDMA WRITE payload bandwidth;
- the sum of simultaneous independent path measurements;
- not Ethernet line rate, NCCL bandwidth, GPU bandwidth, latency, or a single
  striped application flow.

DGX Spark does not support GPUDirect RDMA. NVIDIA recommends a host-memory
fallback for verbs applications, so the suite does not pass CUDA-buffer or
other GPUDirect options. See the [DGX Spark CUDA porting guide][nvidia-gdr].

## Reference environment

The sanitized schema-v1 reference data was collected on one two-node DGX Spark
pair. The benchmark JSON itself records kernel, driver, firmware, perftest,
benchmark parameters, and per-trial measurements. The following physical and
operating details are operator-recorded legacy notes rather than fields in the
public JSON:

- two direct 200 Gbit/s QSFP cables, port 0 to port 0 and port 1 to port 1;
- four non-overlapping point-to-point IPv4 subnets, one per logical path;
- Ethernet MTU 9000 on all four paths;
- both ConnectX-7 host links negotiated at PCIe Gen5 x4;
- 200,000 Mbit/s carrier and active RoCE on every path;
- kernel `6.17.0-1029-nvidia`, driver `mlx5_core`, and ConnectX firmware
  `28.45.4028` on both nodes;
- perftest `6.20` on both nodes;
- CPU cores 5 through 8 used in path order as benchmark affinity targets.

The role labels `head` and `worker` in published JSON are generic. Private
hostnames, management addresses, fabric addresses, usernames, MAC addresses,
serial numbers, and raw GIDs are intentionally absent.

The public legacy evidence does not record the exact DGX OS image, BIOS, power
mode, clocks/governor, thermal history, cable vendor/model, Python version,
tool commit, or complete PCIe capability state. Relaxed ordering was observed
enabled on the pair but is not encoded in the result. These omissions limit
cross-system attribution and must not be silently filled with present-day
assumptions.

## Preflight and postflight

Before a run, use the read-only check:

```bash
./spark-four-hca check --config private-pair.toml
```

It verifies all four canonical interface mappings, configured addresses, MTU,
carrier, 200 Gbit/s link rate, RoCE state, active RDMA MTU, PCIe generation and
width, peer reachability, and the allowlisted error counters. The operator recorded
104 passes, no warnings, and no failures before and after the historical
topology study. The schema-v1 JSON does not embed those reports or counter
deltas, so the statement is legacy operator-recorded evidence rather than a
machine-verifiable field in the published files.

Current benchmark output is schema v3. The command captures preflight and
postflight snapshots itself, compares the allowlisted counters, and fails
closed on incomplete coverage, any positive delta, any reset, or an incomplete
postflight. `trials_complete` records execution completion;
`measurement_accepted` records the independent measurement-integrity decision.
Inspect `validation.link_error_counter_delta` and require
`measurement_accepted: true` before publishing a measured result. Standard-
profile performance qualification is a further decision recorded separately
in `qualification.qualified`.

Run on an otherwise idle pair. CPU, unified-memory, PCIe, or RDMA workloads can
change both throughput and variance. Record the power mode, clocks/governor,
thermal state, cable models, DGX OS release, and error-counter deltas in a
private lab notebook even when those identifiers are omitted from public JSON.

## Per-path invocation

For each selected path, the suite starts the server before the client and runs
the equivalent of:

```text
timeout --signal=TERM --kill-after=2s <duration+20>s \
  taskset -c <core> \
  ib_write_bw -d <hca> -i 1 -p <unique-port> -F --report_gbits \
  -s <message-size> -q <qps> -t <tx-depth> -D <duration> [server-address]
```

The reference topology matrix fixed all non-topology variables:

| Variable | Reference value |
| --- | ---: |
| Message size | 65,536 bytes |
| Queue pairs per process | 1 |
| TX depth | 128 |
| Timed duration | 30 seconds |
| Repeats per condition | 5 |
| Direction | `worker-to-head` |

The suite does not force a GID index and does not enable RDMA-CM. Each host can
therefore select its locally correct IPv4 RoCEv2 GID. It also refuses to begin
a trial if an existing `ib_write_bw` process is present, wraps each remote
process in a hard timeout, and never uses a broad `pkill` or `killall` cleanup.

## Experimental design

### Topology controls

The complete control matrix includes:

1. `single-p0-d0`: one cage and one PCIe domain.
2. `same-cage-p0`: one cage through both PCIe domains.
3. `same-domain-d0`: both cages through one PCIe domain.
4. `diagonal-a` and `diagonal-b`: both cages and both domains with two paths.
5. `all-four`: both cages and both domains with four paths.

The same-domain mode is a critical negative control: if adding the second cage
does not materially improve it, the shared x4 root is behaving as the resource
model predicts. The same-cage mode checks the 200 Gbit/s physical-port limit.

### Scheduling

The first full matrix was grouped by condition, with five repeats and a
one-second cooldown. A second run used the then-current interleaved schedule
and a five-second cooldown:

```text
repeat 1: same-cage, diagonal-a, diagonal-b, all-four
repeat 2: same-cage, diagonal-a, diagonal-b, all-four
...
```

Interleaving reduces the chance that time order, temperature, or a transient
background condition is mistaken for a topology effect. Agreement between the
grouped and interleaved medians is stronger evidence than either schedule
alone.

The current suite strengthens this design by deterministically rotating mode
order between repeats. It records both `schedule_seed` and the exact
`schedule_plan` in every new result, so independent runs can reproduce the
order while each mode occupies different ordinal positions. The historical
reference JSON predates that hardening and therefore documents its fixed order
explicitly rather than retroactively rewriting the evidence.

### Statistics

For each condition the JSON records every path result and the simultaneous
aggregate. The summary reports median, mean, minimum, maximum, and population
standard deviation across repeats. Use the median as the primary estimate and
the range and standard deviation as stability indicators. Do not publish only
the best trial.

The output is atomically checkpointed after each trial with
`trials_complete: false` and `measurement_accepted: false`. The [schema-v3
runtime schema](../schemas/result-v3-runtime.schema.json) covers these
checkpoints and rejected or interrupted runs. Only `trials_complete: true`,
matching `completed_trials` and `planned_trials`, a passed postflight counter
delta, and `measurement_accepted: true` constitute an accepted measurement;
the stricter [schema-v3 publication
schema](../schemas/result-v3.schema.json) encodes that shape.

`qualification.qualified` is not an alias for measurement acceptance. It is
true only when the accepted measurement also satisfies the versioned standard
profile and its performance floor. Historical schema-v1 files predate these
fields; there, `complete: true` only records completion of planned trials. The
[v1 schema](../schemas/result-v1.schema.json) describes the included evidence,
and the [v2 schema](../schemas/result-v2.schema.json) remains available for
historical v2 consumers.

## Reproduce the topology study

Start from the generic example and use key-based SSH aliases. Keep the private
configuration outside version control.

```bash
cp examples/pair.toml private-pair.toml
$EDITOR private-pair.toml
./spark-four-hca validate --config private-pair.toml
./spark-four-hca topology --config private-pair.toml
./spark-four-hca check --config private-pair.toml
```

Run the complete grouped control matrix:

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
  --cooldown 1 \
  --schedule grouped \
  --output local-results/topology-grouped.json
```

Then confirm the performance-relevant modes with an interleaved schedule:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode same-cage-p0 \
  --mode diagonal-a \
  --mode diagonal-b \
  --mode all-four \
  --qps 1 \
  --duration 30 \
  --repeats 5 \
  --cooldown 5 \
  --schedule interleaved \
  --output local-results/topology-interleaved.json
```

Repeat the winning condition in the opposite direction before generalizing the
result to the pair. Tune message size, QP count, and TX depth one axis at a
time, with a fresh fixed-baseline condition in each study.

## Publication checklist

Before sharing a result:

- run `spark-four-hca validate-result --input <result.json>` and feature-detect
  `schema_version`; for v3 require `trials_complete: true` and
  `measurement_accepted: true`, inspect `qualification.qualified` separately,
  and for legacy v1 explicitly label the missing current acceptance fields;
- verify trial counts and inspect every trial, not only `summaries`;
- state direction, path set, message size, QPs, TX depth, duration, repeats,
  cooldown, and schedule;
- report median, range, and dispersion;
- include kernel, driver, firmware, perftest, Ethernet MTU, and active RDMA MTU;
- distinguish a concurrent-flow aggregate from a single-flow result;
- require a passed embedded postflight and zero new allowlisted link-error
  counters across all four configured paths for schema v3;
- scan the JSON for hostnames, IP addresses, usernames, MACs, serials, GIDs,
  passwords, and organization-specific names;
- keep raw perftest logs private unless they are independently redacted.

[perftest]: https://github.com/linux-rdma/perftest
[nvidia-gdr]: https://docs.nvidia.com/dgx/dgx-spark-porting-guide/porting/cuda.html#gpudirect-rdma
