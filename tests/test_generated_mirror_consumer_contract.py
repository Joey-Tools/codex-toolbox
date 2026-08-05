from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import NamedTuple, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "Joey-Tools/codex-personal-sync"
VERIFY_SCRIPT = "scripts/verify_generated_sync_source_lock.py"
COMPILE_COMMAND = "python3 -m compileall -q scripts tests"
DISCOVERY_COMMAND = "python3 -m unittest discover -s tests"
PREPARE_STEP_NAME = "Prepare verified generated snapshot"
PREPARE_STEP_ID = "generated_snapshot"
SNAPSHOT_WORKING_DIRECTORY = "${{ steps.generated_snapshot.outputs.path }}"


class WorkflowStep(NamedTuple):
    name: Optional[str]
    step_id: Optional[str]
    working_directory: Optional[str]
    body: str


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

    def workflow_steps(self, job: str) -> list[WorkflowStep]:
        steps_marker = "    steps:\n"
        self.assertIn(steps_marker, job)
        steps = job.split(steps_marker, maxsplit=1)[1]
        matches = list(re.finditer(r"^      - ", steps, re.MULTILINE))
        parsed = []
        for index, match in enumerate(matches):
            body = steps[
                match.start() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(steps)
            ]
            name_match = re.search(
                r"^(?:      -|        ) name:\s*(.+?)\s*$",
                body,
                re.MULTILINE,
            )
            id_match = re.search(r"^        id:\s*(.+?)\s*$", body, re.MULTILINE)
            working_directory_match = re.search(
                r"^        working-directory:\s*(.+?)\s*$",
                body,
                re.MULTILINE,
            )
            parsed.append(
                WorkflowStep(
                    name=name_match.group(1) if name_match else None,
                    step_id=id_match.group(1) if id_match else None,
                    working_directory=(
                        working_directory_match.group(1)
                        if working_directory_match
                        else None
                    ),
                    body=body,
                )
            )
        return parsed

    def test_repo_guidance_declares_generated_source_ownership(self) -> None:
        guidance = self.read("AGENTS.md")
        self.assertIn(CANONICAL_REPOSITORY, guidance)
        self.assertIn("generated-sync-source-lock.json", guidance)
        self.assertIn("read-only", guidance)

    def test_readme_documents_receipt_bound_generation(self) -> None:
        readme = self.read("README.md")
        self.assertIn(CANONICAL_REPOSITORY, readme)
        self.assertIn("generated-sync-source-lock.json", readme)
        self.assertIn(VERIFY_SCRIPT, readme)
        self.assertIn("--snapshot-root", readme)
        self.assertIn(COMPILE_COMMAND, readme)
        self.assertIn(DISCOVERY_COMMAND, readme)

    def test_ci_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")
        self.assertIn(VERIFY_SCRIPT, workflow)
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        self.assertIn("tests/test_release_retention.py", workflow)
        self.assertIn("tests/test_scheduler_doctor.py", workflow)

    def assert_job_uses_one_verified_snapshot(
        self,
        job: str,
        consumer_step_names: tuple[str, ...],
    ) -> None:
        steps = self.workflow_steps(job)
        checkout_steps = [
            step for step in steps if "uses: actions/checkout@v4" in step.body
        ]
        self.assertEqual(len(checkout_steps), 1)
        self.assertIn("persist-credentials: false", checkout_steps[0].body)
        prepare_steps = [step for step in steps if step.name == PREPARE_STEP_NAME]
        self.assertEqual(len(prepare_steps), 1)
        prepare = prepare_steps[0]
        self.assertEqual(prepare.step_id, PREPARE_STEP_ID)
        self.assertIsNone(prepare.working_directory)
        for required_fragment in (
            "set -euo pipefail",
            'mktemp -d "${RUNNER_TEMP}/generated-mirror-snapshot.XXXXXX"',
            'chmod 0700 "$snapshot_root"',
            'snapshot_root="$(cd "$snapshot_root" && pwd -P)"',
            "clone --no-local --no-hardlinks --no-checkout",
            '"$GITHUB_WORKSPACE" "$snapshot_root"',
            'checkout --detach "$GITHUB_SHA"',
            'rev-parse --verify HEAD)" = "$GITHUB_SHA"',
            "GIT_CONFIG_GLOBAL: /dev/null",
            'GIT_CONFIG_NOSYSTEM: "1"',
            'GIT_LFS_SKIP_SMUDGE: "1"',
            'GIT_NO_LAZY_FETCH: "1"',
            'GIT_TERMINAL_PROMPT: "0"',
            "-c core.hooksPath=/dev/null",
            "-c core.fsmonitor=false",
            "-c submodule.recurse=false",
            VERIFY_SCRIPT,
            '--repo-root "$snapshot_root"',
            '--snapshot-root "$snapshot_root"',
            'printf \'path=%s\\n\' "$snapshot_root" >> "$GITHUB_OUTPUT"',
        ):
            self.assertIn(required_fragment, prepare.body)
        self.assertNotIn("flock", prepare.body)
        ordered_fragments = (
            'mktemp -d "${RUNNER_TEMP}/generated-mirror-snapshot.XXXXXX"',
            'chmod 0700 "$snapshot_root"',
            'snapshot_root="$(cd "$snapshot_root" && pwd -P)"',
            "clone --no-local --no-hardlinks --no-checkout",
            'checkout --detach "$GITHUB_SHA"',
            'rev-parse --verify HEAD)" = "$GITHUB_SHA"',
            VERIFY_SCRIPT,
            'printf \'path=%s\\n\' "$snapshot_root" >> "$GITHUB_OUTPUT"',
        )
        self.assertEqual(
            [prepare.body.index(fragment) for fragment in ordered_fragments],
            sorted(prepare.body.index(fragment) for fragment in ordered_fragments),
        )
        for unique_fragment in ordered_fragments:
            self.assertEqual(prepare.body.count(unique_fragment), 1)
        self.assertEqual(prepare.body.count('--repo-root "$snapshot_root"'), 1)
        self.assertEqual(prepare.body.count('--snapshot-root "$snapshot_root"'), 1)

        prepare_index = steps.index(prepare)
        for earlier_step in steps[:prepare_index]:
            self.assertIsNone(
                re.search(r"^        run:", earlier_step.body, re.MULTILINE)
            )
        for later_step in steps[prepare_index + 1 :]:
            if re.search(r"^        run:", later_step.body, re.MULTILINE):
                self.assertEqual(
                    later_step.working_directory,
                    SNAPSHOT_WORKING_DIRECTORY,
                )

        steps_by_name = {step.name: step for step in steps if step.name is not None}
        for consumer_name in consumer_step_names:
            with self.subTest(consumer=consumer_name):
                self.assertIn(consumer_name, steps_by_name)
                consumer = steps_by_name[consumer_name]
                self.assertGreater(steps.index(consumer), prepare_index)
                self.assertEqual(
                    consumer.working_directory,
                    SNAPSHOT_WORKING_DIRECTORY,
                )

    def test_ci_jobs_use_one_private_snapshot_for_every_consumer(self) -> None:
        jobs = self.workflow_jobs(".github/workflows/ci.yml")
        consumers_by_job = {
            "test": (
                "Compile",
                "Run tests",
                "Validate sync manifest changes",
            ),
            "python-39-compatibility": (
                "Compile supported runtime",
                "Run Python 3.9 compatibility regressions",
            ),
            "platform-safety": ("Run platform personal sync safety tests",),
        }
        self.assertEqual(set(jobs), set(consumers_by_job))

        for job_name, consumer_step_names in consumers_by_job.items():
            with self.subTest(job=job_name):
                self.assert_job_uses_one_verified_snapshot(
                    jobs[job_name],
                    consumer_step_names,
                )

    def test_release_gate_discovers_every_generated_test_module(self) -> None:
        workflow = self.read(".github/workflows/release.yml")
        self.assertEqual(workflow.count(VERIFY_SCRIPT), 2)
        self.assertIn(COMPILE_COMMAND, workflow)
        self.assertIn(DISCOVERY_COMMAND, workflow)
        self.assertEqual(workflow.count('- "generated-sync-source-lock.json"'), 2)
        self.assertEqual(workflow.count('- "schema/**"'), 2)

    def test_release_jobs_use_one_private_snapshot_for_every_consumer(self) -> None:
        jobs = self.workflow_jobs(".github/workflows/release.yml")
        consumers_by_job = {
            "release": (
                "Compile helpers",
                "Run tests",
                "Validate sync manifest changes",
                "Build release package",
                "Verify release package",
            ),
            "publish": (
                "Build release package",
                "Verify release package",
                "Publish GitHub release",
            ),
        }
        self.assertEqual(set(jobs), set(consumers_by_job))
        for job_name, consumer_step_names in consumers_by_job.items():
            with self.subTest(job=job_name):
                self.assert_job_uses_one_verified_snapshot(
                    jobs[job_name],
                    consumer_step_names,
                )


if __name__ == "__main__":
    unittest.main()
