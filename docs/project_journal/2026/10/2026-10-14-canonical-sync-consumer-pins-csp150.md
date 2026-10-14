---
id: 20261014-csp150
title: Canonical Sync Consumer Pin Promotion
status: completed
created: 2026-10-14
updated: 2026-10-14
branch: codex/toolbox-pr50-provenance-pin-20261014
pr:
supersedes: []
superseded_by:
---

# Canonical Sync Consumer Pin Promotion

## Summary

- Promote the exact generated Toolbox mirror from canonical personal-sync
  commit `2a81c7add1dfdc54866d5219c0b3109c9d709f31` together with its
  consumer-owned verification pins.
- Keep the fixed automation branch restricted to generated paths. The manual
  promotion includes the unchanged generated commit from Toolbox PR #50 and
  its separately owned consumer pin updates in one landing range.

## Verification Contract

- Receipt SHA-256 is
  `a1bebf66d90ed353ffc8d9a9ea8f7cede1f1610b5c013bdb272a7b5631c36787`.
- The exact eleven-file mapping, canonical source identity, receipt schema,
  byte and mode checks, private snapshot installation, and tamper rejection
  are unchanged. The receipt never selects or updates its own trusted pins.
- A repository-level regression exercises production default pins against the
  committed generated tree, rather than overriding the digest with a fixture.
- Previous canonical commits, including `110f2df`, remain rejected even when a
  fixture supplies a matching receipt digest.

## Evidence

- PR #50 test job `115324942244` and compatibility job `115324942666` failed
  before executing tests: the new receipt digest did not match the old
  consumer pin. The new repository regression reproduced the same mismatch
  before the pin update.
- The canonical stock `check --mirror toolbox` verified all eleven generated
  files against the exact source checkout at `2a81c7a`.
- CPython 3.14.2: all 37 source-lock and consumer workflow contracts passed.
- Native CPython 3.9.6: the complete declared compatibility selection, including
  all source-lock regressions, passed 40 tests.
- Selected Ruff, sync-manifest validation against exact base `c498cf5`, and
  whitespace checks passed. CI/review lifecycle remains in the PR rather than
  in this target-branch journal.
