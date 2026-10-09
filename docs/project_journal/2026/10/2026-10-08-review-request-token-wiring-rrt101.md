---
id: 20261008-rrt101
title: Review Request Token Wiring
status: completed
created: 2026-10-08
updated: 2026-10-08
branch: codex/review-request-token-rollout
pr:
supersedes: []
superseded_by:
---

# Review Request Token Wiring

## Summary
- The installed v2 review-gate controller passes the optional repository secret `CODEX_REVIEW_GATE_REQUEST_TOKEN` through as the action's `review_request_token` input.

## Current State
- The controller retains `github.token` for its existing GitHub API operations and supplies the distinct request token only through the new action input.
- Events, workflow permissions, concurrency, and the existing operation-selection expressions are unchanged.

## Next Steps
- No further repository changes are needed for this consumer-side wiring.

## Evidence
- `.github/workflows/codex-review-gate-controller.yml`
