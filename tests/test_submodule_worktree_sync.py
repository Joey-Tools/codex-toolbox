from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest import mock


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
        self.source_git_dir = (
            self.root / "super" / ".git" / "modules" / "third_party" / "libexample"
        )
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

    def fetch_source(self, source_git_dir: Path) -> None:
        run_git(
            self.root,
            f"--git-dir={source_git_dir}",
            "fetch",
            "--no-tags",
            "origin",
        )

    def add_managed_worktree(
        self,
        source_git_dir: Path,
        target: Path,
        sha: str,
    ) -> None:
        run_git(
            self.root,
            f"--git-dir={source_git_dir}",
            f"--work-tree={target}",
            "worktree",
            "add",
            "--detach",
            str(target),
            sha,
        )

    def filtered_target_sha(self, filter_name: str, required: bool) -> str:
        (self.remote / ".gitattributes").write_text(
            f"payload.bin filter={filter_name}\n",
            encoding="utf-8",
        )
        (self.remote / "payload.bin").write_text(
            "version https://example.invalid/spec/v1\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", ".gitattributes")
        run_git(self.remote, "add", "-f", "payload.bin")
        run_git(self.remote, "commit", "-m", f"add {filter_name} payload")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        self.fetch_source(self.named_source_git_dir)
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            f"filter.{filter_name}.required",
            "true" if required else "false",
        )
        return target_sha

    def test_script_imports_future_annotations(self) -> None:
        first_lines = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()[:4]

        self.assertIn("from __future__ import annotations", first_lines)

    def test_git_reads_disable_lazy_fetch_and_strip_repository_redirection(
        self,
    ) -> None:
        poisoned = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.root / "alternate"),
            "GIT_COMMON_DIR": str(self.root / "common"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(self.root / "attacker"),
            "GIT_DIR": str(self.root / "gitdir"),
            "GIT_INDEX_FILE": str(self.root / "index"),
            "GIT_OBJECT_DIRECTORY": str(self.root / "objects"),
            "GIT_SHALLOW_FILE": str(self.root / "shallow"),
            "GIT_SSH_COMMAND": "false",
            "GIT_WORK_TREE": str(self.root / "worktree"),
        }
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout="ok\n",
            stderr="",
        )
        with mock.patch.dict(os.environ, poisoned, clear=False):
            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=completed,
            ) as subprocess_run:
                result = MODULE.read_git(["cat-file", "-e", self.sha])

        self.assertEqual(result.stdout, "ok\n")
        child_env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(child_env["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(child_env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(child_env["GIT_CONFIG_SYSTEM"], os.devnull)
        for key in poisoned:
            if key in MODULE.SAFE_GIT_ENV:
                self.assertEqual(child_env[key], MODULE.SAFE_GIT_ENV[key])
            else:
                self.assertNotIn(key, child_env)
        command = subprocess_run.call_args.args[0]
        self.assertEqual(
            Path(command[0]).resolve(),
            MODULE.git_runtime().executable,
        )
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn("credential.helper=", command)
        self.assertIn("core.excludesFile=/dev/null", command)

    def test_old_git_is_rejected_before_the_first_repository_command(self) -> None:
        actual_git = MODULE.git_runtime().executable
        old_version = subprocess.CompletedProcess(
            [str(actual_git), "--version"],
            0,
            stdout=b"git version 2.44.9\n",
            stderr=b"",
        )
        with mock.patch.object(MODULE, "_GIT_RUNTIME", None):
            with mock.patch.object(
                MODULE.shutil,
                "which",
                return_value=str(actual_git),
            ):
                with mock.patch.object(
                    MODULE,
                    "run_bounded_bytes",
                    return_value=old_version,
                ) as version_probe:
                    with mock.patch.object(
                        MODULE.subprocess,
                        "run",
                    ) as repository_command:
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            "Git 2.45.0 or newer",
                        ):
                            MODULE.repo_paths(self.root)

        version_probe.assert_called_once()
        repository_command.assert_not_called()

    def test_bounded_command_stops_at_retained_stdout_limit(self) -> None:
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "retained-output limit",
        ):
            MODULE.run_bounded_bytes(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x' * 4096)",
                ],
                timeout_seconds=5,
                stdout_limit=64,
            )

    def test_bounded_command_stops_at_deadline(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(MODULE.PlanError, "deadline"):
            MODULE.run_bounded_bytes(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(5)",
                ],
                timeout_seconds=0.2,
                stdout_limit=64,
            )
        self.assertLess(time.monotonic() - started, 2)

    def test_bounded_command_deadline_survives_closed_capture_pipes(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(MODULE.PlanError, "deadline"):
            MODULE.run_bounded_bytes(
                [
                    sys.executable,
                    "-c",
                    "import os, time; os.close(1); os.close(2); time.sleep(5)",
                ],
                timeout_seconds=0.2,
                stdout_limit=64,
            )
        self.assertLess(time.monotonic() - started, 2)

    def test_process_cleanup_reports_an_unreapable_direct_child(self) -> None:
        class UnreapableProcess:
            pid = 424242

            def __init__(self) -> None:
                self.wait_timeouts: list[float | None] = []

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> None:
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(["unreapable"], timeout)

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        process = UnreapableProcess()
        with mock.patch.object(MODULE.os, "killpg"):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "cleanup-incomplete",
            ):
                MODULE.terminate_process_group(
                    process,
                    cleanup_timeout_seconds=0.01,
                    term_grace_seconds=0.005,
                )

        self.assertEqual(len(process.wait_timeouts), 1)
        self.assertTrue(all(timeout is not None for timeout in process.wait_timeouts))

    def test_bounded_command_rejects_oversized_input_before_spawn(self) -> None:
        with mock.patch.object(MODULE, "GIT_INPUT_LIMIT_BYTES", 4):
            with mock.patch.object(MODULE.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(MODULE.PlanError, "input exceeds"):
                    MODULE.run_bounded_bytes(
                        [sys.executable, "-c", "pass"],
                        input_bytes=b"12345",
                    )

        popen.assert_not_called()

    def test_tracked_status_is_bounded_and_skips_untracked_inventory(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=b"",
            stderr=b"",
        )
        with mock.patch.object(
            MODULE,
            "read_git_bounded",
            return_value=completed,
        ) as bounded_git:
            self.assertFalse(
                MODULE.has_local_changes(
                    self.root,
                    self.sha,
                )
            )

        args = bounded_git.call_args.args[0]
        self.assertIn("status", args)
        self.assertIn("--untracked-files=no", args)
        self.assertIn("--no-renames", args)
        self.assertEqual(
            bounded_git.call_args.kwargs["extra_env"],
            {"GIT_ATTR_SOURCE": self.sha},
        )

    def test_worktree_registry_enforces_a_bounded_record_count(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=b"worktree /first\0HEAD deadbeef\0",
            stderr=b"",
        )
        with mock.patch.object(
            MODULE,
            "MAX_REGISTERED_WORKTREE_FIELDS",
            1,
        ):
            with mock.patch.object(
                MODULE,
                "read_git_bounded",
                return_value=completed,
            ):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "record safety limit",
                ):
                    MODULE.registered_worktree_paths(
                        self.named_source_git_dir,
                    )

    def test_parse_gitmodules_rejects_unsafe_path(self) -> None:
        content = """
[submodule "custom-lib"]
    path = third_party/../libexample
    url = https://example.invalid/libexample.git
"""

        with self.assertRaisesRegex(MODULE.PlanError, "unsafe path segment"):
            MODULE.parse_gitmodules(content, ".gitmodules")

    def test_relative_git_paths_reject_cross_platform_escape_forms(self) -> None:
        unsafe_paths = (
            r"..\outside",
            r"C:\outside",
            "C:/outside",
            r"\\server\share",
            "/absolute/path",
        )
        for unsafe_path in unsafe_paths:
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaises(MODULE.PlanError):
                    MODULE.validate_relative_git_path(
                        unsafe_path,
                        "test path",
                        "test",
                    )

    def test_native_windows_runtime_fails_before_git_discovery(self) -> None:
        with mock.patch.object(MODULE.os, "name", "nt"):
            with mock.patch.object(MODULE.shutil, "which") as which_git:
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "supports only POSIX hosts",
                ):
                    MODULE.discover_git_runtime()

        which_git.assert_not_called()

    def test_bound_target_rejects_a_cross_platform_lexical_escape(self) -> None:
        target_root = self.root / "lexical-target"
        target_root.mkdir()

        with self.assertRaises(MODULE.PlanError):
            MODULE.bind_target_path(
                target_root,
                ("safe/../../outside",),
                "escaped target",
            )

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

        with self.assertRaisesRegex(
            MODULE.PlanError, "explicit top-level paths or --all"
        ):
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
            with self.assertRaisesRegex(
                MODULE.PlanError, "no submodule paths selected"
            ):
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

        def fake_git(
            args: list[str], *, cwd: Path | None = None, check: bool = True
        ) -> str:
            self.assertEqual(
                args[:4], ["ls-files", "-s", "--", "third_party/libexample"]
            )
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

        def fake_git(
            args: list[str], *, cwd: Path | None = None, check: bool = True
        ) -> str:
            self.assertEqual(
                args[:4], ["ls-files", "-s", "--", "third_party/libexample"]
            )
            return f"160000 {'a' * 40} 2\tthird_party/libexample"

        try:
            MODULE.git = fake_git
            with self.assertRaisesRegex(MODULE.PlanError, "unresolved index stage 2"):
                MODULE.expected_sha(self.root, "third_party/libexample")
        finally:
            MODULE.git = original_git

    def test_default_common_git_dir_does_not_suggest_target_submodule_update(
        self,
    ) -> None:
        args = type(
            "Args",
            (),
            {
                "source_common_git_dir": None,
                "source_superproject": None,
            },
        )()

        source_common_git_dir, source_superproject = (
            MODULE.choose_source_common_git_dir(args, self.remote)
        )

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

    def test_checkout_existing_worktree_refuses_to_overwrite_ignored_files(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(MODULE, "has_local_changes", return_value=False):
            with mock.patch.object(
                MODULE,
                "run",
                return_value=completed,
            ) as run_command:
                MODULE.checkout_existing_worktree(
                    self.linked,
                    self.sha,
                    dry_run=False,
                )

        command = run_command.call_args.args[0]
        self.assertIn("--no-overwrite-ignore", command)
        self.assertIn("--no-recurse-submodules", command)

    def test_missing_target_collision_uses_case_sensitive_volume_semantics(
        self,
    ) -> None:
        target_root = self.root / "case-sensitive-target"
        target_root.mkdir()
        policy = MODULE.FilesystemNamePolicy(
            case_sensitive=True,
            normalization="exact",
            source="test",
        )

        first = MODULE.bind_target_path(
            target_root,
            ("Foo",),
            "first",
            policy,
        )
        second = MODULE.bind_target_path(
            target_root,
            ("foo",),
            "second",
            policy,
        )

        self.assertNotEqual(first.collision_tokens, second.collision_tokens)

    def test_missing_target_collision_uses_case_insensitive_volume_semantics(
        self,
    ) -> None:
        target_root = self.root / "case-insensitive-target"
        target_root.mkdir()
        policy = MODULE.FilesystemNamePolicy(
            case_sensitive=False,
            normalization="NFD",
            source="test",
        )

        first = MODULE.bind_target_path(
            target_root,
            ("Caf\u00e9",),
            "first",
            policy,
        )
        second = MODULE.bind_target_path(
            target_root,
            ("CAFE\u0301",),
            "second",
            policy,
        )

        self.assertEqual(first.collision_tokens, second.collision_tokens)

    def test_actual_case_sensitive_directory_overrides_stale_git_hint(self) -> None:
        target_root = self.root / "actual-case-sensitive-target"
        target_root.mkdir()

        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                side_effect=[True, False],
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=True,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=False,
                    ):
                        policy = MODULE.filesystem_name_policy(target_root)

        self.assertTrue(policy.case_sensitive)
        self.assertEqual(policy.normalization, "exact")

    def test_actual_case_insensitive_directory_overrides_stale_git_hint(self) -> None:
        target_root = self.root / "actual-case-insensitive-target"
        target_root.mkdir()

        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                side_effect=[False, False],
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=False,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=False,
                    ):
                        policy = MODULE.filesystem_name_policy(target_root)

        self.assertFalse(policy.case_sensitive)
        self.assertEqual(policy.normalization, "exact")

    def test_descendant_ext4_casefold_anchor_controls_missing_aliases(self) -> None:
        target_root = self.root / "mixed-name-policy-target"
        casefold_anchor = target_root / "casefold-anchor"
        casefold_anchor.mkdir(parents=True)
        sensitive_policy = MODULE.FilesystemNamePolicy(
            case_sensitive=True,
            normalization="exact",
            source="mock-sensitive",
        )
        casefold_policy = MODULE.FilesystemNamePolicy(
            case_sensitive=False,
            normalization="NFD",
            source="mock-ext4-casefold",
        )

        def policy_for(path: Path) -> object:
            if path.resolve() == casefold_anchor.resolve():
                return casefold_policy
            return sensitive_policy

        with mock.patch.object(
            MODULE,
            "filesystem_name_policy",
            side_effect=policy_for,
        ):
            first = MODULE.bind_target_path(
                target_root,
                ("casefold-anchor", "Caf\u00e9"),
                "first",
            )
            second = MODULE.bind_target_path(
                target_root,
                ("casefold-anchor", "CAFE\u0301"),
                "second",
            )
            sensitive_first = MODULE.bind_target_path(
                target_root,
                ("Caf\u00e9",),
                "sensitive first",
            )
            sensitive_second = MODULE.bind_target_path(
                target_root,
                ("CAFE\u0301",),
                "sensitive second",
            )

        self.assertEqual(first.collision_tokens, second.collision_tokens)
        self.assertNotEqual(
            sensitive_first.collision_tokens,
            sensitive_second.collision_tokens,
        )
        self.assertEqual(first.name_policy.source, "mock-ext4-casefold")
        self.assertEqual(
            first.name_policy_anchor.path,
            casefold_anchor.resolve(),
        )

    def test_linux_casefold_flag_overrides_a_case_sensitive_probe(self) -> None:
        target_root = self.root / "linux-casefold-target"
        target_root.mkdir()

        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                side_effect=[False, False],
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=True,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=True,
                    ):
                        policy = MODULE.filesystem_name_policy(target_root)

        self.assertFalse(policy.case_sensitive)
        self.assertEqual(policy.normalization, "NFD")

    def test_collision_index_does_not_scan_the_existing_plan(self) -> None:
        class NonIterableEntries(list[object]):
            def __iter__(self) -> object:
                raise AssertionError("collision index must not scan all entries")

        entries = NonIterableEntries()
        collision_index = MODULE.TargetCollisionIndex()
        for index in range(20_000):
            candidate = SimpleNamespace(
                parent_index=None,
                target=SimpleNamespace(
                    path=Path(f"module-{index}"),
                    collision_tokens=(("missing", f"module-{index}"),),
                ),
            )
            collision_index.add(entries, candidate)
            entries.append(candidate)

        self.assertEqual(len(entries), 20_000)

    def test_new_worktree_rejects_lfs_filter_even_when_nonrequired(self) -> None:
        target_sha = self.filtered_target_sha("lfs", required=False)
        target_super = self.root / "lfs-target"
        target_super.mkdir()

        with self.assertRaisesRegex(MODULE.PlanError, "untrusted content filter"):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "custom-lib",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                fetch_missing=False,
            )

        self.assertFalse((target_super / "lib").exists())

    def test_new_worktree_rejects_required_custom_filter(self) -> None:
        target_sha = self.filtered_target_sha("required-test", required=True)
        target_super = self.root / "required-filter-target"
        target_super.mkdir()

        with self.assertRaisesRegex(MODULE.PlanError, "filter: required-test"):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "custom-lib",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                fetch_missing=False,
            )

    def test_new_worktree_rejects_nonrequired_custom_filter(self) -> None:
        target_sha = self.filtered_target_sha("optional-test", required=False)
        target_super = self.root / "optional-filter-target"
        target_super.mkdir()

        with self.assertRaisesRegex(MODULE.PlanError, "filter: optional-test"):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "custom-lib",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                fetch_missing=False,
            )

    def test_managed_worktree_rejects_new_filtered_payload(self) -> None:
        target_sha = self.filtered_target_sha("managed-filter", required=False)
        target_super = self.root / "managed-filter-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(
            self.named_source_git_dir,
            target,
            self.sha,
        )

        with self.assertRaisesRegex(MODULE.PlanError, "filter: managed-filter"):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "custom-lib",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                fetch_missing=False,
            )

        self.assertEqual(run_git(target, "rev-parse", "HEAD"), self.sha)

    def test_current_clean_filter_is_rejected_before_status_can_execute_it(
        self,
    ) -> None:
        (self.remote / ".gitattributes").write_text(
            "payload.bin filter=marker\n",
            encoding="utf-8",
        )
        (self.remote / "payload.bin").write_text(
            "payload\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", ".gitattributes", "payload.bin")
        run_git(self.remote, "commit", "-m", "add current filter")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (self.remote / "README.md").write_text("target\n", encoding="utf-8")
        run_git(self.remote, "add", "README.md")
        run_git(self.remote, "commit", "-m", "update target")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")

        source = self.clone_named_source("current-filter")
        target_super = self.root / "current-filter-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, base_sha)
        marker = self.root / "clean-filter-executed"
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "filter.marker.clean",
            f"touch {marker}",
        )
        payload = target / "payload.bin"
        payload_stat = payload.stat()
        os.utime(
            payload,
            ns=(
                payload_stat.st_atime_ns,
                payload_stat.st_mtime_ns + 2_000_000_000,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "before tracked-status inspection",
        ):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "current-filter",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                fetch_missing=False,
            )

        self.assertFalse(marker.exists())
        self.assertEqual(run_git(target, "rev-parse", "HEAD"), base_sha)

    def test_later_managed_ignored_conflict_blocks_first_checkout(self) -> None:
        (self.remote / ".gitignore").write_text(
            "generated.bin\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", ".gitignore")
        run_git(self.remote, "commit", "-m", "ignore generated payload")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (self.remote / "generated.bin").write_text(
            "tracked target\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "-f", "generated.bin")
        run_git(self.remote, "commit", "-m", "track generated payload")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        first_source = self.clone_named_source("ignored-first")
        second_source = self.clone_named_source("ignored-second")
        target_super = self.root / "ignored-managed-target"
        target_super.mkdir()
        first_target = target_super / "first"
        second_target = target_super / "second"
        self.add_managed_worktree(first_source, first_target, base_sha)
        self.add_managed_worktree(second_source, second_target, base_sha)
        (second_target / "generated.bin").write_text(
            "local ignored payload\n",
            encoding="utf-8",
        )

        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "ignored-file conflict",
            ):
                MODULE.execute_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                "ignored-first",
                                "first",
                                str(self.remote),
                            ),
                            target_sha,
                        ),
                        (
                            MODULE.Submodule(
                                "ignored-second",
                                "second",
                                str(self.remote),
                            ),
                            target_sha,
                        ),
                    ],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    dry_run=False,
                    fetch_missing=False,
                )

        self.assertEqual(run_git(first_target, "rev-parse", "HEAD"), base_sha)
        self.assertEqual(
            (second_target / "generated.bin").read_text(encoding="utf-8"),
            "local ignored payload\n",
        )

    def test_ignored_conflict_added_after_plan_blocks_checkout(self) -> None:
        (self.remote / ".gitignore").write_text(
            "generated.bin\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", ".gitignore")
        run_git(self.remote, "commit", "-m", "ignore raced payload")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (self.remote / "generated.bin").write_text(
            "tracked target\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "-f", "generated.bin")
        run_git(self.remote, "commit", "-m", "track raced payload")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("ignored-race")
        target_super = self.root / "ignored-race-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, base_sha)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "ignored-race",
                        "lib",
                        str(self.remote),
                    ),
                    target_sha,
                )
            ],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )
        (target / "generated.bin").write_text(
            "raced ignored payload\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.PlanError, "ignored-file conflict"):
            MODULE.apply_sync_plan(plan)

        self.assertEqual(run_git(target, "rev-parse", "HEAD"), base_sha)
        self.assertEqual(
            (target / "generated.bin").read_text(encoding="utf-8"),
            "raced ignored payload\n",
        )

    def test_unrelated_ignored_path_does_not_block_managed_checkout(self) -> None:
        (self.remote / ".gitignore").write_text(
            "cache/\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", ".gitignore")
        run_git(self.remote, "commit", "-m", "ignore unrelated cache")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (self.remote / "README.md").write_text("target\n", encoding="utf-8")
        run_git(self.remote, "add", "README.md")
        run_git(self.remote, "commit", "-m", "update tracked payload")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("ignored-unrelated")
        target_super = self.root / "ignored-unrelated-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, base_sha)
        cache = target / "cache"
        cache.mkdir()
        (cache / "entry.bin").write_text("ignored\n", encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            MODULE.execute_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "ignored-unrelated",
                            "lib",
                            str(self.remote),
                        ),
                        target_sha,
                    )
                ],
                depth=1,
                recursive=False,
                force_replace_empty=False,
                dry_run=False,
                fetch_missing=False,
            )

        self.assertEqual(run_git(target, "rev-parse", "HEAD"), target_sha)
        self.assertEqual(
            (cache / "entry.bin").read_text(encoding="utf-8"),
            "ignored\n",
        )

    def test_later_managed_write_policy_blocks_first_checkout(self) -> None:
        nested = self.remote / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("base\n", encoding="utf-8")
        run_git(self.remote, "add", "nested/file.txt")
        run_git(self.remote, "commit", "-m", "add nested base")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (nested / "file.txt").write_text("target\n", encoding="utf-8")
        run_git(self.remote, "add", "nested/file.txt")
        run_git(self.remote, "commit", "-m", "update nested file")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        first_source = self.clone_named_source("write-policy-first")
        second_source = self.clone_named_source("write-policy-second")
        target_super = self.root / "write-policy-target"
        target_super.mkdir()
        first_target = target_super / "first"
        second_target = target_super / "second"
        self.add_managed_worktree(first_source, first_target, base_sha)
        self.add_managed_worktree(second_source, second_target, base_sha)
        original_probe_access = MODULE.probe_access

        def deny_second_nested(path: Path, mode: int) -> bool:
            if (
                path.resolve() == (second_target / "nested").resolve()
                and mode & os.W_OK
            ):
                return False
            return original_probe_access(path, mode)

        try:
            MODULE.probe_access = deny_second_nested
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "managed checkout parent update",
                ):
                    MODULE.execute_sync_plan(
                        root=target_super,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[
                            (
                                MODULE.Submodule(
                                    "write-policy-first",
                                    "first",
                                    str(self.remote),
                                ),
                                target_sha,
                            ),
                            (
                                MODULE.Submodule(
                                    "write-policy-second",
                                    "second",
                                    str(self.remote),
                                ),
                                target_sha,
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

        self.assertEqual(run_git(first_target, "rev-parse", "HEAD"), base_sha)

    def test_managed_write_policy_drift_blocks_first_checkout(self) -> None:
        nested = self.remote / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("base\n", encoding="utf-8")
        run_git(self.remote, "add", "nested/file.txt")
        run_git(self.remote, "commit", "-m", "add drift base")
        base_sha = run_git(self.remote, "rev-parse", "HEAD")
        (nested / "file.txt").write_text("target\n", encoding="utf-8")
        run_git(self.remote, "add", "nested/file.txt")
        run_git(self.remote, "commit", "-m", "update drift target")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        first_source = self.clone_named_source("write-drift-first")
        second_source = self.clone_named_source("write-drift-second")
        target_super = self.root / "write-drift-target"
        target_super.mkdir()
        first_target = target_super / "first"
        second_target = target_super / "second"
        self.add_managed_worktree(first_source, first_target, base_sha)
        self.add_managed_worktree(second_source, second_target, base_sha)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "write-drift-first",
                        "first",
                        str(self.remote),
                    ),
                    target_sha,
                ),
                (
                    MODULE.Submodule(
                        "write-drift-second",
                        "second",
                        str(self.remote),
                    ),
                    target_sha,
                ),
            ],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )
        second_parent = second_target / "nested"
        second_parent.chmod(0o700)
        try:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "object or policy changed",
            ):
                MODULE.apply_sync_plan(plan)
        finally:
            second_parent.chmod(0o755)

        self.assertEqual(run_git(first_target, "rev-parse", "HEAD"), base_sha)

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
            with self.assertRaisesRegex(
                MODULE.PlanError, "registered.*not a usable managed"
            ):
                MODULE.execute_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                "first", "third_party/first", str(self.remote)
                            ),
                            self.sha,
                        ),
                        (
                            MODULE.Submodule(
                                "second", "third_party/second", str(self.remote)
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
                with self.assertRaisesRegex(
                    MODULE.PlanError, "symlink alias/collision"
                ):
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

    def test_source_gitdir_physical_alias_blocks_complete_plan(self) -> None:
        target_super = self.root / "source-alias-target"
        target_super.mkdir()
        first_source = self.clone_named_source("source-alias-first")
        second_source = self.named_common_git_dir / "modules" / "source-alias-second"
        second_source.symlink_to(first_source, target_is_directory=True)

        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "source gitdir collision or filesystem alias",
            ):
                MODULE.execute_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                "source-alias-first",
                                "first",
                                str(self.remote),
                            ),
                            self.sha,
                        ),
                        (
                            MODULE.Submodule(
                                "source-alias-second",
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

        self.assertFalse((target_super / "first").exists())
        self.assertFalse((target_super / "second").exists())

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
                with self.assertRaisesRegex(
                    MODULE.PlanError, "source gitdir administration"
                ):
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

    def test_anchor_name_policy_drift_blocks_the_first_mutation(self) -> None:
        target_super = self.root / "name-policy-drift-super"
        first_parent = target_super / "first-parent"
        second_parent = target_super / "second-parent"
        first_parent.mkdir(parents=True)
        second_parent.mkdir()
        first_source = self.clone_named_source("name-policy-first")
        self.clone_named_source("name-policy-second")
        stable_policy = MODULE.FilesystemNamePolicy(
            case_sensitive=True,
            normalization="exact",
            source="mock-stable",
        )
        drifted_policy = MODULE.FilesystemNamePolicy(
            case_sensitive=False,
            normalization="NFD",
            source="mock-drifted",
        )
        registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )

        with mock.patch.object(
            MODULE,
            "filesystem_name_policy",
            return_value=stable_policy,
        ):
            plan = MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "name-policy-first",
                            "first-parent/lib",
                            str(self.remote),
                        ),
                        self.sha,
                    ),
                    (
                        MODULE.Submodule(
                            "name-policy-second",
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

        def drift_second_anchor(path: Path) -> object:
            if path.resolve() == second_parent.resolve():
                return drifted_policy
            return stable_policy

        with mock.patch.object(
            MODULE,
            "filesystem_name_policy",
            side_effect=drift_second_anchor,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "name semantics changed",
            ):
                MODULE.apply_sync_plan(plan)

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
            with self.assertRaisesRegex(
                MODULE.PlanError, "existing parent is not a directory"
            ):
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
        bounded_commands: list[list[str]] = []
        original_run = MODULE.run
        original_read_git_bounded = MODULE.read_git_bounded

        def recording_run(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run(args, cwd=cwd, check=check, capture=capture)

        def recording_read_git_bounded(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            bounded_commands.append(args)
            return original_read_git_bounded(args, **kwargs)

        try:
            MODULE.run = recording_run
            MODULE.read_git_bounded = recording_read_git_bounded
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
            MODULE.read_git_bounded = original_read_git_bounded

        git_commands = [
            command for command in commands if command and command[0] == "git"
        ]
        self.assertTrue(any("status" in command for command in bounded_commands))
        self.assertTrue(
            all(
                command[:2] == ["git", "--no-optional-locks"]
                for command in git_commands
            )
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
                with self.assertRaisesRegex(
                    MODULE.PlanError, "cannot complete the recursive plan"
                ):
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

    def test_apply_revalidates_only_each_fetch_source_before_one_full_pass(
        self,
    ) -> None:
        entries = []
        for index in range(3):
            entries.append(
                SimpleNamespace(
                    needs_fetch=True,
                    source_git_dir=self.root / f"source-{index}",
                    target=SimpleNamespace(path=self.root / f"target-{index}"),
                    submodule=MODULE.Submodule(
                        f"module-{index}",
                        f"module-{index}",
                        str(self.remote),
                    ),
                    sha=f"{index + 1:040x}",
                    checkout_preflight=None,
                    target_bindings=(),
                    state="missing",
                    parent_index=None,
                )
            )
        plan = SimpleNamespace(entries=entries, depth=1)
        events: list[str] = []

        def validate(_plan: object) -> None:
            events.append("full")

        def source(entry: object) -> None:
            events.append(f"source:{entry.submodule.name}")

        def fetch(*args: object, **kwargs: object) -> bool:
            submodule = args[2]
            events.append(f"fetch:{submodule.name}")
            return True

        def capture(entry: object) -> tuple[object, tuple[object, ...]]:
            events.append(f"capture:{entry.submodule.name}")
            return mock.sentinel.checkout_receipt, ()

        def revalidate(
            _plan: object,
            entry: object,
            *,
            allow_parent_materialization: bool = False,
        ) -> object:
            del allow_parent_materialization
            events.append(f"entry:{entry.submodule.name}")
            return entry.target

        def add(
            _source: Path,
            target: Path,
            _sha: str,
            dry_run: bool,
        ) -> None:
            self.assertFalse(dry_run)
            events.append(f"add:{target.name}")

        with mock.patch.object(MODULE, "validate_sync_plan", side_effect=validate):
            with mock.patch.object(
                MODULE,
                "revalidate_runtime_source_access",
                side_effect=source,
            ):
                with mock.patch.object(
                    MODULE,
                    "fetch_missing_commit",
                    side_effect=fetch,
                ):
                    with mock.patch.object(
                        MODULE,
                        "capture_checkout_preflight",
                        side_effect=capture,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "revalidate_planned_entry",
                            side_effect=revalidate,
                        ):
                            with mock.patch.object(
                                MODULE,
                                "add_worktree",
                                side_effect=add,
                            ):
                                MODULE.apply_sync_plan(plan)

        self.assertEqual(events.count("full"), 2)
        second_full = len(events) - 1 - events[::-1].index("full")
        self.assertEqual(
            events[:second_full],
            [
                "full",
                "source:module-0",
                "fetch:module-0",
                "source:module-1",
                "fetch:module-1",
                "source:module-2",
                "fetch:module-2",
                "capture:module-0",
                "capture:module-1",
                "capture:module-2",
            ],
        )

    def test_execute_sync_plan_builds_and_prints_before_apply(self) -> None:
        module = MODULE.Submodule(
            "custom-lib", "third_party/libexample", str(self.remote)
        )
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
        module = MODULE.Submodule(
            "custom-lib", "third_party/libexample", str(self.remote)
        )
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
