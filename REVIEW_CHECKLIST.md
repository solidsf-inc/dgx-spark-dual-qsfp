# Public release decisions

The initial source publication uses these explicit boundaries:

- Public host: `https://github.com/solidsf-inc/dgx-spark-dual-qsfp`.
- Initial source version: `0.2.0`; compatibility distribution and CLI names
  remain `dgx-spark-four-hca` and `spark-four-hca`.
- Python package-index publication and GitHub Release artifacts are deferred.
- GitHub private vulnerability reporting is the preferred security channel;
  `security@solidsf.com` is the fallback and conduct-reporting address.
- QP2 remains a provisional default based on four observations from an
  incomplete 14-of-20 trial sweep.
- Schema v3 keeps `trials_complete`, `measurement_accepted`, and
  `qualification.qualified` as separate decisions.
- Ordinary operator output stays under ignored `local-results/`; tracked
  `results/` remains curated, checksummed evidence only.
- The solidSF IDE pointer is optional and has no importer, deep link, account
  binding, upload, or telemetry integration.
- No screenshot or logo is included in the initial source publication.
- The repository states Apache-2.0 licensing, NOTICE attribution, result-data
  scope, trademark disclaimer, and the independent NVIDIA/vendor boundary.

Future tags, artifacts, package uploads, vendor submissions, and announcements
must follow [RELEASING.md](RELEASING.md) and receive their own approval.
