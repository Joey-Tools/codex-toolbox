from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "Joey-Tools/codex-personal-sync"
COMPILE_COMMAND = "python3 -m compileall -q scripts tests"
DISCOVERY_COMMAND = "python3 -m unittest discover -s tests"


class GeneratedMirrorConsumerContractTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    def test_repo_guidance_declares_generated_source_ownership(self) -> None:
        guidance = self.read("AGENTS.md")
        self.assertIn(CANONICAL_REPOSITORY, guidance)
        self.assertIn("generated-sync-source-lock.json", guidance)
        self.assertIn("read-only", guidance)

    def test_readme_documents_receipt_bound_generation(self) -> None:
        readme = self.read("README.md")
        self.assertIn(CANONICAL_REPOSITORY, readme)
        self.assertIn("generated-sync-source-lock.json", readme)
        self.assertIn(COMPILE_COMMAND, readme)
        self.assertIn(DISCOVERY_COMMAND, readme)

    def test_ci_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        self.assertIn("tests/test_release_retention.py", workflow)
        self.assertIn("tests/test_scheduler_doctor.py", workflow)

    def test_release_gate_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/release.yml")
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        self.assertEqual(workflow.count('- "generated-sync-source-lock.json"'), 2)
        self.assertEqual(workflow.count('- "schema/**"'), 2)


if __name__ == "__main__":
    unittest.main()
