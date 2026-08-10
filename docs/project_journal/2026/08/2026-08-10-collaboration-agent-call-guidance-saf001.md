---
id: 20260810-saf001
title: Collaboration Agent Call Guidance
status: completed
created: 2026-08-10
updated: 2026-08-10
branch: codex/daily-skill-friction-20260810-agent-collaboration-guardrails
pr: 22
supersedes: []
superseded_by:
---

# Collaboration Agent Call Guidance

## Summary

- Added public personal guidance for invalid `spawn_agent` fork shapes and exhausted agent-tree handling.

## Current State

- Full-history forks inherit the parent agent role and omit `agent_type`.
- Specialized agent roles use `fork_turns: "none"` or a positive inherited-turn count.
- An unchanged exhausted agent tree is inspected once rather than receiving repeated `spawn_agent` or `followup_task` attempts.
- Consumer-owned regression coverage keeps both invariants in public personal packages without modifying generated Personal Sync tests.

## Next Steps

- Revisit the wording if the collaboration API changes either fork-role compatibility or agent-tree capacity behavior.

## Evidence

- https://github.com/Joey-Tools/codex-toolbox/pull/22
- `personal_codex/AGENTS.md`
- `tests/test_personal_guidelines_content.py`
