from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WIP_BRANCH_HELPER = REPO_ROOT / "personal_codex" / "bin" / "codex-wip-branch"
SQUASH_MERGE_HELPER = REPO_ROOT / "personal_codex" / "bin" / "codex-squash-merge-wip"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def git_commit(repo: Path, message: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            message,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


class CodexGitHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="codex-git-helper.")
        self.repo = Path(self.tmpdir.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        git_commit(self.repo, "init")
        self.default_branch = git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def run_helper(
        self,
        helper: Path,
        *args: str,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(helper), *args],
            cwd=cwd or self.repo,
            check=False,
            text=True,
            capture_output=True,
        )

    def test_wip_branch_helper_creates_prefixed_branch(self) -> None:
        result = self.run_helper(WIP_BRANCH_HELPER, "review-range")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "wip/review-range")
        self.assertEqual(
            git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "wip/review-range",
        )

    def test_wip_branch_helper_rejects_prefixed_topic(self) -> None:
        result = self.run_helper(WIP_BRANCH_HELPER, "wip/already-prefixed")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not include the wip/ prefix", result.stderr)

    def test_squash_merge_helper_switches_target_and_stages_changes(self) -> None:
        self.assertEqual(self.run_helper(WIP_BRANCH_HELPER, "review-range").returncode, 0)
        (self.repo / "file.txt").write_text("feature\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        git_commit(self.repo, "feature change")
        wip_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.run_helper(SQUASH_MERGE_HELPER, self.default_branch)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"squash-ready wip/review-range -> {self.default_branch}", result.stdout)
        self.assertEqual(
            git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            self.default_branch,
        )
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), wip_head)
        staged = git(self.repo, "diff", "--cached", "--name-only")
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertIn("file.txt", staged.stdout.splitlines())

    def test_squash_merge_helper_rejects_non_wip_source(self) -> None:
        self.assertEqual(git(self.repo, "switch", "-c", "feature/test").returncode, 0)

        result = self.run_helper(SQUASH_MERGE_HELPER, self.default_branch)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source branch must start with wip/", result.stderr)

    def test_squash_merge_helper_rejects_missing_source_before_switch(self) -> None:
        result = self.run_helper(
            SQUASH_MERGE_HELPER,
            self.default_branch,
            "wip/does-not-exist",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source branch does not exist", result.stderr)
        self.assertEqual(
            git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            self.default_branch,
        )

    def test_squash_merge_helper_uses_existing_target_worktree(self) -> None:
        wip_worktree = Path(self.tmpdir.name) / "wip-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-b",
                "wip/review-range",
                str(wip_worktree),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        (wip_worktree / "file.txt").write_text("feature\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=wip_worktree, check=True)
        git_commit(wip_worktree, "feature change")

        result = self.run_helper(
            SQUASH_MERGE_HELPER,
            self.default_branch,
            cwd=wip_worktree,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"squash-ready wip/review-range -> {self.default_branch}", result.stdout)
        self.assertEqual(
            git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            self.default_branch,
        )
        self.assertEqual(
            git(wip_worktree, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "wip/review-range",
        )
        staged = git(self.repo, "diff", "--cached", "--name-only")
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertIn("file.txt", staged.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
