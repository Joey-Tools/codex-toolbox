from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PERSONAL_AGENTS_PATH = REPO_ROOT / "personal_codex" / "AGENTS.md"


class PersonalGuidelinesContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agents = PERSONAL_AGENTS_PATH.read_text(encoding="utf-8")

    def test_spawn_agent_fork_contract_is_documented(self) -> None:
        self.assertIn("When a collaboration spawn call applies", self.agents)
        self.assertIn("do not pair it with the runtime's full-history fork", self.agents)
        self.assertIn("omit the override", self.agents)
        self.assertIn(
            "`fork_turns=\"none\"` or a positive turn count",
            self.agents,
        )
        self.assertIn("`fork_context=false`", self.agents)
        self.assertIn("active tool contract", self.agents)

    def test_agent_thread_limit_contract_is_documented(self) -> None:
        self.assertIn("After an agent-thread or agent-tree capacity error", self.agents)
        self.assertIn("query collaboration status once", self.agents)
        self.assertIn(
            "Do not repeat spawn, follow-up, or resume attempts",
            self.agents,
        )
        self.assertIn(
            "reuse a running owner through the exposed message or input operation",
            self.agents,
        )
        self.assertIn("only when a safe close operation is exposed", self.agents)
        self.assertIn("fresh root task", self.agents)
