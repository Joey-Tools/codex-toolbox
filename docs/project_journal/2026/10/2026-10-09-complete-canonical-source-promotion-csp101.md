---
id: 20261009-csp101
title: Complete Canonical Source Promotion
status: completed
created: 2026-10-09
updated: 2026-10-09
branch: codex/complete-canonical-source-replacement-20261009
pr:
supersedes: []
superseded_by:
---

# Complete Canonical Source Promotion

## Summary

Promote the complete eleven-file canonical source group from the repaired,
actually merged personal-sync master into the latest Toolbox master. Preserve
the existing consumer-owned mapping and verification contracts rather than
cherry-picking generated files or weakening the automation branch guard.

## Decisions and Rationale

- Canonical PR #34's twelve review threads were resolved before continuing.
  Its authorized complete replacement PR #36 earned independent current-head
  GitHub Codex review and required CI, then squash-landed as
  `110f2df02ee6388835d1eb6635e09e7f838658fb`, with tree
  `cf48b0910d377ad19fa060fcf04322a5e60a5571` equal to the tested candidate.
- A fresh Toolbox branch starts from master
  `69d5318593cc2acac2596c4c0895f5396877587c`. Old Toolbox PR #36 and its branch
  are retained until complete replacement coverage has been independently
  verified. The original PR's three findings are not discarded as clean review
  evidence for a new PR.
- The canonical sync workflow correctly rejects consumer-owned verifier/test
  edits in its old generated-only automation branch. Do not broaden that
  allowlist or force-rewrite the old branch to make the job green.
- The first local generation exhausted the shared Git-directory aggregate
  inventory budget and left its durable transaction. An independent clean
  local source clone of the same merged commit reduces Git control scanning;
  the unmodified official generator resumes its own identity-bound transaction
  and completes all eleven files. Do not delete the transaction to hide failure,
  increase budgets, or treat a generating receipt as release provenance.
- The consumer-owned verifier independently pins both the canonical commit
  and complete receipt digest. Synthetic fixtures use that owner constant;
  a negative regression still rejects the prior canonical commit even when
  its recomputed receipt digest matches.
- Consumer manifest-capacity tests explicitly require metadata v11 and one
  metadata document, plus the before/after v3 empty-proof documents; terminal
  receipt documents, when present, have their own declared capacity. Each
  captured document is classified by its closed overflow contract and checked
  against that contract's independent capacity. The original v10
  literal and single-serialization-count assumptions are obsolete; no runtime
  projection or overflow boundary is weakened.

## Current State

- Stock generated receipt SHA-256:
  `0935f2b57b585c8a3620a6995554ad9f15917c67a6a4c4f6ed795a273328994b`.
- Mapping digest remains
  `2504ff2345f5bd76b1b965a647dfa424f31849fba228391d9b6f1872ce42f7a6`;
  file-set digest remains
  `defbefa4d2b0c016b9ca3cbdeb49ab6c9cf708562039b7f939398ebe2047958d`.
- Generated tree digest is
  `6ae6d215c418b74e75827cef151057819cce8641f63369a369b8f1fb7e4feaba`.
- Generated engine bytes match the independently validated canonical engine,
  SHA-256 `25f6bc5f616e466bbe076a8464d6e83b24f55a5f3c5296c6be52a73a1fd6fe62`.
- Both canonical fixtures mentioned in the old Toolbox findings are stock
  repaired upstream output: the final-target budget ticket is bound inside
  its fake home; legacy control names are checked by exact parsed identities,
  not platform-dependent inode/device hex widths.

## Validation Boundary

Canonical full native and Linux discovery each passed 1,895 tests at the
identical source tree, with three and twenty-seven skips respectively. These
results establish source compatibility, not this Toolbox PR's remote CI,
review, release, or installation pass. The consumer owns its focused verifier,
packaging and manifest checks; current-head Toolbox CI and GitHub Codex review
remain independently mandatory and are recorded in the PR's delivery checklist.

- Official generator recovery completed all eleven files; its independent
  stock check passed with unchanged mapping/file-set digests.
- Native Python 3.13 consumer verifier, immutable-snapshot, package-builder,
  release-workflow, manifest and both old finding-specific tests passed:
  294 tests in 20.258 seconds. The two earlier attempts are failed evidence,
  not passes.
- Native Python 3.9.6 passed the exact 39-test CI compatibility selection.
  Both supported versions compiled the required helpers; Ruff, whitespace
  and project-journal checks passed.

## Downstream Contract

Private source promotion must bind the actual merged Toolbox commit/tree and
its verified immutable public release. Canonical provenance alone is not a
private base release. Public/private releases, four separately authorized native
account-home installations and preserved-worktree cleanup are separate gates.
