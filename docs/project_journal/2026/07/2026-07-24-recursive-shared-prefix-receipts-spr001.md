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
- Unrelated checkout paths, final target roots, symlinks, replacement objects, and unsafe access policy remain untrusted.
- New-registration failures remove the exact worktree through the held parent descriptor and verify target and registry cleanup.

## Next Steps

- Keep recursive-parent checkout tests in the full helper regression suite when extending nested target planning.

## Evidence

- `personal_codex/skills/submodule-linked-worktrees/scripts/submodule_worktree_sync.py`
- `personal_codex/skills/submodule-linked-worktrees/SKILL.md`
- `personal_codex/skills/submodule-linked-worktrees/references/workflow.md`
- `tests/test_submodule_worktree_sync.py`
- Eight focused recursive-parent/shared-prefix regressions passed in 45.770 seconds.
- The complete helper suite passed 153 tests in 153.742 seconds.
- Ruff lint/format, Python compilation, helper `--help`, skill validation, project-journal validation, and `git diff --check` passed.
