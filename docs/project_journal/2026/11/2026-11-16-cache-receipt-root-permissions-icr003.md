---
id: 20261116-cache-receipt-root-permissions-icr003
title: Cache Receipt Release-Parent Permissions
status: completed
created: 2026-11-16
updated: 2026-11-16
branch: codex/cache-receipt-promotion
pr:
supersedes: []
superseded_by:
---

# Cache Receipt Release-Parent Permissions

## Scope

- Promote canonical `codex-personal-sync` merge `9f43c8e1c06f1fb765a6aa0665ac2734ba7d52ef` with the stock generator.
- Recover receipts written under an existing `0755` releases parent while preserving its recorded object identity and access policy. Quarantine payloads still require `0700`.
- Update the consumer-owned receipt digest and canonical-commit pins together with the generated snapshot; keep its eleven-path inventory unchanged.
- Reuse the existing interrupted-recovery regression and consumer integrity tests. No new runtime guards or test matrix are introduced.

## Verification

- Canonical PR: https://github.com/Joey-Tools/codex-personal-sync/pull/41.
- The generated receipt SHA-256 is `e136c266f31ac80d105da2d46496cccf4efd9bee982404b6139f395454d66767`.
- Native PR CI and release evidence are recorded in the delivery PR and its immutable release.
