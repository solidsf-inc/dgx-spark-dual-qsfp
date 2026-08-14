# DGX Spark dual-QSFP architecture: one ConnectX-7, four logical devices

Each DGX Spark in this study has one ConnectX-7 adapter. The suite models its
four logical RoCE paths, which share two physical QSFP cages and two host-side
PCIe links. The distinction matters: four Linux interfaces do **not** mean four
physical NICs or four independent 200 Gbit/s paths into the GB10 SoC. The
package and CLI retain “four HCA” in their identifiers only for compatibility.

NVIDIA documents two external QSFP ports, each capped at 200 Gbit/s, and two
independent PCIe Gen5 x4 links between the ConnectX-7 NIC and the SoC. Each
physical port appears through both PCIe domains, producing four Ethernet
interfaces and four corresponding RoCE devices when both cables are connected.
See the [DGX Spark ConnectX-7 networking guide][nvidia-topology].

## Canonical path map

The suite uses short path names so that a mode states both resources it uses:
`p0-d2` means physical QSFP port 0 through PCIe domain 2.

| Suite path | Physical QSFP port | PCIe domain | Ethernet interface | RoCE device |
| --- | ---: | ---: | --- | --- |
| `p0-d0` | 0 (left) | 0 | `enp1s0f0np0` | `rocep1s0f0` |
| `p0-d2` | 0 (left) | 2 | `enP2p1s0f0np0` | `roceP2p1s0f0` |
| `p1-d0` | 1 (right) | 0 | `enp1s0f1np1` | `rocep1s0f1` |
| `p1-d2` | 1 (right) | 2 | `enP2p1s0f1np1` | `roceP2p1s0f1` |

The left/right labels follow NVIDIA's rear-panel view. Always confirm the live
mapping with `ibdev2netdev`; do not infer it from cable color or host-specific
network configuration.

## One-way ceilings

PCIe Gen5 transfers at 32 GT/s per lane with 128b/130b encoding. The raw data
rate of one x4 link is therefore:

```text
32 GT/s x 4 lanes x 128/130 = 126.030769 Gbit/s
```

Two such links provide a combined raw ceiling of:

```text
2 x 126.030769 = 252.061538 Gbit/s
```

These are per-direction, pre-transaction ceilings. PCIe TLP and DLLP overhead,
Ethernet and RoCE framing, memory traffic, and software execution all reduce
the application-payload rate. The two QSFP ports offer 400 Gbit/s of aggregate
line rate, but the two x4 host links cap one-way traffic into the SoC well below
400 Gbit/s.

For a path set, the useful architectural cap is the lower of its unique-cage
line rate and its unique-domain PCIe rate:

| Mode family | Cages used | PCIe domains used | Pre-overhead cap | Limiting resource |
| --- | ---: | ---: | ---: | --- |
| Single logical RoCE device | 1 | 1 | 126.03 Gbit/s | one x4 link |
| Same cage | 1 | 2 | 200.00 Gbit/s | one QSFP port |
| Same domain | 2 | 1 | 126.03 Gbit/s | one x4 link |
| Diagonal | 2 | 2 | 252.06 Gbit/s | two x4 links |
| All four | 2 | 2 | 252.06 Gbit/s | two x4 links |

## Why the diagonal is interesting

A diagonal selects one function from each cage and one function from each PCIe
domain:

```text
diagonal-a: p0-d0 + p1-d2
diagonal-b: p1-d0 + p0-d2
```

Both diagonals exercise the same physical resources as the all-four mode, but
with two concurrent `ib_write_bw` processes instead of four. This is not link
bonding and it does not turn one application flow into a 200+ Gbit/s flow. The
suite starts one independent RDMA flow on each selected `/30` path and sums the
simultaneous payload rates.

Whether two diagonal flows can match or beat four flows is an empirical
question. It depends on firmware, driver, perftest settings, CPU placement, and
system load. The suite tests both diagonals because agreement between them is a
useful symmetry check.

## Supported topology boundary

NVIDIA Sync's Cluster Assistant currently specifies one cable per supported
link and states that connecting two devices with two cables will not improve
performance. The dual-cable diagonal experiment in this repository is a
manual topology outside that supported Cluster Assistant contract. It does not
replace or contradict the vendor's supported-configuration guidance; it asks a
different question about concurrent host-memory RoCE traffic over explicitly
configured paths. See the [NVIDIA Sync topology guidance][nvidia-sync].

This is an independent community resource, not an NVIDIA-supported topology
or performance recommendation. Re-check the current vendor documentation
before deployment because support boundaries and software behavior can change.

[nvidia-topology]: https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
[nvidia-sync]: https://docs.nvidia.com/sync/latest/cluster-assistant.html
