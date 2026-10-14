---
id: 20261014-cpp018
title: Linked Submodule Checkout Payload Proof
status: completed
created: 2026-10-14
updated: 2026-10-14
branch: codex/toolbox-pr18-checkout-payload-20261014
pr:
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
- Batch same-attribute normalization through at most 32 held snapshot
  descriptors, retaining full private object-pack validation per batch.
  Preserve the original PR #18 branch and history while delivering its useful
  feature through a fresh review lineage.

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
- Git reads the same descriptor-bound snapshots covered by the child digest
  gate, not mutable staging names. Post-batch source-file, path, parent-chain,
  content and access-policy checks remain required. Descriptor retention is
  bounded per active batch; pack-validation cost still depends on batch and
  attribute-profile count.
- Stock normalization remains authoritative. Historical non-renormalized CRLF
  blobs that differ from the stock normalized hash are rejected, without a raw
  equality bypass that could weaken encoding or conversion checks.

## Evidence

- Frozen pre-fix import `decbc4c` accepted a target-matching index with a missing
  tracked payload. The new high-level regression actually failed with
  `PlanError not raised`, rather than an import or infrastructure error.
- Existing CI job `91398478159` failed because its forged path-list mock also
  intercepted closure enumeration. Restrict that mock to path enumeration;
  keep the verified-private-tree production guard unchanged.
- The earlier CPython 3.14.2 scope passed 278 tests in 530.353 seconds;
  raw log SHA-256 is
  `4ec479f9cbff8efd23281a300e8ecda3e72ebf579821cc8dbe0bacfbbd2bda57`.
  Source and test SHA-256 values were unchanged after the terminal result.
- The earlier native CPython 3.9.6 scope passed eight focused regressions in
  52.231 seconds. These historical results do not prove the later P1 fix.
- The frozen `e3bad53` cost regression actually failed: 45 tracked payloads and
  a pack larger than 1 MB caused 51 parent pack-digest revalidations, exceeding
  the bound of 20. The same regression passes with bounded FD batches; child
  gates remain enabled and are not counted as parent calls.
- Native CPython 3.14.7 and Git 2.55.0: the final complete helper module passed
  284 tests in 561.634 seconds, with one explicit macOS non-UTF-8 filename skip.
  Supervisor exit was 0 with no incomplete result; raw log SHA-256 is
  `fd821388294d67db9e6966dc096bb5332f3f6b83fe85df9457fc1324b89a597a`.
  Exact helper/test inputs were unchanged after the terminal result.
- Nine focused final regressions passed in 40.420 seconds with the same explicit
  platform skip. They cover the real cost bound, descriptor authority, path-name
  replacement, stock per-path conversion equivalence, transient live-file
  mutation, benign timestamps, built-in conversions and raw SHA-1/SHA-256 framing.
- Native CPython 3.9.6 passed eight final focused compatibility regressions in
  43.666 seconds; supervisor exit was 0 with no incomplete result, and raw log
  SHA-256 is `33215a503077c4b8b1e7e3ade3a3ce2c1a63479afc0ca118185ab48ad5a686b4`.
- The eleven-file generated source snapshot, exact-base `66bb888` manifest
  validation, selected Ruff, bytecode compilation, skill validation, and journal
  validation passed. Generated engine/receipt bytes and public manifest links
  are unchanged by this workstream.
- These are local relevant-surface results, not an entire-repository suite claim.
  Remote review and CI lifecycle evidence belongs to the delivered PR's exact
  head; old PR #18, interrupted runs and partial output never supply its pass.
