# Submodule Disk-Saving Workflow

## Trigger Examples

- "This repository has many submodules; can setup use less disk?"
- "Can we hard link or reflink submodules across worktrees?"
- "This checkout is a linked worktree of a large repo. Can submodules reuse the main checkout?"
- "After using submodule linked worktrees, should I still run `git submodule update --init --recursive --depth 1`?"

## Approach Comparison

### Limit The Active Set

Use explicit submodule paths or `submodule.active` when the build does not need every submodule.

```bash
git submodule update --init --recursive --depth 1 -- third_party/libalpha third_party/libbeta
```

This is the simplest and safest disk saver because unused working trees and object stores are never created.

### Use Alternates With `--reference`

Use this when the target checkout should keep ordinary submodule ownership but reuse objects from an existing clone.

```bash
git submodule update --init --recursive \
  --reference /path/to/source/.git/modules/third_party/libexample \
  -- third_party/libexample
```

Notes:

- `git submodule update --reference` takes a reference repository, not a superproject-to-all-submodules mapping.
- For repos with many different submodule URLs, run per path with the matching source repo.
- Do not use `--dissociate` when disk savings matter.
- Alternates break if the referenced object store is removed or pruned past required objects.

### Use Linked Worktrees For Submodules

Use this when the source checkout already has `.git/modules/<submodule-name>` and the target is another worktree that should share those object stores.

Select exact top-level submodule paths before running the helper. An empty path list is rejected rather than expanded to every submodule. Use `--all` only when the task explicitly authorizes the complete top-level set.

For an apply invocation, the helper securely binds the top-level `.gitmodules`, the primary and any shared superproject index file objects/bytes, and the selected stage-0 gitlink rows before preflighting every selected target, including recursive descendants. The receipt is revalidated before each authorized fetch and immediately before the first worktree mutation. A path replacement, byte-content change, access-policy change, or selected-row drift blocks the plan; an mtime-only transition does not. A failure in that phase prevents the apply phase from starting. `--dry-run` stops after the plan/preflight. When the task already names the exact paths and the preflight succeeds, the apply phase does not need a separate confirmation solely because the plan was printed.

For every existing managed worktree, the preflight binds its detached HEAD and semantic index snapshot, derives the bounded `HEAD..target` write set with renames disabled, and binds the identity and access policy of each existing object and parent needed by that write set. It checks only write-relevant ignored prefixes and descendants rather than enumerating unrelated untracked content, then runs a no-update `read-tree` probe. The actual checkout repeats the decisive checks and uses `--no-overwrite-ignore --no-recurse-submodules`.

Every planned source also has a completeness receipt. The helper binds the direct source gitdir and content-binds its config, rejects `commondir` indirection, include, promisor/partial-clone, `.promisor` pack-marker, and alternate-object policy, and revalidates the absence of `commondir`, `objects/info/alternates`, and `objects/info/http-alternates`. It walks the selected commit's complete checkout tree, then uses bounded streaming `cat-file --batch` to read every unique commit/tree/blob payload with lazy fetching disabled and independently recompute its SHA-1 or SHA-256 object id. The receipt binds the unique-object count, aggregate logical bytes, and a SHA-256 digest of the sorted object/type/size inventory. A commit-only or object-header-only result is never sufficient checkout evidence.

Before any tracked-status query or target-tree materialization, the helper enumerates every stage-0 regular-file index path plus current and target blobs with non-converting plumbing. It evaluates index attributes with `check-attr --cached`, tree attributes with `check-attr --source=<sha>`, and local `filter.*.clean` / `filter.*.process` configuration. Any selected driver—including one present only in staged-new index content, `lfs`, a required driver, or a non-required driver—is blocked. The later tracked-only status query pins `GIT_ATTR_SOURCE` to the already-inspected current commit; checkout pins it to the inspected target. The helper deliberately has no trusted filter allowlist, so neither status nor checkout can execute an unreviewed repository filter.

Before the first repository read, the helper resolves one absolute regular-file Git executable, copies its bounded bytes to an owner-private `0500` snapshot, binds the SHA-256 digest of both files, and requires `>=2.45.0` from the snapshot. Every command executes that snapshot after source/snapshot content and access-policy revalidation, so an in-place same-inode rewrite cannot change the launched bytes between preflight and execution. This gate makes `GIT_NO_LAZY_FETCH=1` part of the supported runtime contract rather than an ignored hint on an old Git. Native Windows is rejected before Git discovery because this helper's path and process contracts are POSIX-only.

All new Git inventories have hard deadlines and aggregate retained-byte, input-byte, entry-count, path-length, pathspec-batch, and access-binding ceilings. Status excludes untracked enumeration, worktree registry reads have explicit record caps, and ignored-path probes share one deadline and retained-byte budget. Root and recursive `.gitmodules` reads share a separate deadline and retained-content budget; root reads use one nonblocking, no-follow regular-file descriptor with repeated digest/content-stability checks, while recursive blobs are size-checked before bounded `cat-file` reads. Process cleanup uses bounded TERM/KILL grace and bounded direct-child reap; failure to reap is `cleanup-incomplete`, never an unbounded final wait.

An authorized non-recursive fetch requires one exact transport: the task-selected `.gitmodules` URL must byte-for-byte equal the source repository's sole `remote.origin.url`. The helper securely reads and binds the source `config` regular-file object and bytes, parses that captured content with includes disabled, and rejects `include`/`includeIf`, `core.sshCommand`, `core.gitProxy`, `core.alternateRefsCommand`, bundle-URI policy, HTTP proxy/header policy, `url.*` rewrites, credential helpers, protocol overrides, worktree config, custom upload-pack/proxy policy, and similar redirection or executable keys. It also extracts a frozen object-write policy. The supported projection preserves `fetch.fsckObjects`, its `transfer.fsckObjects` fallback, `core.sharedRepository`, `core.fsync`, and directly reproducible `core.fsyncMethod` values after strict validation. Duplicate values and policy that cannot be reproduced without additional path, filesystem, or ordering state—such as `fetch.fsck.*`, unpack limits, `core.createObject`, `core.fsyncMethod=batch`, or deprecated `core.fsyncObjectFiles`—fail before fetch. The shared-repository projection preserves Git's mode-adjustment setting; it does not claim to bind process umask, ACLs, default ACLs, or group inheritance. Fetch fsck also lacks quarantine, so preserving it does not prove a failed fetch left the object database clean.

The helper binds the source shallow file object/bytes or its absence under the bound source gitdir, then creates an owner-private control gitdir containing the source object format, bare-repository settings, frozen object-write policy, and a private copy of any existing shallow boundary. Before the network child can write source objects, it descriptor-creates and durably fsyncs `codex-submodule-fetch.pending` with the expected shallow state and a transaction identity. Fetch uses that control gitdir, the bound source object directory, and the exact URL rather than `origin`; the child therefore cannot reread the live source config after final revalidation. The helper also holds no-follow descriptors for the source object directory and its direct parent, matches the directory to the transport receipt, and makes the child compare the live absolute parent path, parent descriptor, entry name, and object descriptor immediately before `exec`. Replacing `objects/` with a symlink or another directory before that gate fails before Git starts, rather than redirecting `GIT_OBJECT_DIRECTORY` writes. A malicious same-UID replacement in the residual interval after the child gate but before stock Git opens the absolute path is outside the portable prevention guarantee; later receipt and full-closure checks fail closed and retain the fetch transaction, but cannot undo an already redirected write.

On success, the helper opens and revalidates the exact source gitdir once, then performs every source `shallow` / `shallow.lock` lookup, open, rename, and cleanup relative to that held no-follow directory descriptor. A private shallow boundary may be present or absent: absent-to-absent is a verified no-op, absent-to-present uses macOS `renameatx_np(RENAME_EXCL)` or Linux `renameat2(RENAME_NOREPLACE)`, and a receipt-bound existing source uses `RENAME_SWAP` / `RENAME_EXCHANGE`. Present-to-absent exchanges an owner-bound empty deletion marker, verifies both exchanged objects, and removes the marker and prior boundary with directory durability checks. `shallow.lock` excludes cooperative Git writers whenever mutation is needed. Only after the boundary reaches a verified, durable terminal state and the exact fetched checkout closure has passed full payload hashing does the helper descriptor-unlink and fsync the persistent fetch transaction. If that final directory fsync fails, it recreates and fsyncs a recovery fence before returning failure. A failed fetch, interrupted boundary/object verification, stale `shallow.lock`, or stale fetch transaction makes even an otherwise present commit unavailable to later helper runs; the reported recovery identity binds all visible fence/boundary states.

The helper verifies the prior object's receipt-bound identity, complete bytes, and effective access before deletion. A mismatch is atomically swapped back, while rollback, post-publication recheck, final-directory-fsync, or ownership uncertainty emits a `source-shallow-cas-v1` recovery identity containing directory, expected-state, `shallow`, and lock fingerprints/digests. A stale lock fence is retained whenever recovery ownership remains provable; a durability-unverified committed deletion is reported distinctly. Missing kernel or filesystem support and cross-device results fail closed without a plain-rename fallback. These controls prevent path replacement from redirecting the write and prevent pre-publication lost updates. They do not claim a portable linearizable defense against a malicious same-UID process that ignores `shallow.lock` and mutates entries during post-swap verification; such ambiguity fails closed and retains recoverable objects where ownership can still be established.

SSH transport additionally binds the resolved SSH executable and disables user SSH config, ProxyCommand/ProxyJump, hostname canonicalization, and local commands. Before each fetch it revalidates the superproject receipt, that entry's source access, source config content/object, source shallow state, private control gitdir, and SSH executable when applicable; after all fetches finish, it creates any deferred checkout receipts and performs one complete plan revalidation.

For a new target, the helper first creates every missing directory component relative to held no-follow directory descriptors. It holds the exact final target and direct parent objects through the write. Git registers the worktree with `--no-checkout` from the held target descriptor and names that exact directory as `.`, so a raced symlink at the original final pathname cannot redirect Git's writes. The child also receives an explicit receipt-bound absolute `GIT_COMMON_DIR`, which takes precedence over any `commondir` inserted after preflight and keeps registry writes in the selected source; late policy insertion is still detected and fails postvalidation. Before checkout, the helper descriptor-reads and content-binds the target's `.git` file, requires its admin directory to be one direct child of the selected source's `worktrees/`, content-binds that admin's ordinary `gitdir` backlink, and requires the backlink to resolve exactly to the current target's `.git`. This prevents two worktrees under one source from silently borrowing each other's HEAD/index. It holds no-follow descriptors for both the source and admin entries plus their direct parents. Immediately before `exec`, the child compares the live absolute parent paths, parent descriptors, entry names, and object descriptors against the receipt-bound identity/access-policy tuples. It invokes Git only after those checks, with explicit `--git-dir=<bound-admin> --work-tree=.` plus the common-dir override, so Git does not rediscover a raced `.git` redirect for HEAD/index writes and a parent-observed admin/source replacement fails before Git starts. It revalidates the control receipt, backlink, and final entry after checkout, then revalidates common gitdir, HEAD, source completeness, and the closure digest. Existing managed targets skip registration and use the same explicit bound admin/common control plane and checkout/post-validation from their held target descriptor. A malicious same-UID process that replaces a source/admin path in the residual interval after the child identity check but before stock Git opens that absolute path is outside this portable helper's prevention guarantee; postvalidation still detects it, but POSIX/macOS expose no directory-fd Git interface that can make that interval linearizable. The conceptual registration operation, with the process cwd already anchored to the held target directory, is:

```bash
git --git-dir=/path/to/source/.git/modules/third_party/libexample \
  worktree add --detach --no-checkout \
  . \
  <gitlink-sha>
```

Important details:

- Use detached HEAD. Git does not allow the same branch checked out in multiple worktrees from one repo.
- Use the submodule name for `.git/modules/<name>` and the submodule path for the checkout location.
- Read the expected SHA from the target superproject index, not from the source checkout's current submodule working tree.
- For nested submodules, read `.gitmodules` and gitlink SHAs from the parent source repo at the target commit.
- Passing `--work-tree` is useful when a source `.git/modules/<path>` repo exists but its original `core.worktree` points at a deleted worktree.

## Failure Modes

### Missing Source Repo

The source repo for `.git/modules/<submodule-name>` must already exist. Initialize it in the source checkout first:

```bash
git -C /path/to/source-superproject submodule update --init --depth 1 -- third_party/libexample
```

If automation policy forbids cloning, stop and report the missing source repo.

### Missing Commit In A Shallow Source

The helper first checks:

```bash
git --git-dir=<source-gitdir> --work-tree=<target-path> cat-file -e <sha>^{commit}
```

If the commit is missing, the default is to stop without a network mutation and report the exact planned fetch. The helper may attempt the fetch only when the task explicitly authorizes missing-commit fetches and the invocation includes `--fetch-missing`. The displayed shape below is conceptual; the helper binds the source object directory in its frozen environment, substitutes the receipt's exact URL, and applies its closed transport options:

```bash
GIT_OBJECT_DIRECTORY=<source-gitdir>/objects git --git-dir=<private-control-gitdir> fetch --depth 1 <exact-url> <sha>
```

`--dry-run --fetch-missing` prints this action without executing it. If a missing parent commit prevents recursive `.gitmodules` inspection, both plan-only and apply runs stop before any fetch because the helper cannot prove the complete recursive target set first. Fetch that commit separately and rerun, or explicitly use `--no-recursive --fetch-missing` for the selected parent and then rerun the recursive plan once the object is local. The helper never performs a recursive preflight mutation merely to discover more targets.

Some servers reject raw SHA fetches. In that case, report the failure and request or use separately authorized branch/tag fetch semantics before rerunning:

```bash
GIT_OBJECT_DIRECTORY=<source-gitdir>/objects git --git-dir=<private-control-gitdir> fetch --depth 100 <exact-url> <branch-or-tag>
```

Do not silently unshallow; full history fetches can be much larger than the user intended.

### Existing Target Directory

Safe cases:

- Missing path.
- Empty directory with `--force-replace-empty`.
- Existing linked worktree whose `git rev-parse --git-common-dir` is exactly the intended source gitdir.

Unsafe cases:

- Non-empty directory that is not already a linked worktree for the intended source repo.
- Standard submodule checkout owned by the target superproject.
- Worktree with local changes.

Stop and report rather than deleting or overwriting.

### Ignored Or Filtered Checkout Content

An ignored untracked path that overlaps a managed target write is a preflight error even though ordinary `git status` omits it. Move or preserve that content outside the worktree and rerun; the helper will not ask Git to overwrite it.

A target-tree `filter=<driver>` attribute is also a preflight error. Install/configure the filter separately and use an ordinary repository workflow that intentionally trusts it, or remove the attribute requirement in an authorized source change. This helper never treats a missing optional filter as a safe raw checkout.

### Filesystem Name Aliases

The plan binds name comparison separately at every target's deepest existing parent directory rather than assuming the repository root describes descendant mounts or ext4 casefold directories. Existing components are keyed by device/inode identity; missing suffixes use that anchor's case and Unicode policy. A bounded trie detects aliases and ancestor overlap without a pairwise plan scan; DFS-active ancestor metadata makes each insertion linear in path depth instead of repeatedly walking logical parent links. Every anchor object and policy is revalidated before apply, so a later target's policy drift blocks the first mutation.

On macOS the probe reads the anchor volume's case capability and always uses filesystem-derived canonical Unicode comparison, even when `core.precomposeUnicode=false`. On Linux the helper opens the anchor once with no-follow directory flags, then uses that same descriptor for the actual lookup probe, directory casefold ioctl, and bounded `fstatfs` classification. A lookup proof checks the original entry before and after the alternate-case lookup; deletion, replacement, or unreadability is inconclusive and fails closed. When both exact spellings are enumerated as separate dirents, a shared inode is treated as a hardlink proof of case sensitivity rather than as a case-insensitive alias. An empty directory is accepted as case-sensitive only for a known direct-filesystem/flag combination.

Case evidence does not prove Unicode normalization behavior. Linux uses literal Unicode collision keys only for a separately allowlisted byte-exact filesystem with the directory casefold flag explicitly disabled. An unknown CIFS/FUSE/NFS-like filesystem with a valid existing-entry case lookup uses conservative NFD collision keys, which may reject extra paths but does not rewrite names on disk. An empty unknown filesystem without case evidence, XFS without a filesystem-wide ASCII-CI proof, and every OverlayFS merged directory fail closed before any fetch or target mutation; repository `core.ignoreCase` and `core.precomposeUnicode=false` are never treated as authority for literal names. Git paths containing backslashes, drive prefixes, UNC forms, absolute paths, or unsafe segments fail before target construction, and the lexical target is proved to remain under the selected root.

## Example Large Repo Pattern

This workflow is useful in large linked-worktree setups where:

- The target repo was a linked worktree.
- The source superproject's `.git/modules/<submodule-name>` tree was already populated.
- Many submodule repos were shallow.
- Several top-level submodules had nested submodules.

The reusable lesson is to use the target superproject index for desired gitlink SHAs,
use the source `.git/modules` tree for object storage, and keep the submodule checkouts detached.
