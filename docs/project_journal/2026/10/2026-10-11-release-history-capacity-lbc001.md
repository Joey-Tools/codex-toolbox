---
id: 20261011-lbc001
title: Finite Complete Release History Capacity
status: completed
created: 2026-10-11
updated: 2026-10-11
branch: wip/release-history-capacity-20261011
pr:
supersedes: []
superseded_by:
---

# Finite Complete Release History Capacity

## Summary

Raise the consumer-owned compressed history ceiling from 256 MiB to 1 GiB.
Private PR #223 exposed a deterministic capacity failure: all 188 published
archive assets totalled 271,793,148 bytes, exceeding 268,435,456 bytes before
any archive download. Recent individual archives are approximately 4.3 MiB;
512 MiB would run out before the existing 256-release count ceiling at that
size. A finite 1 GiB ceiling supplies headroom without trimming history.

## Preserved Boundaries

- Every complete historical release still requires metadata, immutability,
  checksum, archive-manifest and exact Git-tree verification.
- The independent 256-release count, 64 MiB per archive, checksum ceilings,
  4 GiB expanded streaming-scan ceiling and Git blob budgets are unchanged.
- Regression coverage accepts synthetic metadata at the observed aggregate
  and exactly 1 GiB, but rejects one extra compressed byte before downloads.
  Metadata-only fixtures are not proof that real release archives passed.

## Downstream Contract

Private promotion must consume the actual merged Toolbox source and a verified
immutable public release through the stock generator. Do not edit private
generated validator copies or rerun the old deterministic failure as recovery.
Own-PR remote review and CI, downstream promotion, merge authorization and
release verification remain separate delivery gates.

## Evidence

- Native CPython 3.14.2 passed all 258 related release-baseline, manifest,
  public-workflow and package-builder tests. Native CPython 3.9.6 passed the
  declared compatibility selection plus new regressions (41 tests).
- The stock verifier checked and snapshotted all 11 unchanged generated source
  files. Changed-source Ruff, exact-base manifest and journal validation passed.
- Private release job: https://github.com/Joey-Tools/codex-private-workflows/actions/runs/38161038344/job/114533810442
- Complete paginated release metadata independently reproduced the same total.
