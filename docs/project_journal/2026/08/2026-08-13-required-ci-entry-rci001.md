---
id: 20260813-rci001
title: Reusable Required CI Entry
status: completed
created: 2026-08-13
updated: 2026-08-13
branch: codex/daily-skill-friction-20260813-codex-toolbox-codex-review-v2
pr:
supersedes: []
superseded_by:
---

# Reusable Required CI Entry

## Summary

- Added a reusable workflow entry for the repository's effective required CI
  closure.

## Current State

- `.github/workflows/required-ci.yml` accepts only `workflow_call`, keeps
  read-only repository permissions, and runs the existing Linux `test` job.
- Python 3.9 compatibility and cross-platform diagnostics remain outside the
  required closure.
- The existing CI workflow and ruleset are unchanged.

## Next Steps

- None for this repository-local entry; campaign-level routing and ruleset
  activation remain outside this repository.

## Evidence

- `.github/workflows/required-ci.yml`
- `tests/test_required_ci_workflow.py`
- `actionlint .github/workflows/required-ci.yml`
- `python3 -B tests/test_required_ci_workflow.py`
