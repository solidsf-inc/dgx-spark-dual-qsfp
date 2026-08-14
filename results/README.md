# Results

Only sanitized parsed benchmark JSON belongs here. Raw perftest logs can expose
private IP addresses and GIDs and must remain outside version control.

A publishable result uses generic `pair_label` and node labels, and includes:

- topology modes and exact logical RoCE device mapping;
- unidirectional test direction;
- message size, QPs, TX depth, duration, and repeat count;
- per-path payload rate;
- aggregate median, minimum, maximum, and population standard deviation;
- kernel, mlx5 driver, and ConnectX firmware versions.

Do not add check reports, SSH aliases, management addresses, MAC addresses,
cable serials, usernames, or unrelated system inventory.

Create ordinary run output in the ignored `local-results/` directory, not in
this curated evidence directory. Then create an aggregate-only Markdown review
artifact without copying metadata or per-trial topology details:

```bash
./spark-four-hca summarize \
  --input local-results/run.json \
  --output local-results/run.md
```

Included evidence:

- `reference-topology-2026-08-13.json`: completed grouped control matrix.
- `reference-interleaved-2026-08-13.json`: completed interleaved confirmation.
- `checkpoint-qps-exploratory-2026-08-13.json`: intentionally stopped at
  14/20 trials; useful exploratory evidence, not a completed sweep.

All three included artifacts are historical schema v1. In v1,
`complete: true` records completion of planned trials and does not establish
the current postflight acceptance contract. The operator-recorded 104-check
pre/postflight evidence is not embedded in these files. Current schema-v3
results include validation and link-error-counter deltas. Require
`trials_complete: true` and `measurement_accepted: true` before calling the
measurement accepted, then inspect `qualification.qualified` separately before
calling it a standard-profile qualification.

Schemas:

- [`result-v1.schema.json`](../schemas/result-v1.schema.json) validates the
  historical evidence shape, including complete and incomplete v1 files.
- [`result-v2.schema.json`](../schemas/result-v2.schema.json) is deliberately
  the historical v2 accepted/publication shape.
- [`result-v3-runtime.schema.json`](../schemas/result-v3-runtime.schema.json)
  covers current checkpoints, interrupted runs, rejected measurements, and
  accepted measurements.
- [`result-v3.schema.json`](../schemas/result-v3.schema.json) is the stricter
  current publication shape for a completed, measurement-accepted result.
  Qualification remains an independent field inside that result.

Before relying on either stored summaries or status fields, run:

```bash
./spark-four-hca validate-result --input local-results/run.json
```

The semantic validator checks cross-field cardinality, schedule, arithmetic,
summary, counter-delta, acceptance, and qualification relationships that JSON
Schema alone cannot express.

The producer/source limitation for these unreleased legacy files is recorded
in [`PROVENANCE.md`](PROVENANCE.md). Their SHA-256 digests, not the historical
version string alone, are their stable identities.

The human-readable interpretation and claim boundary are in
`docs/reference-results-2026-08-13.md`.

Verify the evidence files before analysis with `sha256sum -c SHA256SUMS`
(or `shasum -a 256 -c SHA256SUMS` on macOS).
