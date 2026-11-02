---
id: 20261102-rhe001
title: Expanded Release History Scan Capacity
status: completed
created: 2026-11-02
updated: 2026-11-02
branch: codex/daily-skill-friction-2026-11-02-codex-toolbox-release-history-budget
pr:
supersedes: []
superseded_by:
---

# Expanded Release History Scan Capacity

## Summary

Raise the consumer-owned cumulative expanded archive scan ceiling from 4 GiB
to 8 GiB. A measured complete-history pass covered 207 complete releases and
207 unique asset pairs, with 357,075,686 compressed bytes and 4,313,047,040
bytes across two gzip passes. API asset digests, checksums, and unchanged release
identities were verified; these cost measurements do not prove archive manifest
or Git-tree equivalence. The measured run took 294.317 seconds and
peaked at 4,499,568 temporary-disk bytes. Its metadata SHA-256 was
`1e74db6276c7ea70c9a6c9396405f39aae3e2ab2734035cd6ddb83b3541452da`.

At the 256-release count ceiling, adding 49 pairs at the measured peak of
52,101,120 expanded bytes per pair yields 6,866,001,920 bytes. A further 25%
capacity allowance is 8,582,502,400 bytes, just below the 8 GiB ceiling of
8,589,934,592 bytes. This is a sizing estimate from observed data, not a
guarantee about future archive sizes.

## Preserved Boundaries

- The 256 MiB per streaming pass, 64 MiB per compressed archive, 1 GiB
  aggregate compressed archive, and 256 complete-release limits are unchanged.
- The two-pass verification algorithm, complete-history scan, asset-pair
  cache, manifest checks and Git-tree hash checks are unchanged.
- The max-release-count regression retains exact-cap acceptance and rejection
  of one byte beyond the expanded budget. A profile regression models the
  observed 207-pair aggregate plus 49 peak-sized pairs; it rejects the former
  4 GiB ceiling and accepts the new 8 GiB ceiling.

## Evidence

- Measurement values above establish the observed history profile and the
  bounded capacity estimate. They do not predict the size of future archives.
- Python 3.13.0 passed the release-baseline, manifest-transition, package-builder,
  and public-release workflow modules. The first three modules ran 221 tests;
  release-baseline plus public-release workflow ran 105 tests after final cleanup.
  These selections overlap and are not a full-suite claim.
- The stock generated-source verifier accepted all eleven unchanged managed
  files. Ruff 0.13.2, exact-base manifest validation, and journal validation passed.
