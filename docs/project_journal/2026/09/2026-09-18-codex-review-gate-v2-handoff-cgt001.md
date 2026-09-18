---
id: 20260918-cgt001
title: Codex Review Gate v2 Handoff
status: active
created: 2026-09-18
updated: 2026-09-18
branch: codex/organization-v2-handoff
pr:
supersedes: []
superseded_by:
---

# Codex Review Gate v2 Handoff

## Summary

- Install the canonical v2 verifier and controller while preserving the v1 status producer during the organization-wide dual-protection handoff.

## Current State

- `codex/github-review-gate` is produced by the canonical pull-request verifier using `JoeyTeng/codex-review-gate-action@v2`.
- The controller provides bot-comment and manual-dispatch recovery entry points without changing organization rulesets.
- `codex/review-gate` remains available through the controlled legacy bridge until every migration target has verified v2 production.
- CODEOWNERS protects the workflow control plane under `@JoeyTeng` ownership.

## Next Steps

- Keep the legacy bridge until the organization ruleset requires v2 and no longer requires v1.
- Remove the legacy bridge in a separate cleanup after the organization-wide cutover is verified.

## Evidence

- `.github/workflows/codex-review-gate.yml`
- `.github/workflows/codex-review-gate-controller.yml`
- `.github/workflows/codex-review-gate-legacy-bridge.yml`
- `.github/CODEOWNERS`
