from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "personal_codex"
    / "skills"
    / "submodule-linked-worktrees"
    / "scripts"
    / "submodule_worktree_sync.py"
)
SPEC = importlib.util.spec_from_file_location("submodule_worktree_sync", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class SubmoduleWorktreeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="submodule-worktree-sync.")
        self.root = Path(self.tmpdir.name)

        self.remote = self.root / "remote"
        self.standard = self.root / "standard"
        self.source_git_dir = self.root / "super" / ".git" / "modules" / "third_party" / "libexample"
        self.named_common_git_dir = self.root / "named-super" / ".git"
        self.named_source_git_dir = self.named_common_git_dir / "modules" / "custom-lib"
        self.linked = self.root / "linked"

        run_git(self.root, "init", str(self.remote))
        run_git(self.remote, "config", "user.email", "test@example.com")
        run_git(self.remote, "config", "user.name", "Test User")
        (self.remote / "README.md").write_text("example\n", encoding="utf-8")
        run_git(self.remote, "add", "README.md")
        run_git(self.remote, "commit", "-m", "init")
        self.sha = run_git(self.remote, "rev-parse", "HEAD")

        self.source_git_dir.parent.mkdir(parents=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(self.source_git_dir),
            str(self.remote),
            str(self.standard),
        )
        self.named_source_git_dir.parent.mkdir(parents=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(self.named_source_git_dir),
            str(self.remote),
            str(self.root / "named-standard"),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_script_imports_future_annotations(self) -> None:
        first_lines = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()[:4]

        self.assertIn("from __future__ import annotations", first_lines)

    def test_parse_gitmodules_rejects_unsafe_path(self) -> None:
        content = """
[submodule "custom-lib"]
    path = third_party/../libexample
    url = https://example.invalid/libexample.git
"""

        with self.assertRaisesRegex(MODULE.PlanError, "unsafe path segment"):
            MODULE.parse_gitmodules(content, ".gitmodules")

    def test_parse_gitmodules_rejects_unsafe_name(self) -> None:
        content = """
[submodule "../custom-lib"]
    path = third_party/libexample
    url = https://example.invalid/libexample.git
"""

        with self.assertRaisesRegex(MODULE.PlanError, "unsafe path segment"):
            MODULE.parse_gitmodules(content, ".gitmodules")

    def test_source_gitdir_rejects_symlink_escape(self) -> None:
        common_git_dir = self.root / "escape-super" / ".git"
        modules_dir = common_git_dir / "modules"
        outside = self.root / "outside-gitdir"
        modules_dir.mkdir(parents=True)
        outside.mkdir()
        (modules_dir / "custom-lib").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(MODULE.PlanError, "source gitdir.*escapes"):
            MODULE.source_git_dir_for(common_git_dir, "custom-lib")

    def test_worktree_path_rejects_symlink_escape(self) -> None:
        target_super = self.root / "target-super"
        outside = self.root / "outside-worktree"
        target_super.mkdir()
        outside.mkdir()
        (target_super / "escape").symlink_to(outside, target_is_directory=True)
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="escape/libexample",
            url=str(self.remote),
        )

        with self.assertRaisesRegex(MODULE.PlanError, "worktree path.*escapes"):
            MODULE.sync_one(
                root=self.root,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                parent_source_git_dir=None,
                parent_root=target_super,
                submodule=submodule,
                sha=self.sha,
                depth=1,
                recursive=False,
                force_replace_empty=False,
                dry_run=True,
            )

    def test_expected_sha_rejects_unmerged_index_entries(self) -> None:
        original_git = MODULE.git

        def fake_git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
            self.assertEqual(args[:4], ["ls-files", "-s", "--", "third_party/libexample"])
            return "\n".join(
                [
                    f"160000 {'a' * 40} 1\tthird_party/libexample",
                    f"160000 {'b' * 40} 2\tthird_party/libexample",
                    f"160000 {'c' * 40} 3\tthird_party/libexample",
                ]
            )

        try:
            MODULE.git = fake_git
            with self.assertRaisesRegex(MODULE.PlanError, "unresolved index entries"):
                MODULE.expected_sha(self.root, "third_party/libexample")
        finally:
            MODULE.git = original_git

    def test_expected_sha_rejects_nonzero_index_stage(self) -> None:
        original_git = MODULE.git

        def fake_git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
            self.assertEqual(args[:4], ["ls-files", "-s", "--", "third_party/libexample"])
            return f"160000 {'a' * 40} 2\tthird_party/libexample"

        try:
            MODULE.git = fake_git
            with self.assertRaisesRegex(MODULE.PlanError, "unresolved index stage 2"):
                MODULE.expected_sha(self.root, "third_party/libexample")
        finally:
            MODULE.git = original_git

    def test_default_common_git_dir_does_not_suggest_target_submodule_update(self) -> None:
        args = type(
            "Args",
            (),
            {
                "source_common_git_dir": None,
                "source_superproject": None,
            },
        )()

        source_common_git_dir, source_superproject = MODULE.choose_source_common_git_dir(args, self.remote)

        self.assertEqual(source_common_git_dir, (self.remote / ".git").resolve())
        self.assertIsNone(source_superproject)

    def test_standard_separate_gitdir_checkout_is_not_managed(self) -> None:
        with self.assertRaises(MODULE.PlanError):
            MODULE.prepare_target_path(
                self.standard,
                self.source_git_dir,
                force_replace_empty=False,
                dry_run=True,
            )

    def test_linked_worktree_is_managed(self) -> None:
        run_git(
            self.root,
            f"--git-dir={self.source_git_dir}",
            f"--work-tree={self.linked}",
            "worktree",
            "add",
            "--detach",
            str(self.linked),
            self.sha,
        )

        state = MODULE.prepare_target_path(
            self.linked,
            self.source_git_dir,
            force_replace_empty=False,
            dry_run=True,
        )

        self.assertEqual(state, "managed")

    def test_sync_uses_submodule_name_for_source_gitdir(self) -> None:
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            MODULE.sync_one(
                root=self.root,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                parent_source_git_dir=None,
                parent_root=self.root / "target-super",
                submodule=submodule,
                sha=self.sha,
                depth=1,
                recursive=False,
                force_replace_empty=False,
                dry_run=True,
            )

        self.assertIn(".git/modules/custom-lib", output.getvalue())

    def test_recursive_dry_run_stops_when_target_commit_is_missing(self) -> None:
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            with self.assertRaisesRegex(MODULE.PlanError, "cannot plan nested submodules"):
                MODULE.sync_one(
                    root=self.root,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    parent_source_git_dir=None,
                    parent_root=self.root / "target-super",
                    submodule=submodule,
                    sha="f" * 40,
                    depth=1,
                    recursive=True,
                    force_replace_empty=False,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
