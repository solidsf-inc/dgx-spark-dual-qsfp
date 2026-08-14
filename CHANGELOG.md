# Changelog

All notable changes to this project will be documented here. The project uses
Semantic Versioning.

## Unreleased

## 0.2.0 - 2026-08-13

### Added

- Strict four-path DGX Spark topology validation and review-only netplan
  rendering.
- Time-bounded, concurrent `ib_write_bw` orchestration with deterministic
  scheduling, checkpointing, and public-safe summaries.
- Schema-v3 preflight/postflight measurement acceptance with fail-closed
  allowlisted link-error-counter delta checks across all four configured paths.
- Sanitized schema-v1 topology evidence and an intentionally incomplete QP
  checkpoint from 2026-08-13.
- Reproduction, architecture, security, contribution, citation, maintenance,
  and release documentation.
- An optional, non-integrated pointer to the solidSF IDE.
- A schema-v3 result contract that separates completed traffic, accepted
  measurement integrity, and passing the standard performance profile.
- Explicit standard-profile qualification at five trials and a 215 Gbit/s
  median floor, while retaining the 220–224 Gbit/s operating target.
- Deterministic implementation fingerprints for new result provenance.
- GitHub-native issue and pull-request templates with no automatic publication
  workflow or external action dependency.
- Dependency-free semantic result validation that recomputes schedules,
  trial arithmetic, aggregate summaries, counter deltas, and qualification.

### Changed

- Initial public version is 0.2.0 after the result-contract and qualification
  changes.
- Canonical path names and all eleven mode definitions are now immutable
  configuration contracts rather than user-redefinable labels.
- Local runs now target the ignored `local-results/` directory; the tracked
  `results/` directory remains curated, checksummed evidence.
- Existing benchmark evidence is protected from accidental overwrite unless
  the operator explicitly supplies `--force`.
- Live validation now checks route selection, address exclusivity, PCI
  backing, usable CPU affinity, and distinct endpoint identity without
  exposing machine identifiers.

### Evidence boundary

- The completed controlled QP1 result is approximately 7.5% higher aggregate
  payload bandwidth for diagonal placement than the same-cage control on one
  reference pair.
- QP2 is a provisional default based on four observations from an incomplete
  14-of-20 trial sweep. It is not a completed optimization study.
- Historical schema v1 `complete` records trial completion. Schema v2 added an
  overloaded `accepted` field. Current schema v3 records `trials_complete`,
  `measurement_accepted`, and profile qualification separately.
