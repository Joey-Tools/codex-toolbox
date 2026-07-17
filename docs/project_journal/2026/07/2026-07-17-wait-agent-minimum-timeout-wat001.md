---
id: 20260717-wat001
title: Wait Agent Timeout Guidance
status: completed
created: 2026-07-17
updated: 2026-07-17
branch: codex/daily-skill-friction-20260717-codex-toolbox-wait-agent-min-timeout
pr:
supersedes: []
superseded_by:
---

# Wait Agent Timeout Guidance

## Summary

- Added public personal AGENTS guidance for the `wait_agent` timeout contract and recommended polling intervals.

## Current State

- Agents omit `timeout_ms` for the `30000` millisecond default or stay within the supported `10000`–`3600000` millisecond range.
- Guidance distinguishes imminent-response polling at `10000` milliseconds from ordinary or reviewer polling at `30000`–`60000` milliseconds.
- Content regression coverage keeps the invariant in public personal packages.

## Next Steps

- Revisit the numeric bounds if the collaboration API contract changes.

## Evidence

- `personal_codex/AGENTS.md`
- `tests/test_codex_personal_sync.py`
