---
id: 20260724-spr001
title: Recursive Shared Prefix Receipts
status: completed
created: 2026-07-24
updated: 2026-07-24
branch: codex/toolbox-skill-sync-refactor
pr:
supersedes: []
superseded_by:
---

# Recursive Shared Prefix Receipts

## Summary

- Bound shared directory prefixes created by recursive parent checkout before planned child worktrees are materialized.
- Added transactional rollback so a failed prefix binding does not retain a registered parent worktree or plan receipts.

## Current State

- Only strict shared prefixes whose complete participant set belongs to the current parent's direct planned children are eligible.
- Prefix traversal is descriptor-relative and no-follow from the held parent target; receipts bind directory-entry/object identity, owner, group, mode, and effective write/search access.
- Parent-root and checkout-created shared-prefix receipts are validated and published as one plan-state update.
- Deeper descendants collapse to their nearest direct-child worktree subtree when a recursive parent authorizes a shared prefix; ownership stops at each direct child's final root.
- Unrelated checkout paths, final target roots, symlinks, replacement objects, and unsafe access policy remain untrusted.
- New leaf and recursive-parent registrations remain provisional through final source, HEAD, common-gitdir, object-closure, and receipt validation.
- Registration failures remove the exact worktree through the held parent descriptor, verify target and registry cleanup, and remove only still-matching transaction-created parent directories.
- Pre-registration materialization failures remove exact created directory identities in reverse order; replacement, access drift, and non-empty state are preserved with a structured recovery location.
- The target `.git` marker and admin backlink now retain their original descriptors through postvalidation and receipt publication; final HEAD, `commondir`, and index bindings are read only through that retained admin descriptor and are revalidated immediately before and after publication.
- A same-source, same-HEAD redirect to another worktree admin cannot satisfy finalization. New-target rollback atomically preserves each unexpected marker/backlink entry while temporarily restoring the retained original bytes for Git unregister, then verifies target, registry, materialized-parent, and plan-receipt restoration.

## Next Steps

- Keep recursive-parent checkout tests in the full helper regression suite when extending nested target planning.

## Evidence

- `personal_codex/skills/submodule-linked-worktrees/scripts/submodule_worktree_sync.py`
- `personal_codex/skills/submodule-linked-worktrees/SKILL.md`
- `personal_codex/skills/submodule-linked-worktrees/references/workflow.md`
- `tests/test_submodule_worktree_sync.py`
- Eight focused recursive-parent/shared-prefix regressions passed in 45.770 seconds.
- The complete helper suite passed 153 tests in 153.742 seconds.
- Fresh-review follow-up: 16 shared-prefix/finalization/materialization regressions passed in 53.219 seconds, plus the access-policy drift regression passed independently.
- The updated complete helper suite passed 160 tests in 147.698 seconds.
- Final control-transaction follow-up: the complete helper suite passed 165 tests in 157.442 seconds, including same-source/same-HEAD marker retargeting at every finalization phase, admin-backlink retargeting, HEAD/common-dir/index replacement, managed-target coverage, and recursive receipt rollback.
- Ruff lint/format, Python compilation, helper `--help`, skill validation, project-journal validation, and `git diff --check` passed.
