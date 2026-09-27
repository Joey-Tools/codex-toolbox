---
id: 20260918-cgt001
title: Codex Review Gate v2 Handoff
status: completed
created: 2026-09-18
updated: 2026-09-27
branch: codex/organization-v2-handoff
pr:
supersedes: []
superseded_by:
---

# Codex Review Gate v2 Handoff

## Summary

- The canonical v2 verifier and controller replaced the v1 status producer after the organization-wide cutover.

## Current State

- `codex/github-review-gate` is produced by the canonical pull-request verifier using `JoeyTeng/codex-review-gate-action@v2`.
- The controller provides bot-comment and manual-dispatch recovery entry points without changing organization rulesets.
- The v1 `codex/review-gate` legacy bridge has been removed; this repository now exposes only the v2 review-gate control plane.
- CODEOWNERS protects the workflow control plane under `@JoeyTeng` ownership.

## Evidence

- The restored post-cutover receipt was verified at its exact official SHA: `9a8b38f2188a14168423a07639d6662c87e198fe2dd12041f67fc224f363817e`.
- Receipt restoration verification completed before removing `.github/workflows/codex-review-gate-legacy-bridge.yml`.
- `.github/workflows/codex-review-gate.yml`
- `.github/workflows/codex-review-gate-controller.yml`
- `.github/CODEOWNERS`
