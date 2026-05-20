# codex-toolbox

Public Codex release tooling and small helper binaries.

## Test

```bash
python3 -m py_compile \
  scripts/codex_personal_sync.py \
  scripts/build_personal_codex_package.py \
  tests/test_codex_personal_sync.py

python3 -m unittest tests.test_codex_personal_sync tests.test_codex_clean_tmp tests.test_codex_git_helpers
```

## Release

`Public Toolbox Release` validates pull requests and publishes `personal-codex-*`
release assets from `master`. This public release owns the sync runner and small
helper binaries:

- `personal-codex-<full-sha>.tar.gz`
- `personal-codex-<full-sha>.sha256`

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
