# Dual-QSFP physical standard and provisional QP2 profile

This project uses one provisional operational default for raw dual-QSFP
measurements: `diagonal-a` with two queue pairs per `ib_write_bw` process. The
physical/network layout is the stable part of the profile. QP2 is provisional:
it is based on four observations from an intentionally incomplete 14-of-20
trial sweep. The profile is layered so neither result is mistaken for an NCCL
or application-performance guarantee.

The completed controlled finding used QP1. In the interleaved confirmation,
diagonal A and B were approximately 7.5% above the same-cage control while the
other benchmark variables remained fixed. That is the headline result; QP2 is
a separate exploratory reproduction target.

## 1. Physical and network standard

The two DGX Spark systems are connected directly with two cables:

- physical QSFP cage 0 to cage 0;
- physical QSFP cage 1 to cage 1;
- cable assemblies supported by the operator's current NVIDIA guidance (the
  current Sync list names Amphenol NJAAKK-N911 and Luxshare LMTQF022-SD-R);
- 200 Gbit/s carrier on all four exposed logical netdev paths;
- one non-overlapping RFC 1918 `/30` subnet per logical path;
- Ethernet MTU 9000 on all four paths;
- active RDMA MTU 4096 on all four paths;
- no gateway or default route on the point-to-point fabric; and
- a separate management network with key-based SSH access.

Check all four `/30`s against management, VPN, container, orchestration, and
site routes on both hosts. Internal non-overlap is necessary but not
sufficient. Allow ICMP echo between peers and TCP 18515–18518 (or the four
ports beginning at the configured `base_port`) over the direct fabric.

All four canonical interface mappings remain configured and checked even
though the performance default selects two of them. Run the read-only
preflight before using the profile:

```bash
./spark-four-hca check --config private-pair.toml
```

The check must confirm both 200 Gbit/s cages, both PCIe Gen5 x4 domains, active
RoCE, correct peer addresses, jumbo MTU, reachability, and full selected error
counter coverage. The renderer is review-only; this standard does not
authorize the suite to install or apply network configuration.

MTU 9000/RDMA MTU 4096 is this project's manual profile, not a universal DGX
Spark requirement. NVIDIA's published one-cable example currently omits a
jumbo Ethernet MTU and its sample perftest output negotiates a smaller RDMA
MTU. Match every endpoint in a path and label this difference when comparing
results.

## 2. Provisional raw `ib_write_bw` performance profile

The operational mode uses one path from each physical cage and one path from
each PCIe domain:

| Setting | Profile value |
| --- | --- |
| Mode | `diagonal-a` |
| Paths | `p0-d0` and `p1-d2` |
| RoCE devices | `rocep1s0f0` and `roceP2p1s0f1` |
| Concurrent processes | 2 |
| Queue pairs | 2 per process |
| Message size | 65,536 bytes |
| TX depth | 128 |
| Direction | `worker-to-head` |
| CPU affinity | Distinct configured cores for both concurrent paths on both nodes |
| Trial duration | 30 seconds |
| Quick health-check repeats | 3 |
| Qualification/publication repeats | 5 |
| Qualification cooldown | 5 seconds |
| Operating target | 220–224 Gbit/s median |
| Provisional qualification floor | 215 Gbit/s median |

The example configuration sets `qps = 2`, but ordinary CLI defaults remain
exploratory. `--profile standard` fixes every qualification parameter together
and rejects conflicting overrides; it does not upgrade the incomplete QP
sweep into completed evidence. The qualification run is:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --profile standard \
  --output local-results/standard-profile.json
```

For an audit trail, the equivalent fully explicit selection is:

```bash
./spark-four-hca benchmark \
  --config private-pair.toml \
  --mode diagonal-a \
  --qps 2 \
  --message-size 65536 \
  --tx-depth 128 \
  --direction worker-to-head \
  --duration 30 \
  --repeats 5 \
  --cooldown 5 \
  --output local-results/standard-profile-explicit.json
```

This measures the sum of two simultaneous, independent, unidirectional
host-memory RDMA WRITE payload flows. It is not a single striped flow. The
222.88 Gbit/s reference median is based on four QP2 trials from an
intentionally stopped checkpoint; the QP1 topology controls are completed
five-repeat studies. That evidence boundary remains documented in
[`reference-results-2026-08-13.md`](reference-results-2026-08-13.md).

The current schema-v3 benchmark command performs preflight and postflight
checks and compares allowlisted physical link-error counters across all four
configured paths. `trials_complete` records only whether every planned trial
finished. `measurement_accepted` is true only when those trials are complete,
postflight passes, counter coverage is complete, and no counter increases or
resets. `qualification.qualified` then records the separate standard-profile
decision. Accepted output is gated by the strict
[`result-v3.schema.json`](../schemas/result-v3.schema.json) publication schema;
checkpoints and rejected runs use
[`result-v3-runtime.schema.json`](../schemas/result-v3-runtime.schema.json).
Historical schema-v1 files cannot establish either current state.

Measurement integrity and profile qualification are separate decisions. A
clean three-repeat run is useful as a health check but is not a publication
qualification. The standard qualification requires the exact profile above,
five complete trials, at least a 215 Gbit/s median, and
`measurement_accepted: true`. Report the run as qualified only when
`qualification.qualified: true`; an accepted measurement can still be
ineligible for this profile or below its floor. The operating target remains
220–224 Gbit/s; a lower-than-floor result is a regression to investigate, not
permission to lower the floor.

Topology and control studies are a separate profile. They must explicitly pass
`--qps 1` so topology is the independent variable and this operational default
cannot silently alter the experiment. Use the complete commands in
[`methodology.md`](methodology.md); the distinguishing flag is:

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
  --output local-results/topology-control.json
```

## 3. Not yet an NCCL or application standard

The physical/network layout and raw verbs profile do not establish application
settings. In particular, this project has not validated:

- NCCL interface-selection or multi-rail environment variables;
- Linux bonding, multipath routing, ECMP, or automatic flow striping;
- inference, training, collective, TCP, storage, or model-serving throughput;
- GPU-buffer RDMA bandwidth; DGX Spark does not support GPUDirect RDMA; or
- that one application flow can consume the two-path aggregate.

Do not copy `diagonal-a` or the QP count into an NCCL or application
configuration and call it validated. An application standard needs its own
A/B test with workload-level throughput and latency, correctness checks,
reverse-direction coverage where relevant, resource utilization, and a
rollback. Until then, report the result narrowly as raw host-memory
`ib_write_bw` aggregate bandwidth.
