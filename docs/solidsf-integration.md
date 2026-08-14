# Optional solidSF IDE handoff

The DGX Spark benchmark suite is standalone. There is currently no importer,
deep link, account binding, automatic upload, or telemetry connection between
this package and solidSF. Running the suite does not contact solidSF.

Operators who separately want to see the broader platform can sign in to the
[solidSF workspace](https://app.solidsf.com/) or visit
[solidsf.com](https://solidsf.com/). The workspace source includes an **Open
IDE** control that presents approved download and activation options to
eligible signed-in accounts. Availability depends on account and deployed
platform state; this package does not distribute, activate, or update the IDE.
That handoff is an optional manual action, not a step in benchmark execution
or result acceptance.

## Current boundary

- The CLI reads a local TOML configuration and reaches only operator-provided
  SSH aliases.
- Parsed JSON and Markdown summaries remain local unless the operator
  deliberately moves them.
- Private pair TOML files and raw perftest logs are not suitable for upload or
  publication without independent review and redaction.
- This repository contains no solidSF platform client, authentication flow,
  private platform code, or hidden network callback.
- No retired registration URL, installer script, custom URI scheme, or
  unverified release-asset URL is embedded here. The authenticated workspace
  remains the authority for IDE availability.

## Future integration contract for review

If a result importer is proposed later, it should be a separate reviewed
change and should:

1. require explicit user file selection and confirmation before transfer;
2. accept sanitized parsed result JSON, never private pair TOML or raw logs;
3. feature-detect [schema v1](../schemas/result-v1.schema.json), historical
   [schema v2](../schemas/result-v2.schema.json), and current [schema
   v3](../schemas/result-v3.schema.json) rather than assuming one shape;
4. semantically validate imported JSON and prefer schema v3; require
   `trials_complete: true` and `measurement_accepted: true` before labeling a
   measurement accepted, and display `qualification.qualified` as a separate
   standard-profile decision;
5. label schema-v1 `complete` as legacy trial completion with current
   postflight acceptance and qualification unknown, and preserve the analogous
   legacy meaning of schema-v2 `accepted` without presenting it as v3
   qualification;
6. preserve hashes, evidence filenames, parameters, trial counts, and the
   host-memory/concurrent-flow metric boundary;
7. display the completed QP1 approximately 7.5% controlled finding separately
   from the four-trial, incomplete QP2 checkpoint; and
8. make any telemetry or account-linked behavior explicit, opt-in, documented,
   and independently reviewable.

No item in this future contract is implemented or promised by this release
repository. A normal web link to the IDE is not a result deep link.
