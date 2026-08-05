---
id: 20260801-psm001
title: Personal Sync Generated Mirror Bridge
status: completed
created: 2026-08-01
updated: 2026-08-05
branch: codex/personal-sync-generated-mirror-bridge
pr: 20
supersedes: []
superseded_by:
---

# Personal Sync Generated Mirror Bridge

## Summary

- Toolbox consumer guidance, CI/release discovery, and the generated mirror
  are bound to the squash-landed canonical personal-sync release input.

## Current State

- Root guidance distinguishes consumer-owned release aggregation from
  receipt-bound generated sources owned by `Joey-Tools/codex-personal-sync`.
- CI and release gates first verify the exact generated receipt and its six
  managed files, then discover every test module, including generated
  retention and scheduler/doctor suites.
- The six canonical files and `generated-sync-source-lock.json` bind landed
  canonical commit `14914ca17172f00a5759758a50cf7c0295e4a42f`, tree
  `e5d81cb98194cc56872b9a4cdea83aee88c0fd2a`.
- The generated receipt records mapping digest
  `3e26648dd65526e759089c5acf5a9f429f3df0f5adc8dbe94b3856954b801ece`,
  file-set digest
  `c280b934568b6bc8df0c993b91d3e2e051970a8395870bf0419fc475556af7ad`,
  and tree digest
  `b52444964c14b4703edd477790fedee46dd52d8698bcbd38cabfb936854e67df`.
- The consumer verifier pins the complete receipt SHA-256
  `04b8be42769d63872ba0643dcf593f3956a0ec88f42014aa39a00c24e13bdc07`
  outside the receipt itself, then checks the exact canonical identity,
  closed six-file mapping, recomputed digests, target modes, and target bytes
  before CI tests or either release package build can proceed.

## Next Steps

- Preserve the generated receipt as the source of truth for this toolbox tree.
- After squash landing, downstream private generation must bind the actual
  landed toolbox commit and prove its tree equals the reviewed PR-head tree.

## Evidence

- Branch `codex/personal-sync-generated-mirror-bridge`
- Toolbox PR #20
- Canonical workstream: `Joey-Tools/codex-personal-sync` PR #6
- Canonical landed commit `14914ca17172f00a5759758a50cf7c0295e4a42f`
- Initial generated-head BL delivery gates: Python 3.13 compile, 1,108-test
  discovery with two expected skips, and the nine-test Python 3.9
  compatibility lane.
- Fresh named-single review identified the missing consumer-side receipt gate;
  the superseding head adds an explicit verifier to CI and both release build
  paths before package construction.
- Superseding verifier gates on BL: production-tree verification, 20 focused
  verifier/consumer-contract tests, 1,124-test Python 3.13 discovery with two
  expected skips, and 25 Python 3.9 compatibility tests all passed.
