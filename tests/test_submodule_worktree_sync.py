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

    def test_recursive_dry_run_stops_when_authorized_fetch_is_only_planned(self) -> None:
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
                    fetch_missing=True,
                )

        self.assertIn("would fetch missing commit", output.getvalue())

    def test_execute_sync_plan_preflights_every_path_before_apply(self) -> None:
        modules = [
            MODULE.Submodule("first", "third_party/first", str(self.remote)),
            MODULE.Submodule("second", "third_party/second", str(self.remote)),
        ]
        calls: list[tuple[str, bool]] = []
        original_sync_one = MODULE.sync_one

        def fake_sync_one(**kwargs: object) -> None:
            module = kwargs["submodule"]
            self.assertIsInstance(module, MODULE.Submodule)
            calls.append((module.path, bool(kwargs["dry_run"])))

        try:
            MODULE.sync_one = fake_sync_one
            with redirect_stdout(io.StringIO()):
                MODULE.execute_sync_plan(
                    root=self.root,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[(modules[0], "a" * 40), (modules[1], "b" * 40)],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=False,
                    fetch_missing=False,
                )
        finally:
            MODULE.sync_one = original_sync_one

        self.assertEqual(
            calls,
            [
                ("third_party/first", True),
                ("third_party/second", True),
                ("third_party/first", False),
                ("third_party/second", False),
            ],
        )

    def test_execute_sync_plan_does_not_apply_after_failed_preflight(self) -> None:
        modules = [
            MODULE.Submodule("first", "third_party/first", str(self.remote)),
            MODULE.Submodule("second", "third_party/second", str(self.remote)),
        ]
        calls: list[tuple[str, bool]] = []
        original_sync_one = MODULE.sync_one

        def fake_sync_one(**kwargs: object) -> None:
            module = kwargs["submodule"]
            self.assertIsInstance(module, MODULE.Submodule)
            dry_run = bool(kwargs["dry_run"])
            calls.append((module.path, dry_run))
            if module.path == "third_party/second" and dry_run:
                raise MODULE.PlanError("second path failed preflight")

        try:
            MODULE.sync_one = fake_sync_one
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(MODULE.PlanError, "failed preflight"):
                    MODULE.execute_sync_plan(
                        root=self.root,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[(modules[0], "a" * 40), (modules[1], "b" * 40)],
                        depth=1,
                        recursive=False,
                        force_replace_empty=False,
                        dry_run=False,
                        fetch_missing=False,
                    )
        finally:
            MODULE.sync_one = original_sync_one

        self.assertEqual(
            calls,
            [
                ("third_party/first", True),
                ("third_party/second", True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
