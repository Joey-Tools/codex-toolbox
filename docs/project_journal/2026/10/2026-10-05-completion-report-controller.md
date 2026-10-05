---
id: 20261005-crc101
title: Completion Report Controller Update
status: completed
created: 2026-10-05
updated: 2026-10-05
branch: codex/completion-report-v217
pr:
supersedes: []
superseded_by:
---

# Completion Report Controller Update

## Summary
- Updated the installed controller to the hardened canonical template with two focused protections: per-run concurrency fallback for completions without a PR association, and a narrower ref-qualified workflow-path guard.

## Current State
- For eligible canonical verifier completion events, the controller uses `report-completion` only to update diagnostic metadata. This snapshot is not review evidence and has no authority over required check statuses.
- The opt-in `CODEX_REVIEW_GATE_AUTO_REQUEST` path remains limited to an exact opt-in, the first failed attempt, and one uniquely associated PR. Other events do not request a review.
- PR-associated concurrency grouping is unchanged. When PR, issue, and dispatch-input keys are absent, `workflow_run.id` with `github.run_id` as the final fallback prevents unrelated completion runs from sharing the empty group; `cancel-in-progress: false` is unchanged. This avoids replacing pending diagnostics for unrelated no-association runs.
- The path filter accepts the exact verifier path or a ref-qualified path beginning with `@refs/pull/` and ending with `/merge`, preserving the tested pull-request ref form while rejecting arbitrary `@` lookalikes. Runtime identity validation is unchanged.
- The verifier, `CODEOWNERS`, ruleset, repository variables, permission values, workflow triggers, and runner configuration are unchanged. This entry does not claim a live rollout, merge, or canary execution.

## Next Steps
- No additional repository files are in scope for this controller update. Live rollout status must be established through an authorized delivery and its actual runtime evidence.

## Evidence
- Action v2.1.7 is published. Canonical controller: `templates/codex-gated-repo/.github/workflows/codex-review-gate-controller.yml`.
- `cmp` confirmed the installed controller matches the hardened canonical template. The source bootstrap `--prepare-worktree` dry-run for this worktree reported no changes to the controller, verifier, or `CODEOWNERS`.
- `actionlint` 1.7.12 passed on the controller and verifier workflows; `git diff --check` passed.
- The project-journal `validate --repo` command passed. No online rollout, merge, or canary validation was performed.
