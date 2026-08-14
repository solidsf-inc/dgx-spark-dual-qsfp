# Release preparation

The canonical public source repository is
`https://github.com/solidsf-inc/dgx-spark-dual-qsfp`. Repository publication
does not authorize a Python package-index upload, GitHub Release artifact,
vendor submission, or announcement; each remains a separate maintainer action.

This repository intentionally has no hosted workflow. Reviewers run the
commands below from a clean checkout and record the source commit, interpreter
version, commands, and outputs. Do not add an external action, package,
toolchain, secret, or publishing workflow without a separate security review.

## Release checklist

1. Confirm the source tree contains only this standalone utility and no private
   solidSF application code, credentials, infrastructure names, or raw logs.
2. Review [README.md](README.md), [NOTICE](NOTICE),
   [SECURITY.md](SECURITY.md), [CITATION.cff](CITATION.cff), and every included
   result and checksum.
3. Confirm the headline is the completed controlled QP1 approximately 7.5%
   result and that every QP2 mention says four observations and incomplete
   sweep.
4. From a clean local checkout, run the test and static-check suite:

   ```bash
   make check
   ```

5. Locally exercise the supported Python 3.11–3.14 interpreters. Record
   unavailable interpreters rather than claiming they were tested.
6. Locally install the pinned release-time build tools, then use the
   deterministic release path. `dist/` must be absent or
   empty before the candidate build; preserve any earlier reviewed artifacts
   elsewhere rather than mixing generations.

   ```bash
   python -m pip install \
     build==1.3.0 \
     setuptools==80.9.0 \
     wheel==0.45.1
   make release
   make verify-release
   tar -tf dist/dgx-spark-four-hca-*-repo.tar.gz
   tar -tf dist/dgx_spark_four_hca-*.tar.gz
   unzip -l dist/*.whl
   ```

   These are release-time development tools, not runtime dependencies. The
   release tooling builds a wheel, canonical source distribution, standalone
   repository archive, and checksums; `verify-release` repeats and compares the
   build and performs clean-install CLI checks.

   Raw `python -m build` output is not a publishable project artifact because
   the source-distribution metadata still needs the canonical normalization in
   `scripts/release.py`. Do not run `twine` or upload to a package index until
   package-index publication is explicitly approved.
7. In addition to the automated clean-install checks, inspect the installed
   CLI surface:

   ```bash
   spark-four-hca --help
   spark-four-hca theory
   spark-four-hca validate --config examples/pair.toml
   ```

8. Verify `LICENSE` and `NOTICE` are present in the wheel and both source
   archives, and that all top-level governance documents, templates, docs,
   examples, tests, and sanitized evidence intended for source review are
   present.
9. Verify the complete evidence inventory and its checksums through the same
   platform-independent release tool used by the build:

   ```bash
   python3 scripts/release.py verify-results
   ```
10. Run Markdown, local-link, TOML, YAML, sensitive-string, and package-metadata
    checks available in the review environment.
11. Obtain explicit signoff for technical claims, security/privacy, licensing,
    naming/trademarks, the solidSF IDE pointer, version, tag, and any artifact
    destinations.

A successful build is not authorization to upload packages, create a GitHub
Release, announce results, or send material to a vendor.

## Version source

Package metadata reads the version dynamically from
`spark_four_hca.__version__`. Do not add a second machine-readable version
literal to package metadata. A human review checklist may name the proposed
candidate. The version change, changelog promotion, tag, and artifact names
must agree in the reviewed release change.

## Evidence corrections

Do not rewrite published measurements in place. If a claim or sanitized result
needs correction, retain the original checksum where practical, add a clearly
named corrected artifact, explain the change in the changelog and result
documentation, and obtain the same review required for a release.
