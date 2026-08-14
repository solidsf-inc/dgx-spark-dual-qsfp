## Summary

Describe the narrow problem and outcome.

## Change class

- [ ] Documentation or metadata only
- [ ] Read-only validation
- [ ] Benchmark load or orchestration
- [ ] Host-configuration rendering
- [ ] Result data or interpretation

## Claim boundary

- [ ] I preserve “one ConnectX-7, four logical RoCE devices.”
- [ ] I keep the controlled QP1 approximately 7.5% result separate from the
      four-trial, incomplete QP2 checkpoint.
- [ ] I distinguish concurrent host-memory `ib_write_bw` aggregate bandwidth
      from NCCL, GPU, line-rate, bidirectional, and single-flow metrics.
- [ ] New schema-v3 measurement claims require `trials_complete: true`,
      `measurement_accepted: true`, and passed postflight counter deltas.
- [ ] I report `qualification.qualified` separately from measurement
      acceptance.

## Verification

List exact local commands and results, including `make check`. For documentation,
include Markdown, local-link, TOML/YAML, package-build, and sensitive-string
checks that were available. This repository intentionally has no hosted workflow,
so local verification evidence is required.

## Safety, privacy, and licensing

- [ ] No credentials, private hostnames/addresses, raw GIDs, MAC addresses,
      serial numbers, pair TOML, SSH configuration, or raw logs are included.
- [ ] Remote behavior and load are accurately described.
- [ ] Added material is compatible with Apache-2.0 and attribution is present.
- [ ] Vendor/community observations are labeled and primary sources support
      hardware or tuning claims.
- [ ] I followed the code of conduct and used private security reporting where
      appropriate.
