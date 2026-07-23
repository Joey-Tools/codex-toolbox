from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
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

    def clone_named_source(self, name: str) -> Path:
        source_git_dir = self.named_common_git_dir / "modules" / name
        source_git_dir.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(source_git_dir),
            str(self.remote),
            str(self.root / f"{name}-standard"),
        )
        return source_git_dir

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

    def test_parse_gitmodules_rejects_duplicate_paths(self) -> None:
        content = """
[submodule "first"]
    path = third_party/libexample
    url = https://example.invalid/first.git
[submodule "second"]
    path = third_party/libexample
    url = https://example.invalid/second.git
"""

        with self.assertRaisesRegex(MODULE.PlanError, "duplicate submodule path"):
            MODULE.parse_gitmodules(content, ".gitmodules")

    def test_filter_submodules_requires_explicit_paths_or_all(self) -> None:
        modules = [
            MODULE.Submodule(
                name="custom-lib",
                path="third_party/libexample",
                url=str(self.remote),
            )
        ]

        with self.assertRaisesRegex(MODULE.PlanError, "explicit top-level paths or --all"):
            MODULE.filter_submodules(modules, [])

    def test_filter_submodules_rejects_empty_path(self) -> None:
        modules = [
            MODULE.Submodule(
                name="custom-lib",
                path="third_party/libexample",
                url=str(self.remote),
            )
        ]

        with self.assertRaisesRegex(MODULE.PlanError, "must not be empty"):
            MODULE.filter_submodules(modules, [""])

    def test_filter_submodules_requires_exclusive_all_selection(self) -> None:
        modules = [
            MODULE.Submodule(
                name="custom-lib",
                path="third_party/libexample",
                url=str(self.remote),
            )
        ]

        with self.assertRaisesRegex(MODULE.PlanError, "either explicit.*or --all"):
            MODULE.filter_submodules(
                modules,
                ["third_party/libexample"],
                all_paths=True,
            )

    def test_filter_submodules_supports_explicit_all_selection(self) -> None:
        modules = [
            MODULE.Submodule(
                name="custom-lib",
                path="third_party/libexample",
                url=str(self.remote),
            )
        ]

        self.assertEqual(
            MODULE.filter_submodules(modules, [], all_paths=True),
            modules,
        )

    def test_main_rejects_empty_selection_before_repo_lookup(self) -> None:
        args = type(
            "Args",
            (),
            {
                "depth": 1,
                "paths": [],
                "all_paths": False,
            },
        )()
        original_parse_args = MODULE.parse_args
        original_repo_paths = MODULE.repo_paths

        def fail_repo_lookup(repo: Path) -> tuple[Path, Path, Path]:
            self.fail(f"unexpected repo lookup for {repo}")

        try:
            MODULE.parse_args = lambda: args
            MODULE.repo_paths = fail_repo_lookup
            with self.assertRaisesRegex(MODULE.PlanError, "no submodule paths selected"):
                MODULE.main()
        finally:
            MODULE.parse_args = original_parse_args
            MODULE.repo_paths = original_repo_paths

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

    def test_later_stale_registration_blocks_all_apply_mutations(self) -> None:
        target_super = self.root / "target-super"
        target_super.mkdir()
        first_source_git_dir = self.named_common_git_dir / "modules" / "first"
        second_source_git_dir = self.named_common_git_dir / "modules" / "second"
        for name, source_git_dir in (
            ("first", first_source_git_dir),
            ("second", second_source_git_dir),
        ):
            source_git_dir.parent.mkdir(parents=True, exist_ok=True)
            run_git(
                self.root,
                "clone",
                "--separate-git-dir",
                str(source_git_dir),
                str(self.remote),
                str(self.root / f"{name}-standard"),
            )

        stale_target = target_super / "third_party" / "second"
        stale_target.parent.mkdir(parents=True)
        run_git(
            self.root,
            f"--git-dir={second_source_git_dir}",
            f"--work-tree={stale_target}",
            "worktree",
            "add",
            "--detach",
            str(stale_target),
            self.sha,
        )
        shutil.rmtree(stale_target)

        first_target = target_super / "third_party" / "first"
        first_registry_before = run_git(
            self.root,
            f"--git-dir={first_source_git_dir}",
            "worktree",
            "list",
            "--porcelain",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(MODULE.PlanError, "registered.*not a usable managed"):
                MODULE.execute_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule("first", "third_party/first", str(self.remote)),
                            self.sha,
                        ),
                        (
                            MODULE.Submodule("second", "third_party/second", str(self.remote)),
                            self.sha,
                        ),
                    ],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=False,
                    fetch_missing=False,
                )

        self.assertNotIn("preflight complete; applying plan", output.getvalue())
        self.assertFalse(first_target.exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source_git_dir}",
                "worktree",
                "list",
                "--porcelain",
            ),
            first_registry_before,
        )

    def test_later_symlink_alias_collision_blocks_all_apply_mutations(self) -> None:
        target_super = self.root / "alias-target-super"
        real_parent = target_super / "real"
        real_parent.mkdir(parents=True)
        (target_super / "alias").symlink_to(real_parent, target_is_directory=True)
        first_source = self.clone_named_source("alias-first")
        self.clone_named_source("alias-second")
        (self.remote / "ALIAS.md").write_text("alias\n", encoding="utf-8")
        run_git(self.remote, "add", "ALIAS.md")
        run_git(self.remote, "commit", "-m", "alias")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        first_target = real_parent / "lib"
        registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        commands: list[list[str]] = []
        original_run = MODULE.run

        def recording_run(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run(args, cwd=cwd, check=check, capture=capture)

        try:
            MODULE.run = recording_run
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(MODULE.PlanError, "symlink alias/collision"):
                    MODULE.execute_sync_plan(
                        root=target_super,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[
                            (
                                MODULE.Submodule(
                                    "alias-first",
                                    "real/lib",
                                    str(self.remote),
                                ),
                                missing_sha,
                            ),
                            (
                                MODULE.Submodule(
                                    "alias-second",
                                    "alias/lib",
                                    str(self.remote),
                                ),
                                self.sha,
                            ),
                        ],
                        depth=1,
                        recursive=False,
                        force_replace_empty=False,
                        dry_run=False,
                        fetch_missing=True,
                    )
        finally:
            MODULE.run = original_run

        self.assertFalse(first_target.exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )
        self.assertFalse(any("fetch" in command for command in commands))

    def test_later_unwritable_target_parent_blocks_first_target_mutation(self) -> None:
        target_super = self.root / "target-policy-super"
        first_parent = target_super / "first-parent"
        denied_parent = target_super / "denied-parent"
        first_parent.mkdir(parents=True)
        denied_parent.mkdir()
        first_source = self.clone_named_source("policy-first")
        self.clone_named_source("policy-second")
        registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        original_probe_access = MODULE.probe_access

        def deny_later_parent(path: Path, mode: int) -> bool:
            if path.resolve() == denied_parent.resolve() and mode & os.W_OK:
                return False
            return original_probe_access(path, mode)

        try:
            MODULE.probe_access = deny_later_parent
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(MODULE.PlanError, "target parent creation"):
                    MODULE.execute_sync_plan(
                        root=target_super,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[
                            (
                                MODULE.Submodule(
                                    "policy-first",
                                    "first-parent/lib",
                                    str(self.remote),
                                ),
                                self.sha,
                            ),
                            (
                                MODULE.Submodule(
                                    "policy-second",
                                    "denied-parent/lib",
                                    str(self.remote),
                                ),
                                self.sha,
                            ),
                        ],
                        depth=1,
                        recursive=False,
                        force_replace_empty=False,
                        dry_run=False,
                        fetch_missing=False,
                    )
        finally:
            MODULE.probe_access = original_probe_access

        self.assertFalse((first_parent / "lib").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )

    def test_later_unwritable_source_admin_blocks_first_target_mutation(self) -> None:
        target_super = self.root / "source-policy-super"
        target_super.mkdir()
        first_source = self.clone_named_source("source-policy-first")
        second_source = self.clone_named_source("source-policy-second")
        registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        original_probe_access = MODULE.probe_access

        def deny_later_source(path: Path, mode: int) -> bool:
            if path.resolve() == second_source.resolve() and mode & os.W_OK:
                return False
            return original_probe_access(path, mode)

        try:
            MODULE.probe_access = deny_later_source
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(MODULE.PlanError, "source gitdir administration"):
                    MODULE.execute_sync_plan(
                        root=target_super,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[
                            (
                                MODULE.Submodule(
                                    "source-policy-first",
                                    "first",
                                    str(self.remote),
                                ),
                                self.sha,
                            ),
                            (
                                MODULE.Submodule(
                                    "source-policy-second",
                                    "second",
                                    str(self.remote),
                                ),
                                self.sha,
                            ),
                        ],
                        depth=1,
                        recursive=False,
                        force_replace_empty=False,
                        dry_run=False,
                        fetch_missing=False,
                    )
        finally:
            MODULE.probe_access = original_probe_access

        self.assertFalse((target_super / "first").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )

    def test_policy_drift_between_plan_and_apply_blocks_first_mutation(self) -> None:
        target_super = self.root / "drift-super"
        first_parent = target_super / "first-parent"
        second_parent = target_super / "second-parent"
        first_parent.mkdir(parents=True)
        second_parent.mkdir()
        second_parent.chmod(0o755)
        first_source = self.clone_named_source("drift-first")
        self.clone_named_source("drift-second")
        registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "drift-first",
                        "first-parent/lib",
                        str(self.remote),
                    ),
                    self.sha,
                ),
                (
                    MODULE.Submodule(
                        "drift-second",
                        "second-parent/lib",
                        str(self.remote),
                    ),
                    self.sha,
                ),
            ],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )

        second_parent.chmod(0o700)
        try:
            with self.assertRaisesRegex(MODULE.PlanError, "object or policy changed"):
                MODULE.apply_sync_plan(plan)
        finally:
            second_parent.chmod(0o755)

        self.assertFalse((first_parent / "lib").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )

    def test_benign_target_parent_entry_churn_preserves_bound_policy(self) -> None:
        target_super = self.root / "benign-churn-super"
        target_parent = target_super / "parent"
        target_parent.mkdir(parents=True)
        self.clone_named_source("benign-churn")
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "benign-churn",
                        "parent/lib",
                        str(self.remote),
                    ),
                    self.sha,
                )
            ],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )

        (target_parent / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        self.assertEqual(
            run_git(target_parent / "lib", "rev-parse", "HEAD"),
            self.sha,
        )

    def test_missing_target_rejects_non_directory_parent(self) -> None:
        target_super = self.root / "target-super"
        target_super.mkdir()
        (target_super / "blocked").write_text("not a directory\n", encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(MODULE.PlanError, "existing parent is not a directory"):
                MODULE.sync_one(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    parent_source_git_dir=None,
                    parent_root=target_super,
                    submodule=MODULE.Submodule(
                        "custom-lib",
                        "blocked/libexample",
                        str(self.remote),
                    ),
                    sha=self.sha,
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=True,
                )

    def test_dry_run_read_queries_preserve_linked_worktree_index(self) -> None:
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
        readme_path = self.linked / "README.md"
        readme_stat = readme_path.stat()
        os.utime(
            readme_path,
            ns=(readme_stat.st_atime_ns, readme_stat.st_mtime_ns + 2_000_000_000),
        )

        index_path = Path(run_git(self.linked, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = (self.linked / index_path).resolve()

        def index_metadata() -> tuple[int, int, int, int, int, int]:
            stat = index_path.stat()
            return (
                stat.st_dev,
                stat.st_ino,
                stat.st_mode,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )

        index_bytes_before = index_path.read_bytes()
        index_metadata_before = index_metadata()
        commands: list[list[str]] = []
        original_run = MODULE.run

        def recording_run(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run(args, cwd=cwd, check=check, capture=capture)

        try:
            MODULE.run = recording_run
            with redirect_stdout(io.StringIO()):
                MODULE.sync_one(
                    root=self.root,
                    common_git_dir=self.root / "super" / ".git",
                    source_superproject=None,
                    parent_source_git_dir=None,
                    parent_root=self.root,
                    submodule=MODULE.Submodule(
                        "third_party/libexample",
                        "linked",
                        str(self.remote),
                    ),
                    sha=self.sha,
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=True,
                )
        finally:
            MODULE.run = original_run

        git_commands = [command for command in commands if command and command[0] == "git"]
        self.assertTrue(any("status" in command for command in git_commands))
        self.assertTrue(
            all(command[:2] == ["git", "--no-optional-locks"] for command in git_commands)
        )
        self.assertEqual(index_path.read_bytes(), index_bytes_before)
        self.assertEqual(index_metadata(), index_metadata_before)

    def test_cli_targeted_apply_preflights_then_adds_worktree(self) -> None:
        (self.remote / "CLI.md").write_text("cli\n", encoding="utf-8")
        run_git(self.remote, "add", "CLI.md")
        run_git(self.remote, "commit", "-m", "cli")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        target = self.root / "target-super"
        run_git(self.root, "init", str(target))
        (target / ".gitmodules").write_text(
            "\n".join(
                [
                    '[submodule "custom-lib"]',
                    "    path = third_party/libexample",
                    f"    url = {self.remote}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_git(target, "add", ".gitmodules")
        run_git(
            target,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{target_sha},third_party/libexample",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--repo",
                str(target),
                "--source-common-git-dir",
                str(self.named_common_git_dir),
                "--no-recursive",
                "--fetch-missing",
                "--",
                "third_party/libexample",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertIn("preflight 1 top-level submodule path(s)", result.stdout)
        self.assertIn("fetch missing commit for third_party/libexample", result.stdout)
        self.assertIn("preflight complete; applying plan", result.stdout)
        self.assertEqual(
            run_git(target / "third_party" / "libexample", "rev-parse", "HEAD"),
            target_sha,
        )

    def test_recursive_apply_uses_complete_parent_first_plan(self) -> None:
        child_remote = self.root / "nested-child-remote"
        run_git(self.root, "init", str(child_remote))
        run_git(child_remote, "config", "user.email", "test@example.com")
        run_git(child_remote, "config", "user.name", "Test User")
        (child_remote / "CHILD.md").write_text("child\n", encoding="utf-8")
        run_git(child_remote, "add", "CHILD.md")
        run_git(child_remote, "commit", "-m", "child")
        child_sha = run_git(child_remote, "rev-parse", "HEAD")

        parent_remote = self.root / "nested-parent-remote"
        run_git(self.root, "init", str(parent_remote))
        run_git(parent_remote, "config", "user.email", "test@example.com")
        run_git(parent_remote, "config", "user.name", "Test User")
        (parent_remote / ".gitmodules").write_text(
            "\n".join(
                [
                    '[submodule "nested"]',
                    "    path = nested",
                    f"    url = {child_remote}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_git(parent_remote, "add", ".gitmodules")
        run_git(
            parent_remote,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{child_sha},nested",
        )
        run_git(parent_remote, "commit", "-m", "parent")
        parent_sha = run_git(parent_remote, "rev-parse", "HEAD")

        source_common = self.root / "recursive-source" / ".git"
        parent_source = source_common / "modules" / "parent"
        parent_source.parent.mkdir(parents=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(parent_source),
            str(parent_remote),
            str(self.root / "parent-standard"),
        )
        child_source = parent_source / "modules" / "nested"
        child_source.parent.mkdir(parents=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(child_source),
            str(child_remote),
            str(self.root / "child-standard"),
        )
        target_super = self.root / "recursive-target"
        target_super.mkdir()

        with redirect_stdout(io.StringIO()):
            MODULE.execute_sync_plan(
                root=target_super,
                common_git_dir=source_common,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule("parent", "parent", str(parent_remote)),
                        parent_sha,
                    )
                ],
                depth=1,
                recursive=True,
                force_replace_empty=False,
                dry_run=False,
                fetch_missing=False,
            )

        self.assertEqual(
            run_git(target_super / "parent", "rev-parse", "HEAD"),
            parent_sha,
        )
        self.assertEqual(
            run_git(target_super / "parent" / "nested", "rev-parse", "HEAD"),
            child_sha,
        )

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

    def test_missing_commit_requires_explicit_fetch_authorization(self) -> None:
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        commands: list[list[str]] = []
        original_run = MODULE.run

        def recording_run(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run(args, cwd=cwd, check=check, capture=capture)

        try:
            MODULE.run = recording_run
            with self.assertRaisesRegex(MODULE.PlanError, "network fetch is disabled"):
                MODULE.fetch_missing_commit(
                    self.named_source_git_dir,
                    self.root / "target-super" / "third_party" / "libexample",
                    submodule,
                    "f" * 40,
                    1,
                    fetch_missing=False,
                    dry_run=False,
                )
        finally:
            MODULE.run = original_run

        self.assertFalse(any("fetch" in command for command in commands))

    def test_authorized_missing_commit_fetches_shallow_target(self) -> None:
        (self.remote / "SECOND.md").write_text("second\n", encoding="utf-8")
        run_git(self.remote, "add", "SECOND.md")
        run_git(self.remote, "commit", "-m", "second")
        second_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )

        with redirect_stdout(io.StringIO()):
            self.assertTrue(
                MODULE.fetch_missing_commit(
                    self.named_source_git_dir,
                    self.root / "target-super" / "third_party" / "libexample",
                    submodule,
                    second_sha,
                    1,
                    dry_run=False,
                    fetch_missing=True,
                )
            )
        self.assertTrue(
            MODULE.commit_exists(
                self.named_source_git_dir,
                self.root / "target-super" / "third_party" / "libexample",
                second_sha,
            )
        )

    def test_recursive_missing_commit_never_fetches_before_complete_plan(self) -> None:
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        output = io.StringIO()
        commands: list[list[str]] = []
        original_run = MODULE.run

        def recording_run(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run(args, cwd=cwd, check=check, capture=capture)

        try:
            MODULE.run = recording_run
            with redirect_stdout(output):
                with self.assertRaisesRegex(MODULE.PlanError, "cannot complete the recursive plan"):
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
                        dry_run=False,
                        fetch_missing=True,
                    )
        finally:
            MODULE.run = original_run

        self.assertNotIn("would fetch missing commit", output.getvalue())
        self.assertFalse(any("fetch" in command for command in commands))

    def test_execute_sync_plan_builds_and_prints_before_apply(self) -> None:
        module = MODULE.Submodule("custom-lib", "third_party/libexample", str(self.remote))
        calls: list[str] = []
        sentinel = object()
        original_build_sync_plan = MODULE.build_sync_plan
        original_print_sync_plan = MODULE.print_sync_plan
        original_apply_sync_plan = MODULE.apply_sync_plan

        def fake_build_sync_plan(**kwargs: object) -> object:
            self.assertEqual(kwargs["planned_modules"], [(module, self.sha)])
            calls.append("build")
            return sentinel

        def fake_print_sync_plan(plan: object) -> None:
            self.assertIs(plan, sentinel)
            calls.append("print")

        def fake_apply_sync_plan(plan: object) -> None:
            self.assertIs(plan, sentinel)
            calls.append("apply")

        try:
            MODULE.build_sync_plan = fake_build_sync_plan
            MODULE.print_sync_plan = fake_print_sync_plan
            MODULE.apply_sync_plan = fake_apply_sync_plan
            with redirect_stdout(io.StringIO()):
                MODULE.execute_sync_plan(
                    root=self.root,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[(module, self.sha)],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=False,
                    fetch_missing=False,
                )
        finally:
            MODULE.build_sync_plan = original_build_sync_plan
            MODULE.print_sync_plan = original_print_sync_plan
            MODULE.apply_sync_plan = original_apply_sync_plan

        self.assertEqual(calls, ["build", "print", "apply"])

    def test_execute_sync_plan_does_not_apply_after_failed_preflight(self) -> None:
        module = MODULE.Submodule("custom-lib", "third_party/libexample", str(self.remote))
        calls: list[str] = []
        original_build_sync_plan = MODULE.build_sync_plan
        original_print_sync_plan = MODULE.print_sync_plan
        original_apply_sync_plan = MODULE.apply_sync_plan

        def failed_build_sync_plan(**kwargs: object) -> object:
            calls.append("build")
            raise MODULE.PlanError("failed preflight")

        def unexpected_print(plan: object) -> None:
            calls.append("print")

        def unexpected_apply(plan: object) -> None:
            calls.append("apply")

        try:
            MODULE.build_sync_plan = failed_build_sync_plan
            MODULE.print_sync_plan = unexpected_print
            MODULE.apply_sync_plan = unexpected_apply
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(MODULE.PlanError, "failed preflight"):
                    MODULE.execute_sync_plan(
                        root=self.root,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[(module, self.sha)],
                        depth=1,
                        recursive=False,
                        force_replace_empty=False,
                        dry_run=False,
                        fetch_missing=False,
                    )
        finally:
            MODULE.build_sync_plan = original_build_sync_plan
            MODULE.print_sync_plan = original_print_sync_plan
            MODULE.apply_sync_plan = original_apply_sync_plan

        self.assertEqual(calls, ["build"])


if __name__ == "__main__":
    unittest.main()
