# Historical evidence provenance

The three JSON files in this directory were captured on 2026-08-13 by an
unreleased prototype that reported `suite_version: 0.1.0`. Their exact source
commit is unavailable: the prototype lived in an uncommitted review tree.
Accordingly, the current package does **not** claim that it generated these
legacy files, and their version string must not be used to reconstruct source.

Their stable identities are the SHA-256 digests in `SHA256SUMS`. The files are
retained byte-for-byte as historical schema-v1 evidence. Corrections or new
measurements must be added as new artifacts with new digests rather than
rewriting these files.

Current schema-v3 results add an implementation fingerprint derived from the
installed package source. That fingerprint provides producer provenance for a
new run without exposing a checkout path or relying on a Git commit.
