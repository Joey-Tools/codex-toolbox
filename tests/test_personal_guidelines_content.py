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
        self.assertIn("When using `spawn_agent`", self.agents)
        self.assertIn("do not combine an `agent_type` override", self.agents)
        self.assertIn("`fork_turns` omitted or `\"all\"`", self.agents)
        self.assertIn("omit `agent_type` to inherit the parent role", self.agents)
        self.assertIn(
            "set `fork_turns` to `\"none\"` or a positive turn count",
            self.agents,
        )

    def test_agent_thread_limit_contract_is_documented(self) -> None:
        self.assertIn("After `agent thread limit reached`", self.agents)
        self.assertIn("inspect `list_agents` once", self.agents)
        self.assertIn(
            "do not repeat the same `spawn_agent` or `followup_task`",
            self.agents,
        )
        self.assertIn(
            "reuse an existing running owner with `send_message`",
            self.agents,
        )
        self.assertIn("fresh root task", self.agents)
