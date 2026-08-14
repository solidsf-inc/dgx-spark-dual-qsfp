# Contributing

Keep changes focused on reproducible DGX Spark QSFP/RoCE measurement. Every
behavior change needs a fixture-backed unit test and a clear statement of
whether it is read-only, benchmark load, or host mutation.

Use GitHub issues and pull requests for public, non-sensitive discussion. Do
not publish a fork containing private infrastructure or unreviewed evidence on
the project's behalf.

Do not submit credentials, private infrastructure details, vendor firmware,
proprietary application code, or results that cannot be distinguished from
NCCL/GPU/bidirectional metrics. Cite primary vendor documentation for hardware
or tuning claims and label inferred or community-observed behavior as such.

## Before proposing a change

- Read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and
  [SECURITY.md](SECURITY.md).
- Keep the “one physical ConnectX-7, four logical RoCE devices” distinction
  explicit. Compatibility package and CLI names do not establish four NICs.
- Preserve the completed controlled QP1 approximately 7.5% claim separately
  from the four-trial, incomplete QP2 checkpoint.
- Add fixture-backed tests for behavior changes and update schema/migration
  notes when output changes.
- Run `make check` and include the exact command and result in the review.
- Keep formatting and documentation dependency-light so a source checkout can
  be inspected offline.

## Results and reproductions

Submit only sanitized parsed JSON. Do not submit raw perftest logs, pair TOML,
SSH aliases, IP addresses, raw GIDs, MAC addresses, serial numbers, usernames,
or organization-specific labels. State the result schema version. For current
schema v3, do not call the measurement accepted unless `trials_complete: true`,
`measurement_accepted: true`, and the embedded postflight counter comparison
passed. Do not call it standard-profile qualified unless
`qualification.qualified: true`. Schema-v1 `complete` and schema-v2 `accepted`
are historical fields and must not be presented as current v3 qualification.

Run `spark-four-hca validate-result --input <result.json>` before proposing a
result. Store ordinary run and summary artifacts under ignored
`local-results/`; the tracked `results/` directory is curated evidence only.

Report every planned and completed trial, the median, range, dispersion,
direction, logical RoCE device/path selection, QPs, message size, TX depth,
duration, cooldown, schedule, software/firmware context, and known environment
gaps. Negative and incomplete results are useful when labeled honestly.

## Licensing

By contributing, you agree that your contribution is licensed under the
Apache License, Version 2.0, unless a reviewed file explicitly states another
license. Do not contribute material you do not have permission to license.
