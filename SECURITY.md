# Security policy

This tool invokes fixed, validated commands through operator-provided SSH
aliases. It does not accept passwords, private keys, or sudo credentials and
does not modify remote configuration during checks or benchmarks. Benchmarks
do create temporary perftest processes and substantial CPU, memory, PCIe, and
network load. Netplan output is rendered locally for human review and is never
installed by the suite.

The SSH client still honors the operator's local SSH configuration, including
any jump hosts or proxy commands. Review aliases before use. Run only against
systems you are authorized to test, over a separate management path, and in a
quiescent window.

Report security issues through [GitHub private vulnerability
reporting](https://github.com/solidsf-inc/dgx-spark-dual-qsfp/security/advisories/new)
or privately to security@solidsf.com. Do not open a public issue containing
credentials, hostnames, management addresses, raw GIDs, MAC addresses, serial
numbers, or unredacted logs.

Include the package version, affected command, local versus remote impact, a
minimal sanitized reproduction, and whether remote processes remain. Do not
attach pair TOML, raw logs, SSH configuration, or check reports without first
agreeing on a secure transfer method. The maintainers will acknowledge a
report when it is received; response and remediation timelines depend on
severity and reproducibility.

Before publishing a result, inspect it and verify that it contains only the
intended generic node labels and parsed benchmark metadata. Never publish raw
logs merely because Git ignores their default directory.

Current schema-v3 benchmark output fails closed when postflight or complete
coverage for the allowlisted counters across all four configured paths is
unavailable, when a counter increases or resets, or when validation is
interrupted. `trials_complete`, `measurement_accepted`, and
`qualification.qualified` are distinct decisions and must not be conflated.
Historical schema-v1 evidence predates these embedded acceptance fields and
must be labeled accordingly.

The package has no updater, solidSF importer, deep link, automatic upload, or
telemetry. It does not contact `solidsf.com` or `app.solidsf.com`; links in the
documentation are optional operator navigation only.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| Earlier prototypes | No |
