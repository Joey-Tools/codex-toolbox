---
id: 20260801-psm001
title: Personal Sync Generated Mirror Bridge
status: active
created: 2026-08-01
updated: 2026-08-01
branch: codex/personal-sync-generated-mirror-bridge
pr:
supersedes: []
superseded_by:
---

# Personal Sync Generated Mirror Bridge

## Summary

- Toolbox consumer guidance and CI/release discovery own the one-time wiring
  needed before canonical personal-sync generation becomes authoritative.

## Current State

- Root guidance distinguishes consumer-owned release aggregation from
  receipt-bound generated sources owned by `Joey-Tools/codex-personal-sync`.
- CI and release gates discover every test module, including generated
  retention and scheduler/doctor suites when they arrive.
- No unpublished canonical engine, schema, test, lock, or receipt bytes are
  copied by this wiring phase.

## Next Steps

- After canonical personal-sync lands, generate the exact toolbox mapping from
  that committed source SHA and append the generated mirror/receipt commit.
- Validate source lock freshness, idempotent generation, receipt digests,
  dual-runtime tests, admission, and the frozen whole-range review.

## Evidence

- Branch `codex/personal-sync-generated-mirror-bridge`
- Canonical workstream: `Joey-Tools/codex-personal-sync` PR #5
