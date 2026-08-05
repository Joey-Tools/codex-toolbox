from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "Joey-Tools/codex-personal-sync"
VERIFY_COMMAND = "python3 scripts/verify_generated_sync_source_lock.py"
COMPILE_COMMAND = "python3 -m compileall -q scripts tests"
DISCOVERY_COMMAND = "python3 -m unittest discover -s tests"


class GeneratedMirrorConsumerContractTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    def workflow_jobs(self, relative_path: str) -> dict[str, str]:
        workflow = self.read(relative_path)
        jobs = workflow.split("\njobs:\n", maxsplit=1)[1]
        matches = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\s*$", jobs, re.MULTILINE))
        return {
            match.group(1): jobs[
                match.start() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(jobs)
            ]
            for index, match in enumerate(matches)
        }

    def test_repo_guidance_declares_generated_source_ownership(self) -> None:
        guidance = self.read("AGENTS.md")
        self.assertIn(CANONICAL_REPOSITORY, guidance)
        self.assertIn("generated-sync-source-lock.json", guidance)
        self.assertIn("read-only", guidance)

    def test_readme_documents_receipt_bound_generation(self) -> None:
        readme = self.read("README.md")
        self.assertIn(CANONICAL_REPOSITORY, readme)
        self.assertIn("generated-sync-source-lock.json", readme)
        self.assertIn(VERIFY_COMMAND, readme)
        self.assertIn(COMPILE_COMMAND, readme)
        self.assertIn(DISCOVERY_COMMAND, readme)

    def test_ci_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")
        self.assertIn(VERIFY_COMMAND, workflow)
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        self.assertLess(workflow.index(VERIFY_COMMAND), workflow.index(COMPILE_COMMAND))
        self.assertIn("tests/test_release_retention.py", workflow)
        self.assertIn("tests/test_scheduler_doctor.py", workflow)

    def test_ci_receipt_gates_each_generated_code_consumer_job(self) -> None:
        jobs = self.workflow_jobs(".github/workflows/ci.yml")
        consumers_by_job = {
            "test": (COMPILE_COMMAND, DISCOVERY_COMMAND),
            "python-39-compatibility": (
                "python3 -m py_compile",
                "python3 -m unittest",
            ),
            "platform-safety": ('python3 -m unittest "${modules[@]}"',),
        }

        for job_name, consumer_commands in consumers_by_job.items():
            with self.subTest(job=job_name):
                job = jobs[job_name]
                self.assertEqual(job.count(VERIFY_COMMAND), 1)
                verifier_index = job.index(VERIFY_COMMAND)
                for command in consumer_commands:
                    self.assertIn(command, job)
                    self.assertLess(verifier_index, job.index(command))

    def test_release_gate_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/release.yml")
        self.assertEqual(workflow.count(VERIFY_COMMAND), 2)
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        release_job, publish_job = workflow.split("\n  publish:\n", maxsplit=1)
        self.assertLess(
            release_job.index(VERIFY_COMMAND),
            release_job.index("python3 scripts/build_personal_codex_package.py"),
        )
        self.assertLess(
            publish_job.index(VERIFY_COMMAND),
            publish_job.index("python3 scripts/build_personal_codex_package.py"),
        )
        self.assertEqual(workflow.count('- "generated-sync-source-lock.json"'), 2)
        self.assertEqual(workflow.count('- "schema/**"'), 2)


if __name__ == "__main__":
    unittest.main()
