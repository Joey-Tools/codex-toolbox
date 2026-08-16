---
id: 20260801-psm001
title: Personal Sync Generated Mirror Bridge
status: completed
created: 2026-08-01
updated: 2026-08-16
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
  canonical commit `36a6f06222dec0ac22560c70d1199bc0e2862d2a`, tree
  `6a463ee1362c4887d95ce58ce128c1f4aa138cef`.
- The generated receipt records mapping digest
  `3e26648dd65526e759089c5acf5a9f429f3df0f5adc8dbe94b3856954b801ece`,
  file-set digest
  `c280b934568b6bc8df0c993b91d3e2e051970a8395870bf0419fc475556af7ad`,
  and tree digest
  `3b1d1771a4c0ee97c2a34a9f948f8665779d76445cca47aa883480394f1b7b74`.
- The consumer verifier pins the complete receipt SHA-256
  `356f79a1008d269284c4908b28b1d72966b2050d17abfd788393be83ccaed292`
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
- Downstream private generation must bind the actual landed toolbox commit
  and prove its tree equals the reviewed PR-head tree.

## Evidence

- Branch `codex/personal-sync-generated-mirror-bridge`
- Toolbox PR #20
- Canonical workstream: `Joey-Tools/codex-personal-sync` PR #8
- Canonical reviewed head `8f3e8aed813fd1e15c59916ff1c5a7f6f4315781`
  and squash-landed commit `e57140e16a68db24dbdd883de665283538234730`
  have the identical tree `13470ade1303992d81d02dc606ad66da7b6dd3a7`.
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
- Canonical Personal Sync PR #11 reviewed exact head
  `e8d13b419666f928b1d60adf6b249983b6cfc4e4` and squash-landed commit
  `b4e74d7f35226801483a63ebe605b1298d60dc8e` with identical tree
  `d7313b8dce755f58d13726dccfe60d1fb4cfee6c`. That follow-up adds the
  consumer-visible retained-recovery failure matrix and the corrected marker
  parent-`fsync` after-effect regression; production runtime semantics are
  unchanged.
- Stock generation from the exact landed commit updated the engine, engine
  tests, scheduler/doctor tests, and receipt. The mapping digest remains
  `3e26648dd65526e759089c5acf5a9f429f3df0f5adc8dbe94b3856954b801ece`
  and the file-set digest remains
  `c280b934568b6bc8df0c993b91d3e2e051970a8395870bf0419fc475556af7ad`;
  the consumer-owned verifier independently pins the new canonical commit and
  complete receipt SHA-256 before generated code can run.
- Canonical Personal Sync PR #13 reviewed exact head
  `5f0a90e3413f4f7f4798f41fdd3957249e2ed6ce` and squash-landed commit
  `0392f733e5ba79b6ffed62f46f2be2dd1536a8db`. The generated mirror carries
  the hardened Aqua scheduler profile and terminal whole-release identity
  revalidation used by the private role-aware sync controller.
- Canonical Personal Sync PR #14 reviewed exact head
  `b4aa42e8ed1f36e0e96eb980acd4ae81fe35e707` and squash-landed commit
  `36a6f06222dec0ac22560c70d1199bc0e2862d2a` with identical tree
  `6a463ee1362c4887d95ce58ce128c1f4aa138cef`. The change adds Darwin UID,
  POSIX write-authority, and extended-ACL admission; bounded retained-FD
  content revalidation; directory closure; and install FD-budget protection.
- The canonical `Sync toolbox mirror` run `31937481557` stopped at its initial
  credential gate because `CODEX_TOOLBOX_SYNC_TOKEN` was absent. It performed
  no target checkout, branch publication, or pull-request mutation. The mirror
  was therefore generated locally from the exact clean landed commit into
  owner-private, non-hardlinked source and consumer clones.
- Stock generation changed only the engine, engine tests, and receipt. The
  generator's immediate `check` passed; the mapping and file-set digests stayed
  `3e26648dd65526e759089c5acf5a9f429f3df0f5adc8dbe94b3856954b801ece`
  and `c280b934568b6bc8df0c993b91d3e2e051970a8395870bf0419fc475556af7ad`,
  while the generated tree digest became
  `3b1d1771a4c0ee97c2a34a9f948f8665779d76445cca47aa883480394f1b7b74`.
- The consumer verifier now pins the landed canonical commit and receipt
  SHA-256 `356f79a1008d269284c4908b28b1d72966b2050d17abfd788393be83ccaed292`.
  A mode-`0700` production verifier probe installed all six generated files
  into a private snapshot successfully.
- Pre-commit delivery gates passed 35 focused verifier/consumer-contract tests
  and 899 generated engine, reconciliation, retention, and scheduler/doctor
  tests on Python 3.13.0 with two expected skips. Python 3.9.6 passed the four
  CI compile targets and its exact 38-test compatibility matrix. Ruff lint,
  consumer-owned Ruff format checks, actionlint, manifest-change validation,
  project-journal validation, and `git diff --check` also passed. The generated
  canonical files remain byte-for-byte stock output and are intentionally not
  reformatted by the consumer.
