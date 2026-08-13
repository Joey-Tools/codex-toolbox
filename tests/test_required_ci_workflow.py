from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def top_level_job_ids(workflow: str) -> list[str]:
    in_jobs = False
    job_ids: list[str] = []
    for line in workflow.splitlines():
        if line == "jobs:":
            in_jobs = True
            continue
        if in_jobs and line and not line.startswith(" "):
            break
        if (
            in_jobs
            and line.startswith("  ")
            and not line.startswith("    ")
            and line.endswith(":")
        ):
            job_ids.append(line[2:-1])
    return job_ids


def checkout_steps(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    steps: list[str] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("- uses: actions/checkout@"):
            continue
        indent = len(line) - len(line.lstrip())
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and (
                candidate_indent < indent
                or (
                    candidate_indent == indent
                    and candidate.lstrip().startswith("- ")
                )
            ):
                break
            end += 1
        steps.append("\n".join(lines[index:end]))
    return steps


class RequiredCiWorkflowTests(unittest.TestCase):
    def test_entry_wraps_only_the_required_linux_test(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/required-ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      repository:\n"
            "        required: true\n"
            "        type: string\n"
            "      ref:\n"
            "        required: true\n"
            "        type: string\n"
            "\n"
            "permissions:\n",
            workflow,
        )
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertEqual(top_level_job_ids(workflow), ["test"])
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        checkout = checkout_steps(workflow)
        self.assertGreater(len(checkout), 0)
        self.assertEqual(
            workflow.count("repository: ${{ inputs.repository }}"), len(checkout)
        )
        self.assertEqual(workflow.count("ref: ${{ inputs.ref }}"), len(checkout))
        self.assertEqual(workflow.count("persist-credentials: false"), len(checkout))
        for step in checkout:
            self.assertIn("repository: ${{ inputs.repository }}", step)
            self.assertIn("ref: ${{ inputs.ref }}", step)
            self.assertEqual(step.count("persist-credentials: false"), 1)
        self.assertIn(
            'clone --no-local --no-hardlinks --no-checkout \\',
            workflow,
        )
        self.assertIn(
            'checkout --detach "$GITHUB_SHA"',
            workflow,
        )
        self.assertIn(
            'python3 "$snapshot_root/scripts/verify_generated_sync_source_lock.py"',
            workflow,
        )
        self.assertIn("python3 -m compileall -q scripts tests", workflow)
        self.assertIn("python3 -m unittest discover -s tests", workflow)
        self.assertIn(
            'python3 scripts/validate_sync_manifest_changes.py --base-ref "$BASE_REF"',
            workflow,
        )
        for forbidden in (
            "pull_request:",
            "pull_request_target:",
            "push:",
            "python-39-compatibility",
            "platform-safety",
            "macos-latest",
            "secrets.",
            "contents: write",
            "id-" + "token: write",
            "statuses: write",
        ):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
