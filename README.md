# codex-toolbox

Public Codex release tooling and small helper binaries.

The personal sync runtime and release validation tooling support Python 3.9 or newer.

## Test

```bash
python3 -m py_compile \
  scripts/codex_personal_sync.py \
  scripts/build_personal_codex_package.py \
  scripts/validate_sync_manifest_changes.py \
  tests/test_codex_personal_sync.py \
  tests/test_package_builder_safety.py \
  tests/test_personal_sync_reconciliation_safety.py \
  tests/test_release_manifest_baseline.py \
  tests/test_sync_manifest_changes.py \
  tests/test_submodule_worktree_sync.py

python3 -m unittest \
  tests.test_codex_personal_sync \
  tests.test_codex_clean_tmp \
  tests.test_codex_git_helpers \
  tests.test_package_builder_safety \
  tests.test_personal_sync_reconciliation_safety \
  tests.test_release_manifest_baseline \
  tests.test_sync_manifest_changes \
  tests.test_submodule_worktree_sync
```

## Release

`Public Toolbox Release` validates pull requests and publishes `personal-codex-*`
release assets from `master`. This public release owns the sync runner, small
helper binaries, and public skills:

- `personal-codex-<full-sha>.tar.gz`
- `personal-codex-<full-sha>.sha256`

Publishing requires the repository's immutable Releases setting to remain
enabled. Configure the `IMMUTABLE_RELEASES_READ_TOKEN` Actions secret with a
fine-grained personal access token that has repository `Administration` read
permission. The workflow uses this token only to verify that setting; Release
mutations continue to use the built-in `GITHUB_TOKEN` with `contents: write`.

Public-only machines install this repo directly:

```bash
python3 scripts/codex_personal_sync.py install \
  --repo Joey-Tools/codex-toolbox \
  --home "$HOME/.codex" \
  --dry-run
```

Private machines should bootstrap this public runner once, then use
`Joey-Tools/codex-private-workflows` with `install-private` or scheduler
`--mode private`.

## Managed link state

The installer records links it owns in
`~/.codex/personal-sync/state/managed-links.json`. Manifests remain version 1
and must add an append-only `removed_links` entry whenever a managed target is
retired or moved to another owner:

```json
{
  "removed_links": [
    {
      "id": "2026-07-15-remove-example-skill",
      "source": "personal_codex/skills/example-skill",
      "target": "skills/example-skill",
      "kind": "skill"
    }
  ]
}
```

Use a new stable `id` for each removal episode, including a second removal
after a target was reintroduced. `legacy: true` is reserved for an exact old
link that has no matching prior manifest entry. `replacement_target` records
a rename or cross-owner move. During the first installed transition, the
combined release set must provide that target before the old link can be
removed. A separate removal record for the replacement marks its later
retirement by listing the original `owner:id` key in
`retires_replacements`. This explicit relationship allows machines that
skipped the intermediate release to converge without confusing an older
removal episode with the replacement's retirement.

Layered installs reconcile the public and private manifests as one desired
state. On first use, exact links from valid local Release manifests are adopted
into the ownership ledger. Once that ledger exists, a matching local symlink is
not adopted unless it is already recorded or the current transaction creates or
replaces it. A legacy symlink is only quarantined when its owner,
source, and target exactly match a `removed_links` entry. Regular files,
directories, symlink parents, and unrelated symlinks remain untouched. The
same reconciliation path updates the ledger after install, rollback, and
overlay uninstall, and a retry converges after an interrupted verification.
New replacement links are created and verified before obsolete links are
removed. Manifest targets use a portable Unicode and case-folded identity, so
case-only aliases, Unicode spelling aliases, and variants of the reserved
`personal-sync/**` state, Release, pointer, or quarantine paths are rejected.

Release CI compares the current manifest with the most recent complete GitHub
Release and fails closed when that baseline cannot be authenticated locally.
It also batch-loads every authenticated complete Release manifest and rejects
target hierarchy or transaction-capacity failures for clients that skip one or
more intermediate Releases.
It requires a new `removed_links` entry whenever an active `source` / `target`
/ `kind` identity disappears, including a source or kind change at the same
target. Existing removal history must remain unchanged so machines that skip
releases can still converge. Release builds use `--require-clean-sources` to
bind the requested package SHA to `HEAD` and require every packaged file to
match a regular stage-zero index entry from that commit. They also reject
untracked files, source paths with symlink ancestors, Git submodule `gitlink`
entries, and nested `.git` metadata. Runtime installs do not assume a Git
checkout; local preservation decisions come from the validated ledger,
installed Release manifests, and explicit removal history.
