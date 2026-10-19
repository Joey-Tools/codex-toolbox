---
id: 20261019-public-cache-release
title: Public Toolbox Installer Cache Recovery
status: completed
created: 2026-10-19
updated: 2026-10-19
branch: wip/installer-cache-release
pr:
supersedes: []
superseded_by:
---

# Public Toolbox Installer Cache Recovery

## Summary

- Promote the generated Toolbox mirror from canonical merge `bf62452bb8dab2612d3af9b8795b2c8d67c0ec37` and land the consumer-owned integrity pins with it.
- Bind `EXPECTED_RECEIPT_SHA256` and `EXPECTED_CANONICAL_COMMIT` to the generated snapshot; retain the stale-canonical rejection for `2a81c7add1dfdc54866d5219c0b3109c9d709f31` alongside the existing negative fixtures.

## Scope

- Generate and verify the receipt plus eleven managed files with the stock canonical mirror tool.
- Update only the Toolbox consumer verifier pins and the previous-canonical negative fixture for this source advancement.
- Keep source provenance tied to canonical merge `bf62452bb8dab2612d3af9b8795b2c8d67c0ec37`. The public release workflow binds package identity to the landed Toolbox commit.

## Validation

- Canonical merge: `bf62452bb8dab2612d3af9b8795b2c8d67c0ec37`.
- The stock canonical generator produced 11 managed files from that exact source; its source check reported all 11 synchronized.
- Python 3.13.0 and Python 3.9.6 each passed all 31 tests in `tests.test_generated_sync_source_lock`.
- Ruff 0.13.2 passed on the verifier and test with `--no-cache`.

## Operational Boundary

- Healthy schedulers consume the public release through normal installation. An already-blocked older engine may still require a verified-engine bootstrap.
