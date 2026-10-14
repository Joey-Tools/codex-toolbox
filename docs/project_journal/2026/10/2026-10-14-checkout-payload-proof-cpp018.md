---
id: 20261014-cpp018
title: Linked Submodule Checkout Payload Proof
status: completed
created: 2026-10-14
updated: 2026-10-14
branch: codex/toolbox-pr18-checkout-payload-20261014
pr: https://github.com/Joey-Tools/codex-toolbox/pull/18
supersedes: []
superseded_by:
---

# Linked Submodule Checkout Payload Proof

## Summary

- Preserve the assigned linked-worktree feature and its prior index, attributes,
  recursive metadata, source-policy, process cleanup, and rollback guards.
- Import the current default branch with an ordinary signed merge, preserving
  both feature history and the canonical-sync consumer pin promotion.
- Add a separate descriptor-bound tracked-payload proof before publishing new
  and managed checkout receipts; a successful checkout and target index alone
  are insufficient.

## Protected Properties

- Object identity: no-follow target-root traversal and device/inode/type binding
  detect replacement. Owner/group/mode and effective read checks protect access
  policy. Timestamp-only changes and directory child churn remain benign.
- Content: stock Git hashes an independently leased snapshot of the exact
  observed bytes against target blobs in the verified private object view.
  Built-in Git conversions, symlink representations, and filemode semantics are
  preserved; custom filters remain rejected.
- A compact aggregate digest is repeated before receipt publication. Gitlink
  payloads remain owned by their recursive child checkout. Existing finite
  path/byte limits and deadline checks are retained. The same-UID final-check
  race is documented rather than represented as atomic prevention.

## Evidence

- Frozen pre-fix import `decbc4c` accepted a target-matching index with a missing
  tracked payload. The new high-level regression actually failed with
  `PlanError not raised`, rather than an import or infrastructure error.
- Existing CI job `91398478159` failed because its forged path-list mock also
  intercepted closure enumeration. Restrict that mock to path enumeration;
  keep the verified-private-tree production guard unchanged.
- CPython 3.14.2: the complete helper module passed 278 tests in 530.353 seconds;
  raw log SHA-256 is
  `4ec479f9cbff8efd23281a300e8ecda3e72ebf579821cc8dbe0bacfbbd2bda57`.
  Source and test SHA-256 values were unchanged after the terminal result.
- Native CPython 3.9.6: eight focused payload, conversion, mode, private-view,
  index, and recursive compatibility regressions passed in 52.231 seconds.
- The eleven-file generated source snapshot, exact-base `66bb888` manifest
  validation, selected Ruff, bytecode compilation, skill validation, and journal
  validation passed. Generated engine/receipt bytes and public manifest links
  are unchanged by this workstream.
- These are local relevant-surface results, not a claim that the future PR head
  passed the entire repository suite. Remote review and CI lifecycle evidence
  remains on PR #18; interrupted or partial output never supplies a pass.
