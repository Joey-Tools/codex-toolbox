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

- Added public personal guidance for invalid collaboration fork shapes and exhausted agent-tree handling across supported runtime schemas.

## Current State

- Full-history forks do not combine with role, model, or reasoning overrides.
- Specialized agents use the active runtime's zero-history or bounded-history field instead of assuming one schema's parameter names.
- An unchanged exhausted agent tree is inspected once rather than receiving repeated spawn, follow-up, or resume attempts.
- Recovery uses only status, messaging, or safe-close operations actually exposed by the active tool contract.
- Consumer-owned regression coverage keeps both invariants in public personal packages without modifying generated Personal Sync tests.

## Next Steps

- Revisit the wording if the collaboration API changes either fork-role compatibility or agent-tree capacity behavior.

## Evidence

- https://github.com/Joey-Tools/codex-toolbox/pull/22
- `personal_codex/AGENTS.md`
- `tests/test_personal_guidelines_content.py`
