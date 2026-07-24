---
name: submodule-linked-worktrees
description: Safely set up or sync large Git submodule repositories with disk-saving linked worktrees and shared `.git/modules` object stores. Use when Codex is asked about saving disk space for submodules, replacing `git submodule update --init --recursive` in linked worktrees, planning explicit submodule-path changes, authorizing all-path or missing-commit fetch operations, using `--reference`/alternates/ref clones, avoiding hard-linked submodule worktrees, or syncing submodule checkout SHAs across macOS/Linux Git worktrees.
---

# Submodule Linked Worktrees

## Overview

Use this skill to reduce duplicated Git object storage in repositories with many submodules, especially when a large checkout is used through multiple linked worktrees.

The bundled POSIX-only helper creates detached submodule linked worktrees that reuse an existing source repo under `.git/modules`. It requires Git 2.45 or newer, locates source repos by submodule name and worktrees by submodule path, requires explicit top-level paths unless the task authorizes `--all`, and preflights the complete selected set before changing any target worktree. It does not hard link working-tree files and does not replace ordinary submodule workflows unless the repository shape calls for it.

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

Use `scripts/submodule_worktree_sync.py` from this skill when linked submodule worktrees are the right model. Resolve the exact top-level paths from the task before invocation. Passing no paths is an error; it never means all submodules.

Optional plan-only run:

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

The targeted sync automatically runs the same full preflight before it applies the plan. For an existing managed worktree, that preflight binds the current HEAD and index, derives the bounded target write set, checks every existing write ancestor/object, inventories ignored conflicts, and performs Git's checkout dry run. Once the task has resolved the exact target paths, a successful preflight may proceed to apply without a redundant confirmation step. Keep `--dry-run` when the task asks only for a plan or when another unresolved safety decision remains.

Use `--all` only when the task explicitly authorizes every top-level submodule:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/submodule-linked-worktrees/scripts/submodule_worktree_sync.py" \
  --repo /path/to/target-worktree \
  --source-superproject /path/to/canonical-checkout \
  --force-replace-empty \
  --all
```

Missing target commits are read-only failures by default. Use `--fetch-missing` only when the task explicitly authorizes shallow network fetches from the exact `.gitmodules` URL. The helper requires that URL to equal the source repository's sole `remote.origin.url`, binds the source config object and bytes, rejects executable/credential/proxy/include/bundle/URL-rewrite configuration, freezes a closed child environment, and fetches the exact URL instead of a mutable remote name. The child uses an owner-private control gitdir and the bound source object directory, so it never rereads the live source config after final revalidation. Its bound config reproduces the source's supported fsck, shared-repository, and fsync object-write policy; unsupported legacy or path-dependent object policy fails closed. Before network object writes, the helper persists `codex-submodule-fetch.pending`; a failed or interrupted fetch retains that recovery fence, and every later commit-available fast path blocks until the transaction and shallow boundary are recovered. The source shallow file or its proved absence is also bound. For absent-to-present publication, the helper freezes and revalidates the supported `core.sharedRepository`, umask, and parent ownership-inheritance contract, then verifies the installed mode and owner/group instead of forcing `0600`. A successful fetch installs either the fetched boundary or its proved absence, streams and hashes the complete target commit/tree/blob payload closure, and only then durably clears the persistent transaction. With `--dry-run`, it prints the bound missing-commit fetch but does not execute it. A recursive plan whose parent commit is missing always stops before fetching because its complete descendant target set is not yet knowable; prefetch separately, or use an explicitly authorized non-recursive fetch and then rerun recursively.

If `--source-superproject` is omitted, the script uses the target repo's own `git rev-parse --git-common-dir`. This works for many Git linked worktrees because their common gitdir is the canonical checkout's `.git` directory.

Use `--source-common-git-dir /path/to/repo/.git` only when there is no usable source worktree but the `.git/modules` tree is known and intentionally kept.

## Safety Rules

- Resolve and pass exact top-level submodule paths. Never translate an empty path list into all submodules.
- Use `--all` only when the task itself authorizes the complete top-level set; convenience or a vague request to "set up submodules" is not enough.
- The helper always preflights the complete selected set before applying target-worktree changes. Use `--dry-run` to stop after that preflight, not as a substitute for resolving scope.
- Before any repository read, the helper resolves one absolute Git executable, copies its bounded bytes into an owner-private executable snapshot, requires version 2.45 or newer from that snapshot, and binds the source and snapshot content digest plus object/access policy. Every later Git command executes the snapshot after revalidation, uses a controlled environment that rejects ambient repository/index/object/config redirection, and disables promisor lazy fetches. Only an explicit `--fetch-missing` operation may perform network I/O.
- The CLI binds the top-level `.gitmodules`, the primary/shared superproject index objects and bytes, and the exact selected stage-0 gitlink rows into the plan receipt. It revalidates them before every authorized fetch and immediately before the first target mutation. Object identity, content digest, and effective access are protected; timestamp-only changes are not treated as content changes.
- Each direct source gitdir and config is bound with includes disabled. `commondir` indirection, promisor/partial-clone config, `.promisor` pack markers, and `objects/info/{alternates,http-alternates}` are rejected for linked-worktree apply; use the canonical common gitdir and materialize the required objects there first. Before every worktree write, the helper enumerates the exact target commit/tree/blob checkout closure with lazy fetching disabled, streams every unique payload under hard time/byte caps, independently recomputes each Git object id, and binds a count, logical-byte total, and SHA-256 inventory digest. A present commit whose tree/blob closure is missing or corrupt is unavailable, not checkout-ready.
- An authorized fetch runs from a receipt-bound owner-private control gitdir against the exact URL, with the source object directory fixed in the environment and HTTP redirects/proxies/extra headers disabled. The helper holds no-follow descriptors for that object directory and its direct parent, matches them to the transport receipt, and makes the child recheck the live parent path, entry, and object identities immediately before `exec`; a path replacement already visible at that gate fails before Git can write through `GIT_OBJECT_DIRECTORY`. As with managed worktree control paths, a malicious same-UID replacement in the residual interval after the child gate but before stock Git opens the absolute object path is outside this portable prevention guarantee; later receipt/closure checks retain the recovery fence and fail closed, but cannot undo an already redirected write. The child config contains repository format, bare-repository settings, the frozen supported object-write policy, and a private copy of any existing shallow boundary; the live source config cannot redirect or silently weaken fsck, shared-mode, or durability policy after revalidation. Auto-maintenance and commit-graph writes are disabled. After a successful fetch, the helper holds the receipt-bound source gitdir descriptor and treats both a present and absent private shallow boundary as receipt state. It acquires `shallow.lock` when publication or deletion is required, then uses atomic no-replace or exchange CAS before cleanup; unsupported primitives fail closed, and post-publication recheck or durability uncertainty returns a `source-shallow-cas-v1` recovery identity and retains a stale fence when recovery ownership remains provable. SSH URLs use a bound absolute SSH executable with user SSH config, proxy commands/jumps, hostname canonicalization, and local commands disabled; ordinary authentication may still use the frozen `SSH_AUTH_SOCK`/`HOME` values.
- Git inventories used by checkout preflight have hard time, retained-output, input, path-count, path-length, and access-binding limits. Root and recursive `.gitmodules` reads also share one deadline and retained-content budget, with a per-file ceiling and descriptor-safe regular-file checks. A repository beyond those limits fails closed instead of falling back to an unbounded command.
- Missing-path alias checks bind and revalidate the deepest existing parent directory's case and Unicode-normalization semantics. Distinct `Foo`/`foo` paths remain distinct on a proved case-sensitive directory; aliases are rejected on a case-insensitive, ext4-casefold, or normalization-insensitive anchor. Linux lookup, casefold-flag, and filesystem probes share one no-follow directory descriptor; a lookup proof revalidates the chosen entry before and after its alternate-case lookup and distinguishes two exact hardlink dirents from case-insensitive aliasing. Literal Unicode comparison is enabled only for a known byte-exact filesystem with casefold explicitly disabled; an otherwise usable unknown filesystem gets conservative NFD collision keys. Empty unknown filesystems without case evidence, XFS without an ASCII-CI proof, and OverlayFS fail before any fetch or worktree mutation instead of trusting `core.ignoreCase` or assuming semantics. A bounded trie uses active recursive-ancestor metadata so each insertion is linear in target depth rather than repeatedly walking the parent chain; different raw spellings of one normalized shared prefix are rejected before apply.
- Before any tracked-status command, the helper inspects the complete stage-0 regular-file index plus current- and target-tree attributes and local clean/process filter configuration with non-converting bounded Git plumbing. It rejects LFS and both required and non-required custom filters, never executes repository-defined filters, and never accepts a raw pointer as a successful checkout.
- Existing managed checkouts use `--no-overwrite-ignore`. The complete plan inventories ignored conflicts before the first target checkout and repeats the check during final revalidation.
- Missing target components are created with `mkdirat`-style descriptor-relative operations from the receipt-bound root; pathname-recursive `mkdir` is not used. The helper holds the exact target and parent directory objects through the Git write, creates new worktrees with `--no-checkout`, revalidates the target entry before content checkout, runs checkout from the held target descriptor, and post-validates target identity, common gitdir, HEAD, source policy, and object-closure receipt.
- Exact selected siblings may reuse a missing strict ancestor only after one participant publishes the same descriptor-created directory identity as a plan-owned receipt. Final worktree roots never enter the sibling receipt pool; replacement, access-policy drift, or name-semantics drift remains an error.
- Git paths containing Windows drive/UNC forms or backslashes are rejected. Native Windows execution is unsupported rather than silently applying different separator or process semantics.
- Start with one small submodule path unless the task explicitly calls for a broader set.
- Do not let the helper overwrite non-empty directories. It intentionally refuses non-empty paths that are not already managed linked worktrees.
- Do not clean or deinitialize submodules as part of this workflow unless the user explicitly approves the destructive cleanup.
- If a source repo is missing, initialize it in the source checkout first; do not clone repositories during automation unless the user requested that.
- Do not pass `--fetch-missing` unless task semantics explicitly authorize fetching missing commits. Without it, the helper reports the path, URL, target SHA, source gitdir, and planned fetch command without network mutation.
- If shallow fetching a raw commit SHA fails, report the path, URL, target SHA, and source gitdir. Do not silently unshallow or fetch the full history.
- After deleting target worktrees, use `git worktree prune` on the relevant source repos only when stale worktree records need cleanup.

## Validation

For script edits, run from the skill directory:

```bash
python3 -m py_compile scripts/submodule_worktree_sync.py
python3 scripts/submodule_worktree_sync.py --help
```

For repository source updates, also run from the repository root:

```bash
python3 -m unittest tests.test_submodule_worktree_sync
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
