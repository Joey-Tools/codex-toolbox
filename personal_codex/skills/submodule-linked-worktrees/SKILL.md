---
name: submodule-linked-worktrees
description: Set up large Git submodule repositories with disk-saving linked worktrees and shared `.git/modules` object stores. Use when Codex is asked about saving disk space for submodules, replacing `git submodule update --init --recursive` in linked worktrees, using `--reference`/alternates/ref clones, avoiding hard-linked submodule worktrees, or syncing submodule checkout SHAs across macOS/Linux Git worktrees.
---

# Submodule Linked Worktrees

## Overview

Use this skill to reduce duplicated Git object storage in repositories with many submodules, especially when a large checkout is used through multiple linked worktrees.

The bundled helper creates detached submodule linked worktrees that reuse an existing source repo under `.git/modules`. It locates source repos by submodule name and worktrees by submodule path. It does not hard link working-tree files and does not replace ordinary submodule workflows unless the repository shape calls for it.

## Decision Path

1. Inspect the target repo before choosing a setup.
   - Run `git rev-parse --show-toplevel --git-dir --git-common-dir`.
   - Inspect `.gitmodules` with `git config --file .gitmodules --get-regexp 'submodule\..*\.(path|url|shallow|branch)'`.
   - Check current state with `git submodule status --recursive`.

2. Prefer standard submodule setup when disk sharing is not the point.
   - Use `git submodule update --init --recursive --depth 1 -- <paths>` for ordinary shallow setup.
   - Use `submodule.active` or explicit path arguments to avoid initializing unused vendor trees.

3. Prefer `--reference`/alternates when the user wants standard submodule ownership but less object duplication.
   - Use one reference repo per submodule repo, not one superproject reference for all submodules.
   - Do not pass `--dissociate` when the goal is disk savings.
   - Warn that alternates depend on the referenced object store not being pruned aggressively.

4. Prefer linked submodule worktrees when the target is itself a linked worktree and a canonical checkout already has populated `.git/modules/<submodule-name>` source repos.
   - Keep submodule worktrees on detached HEADs.
   - Treat `git submodule update --init --recursive` as a competing owner for those paths after conversion.
   - Sync by reading gitlink SHAs from the current superproject index.

5. Do not use hard links for submodule working-tree files.
   - Editors, build tools, and Git checkout can mutate files in place.
   - Hard-linked working trees can leak changes across checkouts.
   - Filesystem clone/reflink copies are acceptable only as one-time local copies, not as the management model.

## Helper Script

Use `scripts/submodule_worktree_sync.py` from this skill when linked submodule worktrees are the right model.

Typical dry run:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/submodule-linked-worktrees/scripts/submodule_worktree_sync.py" \
  --repo /path/to/target-worktree \
  --source-superproject /path/to/canonical-checkout \
  --dry-run \
  --force-replace-empty \
  -- third_party/libexample
```

Typical targeted sync:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/submodule-linked-worktrees/scripts/submodule_worktree_sync.py" \
  --repo /path/to/target-worktree \
  --source-superproject /path/to/canonical-checkout \
  --force-replace-empty \
  -- third_party/libexample
```

If `--source-superproject` is omitted, the script uses the target repo's own `git rev-parse --git-common-dir`. This works for many Git linked worktrees because their common gitdir is the canonical checkout's `.git` directory.

Use `--source-common-git-dir /path/to/repo/.git` only when there is no usable source worktree but the `.git/modules` tree is known and intentionally kept.

## Safety Rules

- Run `--dry-run` first for any repo that has not used this helper before.
- Start with one small submodule path before attempting all submodules.
- Do not let the helper overwrite non-empty directories. It intentionally refuses non-empty paths that are not already managed linked worktrees.
- Do not clean or deinitialize submodules as part of this workflow unless the user explicitly approves the destructive cleanup.
- If a source repo is missing, initialize it in the source checkout first; do not clone repositories during automation unless the user requested that.
- If shallow fetching a raw commit SHA fails, report the path, URL, target SHA, and source gitdir. Do not silently unshallow or fetch the full history.
- After deleting target worktrees, use `git worktree prune` on the relevant source repos only when stale worktree records need cleanup.

## Validation

For script edits, run:

```bash
python3 -m py_compile scripts/submodule_worktree_sync.py
python3 scripts/submodule_worktree_sync.py --help
```

For a real repo, validate in this order:

```bash
python3 scripts/submodule_worktree_sync.py --repo <target> --dry-run --force-replace-empty --no-recursive -- <small-submodule>
python3 scripts/submodule_worktree_sync.py --repo <target> --dry-run --force-replace-empty -- <nested-submodule>
python3 scripts/submodule_worktree_sync.py --repo <target> --force-replace-empty --no-recursive -- <small-submodule>
git -C <target>/<small-submodule> rev-parse HEAD --git-common-dir
git -C <target> status --short --untracked-files=no -- <small-submodule>
```

## References

- Read `references/workflow.md` when deciding between shallow submodule update, alternates, and linked submodule worktrees, or when explaining failure modes.
