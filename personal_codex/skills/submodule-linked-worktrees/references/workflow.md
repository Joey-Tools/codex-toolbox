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

The helper checks:

```bash
git --git-dir=<source-gitdir> --work-tree=<target-path> cat-file -e <sha>^{commit}
```

If missing, it attempts:

```bash
git --git-dir=<source-gitdir> --work-tree=<target-path> fetch --depth 1 origin <sha>
```

Some servers reject raw SHA fetches. In that case, fetch a branch or tag that contains the commit, then rerun:

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

## Example Large Repo Pattern

This workflow is useful in large linked-worktree setups where:

- The target repo was a linked worktree.
- The source superproject's `.git/modules/<submodule-name>` tree was already populated.
- Many submodule repos were shallow.
- Several top-level submodules had nested submodules.

The reusable lesson is to use the target superproject index for desired gitlink SHAs,
use the source `.git/modules` tree for object storage, and keep the submodule checkouts detached.
