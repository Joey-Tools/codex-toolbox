---
id: 20260717-wat001
title: Wait Agent Minimum Timeout
status: completed
created: 2026-07-17
updated: 2026-07-17
branch: codex/daily-skill-friction-20260717-codex-toolbox-wait-agent-min-timeout
pr:
supersedes: []
superseded_by:
---

# Wait Agent Minimum Timeout

## Summary

- Added a public personal AGENTS invariant for the `wait_agent` minimum timeout.

## Current State

- Agents omit `timeout_ms` or use at least `10000` milliseconds when polling collaborators.
- Content regression coverage keeps the invariant in public personal packages.

## Next Steps

- Prefer a tool-side clamp if the collaboration API later provides one.

## Evidence

- `personal_codex/AGENTS.md`
- `tests/test_codex_personal_sync.py`
