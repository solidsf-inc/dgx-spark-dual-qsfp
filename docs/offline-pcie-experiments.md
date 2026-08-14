# Offline PCIe experiments

These experiments are deliberately not automated. They can drop all four RoCE
paths and should be attempted only on an idle pair with independent management
access, a saved PCIe configuration snapshot, and a reboot-based rollback.

## Baseline first

Complete the topology matrix before changing PCIe controls. Capture, for all
four functions on both nodes:

```bash
sudo lspci -vv -s 0000:01:00.0
sudo lspci -vv -s 0000:01:00.1
sudo lspci -vv -s 0002:01:00.0
sudo lspci -vv -s 0002:01:00.1
```

Confirm both roots are 32 GT/s x4 and record `DevCap`, `DevCtl`, `DevCap2`, and
`DevCtl2`. Also save `mstconfig` output, driver/firmware versions, interface
addresses, and pre-test error counters.

PCIe relaxed ordering was observed enabled on the reference pair. That is one
pair-specific observation, not a DGX Spark invariant, and it is not encoded in
the included legacy result JSON. Record the live state on every endpoint and
root instead of assuming it. This suite neither enables nor disables relaxed
ordering.

## 10-bit tags

NVIDIA's Grace guide recommends 10-bit tags for capable high-bandwidth devices
because the larger tag space permits more outstanding PCIe transactions. Both
the endpoint and its root path must advertise support. The guide requires the
InfiniBand driver to be unloaded before changing the setting.

Do not copy an offset or `setpci` write from another machine. Extended
capability layouts and control-word preservation matter. Derive the exact
operation from the live capability structure and the current NVIDIA guide,
then have a second operator review it. A reboot is the clean rollback for a
volatile test.

Run the exact winning diagonal/all-four benchmark before and after, holding
message size, QPs, TX depth, CPU affinity, direction, and duration constant.
Five repeats per condition is the minimum useful comparison.

## Max Read Request Size

The same guide discusses larger MRRS values for throughput-oriented PCIe
devices. The DGX Spark endpoint and root constraints must be respected; a
larger request size can expose platform or device issues and is not universally
beneficial.

Treat MRRS as a separate experiment after restoring the 10-bit-tag baseline.
Never change both variables in one condition. Record AER and mlx5 counters
before and after every run, and stop on any corrected or uncorrected PCIe
errors, link retrain, RoCE error, timeout, or throughput regression.

## Acceptance and rollback

Accept an offline setting only if:

1. Both directions improve consistently across five or more repeats.
2. Median payload bandwidth improves by more than normal run-to-run variation.
3. No AER, CRC, symbol, FEC-uncorrectable, discard, timeout, or link-down
   counter appears.
4. The result survives a cold reboot when the setting is intended to persist.
5. A supported workload remains correct after the low-level fabric test.

Otherwise reboot into the vendor default and report the negative result. A
negative A/B is valuable evidence and should not be hidden.

Reference: [NVIDIA Grace Performance Tuning Guide — Optimizing I/O](https://docs.nvidia.com/dccpu/grace-perf-tuning-guide/optimizing-io.html).
