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

For an apply invocation, the helper first resolves gitlink SHAs and preflights every selected target, including recursive descendants, before changing a target worktree. A failure in that phase prevents the apply phase from starting. `--dry-run` stops after the plan/preflight. When the task already names the exact paths and the preflight succeeds, the apply phase does not need a separate confirmation solely because the plan was printed.

For every existing managed worktree, the preflight binds its detached HEAD and semantic index snapshot, derives the bounded `HEAD..target` write set with renames disabled, and binds the identity and access policy of each existing object and parent needed by that write set. It checks only write-relevant ignored prefixes and descendants rather than enumerating unrelated untracked content, then runs a no-update `read-tree` probe. The actual checkout repeats the decisive checks and uses `--no-overwrite-ignore --no-recurse-submodules`.

Before any tracked-status query or target-tree materialization, the helper enumerates current and target blobs with non-converting `ls-tree`, evaluates each tree's `filter` attributes with `check-attr --source=<sha>`, and inspects local `filter.*.clean` / `filter.*.process` configuration. Any selected driver—including `lfs`, a required driver, or a non-required driver—is blocked. The later tracked-only status query pins `GIT_ATTR_SOURCE` to the already-inspected current commit; checkout pins it to the inspected target. The helper deliberately has no trusted filter allowlist, so neither status nor checkout can execute an unreviewed repository filter.

Before the first repository read, the helper resolves one absolute regular-file Git executable, requires `>=2.45.0`, records its object/access-policy fingerprint, and revalidates that identity before every invocation. This gate makes `GIT_NO_LAZY_FETCH=1` part of the supported runtime contract rather than an ignored hint on an old Git. Native Windows is rejected before Git discovery because this helper's path and process contracts are POSIX-only.

All new Git inventories have hard deadlines and aggregate retained-byte, input-byte, entry-count, path-length, pathspec-batch, and access-binding ceilings. Status excludes untracked enumeration, worktree registry reads have explicit record caps, and ignored-path probes share one deadline and retained-byte budget. Process cleanup uses bounded TERM/KILL grace and bounded direct-child reap; failure to reap is `cleanup-incomplete`, never an unbounded final wait. An authorized non-recursive fetch first revalidates only that entry's original source binding; after all fetches finish, the helper creates any deferred checkout receipts and performs one complete plan revalidation. It does not rerun the full plan once per fetch.

The core operation is:

```bash
git --git-dir=/path/to/source/.git/modules/third_party/libexample \
  --work-tree=/path/to/target/third_party/libexample \
  worktree add --detach \
  /path/to/target/third_party/libexample \
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

If the commit is missing, the default is to stop without a network mutation and report the exact planned fetch. The helper may attempt the fetch only when the task explicitly authorizes missing-commit fetches and the invocation includes `--fetch-missing`:

```bash
git --git-dir=<source-gitdir> --work-tree=<target-path> fetch --depth 1 origin <sha>
```

`--dry-run --fetch-missing` prints this action without executing it. If a missing parent commit prevents recursive `.gitmodules` inspection, both plan-only and apply runs stop before any fetch because the helper cannot prove the complete recursive target set first. Fetch that commit separately and rerun, or explicitly use `--no-recursive --fetch-missing` for the selected parent and then rerun the recursive plan once the object is local. The helper never performs a recursive preflight mutation merely to discover more targets.

Some servers reject raw SHA fetches. In that case, report the failure and request or use separately authorized branch/tag fetch semantics before rerunning:

```bash
git --git-dir=<source-gitdir> --work-tree=<target-path> fetch --depth 100 origin <branch-or-tag>
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

The plan binds name comparison separately at every target's deepest existing parent directory rather than assuming the repository root describes descendant mounts or ext4 casefold directories. Existing components are keyed by device/inode identity; missing suffixes use that anchor's case and Unicode policy. A bounded trie detects aliases and ancestor overlap without a pairwise plan scan. Every anchor object and policy is revalidated before apply, so a later target's policy drift blocks the first mutation.

On macOS the probe reads the anchor volume's case capability and uses canonical Unicode comparison. On Linux it combines an actual directory lookup probe with the ext4 casefold flag; repository `core.ignoreCase` / `core.precomposeUnicode` is only a fallback when direct evidence is unavailable. Git paths containing backslashes, drive prefixes, UNC forms, absolute paths, or unsafe segments fail before target construction, and the lexical target is proved to remain under the selected root.

## Example Large Repo Pattern

This workflow is useful in large linked-worktree setups where:

- The target repo was a linked worktree.
- The source superproject's `.git/modules/<submodule-name>` tree was already populated.
- Many submodule repos were shallow.
- Several top-level submodules had nested submodules.

The reusable lesson is to use the target superproject index for desired gitlink SHAs,
use the source `.git/modules` tree for object storage, and keep the submodule checkouts detached.
