---
id: 20260715-bco002
title: Bounded Command Output Invariant
status: completed
created: 2026-07-15
updated: 2026-07-15
branch: codex/daily-skill-friction-20260715-codex-toolbox-bounded-command-output-invariant
pr:
supersedes: []
superseded_by:
---

# Bounded Command Output Invariant

## Summary

- Replaced command-specific output recipes in the public personal AGENTS guidance with one self-contained invariant.

## Current State

- Public installs remain safe even when the separate `bounded-command-output` skill is not installed.
- Concrete search, log, process, and build recipes live in the canonical workflow-hygiene skill instead of the public AGENTS file.

## Next Steps

- Keep command-specific recipes in the canonical workflow-hygiene skill rather than growing this public AGENTS file again.

## Evidence

- `personal_codex/AGENTS.md`
- `tests/test_codex_personal_sync.py`
