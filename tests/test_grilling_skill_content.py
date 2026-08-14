from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "personal_codex" / "skills" / "grilling"


class GrillingSkillContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.skill_words = " ".join(cls.skill.split())
        cls.metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )

    def test_skill_is_explicit_only(self) -> None:
        self.assertIn(
            "Use only when the user explicitly invokes `$grilling`.",
            self.skill_words,
        )
        self.assertIn(
            'default_prompt: "Use $grilling to stress-test this plan',
            self.metadata,
        )
        self.assertIn("allow_implicit_invocation: false", self.metadata)

    def test_interactive_contract_is_documented(self) -> None:
        self.assertIn("use it for every decision batch", self.skill_words)
        self.assertIn(
            "exactly three mutually exclusive choices", self.skill_words
        )
        self.assertIn(
            "The client supplies the free-form answer path", self.skill_words
        )
        self.assertIn(
            "Do not set or describe a timeout, countdown", self.skill_words
        )
        self.assertIn(
            "In Default mode, use the text fallback below", self.skill_words
        )
        self.assertIn(
            "Treat an empty `answers` object as unanswered", self.skill_words
        )
        self.assertNotIn(
            "Wait indefinitely for the user's response", self.skill_words
        )
        self.assertIn("After every answer batch, recompute", self.skill_words)

    def test_public_manifest_distributes_skill(self) -> None:
        manifest = json.loads(
            (REPO_ROOT / "personal_codex" / "public-sync-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            {
                "source": "personal_codex/skills/grilling",
                "target": "skills/grilling",
                "kind": "skill",
            },
            manifest["links"],
        )
