---
id: 20260724-spr001
title: Recursive Shared Prefix Receipts
status: completed
created: 2026-07-24
updated: 2026-07-27
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
- Capture and every final revalidation now prove `.git -> admin`, admin backlink, and `commondir -> source` endpoints through component-by-component no-follow descriptor chains, with the pointer file bound again after each endpoint walk.
- Managed and new checkout paths share one pre-Git gate that binds a fresh source lease to both preflight source fingerprints and retains it through checkout or rollback.
- Ambiguous-registration and final rollback registry queries now run through that retained source lease's subprocess identity gate and revalidate it before and after parsing; source replacement preserves recovery state instead of authorizing target-parent cleanup.
- New registrations also retain a bounded, descriptor-bound raw inventory of direct `worktrees/` admin entries before and after add. Missing or invalid `gitdir` records that Git omits from its semantic list are preserved with a `worktree-registration-recovery-v1` locator; only unchanged direct-entry identity/access state authorizes cleanup.
- The post-add inventory is bound to the same held `worktrees/` parent object as the managed control receipt. Normal and recovered-control rollback remain disabled until the raw transition proves ownership of exactly that new receipt-bound admin entry.
- Known rollback gives recursive `git worktree remove` a child exec gate over the materialized target's held parent/name/object descriptors, verifies the exact receipt-bound admin name is absent from its held parent, and requires the raw inventory to return to its pre-add baseline before transaction-created target parents can be removed. A target-entry replacement visible before exec preserves both registration and replacement for recovery; internal files changing inside an unchanged peer admin directory do not alter the protected direct-entry property.
- Recursive shared-prefix policy failures no longer widen permissions on surviving managed checkouts; owner access is adjusted only for a newly registered transaction-owned parent that proceeds through full-worktree rollback.
- Finalization parses the immutable descriptor-captured index bytes, rejects zero skip-hash checksums, noncanonical ordering, conflict/hidden/unknown extension state, validates any cache-tree against the target tree, and requires the complete canonical stage-0 mode/object-id/raw-path sequence to equal that tree. Cache-tree siblings use Git's `subtree_name_cmp`/`write_one` length-first then raw-byte ordering, which is distinct from tree-entry ordering. The protected property is the captured index semantics, not working-tree stat-cache or file-content bytes.
- Managed preflight and final revalidation now descriptor-read that raw index and run the same parser against the current HEAD before the checkout probe or real checkout can be invoked; split-index `link` state and unknown optional extensions therefore fail before mutation.
- Authorized fetch URL classification now recognizes username-less `host:path` and other Git-compatible scp-style forms before generic schemes, so every Git-SSH form receives the private SSH executable snapshot while `://` schemes retain strict allowlisting.
- Valueless supported fetch booleans preserve Git's implicit-`true` meaning; explicit empty fsck remains `false`, explicit empty `core.sharedRepository` becomes `umask`, and explicit fsck values are normalized through the bound Git runtime's boolean parser. Unsupported valueless keys and malformed boolean values remain blocked.
- Shared-prefix publication and revalidation require both the bound owner write/search mode bits and effective write/search access, so root or another DAC-bypass context cannot bless a `0555` ancestor.
- Superproject split-index discovery preserves existing `link` state for the read-only `--shared-index-path` query and binds the shared index object alongside the primary index; same-content shared-index replacement blocks apply before worktree mutation.
- Authorized fetch receipts bind all 257 direct writable object children—`pack` plus all 256 lower-case loose-object fanout names—as absent or exact accessible directory objects. Existing symlinks are rejected, and an absent/present/symlink drift visible at the child exec gate retains the fetch recovery fence before Git writes.
- Authorized fetch launch now also retains descriptor-bound leases for the private control gitdir, its internal control directories, and the exact bytes of `config`, `HEAD`, and optional `shallow`. A gitdir/control-file replacement or same-inode content rewrite visible at the final child gate prevents Git from starting while preserving the persistent fetch recovery identity.
- Existing source shallow replacement locks must preserve the receipt-bound owner, group, and mode before exchange. A shared-repository process that cannot reproduce that policy fails closed without changing the original boundary.
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
- Final fresh-review fixes: the complete helper suite passed 171 tests in 214.607 seconds, including intermediate-symlink pointer retargeting, managed/new source-lease replacement, real skip-hash, v2/v3/v4, cache-tree, hidden-extension, noncanonical-order, and valid-alternate-index regressions.
- The repository-level test matrix passed 892 tests in 360.907 seconds. Repository-wide Ruff lint, changed-file Ruff format, Python compilation, helper `--help`, isolated PyYAML skill validation, project-journal validation, sync-manifest validation, and `git diff --check` also passed.
- Final endpoint/index follow-up: the complete helper suite passed 172 tests in 214.185 seconds, including capture/revalidation races across long index validation, Git cache-tree sibling ordering, EOIE suppression, and non-root empty-tree semantics; the bounded focused re-review reported no actionable findings.
- The final repository-level test matrix passed 931 tests in 376.481 seconds. Ruff lint, changed-file Ruff format, Python compilation, helper `--help`, isolated PyYAML skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed.
- Registry-query/access-policy follow-up: seven focused rollback, source-lease, recursive-prefix, and finalization regressions passed in 34.311 seconds; the three new same-UID source replacement, post-query drift, and managed-mode regressions passed again in 8.047 seconds.
- The complete helper suite passed 175 tests in 243.375 seconds.
- Complete repository coverage passed 934 tests: the README module matrix passed 896 tests in 469.508 seconds, and its omitted public-release workflow module passed 38 tests in 0.599 seconds.
- Repository-wide Ruff lint, changed-file Ruff format, Python compilation, helper `--help`, isolated PyYAML skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed.
- An independent read-only diff audit reported no actionable findings; it confirmed both guarded registry-query failure paths, managed-mode preservation, and the documented stock-Git residual race boundary.
- Raw admin-inventory follow-up: six focused partial-registration, source-replacement, final-query, exact-admin-removal, and full finalization-rollback regressions passed in 25.514 seconds.
- The complete helper suite passed 178 tests in 344.134 seconds. Missing and zero-byte `gitdir` partial admin entries remained at their exact recovery locations even though Git's semantic registry did not report the target.
- Complete repository coverage passed 937 tests: the README module matrix passed 899 tests in 636.607 seconds, and its omitted public-release workflow module passed 38 tests in 0.639 seconds.
- Repository-wide Ruff lint, changed-file Ruff format, Python compilation, helper `--help`, isolated PyYAML skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed for the raw admin-inventory follow-up.
- Ownership/root-binding follow-up: seven focused partial-admin, exact-removal, normal-control, recovered-control, inventory-parent-binding, and final-query regressions passed in 22.429 seconds. The original independent read-only auditor then reported no actionable findings.
- The final complete helper suite passed 181 tests in 498.252 seconds.
- Final repository coverage passed 940 tests: the README module matrix passed 902 tests in 1022.017 seconds, and its omitted public-release workflow module passed 38 tests in 1.264 seconds.
- Repository-wide Ruff fatal-error lint (`E9,F63,F7,F82`), changed-file Ruff format, Python compilation, helper `--help`, isolated PyYAML skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed. An exploratory Ruff 0.16.0 all-rules invocation reported 592 project-wide style and modernization findings; that unconfigured rule set is not the repository's lint gate.
- Exact-head GitHub review follow-up: 11 focused scp-style URL, valueless/native Git boolean, raw split-index, unknown-index-extension, and final-index regressions passed in 10.827 seconds.
- The final reviewer follow-up covering explicit-empty shared-repository policy passed five focused regressions in 5.610 seconds; the read-only re-review reported no findings.
- The final exact-head follow-up complete helper suite passed 190 tests in 451.303 seconds.
- Final complete repository coverage passed 949 tests: the README module matrix passed 911 tests in 654.769 seconds, and its omitted public-release workflow module passed 38 tests in 1.838 seconds.
- GitHub round-two follow-up: 12 focused owner-mode, shared-index, loose-object fanout, and shallow-policy regressions passed in 10.017 seconds; four strengthened race and shared-policy cases passed independently in 1.290 seconds.
- The final complete helper suite passed 196 tests in 133.095 seconds, and the final repository-level module matrix passed 917 tests in 255.066 seconds.
- Repository-wide Ruff fatal-error lint (`E9,F63,F7,F82`), changed-file Ruff format, Python compilation, helper `--help`, skill validation, and `git diff --check` passed. An independent read-only diff audit reported no actionable findings.
- Formal single follow-up: eight focused pack/fanout, rollback target-entry, and cache-tree ordering regressions passed in 4.011 seconds.
- The complete helper suite passed 201 tests in 210.714 seconds, and the complete repository module matrix passed 922 tests in 334.778 seconds.
- The cache-tree ordering finding was refuted by upstream and a real Git 2.53.0 fixture: Git's `cache-tree.c` compares subtree name length before raw bytes, and real indexes serialized `b` before `aa` and `z` before `aa`; the forged tree-entry-style `aa, b` order remains rejected.
- README Python compilation, Ruff fatal-error lint (`E9,F63,F7,F82`), changed-file Ruff format, helper `--help`, skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed for the formal-single follow-up.
- GitHub round-three control-plane follow-up: 14 private gitdir/config, source object/pack/fanout, fetch-policy, shallow-boundary, frozen-environment, and successful-fetch regressions passed in 5.353 seconds.
- The final complete helper suite passed 204 tests in 146.556 seconds, and the final complete repository module matrix passed 925 tests in 247.637 seconds.
- README Python compilation, repository-wide Ruff fatal-error lint (`E9,F63,F7,F82`), changed-file Ruff format, helper `--help`, skill validation, project-journal validation, sync-manifest validation, and `git diff --check` passed for the control-plane follow-up.
