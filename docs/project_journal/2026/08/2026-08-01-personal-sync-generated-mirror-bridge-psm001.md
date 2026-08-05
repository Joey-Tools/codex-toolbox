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
  are bound to one private receipt-consistent snapshot of the squash-landed
  canonical personal-sync release input per consumer job.

## Current State

- Root guidance distinguishes consumer-owned release aggregation from
  receipt-bound generated sources owned by `Joey-Tools/codex-personal-sync`.
- Every CI and release job creates a unique mode-`0700` workspace, locally
  clones without local-object or hard-link sharing and without checkout,
  checks out detached exact `GITHUB_SHA`, and exposes the path only after the
  verifier successfully installs its captured snapshot. All later compile,
  test, manifest-validation, package-build, package-verification, and publish
  consumers in that job use that same step-output working directory.
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
  before any generated-code consumer can proceed.
- Each receipt or managed file is captured by two equal bounded reads from one
  open regular-file descriptor. Device/inode identity, byte content and size,
  and mode/UID/GID access policy are protected; timestamps and hard-link count
  are intentionally excluded as benign metadata churn. Only after all seven
  captured objects validate does the verifier install their exact bytes and
  modes through no-follow directory descriptors and exclusive temporary files.
  The snapshot path is canonicalized and every ancestor binding is opened
  without symlink following and checked against other-UID replacement before
  the root descriptor is accepted.
- The private root's mode excludes other UIDs. The design does not guarantee
  protection against a malicious or cooperative same-UID process mutating the
  snapshot after successful installation; workflows serialize consumers
  behind success and start no concurrent consumer beforehand.

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
- The next fresh named-single review found that two parallel CI jobs were not
  independently receipt-gated and that timestamp/link-count churn was outside
  the verifier's protected properties. The final follow-up gates all three CI
  consumer jobs before generated-code execution and narrows stability checks
  to object identity, content, and mode/ownership access policy, with focused
  benign-churn and true-replacement regressions.
- Final follow-up gates passed 1,128-test Python 3.13 discovery with two
  expected skips, 24 focused verifier/consumer-contract tests on Python 3.13
  and 3.9, and 28 Python 3.9 compatibility tests. Production receipt
  verification, actionlint, Ruff lint/format, manifest validation, project
  journal validation, and diff checks also passed.
- A later fresh named-single review found that sequential live-tree
  revalidation still left a final pathname-replacement window before each
  consumer. The consumer-side follow-up replaces that claim with immutable
  in-memory capture plus one private verified snapshot per workflow job;
  structured workflow tests parse jobs and steps and require every later run
  step to use the identical snapshot working directory.
- Snapshot follow-up gates passed 35 focused verifier/consumer-contract tests,
  standalone production receipt verification, and focused `py_compile` on
  Python 3.13.0 and 3.9.6. An exact local clone/detached-checkout/same-root
  installation probe remained Git-clean; actionlint, Ruff lint/format,
  project-journal validation, and diff checks also passed.
