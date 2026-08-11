---
id: 20260811-grl001
title: Explicit Grilling Skill
status: completed
created: 2026-08-11
updated: 2026-08-11
branch: wip/grilling-skill
pr:
supersedes: []
superseded_by:
---

# Explicit Grilling Skill

## Summary

- Added the third-party `grilling` skill to the public personal release so
  public and layered private installations can consume the same capability.

## Current State

- The vendored skill is pinned to Matt Pocock's upstream commit
  `1495d014303e041c51c29f9e442485ba06f5878d` with MIT attribution.
- `agents/openai.yaml` rejects implicit invocation; users enter through
  `$grilling` only.
- Interactive rounds prefer `request_user_input`, expose three recommended or
  alternative choices plus the client's free-form path, and never auto-resolve
  on a timer.
- Frontiers larger than three questions are batched and recomputed after every
  response.

## Next Steps

- Follow the ordinary public release and layered private-base promotion flow
  when future upstream revisions are intentionally adopted.

## Evidence

- https://github.com/mattpocock/skills/tree/1495d014303e041c51c29f9e442485ba06f5878d/skills/productivity/grilling
- `personal_codex/skills/grilling/`
- `personal_codex/public-sync-manifest.json`
- `tests/test_grilling_skill_content.py`
