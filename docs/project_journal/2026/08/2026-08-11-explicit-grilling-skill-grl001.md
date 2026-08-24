---
id: 20260811-grl001
title: Explicit Grilling Skill
status: completed
created: 2026-08-11
updated: 2026-08-23
branch: wip/adaptive-grilling-modes
pr: https://github.com/Joey-Tools/codex-toolbox/pull/29
supersedes: []
superseded_by:
---

# Explicit Grilling Skill

## Summary

- Added the third-party `grilling` skill to the public personal release so
  public and layered private installations can consume the same capability.
- Evolved its fixed three-choice batches into adaptive proposal and
  alternatives rounds while preserving the design-tree interview.

## Current State

- The vendored skill is pinned to Matt Pocock's upstream commit
  `1495d014303e041c51c29f9e442485ba06f5878d` with MIT attribution.
- `agents/openai.yaml` rejects implicit invocation; users enter through
  `$grilling` only.
- Each frontier node is routed independently: automatic routing uses a fully
  reasoned proposal only when one direction dominates under settled priorities,
  while genuine tradeoffs and protected decisions default to alternatives.
  Explicit presentation preferences can override that form without fabricating
  dominance or relaxing fact, confirmation, and authorization guardrails.
- Proposal rounds include the concrete problem, relevant facts and assumptions,
  recommendation, tradeoffs, strongest alternative, impact, and reopen
  condition before asking for adoption, revision, or rejection.
- Alternatives rounds have no fixed question or option count. Their width is
  bounded by frontier independence, reading load, decision difficulty, and
  consequence weight rather than picker limits.
- Complete context and reasoning are presented before `request_user_input`,
  which is used only when it can faithfully collect the decision. Structured
  text preserves wider frontiers, option sets, and comparisons with no honest
  leader. Text-rendered questions yield control instead of polling, and empty,
  silent, or timed-out interactions never count as adoption.
- A sole feasible direction uses proposal mode only for a non-protected,
  local or reasonably reversible decision. Protected and high-consequence
  nodes retain their alternatives disclosure unless the user explicitly
  overrides presentation; the override does not change authorization.

## Next Steps

- After each public release, advance the layered private base through an
  explicit reviewed promotion; scheduled non-toolbox sync does not move the
  public toolbox pin.

## Evidence

- https://github.com/mattpocock/skills/tree/1495d014303e041c51c29f9e442485ba06f5878d/skills/productivity/grilling
- `personal_codex/skills/grilling/`
- `personal_codex/skills/grilling/references/interaction-modes.md`
- `personal_codex/public-sync-manifest.json`
- `tests/test_grilling_skill_content.py`
