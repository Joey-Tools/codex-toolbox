from __future__ import annotations

import functools
import http.server
import importlib.util
import io
import errno
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from contextlib import ExitStack, redirect_stdout
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


def split_v2_index_entries(
    content: bytes,
    object_id_bytes: int = 20,
) -> tuple[tuple[bytes, ...], tuple[bytes, ...], int]:
    if content[:4] != b"DIRC" or int.from_bytes(content[4:8], "big") != 2:
        raise AssertionError("expected a version-2 Git index")
    body_end = len(content) - object_id_bytes
    entry_count = int.from_bytes(content[8:12], "big")
    chunks: list[bytes] = []
    paths: list[bytes] = []
    offset = 12
    fixed_size = 40 + object_id_bytes + 2
    for _entry_index in range(entry_count):
        entry_start = offset
        flags_offset = entry_start + 40 + object_id_bytes
        if flags_offset + 2 > body_end:
            raise AssertionError("truncated version-2 index entry")
        flags = int.from_bytes(content[flags_offset : flags_offset + 2], "big")
        if flags & 0x4000:
            raise AssertionError("unexpected extended version-2 index entry")
        path_start = entry_start + fixed_size
        path_end = content.find(b"\0", path_start, body_end)
        if path_end < 0:
            raise AssertionError("unterminated version-2 index path")
        relative_size = path_end + 1 - entry_start
        offset = entry_start + ((relative_size + 7) & ~7)
        if offset > body_end:
            raise AssertionError("oversized version-2 index entry")
        chunks.append(content[entry_start:offset])
        paths.append(content[path_start:path_end])
    return tuple(chunks), tuple(paths), offset


def encode_index_v4_strip_count(value: int) -> bytes:
    encoded = [value & 0x7F]
    while value >> 7:
        value = (value >> 7) - 1
        encoded.append(0x80 | (value & 0x7F))
    return bytes(reversed(encoded))


def v2_index_extension_signatures(
    content: bytes,
    object_id_bytes: int = 20,
) -> tuple[bytes, ...]:
    _chunks, _paths, offset = split_v2_index_entries(content, object_id_bytes)
    body_end = len(content) - object_id_bytes
    signatures: list[bytes] = []
    while offset < body_end:
        if offset + 8 > body_end:
            raise AssertionError("truncated index extension")
        signature = content[offset : offset + 4]
        size = int.from_bytes(content[offset + 4 : offset + 8], "big")
        offset += 8
        if offset + size > body_end:
            raise AssertionError("oversized index extension")
        signatures.append(signature)
        offset += size
    return tuple(signatures)


def v2_index_extension_payload(
    content: bytes,
    expected_signature: bytes,
    object_id_bytes: int = 20,
) -> bytes:
    _chunks, _paths, offset = split_v2_index_entries(content, object_id_bytes)
    body_end = len(content) - object_id_bytes
    while offset < body_end:
        if offset + 8 > body_end:
            raise AssertionError("truncated index extension")
        signature = content[offset : offset + 4]
        size = int.from_bytes(content[offset + 4 : offset + 8], "big")
        offset += 8
        if offset + size > body_end:
            raise AssertionError("oversized index extension")
        payload = content[offset : offset + size]
        if signature == expected_signature:
            return payload
        offset += size
    raise AssertionError(f"missing index extension: {expected_signature!r}")


def cache_tree_root_leaf_child_names(
    payload: bytes,
    object_id_bytes: int = 20,
) -> tuple[bytes, ...]:
    component_end = payload.find(b"\0")
    if component_end != 0:
        raise AssertionError("expected cache-tree root node")
    line_end = payload.find(b"\n", component_end + 1)
    if line_end < 0:
        raise AssertionError("truncated cache-tree root header")
    entry_count, subtree_count = (
        int(value) for value in payload[component_end + 1 : line_end].split(b" ", 1)
    )
    offset = line_end + 1 + (object_id_bytes if entry_count >= 0 else 0)
    names: list[bytes] = []
    for _index in range(subtree_count):
        component_end = payload.find(b"\0", offset)
        if component_end < 0:
            raise AssertionError("truncated cache-tree child name")
        names.append(payload[offset:component_end])
        line_end = payload.find(b"\n", component_end + 1)
        if line_end < 0:
            raise AssertionError("truncated cache-tree child header")
        entry_count, child_count = (
            int(value) for value in payload[component_end + 1 : line_end].split(b" ", 1)
        )
        if child_count:
            raise AssertionError("expected leaf cache-tree children")
        offset = line_end + 1 + (object_id_bytes if entry_count >= 0 else 0)
    if offset != len(payload):
        raise AssertionError("cache-tree payload has trailing data")
    return tuple(names)


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

    def create_relocated_fake_git(
        self,
        name: str,
        *,
        version: str = "2.53.0",
    ) -> tuple[Path, str]:
        prefix = self.root / name
        bin_directory = prefix / "bin"
        helper_directory = prefix / "libexec" / "git-core"
        bin_directory.mkdir(parents=True)
        helper_directory.mkdir(parents=True)
        system_exec_path = Path(
            subprocess.run(
                ["git", "--exec-path"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
        )
        for helper_name in MODULE.GIT_TRANSPORT_HELPER_NAMES:
            helper_source = (system_exec_path / helper_name).resolve(strict=True)
            helper_target = helper_directory / helper_name
            shutil.copy2(helper_source, helper_target)
            helper_target.chmod(0o700)
        content = (
            "#!/bin/sh\n"
            'if [ "$1" = "--exec-path" ]; then\n'
            f"  printf '%s\\n' {shlex.quote(str(helper_directory))}\n"
            "else\n"
            f"  printf 'git version {version}\\n'\n"
            "fi\n"
        )
        fake_git = bin_directory / "git"
        fake_git.write_text(content, encoding="utf-8")
        fake_git.chmod(0o700)
        return fake_git, content

    def create_gitlink_remote(
        self,
        name: str,
        children: tuple[tuple[str, str, Path, str], ...] = (),
    ) -> tuple[Path, str]:
        remote = self.root / f"{name}-remote"
        run_git(self.root, "init", str(remote))
        run_git(remote, "config", "user.email", "test@example.com")
        run_git(remote, "config", "user.name", "Test User")
        (remote / "README.md").write_text(f"{name}\n", encoding="utf-8")
        run_git(remote, "add", "README.md")
        if children:
            gitmodules_lines: list[str] = []
            for child_name, child_path, child_remote, child_sha in children:
                gitmodules_lines.extend(
                    [
                        f'[submodule "{child_name}"]',
                        f"    path = {child_path}",
                        f"    url = {child_remote}",
                        "",
                    ]
                )
                run_git(
                    remote,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{child_sha},{child_path}",
                )
            (remote / ".gitmodules").write_text(
                "\n".join(gitmodules_lines),
                encoding="utf-8",
            )
            run_git(remote, "add", ".gitmodules")
        run_git(remote, "commit", "-m", f"create {name}")
        return remote, run_git(remote, "rev-parse", "HEAD")

    def clone_recursive_source(
        self,
        source_git_dir: Path,
        remote: Path,
        standard_name: str,
    ) -> None:
        source_git_dir.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            self.root,
            "clone",
            "--separate-git-dir",
            str(source_git_dir),
            str(remote),
            str(self.root / standard_name),
        )

    def make_grouped_recursive_plan(
        self,
        scenario: str,
        child_paths: tuple[str, str],
        *,
        regular_paths: tuple[str, ...] = (),
    ) -> tuple[
        MODULE.SyncPlan,
        Path,
        Path,
        tuple[tuple[Path, str], ...],
    ]:
        children: list[tuple[str, str, Path, str]] = []
        child_results: list[tuple[Path, str]] = []
        for child_name, child_path in zip(
            ("left", "right"),
            child_paths,
        ):
            remote, sha = self.create_gitlink_remote(
                f"{scenario}-{child_name}",
            )
            children.append((child_name, child_path, remote, sha))
            child_results.append((remote, sha))
        parent_remote, parent_sha = self.create_gitlink_remote(
            f"{scenario}-parent",
            tuple(children),
        )
        for relative_path in regular_paths:
            regular = parent_remote / relative_path
            regular.parent.mkdir(parents=True, exist_ok=True)
            regular.write_text(f"{scenario}\n", encoding="utf-8")
            run_git(parent_remote, "add", relative_path)
        if regular_paths:
            run_git(parent_remote, "commit", "-m", "add regular paths")
            parent_sha = run_git(parent_remote, "rev-parse", "HEAD")

        source_common = self.root / f"{scenario}-source" / ".git"
        parent_source = source_common / "modules" / "parent"
        self.clone_recursive_source(
            parent_source,
            parent_remote,
            f"{scenario}-parent-standard",
        )
        for child_name, _child_path, child_remote, _child_sha in children:
            self.clone_recursive_source(
                parent_source / "modules" / child_name,
                child_remote,
                f"{scenario}-{child_name}-standard",
            )

        target_super = self.root / f"{scenario}-target"
        target_super.mkdir()
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=source_common,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "parent",
                        "parent",
                        str(parent_remote),
                    ),
                    parent_sha,
                )
            ],
            depth=1,
            recursive=True,
            force_replace_empty=False,
            fetch_missing=False,
        )
        return plan, target_super, parent_source, tuple(child_results)

    def make_grouped_recursive_plan_with_grandchild(
        self,
        scenario: str,
    ) -> tuple[MODULE.SyncPlan, Path, Path]:
        grandchild_remote, grandchild_sha = self.create_gitlink_remote(
            f"{scenario}-grandchild",
        )
        left_remote, left_sha = self.create_gitlink_remote(
            f"{scenario}-left",
            (
                (
                    "grandchild",
                    "nested/grandchild",
                    grandchild_remote,
                    grandchild_sha,
                ),
            ),
        )
        right_remote, right_sha = self.create_gitlink_remote(
            f"{scenario}-right",
        )
        parent_remote, parent_sha = self.create_gitlink_remote(
            f"{scenario}-parent",
            (
                ("left", "group/left", left_remote, left_sha),
                ("right", "group/right", right_remote, right_sha),
            ),
        )

        source_common = self.root / f"{scenario}-source" / ".git"
        parent_source = source_common / "modules" / "parent"
        left_source = parent_source / "modules" / "left"
        self.clone_recursive_source(
            parent_source,
            parent_remote,
            f"{scenario}-parent-standard",
        )
        self.clone_recursive_source(
            left_source,
            left_remote,
            f"{scenario}-left-standard",
        )
        self.clone_recursive_source(
            left_source / "modules" / "grandchild",
            grandchild_remote,
            f"{scenario}-grandchild-standard",
        )
        self.clone_recursive_source(
            parent_source / "modules" / "right",
            right_remote,
            f"{scenario}-right-standard",
        )

        target_super = self.root / f"{scenario}-target"
        target_super.mkdir()
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=source_common,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "parent",
                        "parent",
                        str(parent_remote),
                    ),
                    parent_sha,
                )
            ],
            depth=1,
            recursive=True,
            force_replace_empty=False,
            fetch_missing=False,
        )
        return plan, target_super, parent_source

    def fetch_source(self, source_git_dir: Path) -> None:
        run_git(
            self.root,
            f"--git-dir={source_git_dir}",
            "fetch",
            "--no-tags",
            "origin",
        )

    def make_shallow_install_receipt(
        self,
        *,
        initial_content: bytes | None,
        fetched_content: bytes | None,
        shared_repository: str | None = None,
    ) -> MODULE.TransportReceipt:
        source_shallow = self.named_source_git_dir / MODULE.SOURCE_SHALLOW_NAME
        if initial_content is not None:
            source_shallow.write_bytes(initial_content)
        if shared_repository is not None:
            run_git(
                self.root,
                f"--git-dir={self.named_source_git_dir}",
                "config",
                "core.sharedRepository",
                shared_repository,
            )
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        private_shallow = receipt.fetch_git_dir / MODULE.SOURCE_SHALLOW_NAME
        if fetched_content is None:
            private_shallow.unlink(missing_ok=True)
        else:
            private_shallow.write_bytes(fetched_content)
        self.addCleanup(receipt.fetch_guard.cleanup)
        return receipt

    def write_descriptor_relative_file(
        self,
        directory_descriptor: int,
        name: str,
        content: bytes,
    ) -> None:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            self.assertEqual(os.write(descriptor, content), len(content))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def make_target_superproject(
        self,
        name: str,
        sha: str,
        *,
        module_name: str = "custom-lib",
        module_path: str = "third_party/libexample",
        url: str | None = None,
    ) -> tuple[
        Path,
        MODULE.Submodule,
        MODULE.PlanInputReceipt,
    ]:
        target = self.root / name
        run_git(self.root, "init", str(target))
        module = MODULE.Submodule(
            module_name,
            module_path,
            url or str(self.remote),
        )
        (target / ".gitmodules").write_text(
            "\n".join(
                [
                    f'[submodule "{module.name}"]',
                    f"    path = {module.path}",
                    f"    url = {module.url}",
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
            f"160000,{sha},{module.path}",
        )
        _, gitmodules_binding = MODULE.capture_worktree_gitmodules(target)
        self.assertIsNotNone(gitmodules_binding)
        index_receipt = MODULE.capture_superproject_index_receipt(
            target,
            (module.path,),
        )
        return (
            target,
            module,
            MODULE.PlanInputReceipt(
                gitmodules_binding=gitmodules_binding,
                superproject_index=index_receipt,
            ),
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
        MODULE.run(
            [
                "git",
                "-C",
                str(target),
                "update-index",
                "--no-split-index",
                "--no-untracked-cache",
                "--no-fsmonitor",
                "--force-write-index",
            ]
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

    def test_fetch_transport_rejects_adversarial_source_config(self) -> None:
        cases = (
            ("include.path", str(self.root / "included-config")),
            ("includeIf.gitdir:/tmp/.path", str(self.root / "conditional-config")),
            ("core.sshCommand", "sh -c 'touch attacker-marker'"),
            ("core.gitProxy", "sh -c 'touch attacker-marker'"),
            ("http.proxy", "https://proxy.example.invalid"),
            ("http.extraHeader", "Authorization: attacker"),
            ("credential.helper", "!sh -c 'touch attacker-marker'"),
            (
                "url.https://attacker.example.invalid/.insteadOf",
                str(self.remote),
            ),
            ("remote.origin.uploadpack", "sh -c 'touch attacker-marker'"),
            ("protocol.ext.allow", "always"),
            ("extensions.worktreeConfig", "true"),
            ("fetch.bundleURI", "https://attacker.example.invalid/bundle"),
            ("fetch.bundleCreationToken", "attacker-token"),
            ("transfer.bundleURI", "https://attacker.example.invalid/bundle"),
            ("core.alternateRefsCommand", "sh -c 'touch attacker-marker'"),
            ("ssh.variant", "simple"),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                source = self.clone_named_source(f"unsafe-config-{index}")
                run_git(
                    self.root,
                    f"--git-dir={source}",
                    "config",
                    "--add",
                    key,
                    value,
                )
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "fetch-executable, credential, proxy, include, or URL-redirection",
                ):
                    MODULE.capture_transport_receipt(
                        source,
                        MODULE.Submodule(
                            f"unsafe-config-{index}",
                            f"third_party/unsafe-{index}",
                            str(self.remote),
                        ),
                    )

    def test_fetch_transport_freezes_source_object_write_policy(self) -> None:
        source = self.clone_named_source("object-write-policy")
        settings = (
            ("fetch.fsckObjects", "false"),
            ("transfer.fsckObjects", "true"),
            ("core.sharedRepository", "0660"),
            ("core.fsync", "all,-commit-graph"),
            ("core.fsyncMethod", "fsync"),
        )
        for key, value in settings:
            run_git(
                self.root,
                f"--git-dir={source}",
                "config",
                key,
                value,
            )
        submodule = MODULE.Submodule(
            "object-write-policy",
            "third_party/object-write-policy",
            str(self.remote),
        )

        receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        self.assertEqual(receipt.fetch_object_policy, settings)
        for key, expected in settings:
            self.assertEqual(
                run_git(
                    self.root,
                    f"--git-dir={receipt.fetch_git_dir}",
                    "config",
                    "--get",
                    key,
                ),
                expected,
            )
        MODULE.revalidate_transport_receipt(receipt, submodule)
        with (receipt.fetch_git_dir / "config").open("a", encoding="ascii") as stream:
            stream.write("[fetch]\n\tfsckObjects = true\n")
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "content changed after preflight",
        ):
            MODULE.revalidate_transport_receipt(receipt, submodule)

    def test_fetch_transport_normalizes_valueless_supported_booleans(self) -> None:
        source = self.clone_named_source("valueless-object-policy")
        config_path = source / "config"
        with config_path.open("ab") as stream:
            stream.write(
                b"[fetch]\n"
                b"\tfsckObjects\n"
                b"[transfer]\n"
                b"\tfsckObjects =\n"
                b"[core]\n"
                b"\tsharedRepository\n"
            )
        submodule = MODULE.Submodule(
            "valueless-object-policy",
            "third_party/valueless-object-policy",
            str(self.remote),
        )

        receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        self.assertEqual(
            receipt.fetch_object_policy,
            (
                ("fetch.fsckObjects", "true"),
                ("transfer.fsckObjects", "false"),
                ("core.sharedRepository", "group"),
            ),
        )
        MODULE.revalidate_transport_receipt(receipt, submodule)

    def test_fetch_transport_normalizes_empty_shared_repository(self) -> None:
        source = self.clone_named_source("empty-shared-repository")
        config_path = source / "config"
        with config_path.open("ab") as stream:
            stream.write(b"[core]\n\tsharedRepository =\n")
        submodule = MODULE.Submodule(
            "empty-shared-repository",
            "third_party/empty-shared-repository",
            str(self.remote),
        )

        receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        self.assertEqual(
            receipt.fetch_object_policy,
            (("core.sharedRepository", "umask"),),
        )
        MODULE.revalidate_transport_receipt(receipt, submodule)

    def test_fetch_policy_boolean_uses_git_native_semantics(self) -> None:
        source = self.named_source_git_dir / "config"
        cases = (
            ("true", "true"),
            ("yes", "true"),
            ("on", "true"),
            ("1", "true"),
            ("2", "true"),
            ("", "false"),
            ("false", "false"),
            ("no", "false"),
            ("off", "false"),
            ("0", "false"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    MODULE.normalize_fetch_policy_boolean(
                        value,
                        "fetch.fsckobjects",
                        source,
                    ),
                    expected,
                )

        for value in ("maybe", "truthy"):
            with self.subTest(invalid=value):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "invalid fetch object-policy boolean",
                ):
                    MODULE.normalize_fetch_policy_boolean(
                        value,
                        "fetch.fsckobjects",
                        source,
                    )

    def test_fetch_transport_rejects_unsupported_valueless_policy(self) -> None:
        source = self.clone_named_source("unsupported-valueless-policy")
        config_path = source / "config"
        with config_path.open("ab") as stream:
            stream.write(b"[core]\n\tfsync\n")

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "unsupported valueless entry",
        ):
            MODULE.capture_transport_receipt(
                source,
                MODULE.Submodule(
                    "unsupported-valueless-policy",
                    "third_party/unsupported-valueless-policy",
                    str(self.remote),
                ),
            )

    def test_fetch_transport_rejects_malformed_supported_boolean(self) -> None:
        source = self.clone_named_source("malformed-object-policy")
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "fetch.fsckObjects",
            "truthy",
        )

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "invalid fetch object-policy boolean",
        ):
            MODULE.capture_transport_receipt(
                source,
                MODULE.Submodule(
                    "malformed-object-policy",
                    "third_party/malformed-object-policy",
                    str(self.remote),
                ),
            )

    def test_shared_repository_normalizes_git_native_values(self) -> None:
        cases = (
            ("", "umask"),
            ("0", "umask"),
            ("false", "umask"),
            ("umask", "umask"),
            ("1", "group"),
            ("true", "group"),
            ("group", "group"),
            ("2", "all"),
            ("all", "all"),
            ("world", "all"),
            ("everybody", "all"),
            ("0640", "0640"),
            ("0660", "0660"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    MODULE.normalize_shared_repository(
                        value,
                        self.named_source_git_dir / "config",
                    ),
                    expected,
                )

    def test_git_init_shared_enums_round_trip_through_policy_capture(self) -> None:
        cases = (
            ("group", "1", "group"),
            ("all", "2", "all"),
        )
        for shared, persisted, expected in cases:
            with self.subTest(shared=shared):
                repository = self.root / f"git-init-shared-{shared}.git"
                run_git(
                    self.root,
                    "init",
                    "--bare",
                    f"--shared={shared}",
                    str(repository),
                )
                observed = run_git(
                    self.root,
                    f"--git-dir={repository}",
                    "config",
                    "--get",
                    "core.sharedRepository",
                )
                self.assertEqual(observed, persisted)
                config_path = repository / "config"
                entries = MODULE.parse_bound_git_config(
                    config_path.read_bytes(),
                    config_path,
                )
                self.assertEqual(
                    MODULE.capture_fetch_object_policy(
                        entries,
                        config_path,
                    ),
                    (("core.sharedRepository", expected),),
                )

    def test_fetch_url_classifies_scp_style_before_generic_schemes(self) -> None:
        cases = (
            ("host:path", True),
            ("host:/absolute/path", True),
            ("user@host:path", True),
            ("http:path", True),
            ("file:/path", True),
            ("ssh://host/path", True),
            ("http://host/path", False),
            ("https://host/path", False),
            ("git://host/path", False),
            ("file:///absolute/path", False),
        )
        for url, expected_ssh in cases:
            with self.subTest(url=url):
                MODULE.validate_approved_fetch_url(url, "third_party/example")
                self.assertEqual(MODULE.transport_uses_ssh(url), expected_ssh)

        for url in (
            "foo://host/path",
            "user@host://path",
            "ext::command",
            "host:\npath",
            "-host:path",
        ):
            with self.subTest(rejected=url):
                with self.assertRaises(MODULE.PlanError):
                    MODULE.validate_approved_fetch_url(
                        url,
                        "third_party/example",
                    )

    def test_username_less_scp_fetch_uses_private_ssh_snapshot(self) -> None:
        source = self.clone_named_source("scp-style-ssh")
        approved_url = "example.invalid:org/repo.git"
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "remote.origin.url",
            approved_url,
        )
        ssh_source = self.root / "scp-style-ssh-executable"
        ssh_source.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        ssh_source.chmod(0o700)
        submodule = MODULE.Submodule(
            "scp-style-ssh",
            "third_party/scp-style-ssh",
            approved_url,
        )

        with mock.patch.object(
            MODULE.shutil,
            "which",
            return_value=str(ssh_source),
        ):
            receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        self.assertIsNotNone(receipt.ssh_executable_snapshot)
        self.assertIsNotNone(receipt.ssh_command)
        MODULE.revalidate_transport_receipt(receipt, submodule)
        command = MODULE.transport_fetch_command(
            source,
            receipt,
            "f" * 40,
            1,
        )
        self.assertIn(
            f"core.sshCommand={receipt.ssh_command}",
            command,
        )
        self.assertEqual(command[-2], approved_url)
        self.assertEqual(command[-1], "f" * 40)

    def test_ssh_fetch_uses_private_snapshot_after_source_mutation(self) -> None:
        trusted_content = "#!/bin/sh\nprintf 'trusted-ssh\\n' >&2\nexit 1\n"
        mutated_content = "#!/bin/sh\nprintf 'mutated-ssh\\n' >&2\nexit 1\n"
        self.assertEqual(len(trusted_content), len(mutated_content))
        original_which = shutil.which

        for mutation in ("replace", "in-place"):
            with self.subTest(mutation=mutation):
                source = self.clone_named_source(f"ssh-snapshot-{mutation}")
                approved_url = f"ssh://example.invalid/ssh-snapshot-{mutation}.git"
                run_git(
                    self.root,
                    f"--git-dir={source}",
                    "config",
                    "remote.origin.url",
                    approved_url,
                )
                ssh_source = self.root / f"ssh-{mutation}"
                ssh_source.write_text(trusted_content, encoding="utf-8")
                ssh_source.chmod(0o700)
                source_inode = ssh_source.stat().st_ino
                submodule = MODULE.Submodule(
                    f"ssh-snapshot-{mutation}",
                    f"third_party/ssh-snapshot-{mutation}",
                    approved_url,
                )

                with mock.patch.object(
                    MODULE.shutil,
                    "which",
                    side_effect=lambda name: (
                        str(ssh_source) if name == "ssh" else original_which(name)
                    ),
                ):
                    receipt = MODULE.capture_transport_receipt(
                        source,
                        submodule,
                    )
                self.addCleanup(receipt.fetch_guard.cleanup)
                ssh_snapshot = receipt.ssh_executable_snapshot
                self.assertIsNotNone(ssh_snapshot)
                self.assertNotEqual(
                    ssh_snapshot.executable,
                    ssh_source.resolve(),
                )
                self.assertEqual(
                    ssh_snapshot.executable.read_text(encoding="utf-8"),
                    trusted_content,
                )
                self.assertEqual(
                    stat.S_IMODE(ssh_snapshot.executable.stat().st_mode),
                    0o500,
                )
                MODULE.revalidate_transport_receipt(receipt, submodule)

                if mutation == "replace":
                    prior_source = ssh_source.with_name(f"{ssh_source.name}-original")
                    ssh_source.rename(prior_source)
                    ssh_source.write_text(mutated_content, encoding="utf-8")
                    ssh_source.chmod(0o700)
                    self.assertNotEqual(ssh_source.stat().st_ino, source_inode)
                else:
                    ssh_source.write_text(mutated_content, encoding="utf-8")
                    self.assertEqual(ssh_source.stat().st_ino, source_inode)

                command = MODULE.transport_fetch_command(
                    source,
                    receipt,
                    "f" * 40,
                    1,
                )
                self.assertIn(
                    f"core.sshCommand={receipt.ssh_command}",
                    command,
                )
                self.assertNotIn(str(ssh_source), receipt.ssh_command)
                result = MODULE.run_bounded_bytes(
                    command,
                    check=False,
                    timeout_seconds=5,
                    stdout_limit=4096,
                    stderr_limit=4096,
                    fixed_env=dict(receipt.git_environment),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"trusted-ssh", result.stderr)
                self.assertNotIn(b"mutated-ssh", result.stderr)

                if mutation == "replace":
                    ssh_source.unlink()
                    prior_source.rename(ssh_source)
                else:
                    ssh_source.write_text(trusted_content, encoding="utf-8")
                MODULE.revalidate_transport_receipt(receipt, submodule)
                ssh_snapshot.executable.chmod(0o700)
                ssh_snapshot.executable.write_text(
                    mutated_content,
                    encoding="utf-8",
                )
                ssh_snapshot.executable.chmod(0o500)
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "snapshot content changed",
                ):
                    MODULE.revalidate_transport_receipt(receipt, submodule)

                snapshot_root = ssh_snapshot.executable.parent
                receipt.fetch_guard.cleanup()
                self.assertFalse(snapshot_root.exists())
                self.assertFalse(receipt.fetch_git_dir.exists())

    def test_source_shallow_creation_policy_matches_shared_repository_modes(
        self,
    ) -> None:
        parent_binding = MODULE.capture_typed_access(
            self.named_source_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "test source shallow parent",
            stat.S_IFDIR,
        )
        cases = (
            ("group", 0o022, 0o664),
            ("group", 0o002, 0o664),
            ("group", 0o027, 0o660),
            ("all", 0o027, 0o664),
            ("0640", 0o077, 0o640),
        )
        for shared_repository, process_umask, expected in cases:
            with self.subTest(
                shared_repository=shared_repository,
                process_umask=oct(process_umask),
            ):
                policy = (("core.sharedRepository", shared_repository),)
                with mock.patch.object(
                    MODULE,
                    "capture_process_umask",
                    return_value=process_umask,
                ):
                    creation = MODULE.capture_source_shallow_creation_policy(
                        parent_binding,
                        policy,
                    )
                self.assertEqual(creation.permissions, expected)
                self.assertEqual(creation.owner, os.geteuid())
                self.assertEqual(creation.group, os.getegid())

    def test_process_umask_capture_does_not_change_parent_policy(self) -> None:
        previous_umask = os.umask(0o027)
        try:
            self.assertEqual(MODULE.capture_process_umask(), 0o027)
            observed_umask = os.umask(0o027)
            self.assertEqual(observed_umask, 0o027)
        finally:
            os.umask(previous_umask)

    def test_fetch_transport_rejects_unreproducible_object_write_policy(
        self,
    ) -> None:
        cases = (
            ("core.fsyncObjectFiles", "true"),
            ("fetch.fsck.missingEmail", "ignore"),
            ("fetch.fsck.skipList", str(self.root / "skip-list")),
            ("fetch.unpackLimit", "1"),
            ("transfer.unpackLimit", "1"),
            ("core.createObject", "link"),
            ("core.fsync", "pack,unknown-component"),
            ("core.fsync", "none,pack"),
            ("core.fsyncMethod", "unknown-method"),
            ("core.fsyncMethod", "batch"),
            ("core.sharedRepository", "unknown-mode"),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                source = self.clone_named_source(f"object-policy-{index}")
                run_git(
                    self.root,
                    f"--git-dir={source}",
                    "config",
                    key,
                    value,
                )
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "cannot reproduce safely|unsupported|mixes",
                ):
                    MODULE.capture_transport_receipt(
                        source,
                        MODULE.Submodule(
                            f"object-policy-{index}",
                            f"third_party/object-policy-{index}",
                            str(self.remote),
                        ),
                    )

        duplicate_source = self.clone_named_source("duplicate-object-policy")
        for value in ("pack", "all"):
            run_git(
                self.root,
                f"--git-dir={duplicate_source}",
                "config",
                "--add",
                "core.fsync",
                value,
            )
        with self.assertRaisesRegex(MODULE.PlanError, "duplicate object-write policy"):
            MODULE.capture_transport_receipt(
                duplicate_source,
                MODULE.Submodule(
                    "duplicate-object-policy",
                    "third_party/duplicate-object-policy",
                    str(self.remote),
                ),
            )

    def test_authorized_fetch_applies_bound_shared_repository_mode(self) -> None:
        source = self.clone_named_source("shared-object-policy")
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "core.sharedRepository",
            "0660",
        )
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "fetch.fsckObjects",
            "true",
        )
        before = {
            path.relative_to(source)
            for path in (source / "objects").rglob("*")
            if path.is_file()
        }

        (self.remote / "SECOND.md").write_text("second\n", encoding="utf-8")
        run_git(self.remote, "add", "SECOND.md")
        run_git(self.remote, "commit", "-m", "second")
        (self.remote / "THIRD.md").write_text("third\n", encoding="utf-8")
        run_git(self.remote, "add", "THIRD.md")
        run_git(self.remote, "commit", "-m", "third")
        second_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            "shared-object-policy",
            "third_party/shared-object-policy",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        with redirect_stdout(io.StringIO()):
            self.assertTrue(
                MODULE.fetch_missing_commit(
                    source,
                    self.root / "shared-object-target",
                    submodule,
                    second_sha,
                    1,
                    dry_run=False,
                    transport_receipt=receipt,
                    fetch_missing=True,
                )
            )

        new_object_files = [
            path
            for path in (source / "objects").rglob("*")
            if path.is_file() and path.relative_to(source) not in before
        ]
        self.assertTrue(new_object_files)
        for path in new_object_files:
            with self.subTest(path=path):
                self.assertEqual(path.stat().st_mode & 0o077, 0o040)
        shallow = source / MODULE.SOURCE_SHALLOW_NAME
        self.assertTrue(shallow.is_file())
        self.assertIsNotNone(receipt.source_shallow_creation_policy)
        creation_policy = receipt.source_shallow_creation_policy
        self.assertEqual(
            shallow.stat().st_mode & 0o777,
            creation_policy.permissions,
        )
        self.assertEqual(shallow.stat().st_uid, creation_policy.owner)
        self.assertEqual(shallow.stat().st_gid, creation_policy.group)
        run_git(
            self.root,
            f"--git-dir={source}",
            "fsck",
            "--full",
        )

    def test_fetch_transport_requires_one_exact_approved_url(self) -> None:
        source = self.clone_named_source("mismatched-origin")
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "remote.origin.url",
            str(self.root / "other-remote"),
        )
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "does not match the task-approved",
        ):
            MODULE.capture_transport_receipt(
                source,
                MODULE.Submodule(
                    "mismatched-origin",
                    "third_party/mismatched-origin",
                    str(self.remote),
                ),
            )

        duplicate_source = self.clone_named_source("duplicate-origin")
        run_git(
            self.root,
            f"--git-dir={duplicate_source}",
            "config",
            "--add",
            "remote.origin.url",
            str(self.remote),
        )
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "exactly one remote.origin.url",
        ):
            MODULE.capture_transport_receipt(
                duplicate_source,
                MODULE.Submodule(
                    "duplicate-origin",
                    "third_party/duplicate-origin",
                    str(self.remote),
                ),
            )

    def test_fetch_transport_binding_allows_mtime_only_but_rejects_content_change(
        self,
    ) -> None:
        source = self.clone_named_source("transport-content")
        submodule = MODULE.Submodule(
            "transport-content",
            "third_party/transport-content",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(source, submodule)
        config_path = source / "config"
        config_stat = config_path.stat()
        os.utime(
            config_path,
            ns=(
                config_stat.st_atime_ns,
                config_stat.st_mtime_ns + 1_000_000_000,
            ),
        )
        MODULE.revalidate_transport_receipt(receipt, submodule)

        content = config_path.read_bytes()
        old = os.fsencode(str(self.remote))
        replacement = old[:-1] + bytes([old[-1] ^ 1])
        self.assertEqual(len(old), len(replacement))
        self.assertIn(old, content)
        config_path.write_bytes(content.replace(old, replacement, 1))
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "content changed after preflight",
        ):
            MODULE.revalidate_transport_receipt(receipt, submodule)

    def test_fetch_transport_binding_rejects_config_object_replacement(self) -> None:
        source = self.clone_named_source("transport-object")
        submodule = MODULE.Submodule(
            "transport-object",
            "third_party/transport-object",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(source, submodule)
        config_path = source / "config"
        replacement = source / "config.replacement"
        replacement.write_bytes(config_path.read_bytes())
        replacement.chmod(config_path.stat().st_mode & 0o777)
        os.replace(replacement, config_path)

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "object or access policy changed",
        ):
            MODULE.revalidate_transport_receipt(receipt, submodule)

    def test_fetch_transport_binds_existing_and_absent_source_shallow_state(
        self,
    ) -> None:
        existing_source = self.clone_named_source("transport-existing-shallow")
        existing_shallow = existing_source / "shallow"
        existing_shallow.write_text(f"{self.sha}\n", encoding="ascii")
        existing_submodule = MODULE.Submodule(
            "transport-existing-shallow",
            "third_party/transport-existing-shallow",
            str(self.remote),
        )
        existing_receipt = MODULE.capture_transport_receipt(
            existing_source,
            existing_submodule,
        )
        existing_shallow.write_text(f"{'f' * 40}\n", encoding="ascii")
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "content changed after preflight",
        ):
            MODULE.revalidate_transport_receipt(
                existing_receipt,
                existing_submodule,
            )

        absent_source = self.clone_named_source("transport-absent-shallow")
        absent_submodule = MODULE.Submodule(
            "transport-absent-shallow",
            "third_party/transport-absent-shallow",
            str(self.remote),
        )
        absent_receipt = MODULE.capture_transport_receipt(
            absent_source,
            absent_submodule,
        )
        (absent_source / "shallow").write_text(f"{self.sha}\n", encoding="ascii")
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "appeared after preflight",
        ):
            MODULE.revalidate_transport_receipt(
                absent_receipt,
                absent_submodule,
            )

    def test_private_fetch_control_files_keep_independent_size_limits(self) -> None:
        self.assertEqual(MODULE.MAX_SOURCE_CONFIG_BYTES, 4 * 1024 * 1024)
        self.assertEqual(MODULE.MAX_GITDIR_FILE_BYTES, 64 * 1024)
        self.assertEqual(MODULE.MAX_SOURCE_SHALLOW_BYTES, 64 * 1024 * 1024)
        shallow_content = b"x" * (MODULE.MAX_SOURCE_CONFIG_BYTES + 1)
        guard, _gitdir, _directory_bindings, file_bindings = (
            MODULE.capture_fetch_control_gitdir(
                "sha1",
                "third_party/large-shallow",
                shallow_content,
                (),
            )
        )
        self.addCleanup(guard.cleanup)
        bindings = {binding.path.name: binding for binding in file_bindings}

        self.assertEqual(
            bindings["config"].maximum_bytes,
            MODULE.MAX_SOURCE_CONFIG_BYTES,
        )
        self.assertEqual(
            bindings["HEAD"].maximum_bytes,
            MODULE.MAX_GITDIR_FILE_BYTES,
        )
        self.assertEqual(
            bindings["shallow"].maximum_bytes,
            MODULE.MAX_SOURCE_SHALLOW_BYTES,
        )
        self.assertEqual(bindings["shallow"].size, len(shallow_content))

    def test_private_fetch_shallow_rejects_content_over_its_own_limit(
        self,
    ) -> None:
        with mock.patch.object(MODULE, "MAX_SOURCE_SHALLOW_BYTES", 1024):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "isolated fetch shallow boundary.*exceeds the 1024-byte",
            ):
                MODULE.capture_fetch_control_gitdir(
                    "sha1",
                    "third_party/oversized-shallow",
                    b"x" * 1025,
                    (),
                )

    def test_fetch_command_uses_exact_url_and_closed_transport_overrides(self) -> None:
        submodule = MODULE.Submodule(
            "custom-lib",
            "third_party/libexample",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        command = MODULE.transport_fetch_command(
            self.named_source_git_dir,
            receipt,
            self.sha,
            1,
        )

        self.assertNotIn("origin", command)
        self.assertEqual(command[-2:], [str(self.remote), self.sha])
        self.assertIn("http.proxy=", command)
        self.assertIn("http.extraHeader=", command)
        self.assertIn("http.followRedirects=false", command)
        self.assertIn("core.gitProxy=", command)
        self.assertIn("--no-recurse-submodules", command)
        self.assertIn("--no-write-fetch-head", command)
        self.assertIn("--no-auto-maintenance", command)
        self.assertIn("--no-write-commit-graph", command)
        self.assertIn(f"--git-dir={receipt.fetch_git_dir}", command)
        self.assertNotIn(
            f"--git-dir={self.named_source_git_dir}",
            command,
        )
        self.assertEqual(
            dict(receipt.git_environment)["GIT_OBJECT_DIRECTORY"],
            str((self.named_source_git_dir / "objects").resolve()),
        )
        self.assertEqual(
            dict(receipt.git_environment)["GIT_EXEC_PATH"],
            str(receipt.git_runtime_receipt.exec_path),
        )
        self.assertEqual(
            receipt.source_shallow_path,
            self.named_source_git_dir / "shallow",
        )
        self.assertNotIn("GIT_SHALLOW_FILE", dict(receipt.git_environment))

    def test_authorized_file_fetch_uses_snapshotted_git_helper_closure(
        self,
    ) -> None:
        (self.remote / "FILE-TRANSPORT.md").write_text(
            "file transport\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FILE-TRANSPORT.md")
        run_git(self.remote, "commit", "-m", "file transport")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        approved_url = self.remote.resolve(strict=True).as_uri()
        run_git(
            self.named_source_git_dir,
            "config",
            "remote.origin.url",
            approved_url,
        )
        submodule = MODULE.Submodule(
            "custom-lib",
            "third_party/libexample",
            approved_url,
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.addCleanup(receipt.fetch_guard.cleanup)

        with redirect_stdout(io.StringIO()):
            fetched = MODULE.fetch_missing_commit(
                self.named_source_git_dir,
                self.root / "file-transport-target",
                submodule,
                missing_sha,
                1,
                dry_run=False,
                transport_receipt=receipt,
                fetch_missing=True,
            )

        self.assertTrue(fetched)
        self.assertTrue(
            MODULE.commit_exists(
                self.named_source_git_dir,
                self.root / "file-transport-target",
                missing_sha,
            )
        )

    def test_http_transport_uses_snapshotted_git_helper_closure(
        self,
    ) -> None:
        (self.remote / "HTTP-TRANSPORT.md").write_text(
            "http transport\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "HTTP-TRANSPORT.md")
        run_git(self.remote, "commit", "-m", "http transport")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        bare_remote = self.root / "http-remote.git"
        run_git(
            self.root,
            "clone",
            "--bare",
            str(self.remote),
            str(bare_remote),
        )
        run_git(bare_remote, "update-server-info")

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                del args

        handler = functools.partial(
            QuietHandler,
            directory=str(self.root),
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        server_thread.start()
        approved_url = f"http://127.0.0.1:{server.server_port}/{bare_remote.name}"
        try:
            runtime = MODULE.git_runtime()
            result = MODULE.run_bounded_bytes(
                ["git", "ls-remote", approved_url],
                timeout_seconds=10,
                stdout_limit=4096,
                stderr_limit=4096,
                git_runtime_receipt=runtime,
            )
            self.assertIn(missing_sha.encode("ascii"), result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)

    def test_fetch_child_never_rereads_source_config_after_final_revalidation(
        self,
    ) -> None:
        submodule = MODULE.Submodule(
            "custom-lib",
            "third_party/libexample",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        config_path = self.named_source_git_dir / "config"
        original_revalidate = MODULE.revalidate_transport_receipt

        def revalidate_then_poison(*args: object, **kwargs: object) -> None:
            original_revalidate(*args, **kwargs)
            with config_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    "\n[fetch]\n\tbundleURI = https://attacker.example.invalid/bundle\n"
                )

        failed_fetch = subprocess.CompletedProcess(
            ["git"],
            1,
            stdout=b"",
            stderr=b"blocked test fetch",
        )
        with mock.patch.object(
            MODULE,
            "revalidate_transport_receipt",
            side_effect=revalidate_then_poison,
        ):
            with mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                return_value=failed_fetch,
            ) as bounded_fetch:
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "failed to shallow-fetch",
                    ):
                        MODULE.fetch_missing_commit(
                            self.named_source_git_dir,
                            self.root / "target",
                            submodule,
                            "f" * 40,
                            1,
                            dry_run=False,
                            transport_receipt=receipt,
                            fetch_missing=True,
                        )

        command = bounded_fetch.call_args.args[0]
        self.assertIn(f"--git-dir={receipt.fetch_git_dir}", command)
        self.assertNotIn(
            f"--git-dir={self.named_source_git_dir}",
            command,
        )
        self.assertEqual(
            bounded_fetch.call_args.kwargs["fixed_env"]["GIT_OBJECT_DIRECTORY"],
            str((self.named_source_git_dir / "objects").resolve()),
        )

    def test_authorized_fetch_uses_the_plan_frozen_closed_environment(self) -> None:
        submodule = MODULE.Submodule(
            "custom-lib",
            "third_party/libexample",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        failed_fetch = subprocess.CompletedProcess(
            ["git"],
            1,
            stdout=b"",
            stderr=b"blocked test fetch",
        )
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "attacker-home"),
                "GIT_SSH_COMMAND": "sh -c 'touch attacker-marker'",
                "HTTPS_PROXY": "https://attacker.example.invalid",
            },
            clear=False,
        ):
            with mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                return_value=failed_fetch,
            ) as bounded_fetch:
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "failed to shallow-fetch",
                ):
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "target",
                        submodule,
                        "f" * 40,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertEqual(
            bounded_fetch.call_args.kwargs["fixed_env"],
            dict(receipt.git_environment),
        )
        self.assertIs(
            bounded_fetch.call_args.kwargs["git_runtime_receipt"],
            receipt.git_runtime_receipt,
        )
        self.assertNotEqual(
            bounded_fetch.call_args.kwargs["fixed_env"].get("HOME"),
            str(self.root / "attacker-home"),
        )
        self.assertNotIn(
            "GIT_SSH_COMMAND",
            bounded_fetch.call_args.kwargs["fixed_env"],
        )
        self.assertNotIn(
            "HTTPS_PROXY",
            bounded_fetch.call_args.kwargs["fixed_env"],
        )

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

    def test_git_snapshot_rejects_same_inode_source_content_replacement(
        self,
    ) -> None:
        fake_git, original_content = self.create_relocated_fake_git("fake-git-prefix")
        replacement_content = original_content.replace("2.53.0", "9.99.9", 1)
        self.assertEqual(len(original_content), len(replacement_content))
        original_inode = fake_git.stat().st_ino

        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        try:
            self.assertNotEqual(runtime.executable, runtime.source_executable)
            self.assertEqual(
                runtime.executable.read_text(encoding="utf-8"),
                original_content,
            )
            with mock.patch.object(MODULE, "_GIT_RUNTIME", runtime):
                command = MODULE.safe_command(["git", "--version"])
                self.assertEqual(Path(command[0]), runtime.executable)

                fake_git.write_text(replacement_content, encoding="utf-8")
                self.assertEqual(fake_git.stat().st_ino, original_inode)
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "content changed after version preflight",
                ):
                    MODULE.safe_command(["git", "--version"])

            self.assertEqual(
                runtime.executable.read_text(encoding="utf-8"),
                original_content,
            )
        finally:
            runtime.snapshot_guard.cleanup()

    def test_git_exec_gate_rejects_snapshot_path_replacement_before_exec(
        self,
    ) -> None:
        fake_git, original_content = self.create_relocated_fake_git(
            "exec-gate-path-prefix"
        )
        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        snapshot = runtime.executable
        held_snapshot = snapshot.with_name("git.held")
        replacement = original_content.replace("2.53.0", "9.99.9", 1)
        original_popen = MODULE.subprocess.Popen
        replacement_performed = False

        def replace_snapshot_before_spawn(
            *args: object,
            **kwargs: object,
        ) -> subprocess.Popen[bytes]:
            nonlocal replacement_performed
            self.assertFalse(replacement_performed)
            replacement_performed = True
            snapshot.rename(held_snapshot)
            snapshot.write_text(replacement, encoding="utf-8")
            snapshot.chmod(0o500)
            return original_popen(*args, **kwargs)

        try:
            with mock.patch.object(MODULE, "_GIT_RUNTIME", runtime):
                with mock.patch.object(
                    MODULE.subprocess,
                    "Popen",
                    side_effect=replace_snapshot_before_spawn,
                ):
                    with self.assertRaisesRegex(
                        MODULE.GitError,
                        "failed to start",
                    ):
                        MODULE.run_bounded_bytes(
                            ["git", "--version"],
                            timeout_seconds=5,
                            stdout_limit=256,
                            stderr_limit=256,
                        )
            self.assertTrue(replacement_performed)
        finally:
            snapshot.unlink(missing_ok=True)
            if held_snapshot.exists():
                held_snapshot.rename(snapshot)
            runtime.snapshot_guard.cleanup()

    def test_git_exec_gate_rejects_same_inode_snapshot_rewrite_before_exec(
        self,
    ) -> None:
        fake_git, _original_source = self.create_relocated_fake_git(
            "exec-gate-content-prefix"
        )
        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        snapshot = runtime.executable
        original_content = snapshot.read_bytes()
        rewritten_content = original_content.replace(b"2.53.0", b"9.99.9", 1)
        self.assertEqual(len(rewritten_content), len(original_content))
        original_inode = snapshot.stat().st_ino
        original_popen = MODULE.subprocess.Popen
        replacement_performed = False

        def rewrite_snapshot_before_spawn(
            *args: object,
            **kwargs: object,
        ) -> subprocess.Popen[bytes]:
            nonlocal replacement_performed
            self.assertFalse(replacement_performed)
            replacement_performed = True
            snapshot.chmod(0o700)
            snapshot.write_bytes(rewritten_content)
            snapshot.chmod(0o500)
            self.assertEqual(snapshot.stat().st_ino, original_inode)
            return original_popen(*args, **kwargs)

        try:
            with mock.patch.object(MODULE, "_GIT_RUNTIME", runtime):
                with mock.patch.object(
                    MODULE.subprocess,
                    "Popen",
                    side_effect=rewrite_snapshot_before_spawn,
                ):
                    with self.assertRaisesRegex(
                        MODULE.GitError,
                        "failed to start",
                    ):
                        MODULE.run_bounded_bytes(
                            ["git", "--version"],
                            timeout_seconds=5,
                            stdout_limit=256,
                            stderr_limit=256,
                        )
            self.assertTrue(replacement_performed)
        finally:
            snapshot.chmod(0o700)
            snapshot.write_bytes(original_content)
            snapshot.chmod(0o500)
            runtime.snapshot_guard.cleanup()

    def test_git_runtime_snapshots_a_relocated_transport_helper_closure(
        self,
    ) -> None:
        fake_git, _content = self.create_relocated_fake_git("relocated-git-prefix")
        source_exec_path = (fake_git.parent.parent / "libexec" / "git-core").resolve(
            strict=True
        )
        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        try:
            self.assertEqual(
                runtime.source_exec_path_binding.path,
                source_exec_path,
            )
            self.assertNotEqual(runtime.exec_path, source_exec_path)
            self.assertEqual(runtime.exec_path_binding.path, runtime.exec_path)
            self.assertEqual(
                tuple(receipt.executable.name for receipt in runtime.helper_snapshots),
                MODULE.GIT_TRANSPORT_HELPER_NAMES,
            )
            self.assertTrue(
                all(
                    receipt.executable.parent == runtime.exec_path
                    for receipt in runtime.helper_snapshots
                )
            )
            self.assertEqual(
                MODULE.git_environment(runtime=runtime)["GIT_EXEC_PATH"],
                str(runtime.exec_path),
            )
        finally:
            runtime.snapshot_guard.cleanup()

    def test_git_runtime_rejects_same_inode_helper_snapshot_rewrite(
        self,
    ) -> None:
        fake_git, _content = self.create_relocated_fake_git("helper-rewrite-prefix")
        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        helper_receipt = runtime.helper_snapshots[0]
        helper_snapshot = helper_receipt.executable
        original_content = helper_snapshot.read_bytes()
        rewritten_content = bytearray(original_content)
        rewritten_content[0] ^= 0x01
        original_inode = helper_snapshot.stat().st_ino
        try:
            helper_snapshot.chmod(0o700)
            helper_snapshot.write_bytes(bytes(rewritten_content))
            helper_snapshot.chmod(0o500)
            self.assertEqual(helper_snapshot.stat().st_ino, original_inode)
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "transport helper.*content changed",
            ):
                MODULE.revalidate_git_runtime(runtime)
        finally:
            helper_snapshot.chmod(0o700)
            helper_snapshot.write_bytes(original_content)
            helper_snapshot.chmod(0o500)
            runtime.snapshot_guard.cleanup()

    def test_git_child_revalidates_helper_closure_after_process_exit(
        self,
    ) -> None:
        fake_git, _content = self.create_relocated_fake_git(
            "helper-runtime-drift-prefix"
        )
        with mock.patch.object(MODULE.shutil, "which", return_value=str(fake_git)):
            runtime = MODULE.discover_git_runtime()
        helper_snapshot = runtime.helper_snapshots[0].executable
        original_content = helper_snapshot.read_bytes()
        rewritten_content = bytearray(original_content)
        rewritten_content[-1] ^= 0x01
        original_popen = MODULE.subprocess.Popen
        drifted = False

        def drift_after_spawn(
            *args: object,
            **kwargs: object,
        ) -> subprocess.Popen[bytes]:
            nonlocal drifted
            process = original_popen(*args, **kwargs)
            if not drifted:
                drifted = True
                helper_snapshot.chmod(0o700)
                helper_snapshot.write_bytes(bytes(rewritten_content))
                helper_snapshot.chmod(0o500)
            return process

        try:
            with mock.patch.object(MODULE, "_GIT_RUNTIME", runtime):
                with mock.patch.object(
                    MODULE.subprocess,
                    "Popen",
                    side_effect=drift_after_spawn,
                ):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "helper closure changed while Git was running",
                    ):
                        MODULE.run_bounded_bytes(
                            ["git", "--version"],
                            timeout_seconds=5,
                            stdout_limit=256,
                            stderr_limit=256,
                            git_runtime_receipt=runtime,
                        )
            self.assertTrue(drifted)
        finally:
            helper_snapshot.chmod(0o700)
            helper_snapshot.write_bytes(original_content)
            helper_snapshot.chmod(0o500)
            runtime.snapshot_guard.cleanup()

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

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "pthread_sigmask"),
        "requires POSIX process groups and signal masks",
    )
    def test_bounded_command_forwards_signal_cleans_group_and_redelivers(
        self,
    ) -> None:
        child_pid_path = self.root / "signal-child.pid"
        child_code = (
            "import os,signal,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "signal.signal(signal.SIGHUP,signal.SIG_IGN);"
            "open(sys.argv[1],'w').write(str(os.getpid()));"
            "time.sleep(30)"
        )
        driver_code = (
            "import importlib.util,sys;"
            f"p={str(SCRIPT_PATH)!r};"
            "s=importlib.util.spec_from_file_location('signal_sync',p);"
            "m=importlib.util.module_from_spec(s);"
            "sys.modules[s.name]=m;"
            "s.loader.exec_module(m);"
            "\ntry:\n"
            " m.run_bounded_bytes("
            "[sys.executable,'-c',sys.argv[2],sys.argv[1]],"
            "prepare_git_command=False,timeout_seconds=30)\n"
            "except m.ForwardedProcessSignal as exc:\n"
            " m.redeliver_forwarded_signal(exc)\n"
        )
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-c",
                driver_code,
                str(child_pid_path),
                child_code,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not child_pid_path.exists()
                and supervisor.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            os.kill(supervisor.pid, signal.SIGTERM)
            stdout, stderr = supervisor.communicate(timeout=8)
            self.assertEqual(
                supervisor.returncode,
                -signal.SIGTERM,
                (stdout, stderr),
            )
            child_deadline = time.monotonic() + 3
            while time.monotonic() < child_deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail(f"forwarded-signal child remained alive: {child_pid}")
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=3)

    def test_owner_private_temporary_cleanup_removes_nested_entries(self) -> None:
        guard = MODULE.OwnerPrivateTemporaryDirectory(prefix="owner-private-cleanup.")
        root = Path(guard.name)
        nested = root / "nested"
        nested.mkdir(mode=0o700)
        (nested / "payload").write_bytes(b"payload")
        (root / "link").symlink_to(nested / "payload")

        guard.cleanup()

        self.assertFalse(root.exists())

    def test_owner_private_temporary_cleanup_retains_path_replacement(
        self,
    ) -> None:
        guard = MODULE.OwnerPrivateTemporaryDirectory(
            prefix="owner-private-replacement."
        )
        original = Path(guard.name)
        held = self.root / "held-owner-private-directory"
        original.rename(held)
        original.mkdir(mode=0o700)
        sentinel = original / "sentinel"
        sentinel.write_text("replacement\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(
                MODULE.TemporaryDirectoryCleanupError,
                "recovery_schema: owner-private-temporary-cleanup-v1",
            ) as raised:
                guard.cleanup()
            self.assertEqual(
                raised.exception.status,
                "identity-unverified-retained",
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "replacement\n")
            self.assertTrue(held.is_dir())
        finally:
            shutil.rmtree(original, ignore_errors=True)
            shutil.rmtree(held, ignore_errors=True)

    def test_owner_private_temporary_cleanup_restores_check_to_rename_race(
        self,
    ) -> None:
        guard = MODULE.OwnerPrivateTemporaryDirectory(prefix="owner-private-race.")
        original = Path(guard.name)
        held = self.root / "held-owner-private-race"
        real_rename = MODULE.descriptor_atomic_rename_noreplace
        raced = False

        def race_then_rename(
            directory_descriptor: int,
            source_name: str,
            target_name: str,
        ) -> None:
            nonlocal raced
            if not raced and source_name == original.name:
                raced = True
                original.rename(held)
                original.mkdir(mode=0o700)
                (original / "sentinel").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )
            real_rename(
                directory_descriptor,
                source_name,
                target_name,
            )

        try:
            with mock.patch.object(
                MODULE,
                "descriptor_atomic_rename_noreplace",
                side_effect=race_then_rename,
            ):
                with self.assertRaisesRegex(
                    MODULE.TemporaryDirectoryCleanupError,
                    "unexpected-replacement-retained",
                ):
                    guard.cleanup()
            self.assertTrue(raced)
            self.assertEqual(
                (original / "sentinel").read_text(encoding="utf-8"),
                "replacement\n",
            )
            self.assertTrue(held.is_dir())
        finally:
            shutil.rmtree(original, ignore_errors=True)
            shutil.rmtree(held, ignore_errors=True)

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

    def test_process_cleanup_forwards_original_signal_before_term_and_kill(
        self,
    ) -> None:
        class ReapableProcess:
            pid = 424243

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return -signal.SIGKILL

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        forwarded: list[int] = []

        def record_signal(_pid: int, signum: int) -> None:
            forwarded.append(signum)

        with mock.patch.object(MODULE.os, "killpg", side_effect=record_signal):
            MODULE.terminate_process_group(
                ReapableProcess(),
                initial_signal=signal.SIGHUP,
                cleanup_timeout_seconds=0.1,
                term_grace_seconds=0.02,
            )

        self.assertEqual(
            forwarded,
            [signal.SIGHUP, signal.SIGTERM, signal.SIGKILL],
        )

    def test_process_cleanup_never_signals_an_already_reaped_process_group(
        self,
    ) -> None:
        class ReapedProcess:
            pid = 424244

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                raise AssertionError("an already reaped child must not be waited again")

            def terminate(self) -> None:
                raise AssertionError("an already reaped child must not be terminated")

            def kill(self) -> None:
                raise AssertionError("an already reaped child must not be killed")

        with mock.patch.object(MODULE.os, "killpg") as kill_process_group:
            MODULE.terminate_process_group(
                ReapedProcess(),
                initial_signal=signal.SIGHUP,
                cleanup_timeout_seconds=0.1,
                term_grace_seconds=0.02,
            )

        kill_process_group.assert_not_called()

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

    def test_worktree_gitmodules_rejects_oversized_content_before_reading(
        self,
    ) -> None:
        gitmodules = self.root / ".gitmodules"
        gitmodules.write_bytes(b"x" * 5)

        with mock.patch.object(MODULE, "MAX_GITMODULES_FILE_BYTES", 4):
            with self.assertRaisesRegex(MODULE.PlanError, "per-file limit"):
                MODULE.read_worktree_gitmodules(self.root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO support is required")
    def test_worktree_gitmodules_rejects_fifo_without_blocking(self) -> None:
        gitmodules = self.root / ".gitmodules"
        os.mkfifo(gitmodules)
        started = time.monotonic()

        with self.assertRaisesRegex(MODULE.PlanError, "not a regular file"):
            MODULE.read_worktree_gitmodules(self.root)

        self.assertLess(time.monotonic() - started, 1)

    def test_commit_gitmodules_rejects_oversized_blob_before_content_read(
        self,
    ) -> None:
        object_id = b"a" * 40
        tree_result = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=b"100644 blob " + object_id + b"\t.gitmodules\0",
            stderr=b"",
        )
        size_result = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=b"5\n",
            stderr=b"",
        )

        with mock.patch.object(MODULE, "MAX_GITMODULES_FILE_BYTES", 4):
            with mock.patch.object(
                MODULE,
                "read_git_bounded",
                side_effect=[tree_result, size_result],
            ) as bounded_git:
                with self.assertRaisesRegex(MODULE.PlanError, "per-file limit"):
                    MODULE.read_commit_gitmodules(
                        self.source_git_dir,
                        self.standard,
                        self.sha,
                    )

        self.assertEqual(bounded_git.call_count, 2)

    def test_commit_gitmodules_honors_expired_shared_deadline(self) -> None:
        budget = MODULE.GitmodulesReadBudget(
            deadline=time.monotonic() - 1,
        )

        with mock.patch.object(MODULE, "read_git_bounded") as bounded_git:
            with self.assertRaisesRegex(MODULE.PlanError, "shared .* deadline"):
                MODULE.read_commit_gitmodules(
                    self.source_git_dir,
                    self.standard,
                    self.sha,
                    budget,
                )

        bounded_git.assert_not_called()

    def test_gitmodules_reads_share_one_retained_content_budget(self) -> None:
        root_content = (
            b'[submodule "root"]\n'
            b"    path = root\n"
            b"    url = https://example.invalid/root.git\n"
        )
        (self.root / ".gitmodules").write_bytes(root_content)
        nested_content = (
            b'[submodule "nested"]\n'
            b"    path = nested\n"
            b"    url = https://example.invalid/nested.git\n"
        )
        object_id = b"b" * 40
        tree_result = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=b"100644 blob " + object_id + b"\t.gitmodules\0",
            stderr=b"",
        )
        size_result = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=f"{len(nested_content)}\n".encode(),
            stderr=b"",
        )
        budget = MODULE.GitmodulesReadBudget(
            deadline=time.monotonic() + 5,
            retained_limit=len(root_content) + len(nested_content) - 1,
        )

        MODULE.read_worktree_gitmodules(self.root, budget)
        with mock.patch.object(
            MODULE,
            "read_git_bounded",
            side_effect=[tree_result, size_result],
        ) as bounded_git:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "shared retained-content limit",
            ):
                MODULE.read_commit_gitmodules(
                    self.source_git_dir,
                    self.standard,
                    self.sha,
                    budget,
                )

        self.assertEqual(bounded_git.call_count, 2)

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

    def test_apply_reuses_exact_shared_missing_parent(self) -> None:
        target_super = self.root / "shared-parent-target"
        target_super.mkdir()
        self.clone_named_source("shared-parent-a")
        self.clone_named_source("shared-parent-b")
        modules = [
            (
                MODULE.Submodule(
                    "shared-parent-a",
                    "vendor/a",
                    str(self.remote),
                ),
                self.sha,
            ),
            (
                MODULE.Submodule(
                    "shared-parent-b",
                    "vendor/b",
                    str(self.remote),
                ),
                self.sha,
            ),
        ]
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=modules,
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )

        self.assertEqual(set(plan.shared_missing_ancestors), {("vendor",)})
        MODULE.apply_sync_plan(plan)

        self.assertEqual(
            run_git(target_super / "vendor" / "a", "rev-parse", "HEAD"),
            self.sha,
        )
        self.assertEqual(
            run_git(target_super / "vendor" / "b", "rev-parse", "HEAD"),
            self.sha,
        )

    def test_apply_reuses_multiple_shared_missing_prefix_depths(self) -> None:
        target_super = self.root / "shared-prefix-depth-target"
        target_super.mkdir()
        module_specs = (
            ("shared-depth-a", "vendor/common/a"),
            ("shared-depth-b", "vendor/other/b"),
            ("shared-depth-c", "vendor/common/c"),
        )
        for name, _path in module_specs:
            self.clone_named_source(name)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(name, path, str(self.remote)),
                    self.sha,
                )
                for name, path in module_specs
            ],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
        )

        self.assertEqual(
            set(plan.shared_missing_ancestors),
            {("vendor",), ("vendor", "common")},
        )
        MODULE.apply_sync_plan(plan)

        for _name, path in module_specs:
            with self.subTest(path=path):
                self.assertEqual(
                    run_git(target_super / path, "rev-parse", "HEAD"),
                    self.sha,
                )

    def test_shared_parent_replacement_is_rejected_before_second_git_write(
        self,
    ) -> None:
        target_super = self.root / "shared-parent-race-target"
        target_super.mkdir()
        self.clone_named_source("shared-race-a")
        second_source = self.clone_named_source("shared-race-b")
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "shared-race-a",
                        "vendor/a",
                        str(self.remote),
                    ),
                    self.sha,
                ),
                (
                    MODULE.Submodule(
                        "shared-race-b",
                        "vendor/b",
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
        second_registry_before = run_git(
            self.root,
            f"--git-dir={second_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        quarantined = self.root / "shared-parent-race-quarantined"
        original_record = MODULE.record_materialized_shared_ancestors
        replaced = False

        def replace_after_first_receipt(
            current_plan: object,
            entry: object,
            created_nodes: object,
        ) -> None:
            nonlocal replaced
            original_record(current_plan, entry, created_nodes)
            if not replaced and entry is plan.entries[0]:
                replaced = True
                shared_parent = target_super / "vendor"
                shared_parent.rename(quarantined)
                shared_parent.mkdir()
                (shared_parent / "sentinel").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )

        with mock.patch.object(
            MODULE,
            "record_materialized_shared_ancestors",
            side_effect=replace_after_first_receipt,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "descriptor-bound target parent",
            ) as raised:
                MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertIn(
            "worktree/materialization rollback failed",
            str(raised.exception),
        )
        self.assertEqual(
            (target_super / "vendor" / "sentinel").read_text(encoding="utf-8"),
            "replacement\n",
        )
        self.assertFalse((target_super / "vendor" / "b").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={second_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            second_registry_before,
        )
        self.assertTrue(
            all(
                ancestor.materialized_node is None
                for ancestor in plan.shared_missing_ancestors.values()
            )
        )

    def test_shared_parent_external_state_blocks_before_first_mutation(
        self,
    ) -> None:
        target_super = self.root / "shared-parent-partial-target"
        target_super.mkdir()
        first_source = self.clone_named_source("shared-partial-a")
        self.clone_named_source("shared-partial-b")
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "shared-partial-a",
                        "vendor/a",
                        str(self.remote),
                    ),
                    self.sha,
                ),
                (
                    MODULE.Submodule(
                        "shared-partial-b",
                        "vendor/b",
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
        first_registry_before = run_git(
            self.root,
            f"--git-dir={first_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        (target_super / "vendor" / "b").mkdir(parents=True)
        (target_super / "vendor" / "b" / "sentinel").write_text(
            "external\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "shared target ancestor changed after preflight",
        ):
            MODULE.apply_sync_plan(plan)

        self.assertFalse((target_super / "vendor" / "a").exists())
        self.assertEqual(
            (target_super / "vendor" / "b" / "sentinel").read_text(encoding="utf-8"),
            "external\n",
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={first_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            first_registry_before,
        )

    def test_apply_rejects_parent_symlink_inserted_after_entry_revalidation(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "late-parent-symlink-target",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        outside = self.root / "late-parent-symlink-outside"
        outside.mkdir()
        original_materialize = MODULE.materialize_bound_target_directory

        def replace_parent(bound_target: MODULE.BoundTarget) -> object:
            (target / "third_party").symlink_to(outside, target_is_directory=True)
            return original_materialize(bound_target)

        with mock.patch.object(
            MODULE,
            "materialize_bound_target_directory",
            side_effect=replace_parent,
        ):
            with mock.patch.object(MODULE, "add_worktree") as add:
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "appeared during descriptor-relative materialization",
                ):
                    MODULE.apply_sync_plan(plan)

        add.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_add_rejects_final_target_replacement_before_checkout(self) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "late-final-replacement-target",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        outside = self.root / "late-final-replacement-outside"
        outside.mkdir()
        quarantined = self.root / "late-final-original"
        commands: list[list[str]] = []
        original_run = MODULE.run_git_at_directory_descriptor
        replaced = False

        def replace_final(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal replaced
            commands.append(args)
            if not replaced:
                replaced = True
                worktree_path = target / module.path
                worktree_path.rename(quarantined)
                worktree_path.symlink_to(outside, target_is_directory=True)
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=replace_final,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "path object or access policy changed|entry changed",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertEqual(len(commands), 1)
        self.assertIn("worktree", commands[0])
        self.assertEqual(commands[0][-2], ".")
        self.assertTrue((quarantined / ".git").is_file())
        self.assertEqual(list(outside.iterdir()), [])

    def test_managed_checkout_ignores_late_gitfile_admin_redirect(self) -> None:
        (self.remote / "CONTROL-RACE.md").write_text(
            "target\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "CONTROL-RACE.md")
        run_git(self.remote, "commit", "-m", "managed control target")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("managed-control-race")
        target_super = self.root / "managed-control-race-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, self.sha)
        original_gitfile = (target / ".git").read_bytes()
        expected_admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(expected_admin)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "managed-control-race",
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
        external = self.root / "managed-control-external"
        run_git(self.root, "clone", str(self.remote), str(external))
        run_git(external, "checkout", "--detach", self.sha)
        external_head_before = (external / ".git" / "HEAD").read_bytes()
        external_index_before = (external / ".git" / "index").read_bytes()
        original_run = MODULE.run_git_at_directory_descriptor
        redirected = False

        def redirect_gitfile(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal redirected
            if not redirected and "checkout" in args:
                redirected = True
                (target / ".git").write_text(
                    f"gitdir: {external / '.git'}\n",
                    encoding="utf-8",
                )
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=redirect_gitfile,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "control file",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertTrue(redirected)
        self.assertEqual(
            (external / ".git" / "HEAD").read_bytes(), external_head_before
        )
        self.assertEqual(
            (external / ".git" / "index").read_bytes(),
            external_index_before,
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={expected_admin}",
                "rev-parse",
                "HEAD",
            ),
            target_sha,
        )
        self.assertNotEqual((target / ".git").read_bytes(), original_gitfile)

    def test_managed_checkout_rejects_late_admin_entry_replacement_before_exec(
        self,
    ) -> None:
        (self.remote / "ADMIN-RACE.md").write_text(
            "target\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "ADMIN-RACE.md")
        run_git(self.remote, "commit", "-m", "managed admin target")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("managed-admin-race")
        target_super = self.root / "managed-admin-race-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, self.sha)
        expected_admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(expected_admin)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "managed-admin-race",
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
        external = self.root / "managed-admin-external"
        run_git(self.root, "clone", str(self.remote), str(external))
        run_git(external, "checkout", "--detach", self.sha)
        external_head_before = (external / ".git" / "HEAD").read_bytes()
        external_index_before = (external / ".git" / "index").read_bytes()
        quarantined = self.root / "managed-admin-quarantined"
        original_run = MODULE.run_git_at_directory_descriptor
        replaced = False

        def replace_admin(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal replaced
            if not replaced and "checkout" in args:
                replaced = True
                expected_admin.rename(quarantined)
                expected_admin.symlink_to(
                    external / ".git",
                    target_is_directory=True,
                )
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=replace_admin,
        ):
            with self.assertRaisesRegex(
                MODULE.GitError,
                "failed to start",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertEqual(
            (external / ".git" / "HEAD").read_bytes(),
            external_head_before,
        )
        self.assertEqual(
            (external / ".git" / "index").read_bytes(),
            external_index_before,
        )
        self.assertEqual(
            (quarantined / "HEAD").read_text(encoding="utf-8").strip(),
            self.sha,
        )

    def test_managed_checkout_rejects_cross_worktree_admin_backlink(self) -> None:
        (self.remote / "CROSS-ADMIN.md").write_text(
            "target\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "CROSS-ADMIN.md")
        run_git(self.remote, "commit", "-m", "cross admin target")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("cross-admin")
        target_super = self.root / "cross-admin-target"
        target_super.mkdir()
        first = target_super / "first"
        second = target_super / "second"
        self.add_managed_worktree(source, first, self.sha)
        self.add_managed_worktree(source, second, self.sha)
        first_admin = MODULE.gitdir_file_target(first)
        second_admin = MODULE.gitdir_file_target(second)
        self.assertIsNotNone(first_admin)
        self.assertIsNotNone(second_admin)
        (first / ".git").write_bytes((second / ".git").read_bytes())
        with mock.patch.object(MODULE, "checkout_existing_worktree") as checkout:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "backlink points at a different worktree",
            ):
                MODULE.build_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                "cross-admin",
                                "first",
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
        checkout.assert_not_called()

        self.assertEqual(
            (first_admin / "HEAD").read_text(encoding="utf-8").strip(),
            self.sha,
        )
        self.assertEqual(
            (second_admin / "HEAD").read_text(encoding="utf-8").strip(),
            self.sha,
        )
        self.assertFalse((first / "CROSS-ADMIN.md").exists())
        self.assertFalse((second / "CROSS-ADMIN.md").exists())

    def test_source_completeness_rejects_promisor_and_alternate_policy(self) -> None:
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "core.repositoryFormatVersion",
            "1",
        )
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "extensions.worktreeConfig",
            "true",
        )
        with self.assertRaisesRegex(MODULE.PlanError, "unsupported"):
            MODULE.capture_source_completeness_receipt(
                self.named_source_git_dir,
            )
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "--unset",
            "extensions.worktreeConfig",
        )
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "extensions.partialClone",
            "origin",
        )
        with self.assertRaisesRegex(MODULE.PlanError, "promisor policy"):
            MODULE.capture_source_completeness_receipt(
                self.named_source_git_dir,
            )
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "--unset",
            "extensions.partialClone",
        )
        run_git(
            self.root,
            f"--git-dir={self.named_source_git_dir}",
            "config",
            "core.repositoryFormatVersion",
            "0",
        )
        alternates = self.named_source_git_dir / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(
            str(self.remote / ".git" / "objects") + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MODULE.PlanError, "alternate object database"):
            MODULE.capture_source_completeness_receipt(
                self.named_source_git_dir,
            )
        alternates.unlink()
        promisor = self.named_source_git_dir / "objects" / "pack" / "pack-test.PROMISOR"
        promisor.parent.mkdir(parents=True, exist_ok=True)
        promisor.write_text("promisor\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.PlanError, "promisor pack"):
            MODULE.capture_source_completeness_receipt(
                self.named_source_git_dir,
            )

    def test_source_completeness_rejects_commondir_indirection(self) -> None:
        (self.named_source_git_dir / "commondir").write_text(
            str(self.remote / ".git") + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.PlanError, "commondir indirection"):
            MODULE.capture_source_completeness_receipt(
                self.named_source_git_dir,
            )

    def test_late_commondir_cannot_redirect_worktree_registry_write(self) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "late-commondir-target",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        outside_common = self.root / "late-commondir-outside.git"
        shutil.copytree(self.named_source_git_dir, outside_common)
        self.assertFalse((outside_common / "worktrees").exists())
        original_run = MODULE.run_git_at_directory_descriptor
        inserted = False

        def insert_commondir(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal inserted
            if not inserted and "worktree" in args and "add" in args:
                inserted = True
                (self.named_source_git_dir / "commondir").write_text(
                    str(outside_common) + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    extra_env,
                    {"GIT_COMMON_DIR": str(plan.entries[0].source_git_dir)},
                )
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=insert_commondir,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "commondir indirection",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertTrue(inserted)
        self.assertFalse((outside_common / "worktrees").exists())
        self.assertTrue((self.named_source_git_dir / "worktrees").is_dir())
        gitdir_text = (target / module.path / ".git").read_text(encoding="utf-8")
        self.assertIn(str(self.named_source_git_dir / "worktrees"), gitdir_text)

    def test_target_object_closure_rejects_missing_blob_and_logical_cap(
        self,
    ) -> None:
        source = self.root / "missing-closure.git"
        run_git(self.root, "init", "--bare", str(source))
        missing_blob = "f" * 40
        tree_result = subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                f"--git-dir={source}",
                "mktree",
                "--missing",
            ],
            input=f"100644 blob {missing_blob}\tmissing.txt\n",
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tree = tree_result.stdout.strip()
        commit = run_git(
            self.root,
            f"--git-dir={source}",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            tree,
            "-m",
            "missing blob",
        )
        completeness = MODULE.capture_source_completeness_receipt(source)
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "missing or malformed object",
        ):
            MODULE.target_object_closure(
                source,
                commit,
                completeness,
            )

        normal_completeness = MODULE.capture_source_completeness_receipt(
            self.named_source_git_dir,
        )
        with mock.patch.object(MODULE, "MAX_CHECKOUT_LOGICAL_BYTES", 1):
            with self.assertRaisesRegex(MODULE.PlanError, "logical-size safety limit"):
                MODULE.target_object_closure(
                    self.named_source_git_dir,
                    self.sha,
                    normal_completeness,
                )

    def test_target_object_closure_reads_and_hashes_payload_bytes(self) -> None:
        source = self.clone_named_source("corrupt-payload")
        blob_result = subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                f"--git-dir={source}",
                "hash-object",
                "-w",
                "--stdin",
            ],
            input=b"source-specific payload\n",
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        blob = os.fsdecode(blob_result.stdout).strip()
        tree_result = subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                f"--git-dir={source}",
                "mktree",
            ],
            input=f"100644 blob {blob}\tpayload.txt\n",
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tree = tree_result.stdout.strip()
        commit = run_git(
            self.root,
            f"--git-dir={source}",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            tree,
            "-m",
            "corrupt payload",
        )
        loose_blob = source / "objects" / blob[:2] / blob[2:]
        compressed = loose_blob.read_bytes()
        self.assertGreater(len(compressed), 4)
        loose_blob.chmod(0o600)
        loose_blob.write_bytes(compressed[:-4])
        completeness = MODULE.capture_source_completeness_receipt(source)

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "payload|every required object",
        ):
            MODULE.target_object_closure(
                source,
                commit,
                completeness,
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

    def test_superproject_index_receipt_binds_identity_content_and_gitlink_rows(
        self,
    ) -> None:
        target, module, receipt = self.make_target_superproject(
            "index-receipt-target",
            self.sha,
        )
        index_receipt = receipt.superproject_index
        self.assertEqual(
            index_receipt.selected_gitlinks,
            ((module.path, self.sha),),
        )
        index_path = index_receipt.index_bindings[0].path
        index_stat = index_path.stat()
        os.utime(
            index_path,
            ns=(
                index_stat.st_atime_ns,
                index_stat.st_mtime_ns + 1_000_000_000,
            ),
        )
        MODULE.revalidate_superproject_index_receipt(
            target,
            index_receipt,
        )

        run_git(
            target,
            "update-index",
            "--cacheinfo",
            f"160000,{'f' * 40},{module.path}",
        )
        with self.assertRaisesRegex(
            MODULE.PlanError,
            "superproject index.*changed|selected superproject gitlink rows changed",
        ):
            MODULE.revalidate_superproject_index_receipt(
                target,
                index_receipt,
            )

    def test_superproject_index_receipt_rejects_same_content_object_replacement(
        self,
    ) -> None:
        target, _, receipt = self.make_target_superproject(
            "index-replacement-target",
            self.sha,
        )
        index_binding = receipt.superproject_index.index_bindings[0]
        replacement = index_binding.path.with_name("index.replacement")
        replacement.write_bytes(index_binding.path.read_bytes())
        replacement.chmod(index_binding.fingerprint.permissions)
        os.replace(replacement, index_binding.path)

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "object or access policy changed",
        ):
            MODULE.revalidate_superproject_index_receipt(
                target,
                receipt.superproject_index,
            )

    def test_split_superproject_index_receipt_binds_shared_index_drift(
        self,
    ) -> None:
        target, module, initial_receipt = self.make_target_superproject(
            "split-index-receipt-target",
            self.sha,
        )
        run_git(target, "update-index", "--split-index")
        index_receipt = MODULE.capture_superproject_index_receipt(
            target,
            (module.path,),
        )
        self.assertEqual(len(index_receipt.index_bindings), 2)
        shared_binding = index_receipt.index_bindings[1]
        self.assertTrue(shared_binding.path.name.startswith("sharedindex."))
        self.assertEqual(
            index_receipt.selected_gitlinks,
            ((module.path, self.sha),),
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=MODULE.PlanInputReceipt(
                gitmodules_binding=initial_receipt.gitmodules_binding,
                superproject_index=index_receipt,
            ),
        )

        replacement = shared_binding.path.with_name("sharedindex.replacement")
        replacement.write_bytes(shared_binding.path.read_bytes())
        replacement.chmod(shared_binding.fingerprint.permissions)
        os.replace(replacement, shared_binding.path)

        with mock.patch.object(MODULE, "add_worktree") as add:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "superproject index.*changed",
            ):
                MODULE.apply_sync_plan(plan)
        add.assert_not_called()

    def test_index_drift_blocks_authorized_fetch_before_source_mutation(self) -> None:
        (self.remote / "INDEX-FETCH.md").write_text(
            "index fetch\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "INDEX-FETCH.md")
        run_git(self.remote, "commit", "-m", "index fetch")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        target, module, input_receipt = self.make_target_superproject(
            "index-fetch-target",
            missing_sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, missing_sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=True,
            input_receipt=input_receipt,
        )
        run_git(
            target,
            "update-index",
            "--cacheinfo",
            f"160000,{self.sha},{module.path}",
        )
        with mock.patch.object(MODULE, "fetch_missing_commit") as fetch:
            with mock.patch.object(MODULE, "add_worktree") as add:
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "superproject index.*changed|selected superproject gitlink rows changed",
                ):
                    MODULE.apply_sync_plan(plan)
        fetch.assert_not_called()
        add.assert_not_called()
        self.assertFalse(
            MODULE.commit_exists(
                self.named_source_git_dir,
                target / module.path,
                missing_sha,
            )
        )

    def test_index_drift_blocks_first_worktree_mutation(self) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "index-mutation-target",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        (target / "unrelated.txt").write_text("drift\n", encoding="utf-8")
        run_git(target, "add", "unrelated.txt")
        with mock.patch.object(MODULE, "add_worktree") as add:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "superproject index.*changed",
            ):
                MODULE.apply_sync_plan(plan)
        add.assert_not_called()

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
        self.add_managed_worktree(
            self.source_git_dir,
            self.linked,
            self.sha,
        )
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout="",
            stderr="",
        )
        target_descriptor = os.open(
            self.linked,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        source_lease = MODULE.capture_directory_entry_lease(
            self.source_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "selected source common gitdir",
        )
        try:
            with mock.patch.object(
                MODULE,
                "run_git_at_directory_descriptor",
                return_value=completed,
            ) as run_command:
                MODULE.checkout_existing_worktree(
                    self.linked,
                    self.sha,
                    dry_run=False,
                    target_descriptor=target_descriptor,
                    source_git_dir=self.source_git_dir,
                    source_lease=source_lease,
                )
        finally:
            try:
                source_lease.close()
            finally:
                os.close(target_descriptor)

        command = run_command.call_args.args[0]
        self.assertTrue(any(arg.startswith("--git-dir=") for arg in command))
        self.assertIn("--work-tree=.", command)
        self.assertIn("--no-overwrite-ignore", command)
        self.assertIn("--no-recurse-submodules", command)

    def test_checkout_signal_publishes_retained_worktree_recovery(
        self,
    ) -> None:
        self.add_managed_worktree(
            self.source_git_dir,
            self.linked,
            self.sha,
        )
        target_descriptor = os.open(
            self.linked,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        source_lease = MODULE.capture_directory_entry_lease(
            self.source_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "selected source common gitdir",
        )
        original_run = MODULE.run_git_at_directory_descriptor

        def checkout_then_interrupt(
            args: list[str],
            directory_descriptor: int,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            result = original_run(
                args,
                directory_descriptor,
                **kwargs,
            )
            raise MODULE.ForwardedProcessSignal(signal.SIGHUP)

        try:
            with mock.patch.object(
                MODULE,
                "run_git_at_directory_descriptor",
                side_effect=checkout_then_interrupt,
            ):
                with self.assertRaises(MODULE.ForwardedProcessSignal) as raised:
                    MODULE.checkout_existing_worktree(
                        self.linked,
                        self.sha,
                        dry_run=False,
                        target_descriptor=target_descriptor,
                        source_git_dir=self.source_git_dir,
                        source_lease=source_lease,
                    )
            self.assertTrue(
                any(
                    '"recovery_status":"interrupted-checkout-retained"' in detail
                    for detail in raised.exception.recovery_details
                )
            )
        finally:
            try:
                source_lease.close()
            finally:
                os.close(target_descriptor)

    def test_add_signal_rolls_back_before_publishing_recovery(
        self,
    ) -> None:
        target = MODULE.bind_target_path(
            self.root,
            ("signal-add-worktree",),
            "signal add target",
        )
        lease = MODULE.materialize_bound_target_directory(target)
        source_lease = MODULE.capture_directory_entry_lease(
            self.source_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "selected source common gitdir",
        )
        original_run = MODULE.run_git_at_directory_descriptor
        interrupted = False

        def add_then_interrupt(
            args: list[str],
            directory_descriptor: int,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal interrupted
            result = original_run(
                args,
                directory_descriptor,
                **kwargs,
            )
            if "add" in args and not interrupted:
                interrupted = True
                raise MODULE.ForwardedProcessSignal(signal.SIGTERM)
            return result

        try:
            with mock.patch.object(
                MODULE,
                "run_git_at_directory_descriptor",
                side_effect=add_then_interrupt,
            ):
                with self.assertRaises(MODULE.ForwardedProcessSignal) as raised:
                    MODULE.add_worktree(
                        self.source_git_dir,
                        target.path,
                        self.sha,
                        dry_run=False,
                        lease=lease,
                        source_lease=source_lease,
                    )
            self.assertTrue(interrupted)
            self.assertFalse(target.path.exists())
            self.assertIsNone(
                MODULE.registered_target_path(
                    self.source_git_dir,
                    target.path,
                    source_lease=source_lease,
                )
            )
            self.assertTrue(
                any(
                    '"rollback_status":"complete"' in detail
                    for detail in raised.exception.recovery_details
                )
            )
        finally:
            try:
                source_lease.close()
            finally:
                lease.close()

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
                return_value=False,
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
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0xEF53,
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
                return_value=False,
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
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0xEF53,
                        ):
                            policy = MODULE.filesystem_name_policy(target_root)

        self.assertFalse(policy.case_sensitive)
        self.assertEqual(policy.normalization, "exact")

    def test_unproven_literal_unicode_names_use_conservative_nfd_tokens(
        self,
    ) -> None:
        cases = (
            ("unknown-sensitive", True, False, 0xFF534D42),
            ("unknown-insensitive", False, False, 0xFF534D42),
            ("known-without-casefold-proof", True, None, 0xEF53),
        )
        for name, case_sensitive, casefold, filesystem_magic in cases:
            with self.subTest(name=name):
                target_root = self.root / name
                target_root.mkdir()
                with mock.patch.object(MODULE.sys, "platform", "linux"):
                    with mock.patch.object(
                        MODULE,
                        "local_git_bool",
                        return_value=False,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "probe_directory_case_sensitive",
                            return_value=case_sensitive,
                        ):
                            with mock.patch.object(
                                MODULE,
                                "linux_directory_casefold",
                                return_value=casefold,
                            ):
                                with mock.patch.object(
                                    MODULE,
                                    "linux_filesystem_magic",
                                    return_value=filesystem_magic,
                                ):
                                    policy = MODULE.filesystem_name_policy(target_root)

                first = MODULE.bind_target_path(
                    target_root,
                    ("Caf\u00e9",),
                    "first",
                    policy,
                )
                second = MODULE.bind_target_path(
                    target_root,
                    ("Cafe\u0301" if case_sensitive else "CAFE\u0301",),
                    "second",
                    policy,
                )

                self.assertEqual(policy.case_sensitive, case_sensitive)
                self.assertEqual(policy.normalization, "NFD")
                self.assertEqual(first.collision_tokens, second.collision_tokens)

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
                return_value=False,
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
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0xEF53,
                        ):
                            policy = MODULE.filesystem_name_policy(target_root)

        self.assertFalse(policy.case_sensitive)
        self.assertEqual(policy.normalization, "NFD")

    def test_empty_linux_known_sensitive_filesystem_uses_statfs_and_flags(
        self,
    ) -> None:
        target_root = self.root / "empty-linux-ext4-target"
        target_root.mkdir()
        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                return_value=False,
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=None,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=False,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0xEF53,
                        ):
                            policy = MODULE.filesystem_name_policy(target_root)

        self.assertTrue(policy.case_sensitive)
        self.assertIn("linux-statfs-and-directory-flags", policy.source)

    def test_empty_linux_overlayfs_case_policy_fails_closed(self) -> None:
        target_root = self.root / "empty-linux-overlay-target"
        target_root.mkdir()
        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                return_value=False,
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=None,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=False,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0x794C7630,
                        ):
                            with self.assertRaisesRegex(
                                MODULE.PlanError,
                                "cannot determine target directory case semantics",
                            ):
                                MODULE.filesystem_name_policy(target_root)

    def test_nonempty_linux_overlayfs_ignores_positive_lookup_probe(self) -> None:
        target_root = self.root / "nonempty-linux-overlay-target"
        target_root.mkdir()
        (target_root / "Alpha").write_text("alpha\n", encoding="utf-8")
        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(MODULE, "local_git_bool", return_value=False):
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
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=MODULE.LINUX_OVERLAYFS_MAGIC,
                        ):
                            with self.assertRaisesRegex(
                                MODULE.PlanError,
                                "OverlayFS merged-directory lookup",
                            ):
                                MODULE.filesystem_name_policy(target_root)

    def test_empty_linux_xfs_case_policy_fails_closed(self) -> None:
        target_root = self.root / "empty-linux-xfs-target"
        target_root.mkdir()
        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(MODULE, "local_git_bool", return_value=False):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=None,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=False,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0x58465342,
                        ):
                            with self.assertRaisesRegex(
                                MODULE.PlanError,
                                "cannot determine target directory case semantics",
                            ):
                                MODULE.filesystem_name_policy(target_root)

    def test_linux_name_policy_uses_one_bound_directory_descriptor(self) -> None:
        target_root = self.root / "single-descriptor-linux-target"
        target_root.mkdir()
        descriptors: list[int] = []

        def record_probe(path: Path, descriptor: object = None) -> None:
            self.assertEqual(path, target_root.resolve())
            self.assertIsInstance(descriptor, int)
            descriptors.append(descriptor)
            return None

        def record_casefold(path: Path, descriptor: object = None) -> bool:
            self.assertEqual(path, target_root.resolve())
            self.assertIsInstance(descriptor, int)
            descriptors.append(descriptor)
            return False

        def record_magic(path: Path, descriptor: object = None) -> int:
            self.assertEqual(path, target_root.resolve())
            self.assertIsInstance(descriptor, int)
            descriptors.append(descriptor)
            return 0xEF53

        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(MODULE, "local_git_bool", return_value=False):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    side_effect=record_probe,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        side_effect=record_casefold,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            side_effect=record_magic,
                        ):
                            MODULE.filesystem_name_policy(target_root)

        self.assertEqual(len(descriptors), 3)
        self.assertEqual(len(set(descriptors)), 1)

    def test_case_probe_fails_closed_when_bound_entry_disappears(self) -> None:
        target_root = self.root / "case-probe-entry-deleted"
        target_root.mkdir()
        (target_root / "Alpha").write_text("alpha\n", encoding="utf-8")
        real_stat = MODULE.os.stat
        original_reads = 0

        def unstable_stat(path: object, *args: object, **kwargs: object) -> object:
            nonlocal original_reads
            if path == "Alpha" and kwargs.get("dir_fd") is not None:
                original_reads += 1
                if original_reads == 2:
                    raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(MODULE.os, "stat", side_effect=unstable_stat):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "probe entry became unavailable",
            ):
                MODULE.probe_directory_case_sensitive(target_root)

    def test_case_probe_fails_closed_when_bound_entry_is_replaced(self) -> None:
        target_root = self.root / "case-probe-entry-replaced"
        target_root.mkdir()
        (target_root / "Alpha").write_text("alpha\n", encoding="utf-8")
        replacement = self.root / "case-probe-replacement-object"
        replacement.write_text("replacement\n", encoding="utf-8")
        real_stat = MODULE.os.stat
        replacement_stat = real_stat(replacement, follow_symlinks=False)
        original_reads = 0

        def unstable_stat(path: object, *args: object, **kwargs: object) -> object:
            nonlocal original_reads
            if path == "Alpha" and kwargs.get("dir_fd") is not None:
                original_reads += 1
                if original_reads == 2:
                    return replacement_stat
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(MODULE.os, "stat", side_effect=unstable_stat):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "probe entry changed identity",
            ):
                MODULE.probe_directory_case_sensitive(target_root)

    def test_case_probe_treats_distinct_same_inode_dirents_as_hardlinks(self) -> None:
        target_root = self.root / "case-probe-hardlinks"
        target_root.mkdir()
        backing = target_root / "backing"
        backing.write_text("same inode\n", encoding="utf-8")
        backing_stat = os.stat(backing, follow_symlinks=False)
        scanned = mock.MagicMock()
        scanned.__enter__.return_value = iter(
            [
                SimpleNamespace(name="Alpha"),
                SimpleNamespace(name="alpha"),
            ]
        )
        scanned.__exit__.return_value = False

        with mock.patch.object(MODULE.os, "scandir", return_value=scanned):
            with mock.patch.object(MODULE.os, "stat", return_value=backing_stat):
                self.assertTrue(MODULE.probe_directory_case_sensitive(target_root))

    def test_name_policy_evidence_source_change_preserves_same_semantics(
        self,
    ) -> None:
        target_root = self.root / "policy-source-transition"
        target_root.mkdir()
        bound = MODULE.bind_target_path(
            target_root,
            ("missing",),
            "policy source transition",
            MODULE.FilesystemNamePolicy(
                case_sensitive=True,
                normalization="exact",
                source="initial-statfs-evidence",
            ),
        )
        with mock.patch.object(
            MODULE,
            "filesystem_name_policy",
            return_value=MODULE.FilesystemNamePolicy(
                case_sensitive=True,
                normalization="exact",
                source="later-directory-entry-evidence",
            ),
        ):
            MODULE.revalidate_bound_target(bound)

    def test_empty_linux_unknown_case_policy_blocks_before_any_mutation(self) -> None:
        target_root = self.root / "empty-linux-cifs-like-target"
        target_root.mkdir()
        with mock.patch.object(MODULE.sys, "platform", "linux"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                return_value=False,
            ):
                with mock.patch.object(
                    MODULE,
                    "probe_directory_case_sensitive",
                    return_value=None,
                ):
                    with mock.patch.object(
                        MODULE,
                        "linux_directory_casefold",
                        return_value=None,
                    ):
                        with mock.patch.object(
                            MODULE,
                            "linux_filesystem_magic",
                            return_value=0xFF534D42,
                        ):
                            with mock.patch.object(
                                MODULE,
                                "fetch_missing_commit",
                            ) as fetch:
                                with mock.patch.object(
                                    MODULE,
                                    "add_worktree",
                                ) as add:
                                    with self.assertRaisesRegex(
                                        MODULE.PlanError,
                                        "cannot determine target directory case "
                                        "semantics without mutation",
                                    ):
                                        MODULE.execute_sync_plan(
                                            root=target_root,
                                            common_git_dir=self.named_common_git_dir,
                                            source_superproject=None,
                                            planned_modules=[
                                                (
                                                    MODULE.Submodule(
                                                        "custom-lib",
                                                        "Foo",
                                                        str(self.remote),
                                                    ),
                                                    self.sha,
                                                ),
                                                (
                                                    MODULE.Submodule(
                                                        "case-second",
                                                        "foo",
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
        fetch.assert_not_called()
        add.assert_not_called()

    def test_darwin_canonical_unicode_is_filesystem_derived_when_precompose_false(
        self,
    ) -> None:
        target_root = self.root / "darwin-unicode-target"
        target_root.mkdir()

        with mock.patch.object(MODULE.sys, "platform", "darwin"):
            with mock.patch.object(
                MODULE,
                "local_git_bool",
                return_value=False,
            ):
                with mock.patch.object(
                    MODULE,
                    "darwin_volume_case_sensitive",
                    return_value=True,
                ):
                    policy = MODULE.filesystem_name_policy(target_root)

        self.assertEqual(policy.normalization, "NFD")
        self.assertEqual(
            MODULE.normalized_path_parts(("Caf\u00e9",), policy),
            MODULE.normalized_path_parts(("Cafe\u0301",), policy),
        )

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
                    relative_parts=(f"module-{index}",),
                    collision_tokens=(("missing", f"module-{index}"),),
                ),
            )
            collision_index.add(entries, candidate)
            entries.append(candidate)

        self.assertEqual(len(entries), 20_000)

    def test_collision_index_rejects_aliased_shared_prefix_spellings(
        self,
    ) -> None:
        target_root = self.root / "shared-prefix-alias-target"
        target_root.mkdir()
        policy = MODULE.FilesystemNamePolicy(
            case_sensitive=False,
            normalization="NFD",
            source="test",
        )
        cases = (
            (("Vendor", "a"), ("vendor", "b")),
            (("Caf\u00e9", "a"), ("Cafe\u0301", "b")),
        )
        for first_parts, second_parts in cases:
            with self.subTest(first=first_parts, second=second_parts):
                first = SimpleNamespace(
                    target=MODULE.bind_target_path(
                        target_root,
                        first_parts,
                        "first",
                        policy,
                    )
                )
                second = SimpleNamespace(
                    target=MODULE.bind_target_path(
                        target_root,
                        second_parts,
                        "second",
                        policy,
                    )
                )
                entries = [first]
                collision_index = MODULE.TargetCollisionIndex()
                collision_index.add([], first)
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "components alias",
                ):
                    collision_index.add(entries, second)

    def test_deep_collision_chain_uses_active_ancestor_metadata(self) -> None:
        class NoParentLookups(list[object]):
            def __getitem__(self, index: object) -> object:
                raise AssertionError(
                    f"collision index walked planned parent links at {index}"
                )

        entries = NoParentLookups()
        collision_index = MODULE.TargetCollisionIndex()
        active_ancestors: set[int] = set()
        tokens: list[tuple[object, ...]] = []
        for index in range(1_024):
            tokens.append(("missing", f"level-{index}"))
            candidate = SimpleNamespace(
                parent_index=index - 1 if index else None,
                target=SimpleNamespace(
                    path=Path(f"deep-level-{index}"),
                    relative_parts=tuple(
                        f"level-{level}" for level in range(index + 1)
                    ),
                    collision_tokens=tuple(tokens),
                ),
            )
            collision_index.add(entries, candidate, active_ancestors)
            entries.append(candidate)
            active_ancestors.add(index)

        self.assertEqual(len(entries), 1_024)

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

    def test_staged_new_filtered_file_is_rejected_before_status_executes_filter(
        self,
    ) -> None:
        source = self.clone_named_source("index-filter")
        target_super = self.root / "index-filter-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, self.sha)
        (target / ".gitattributes").write_text(
            "staged.bin filter=marker\n",
            encoding="utf-8",
        )
        staged_payload = target / "staged.bin"
        staged_payload.write_text("staged\n", encoding="utf-8")
        run_git(target, "add", ".gitattributes", "staged.bin")

        marker = self.root / "index-clean-filter-executed"
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "filter.marker.clean",
            f"touch {marker}",
        )
        staged_payload.write_text("working tree changed\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "tree: index",
        ):
            MODULE.build_sync_plan(
                root=target_super,
                common_git_dir=self.named_common_git_dir,
                source_superproject=None,
                planned_modules=[
                    (
                        MODULE.Submodule(
                            "index-filter",
                            "lib",
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

        self.assertFalse(marker.exists())
        self.assertEqual(run_git(target, "rev-parse", "HEAD"), self.sha)

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
        MODULE.run(
            [
                "git",
                "-C",
                str(self.linked),
                "update-index",
                "--no-split-index",
                "--no-untracked-cache",
                "--no-fsmonitor",
                "--force-write-index",
            ]
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

    def test_recursive_apply_parent_with_two_children_uses_parent_receipt(
        self,
    ) -> None:
        left_remote, left_sha = self.create_gitlink_remote("direct-left")
        right_remote, right_sha = self.create_gitlink_remote("direct-right")
        parent_remote, parent_sha = self.create_gitlink_remote(
            "direct-parent",
            (
                ("left", "left", left_remote, left_sha),
                ("right", "right", right_remote, right_sha),
            ),
        )

        source_common = self.root / "direct-recursive-source" / ".git"
        parent_source = source_common / "modules" / "parent"
        self.clone_recursive_source(
            parent_source,
            parent_remote,
            "direct-parent-standard",
        )
        self.clone_recursive_source(
            parent_source / "modules" / "left",
            left_remote,
            "direct-left-standard",
        )
        self.clone_recursive_source(
            parent_source / "modules" / "right",
            right_remote,
            "direct-right-standard",
        )
        target_super = self.root / "direct-recursive-target"
        target_super.mkdir()
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=source_common,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "parent",
                        "vendor/parent",
                        str(parent_remote),
                    ),
                    parent_sha,
                )
            ],
            depth=1,
            recursive=True,
            force_replace_empty=False,
            fetch_missing=False,
        )

        self.assertEqual(
            [entry.parent_index for entry in plan.entries],
            [None, 0, 0],
        )
        final_target_roots = {entry.target.relative_parts for entry in plan.entries}
        self.assertTrue(final_target_roots.isdisjoint(plan.shared_missing_ancestors))
        self.assertEqual(set(plan.shared_missing_ancestors), {("vendor",)})
        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        expected_targets = (
            (target_super / "vendor" / "parent", parent_sha),
            (target_super / "vendor" / "parent" / "left", left_sha),
            (target_super / "vendor" / "parent" / "right", right_sha),
        )
        for target, expected_sha in expected_targets:
            with self.subTest(target=target):
                self.assertEqual(run_git(target, "rev-parse", "HEAD"), expected_sha)
        self.assertEqual(set(plan.applied_target_roots), {0})
        parent_receipt = MODULE.revalidate_applied_target_root(plan, 0)
        parent_target = plan.entries[0].target.path
        self.assertEqual(parent_receipt.relative_parts, ("vendor", "parent"))
        self.assertEqual(parent_receipt.node.path, parent_target)
        self.assertEqual(
            parent_receipt.node.fingerprint,
            MODULE.filesystem_fingerprint(parent_target),
        )
        shared_vendor = plan.shared_missing_ancestors[("vendor",)]
        self.assertIsNotNone(shared_vendor.materialized_node)
        assert shared_vendor.materialized_node is not None
        self.assertEqual(shared_vendor.materialized_node.path, plan.root / "vendor")
        self.assertEqual(
            shared_vendor.materialized_node.fingerprint,
            MODULE.filesystem_fingerprint(plan.root / "vendor"),
        )

    def test_recursive_parent_checkout_binds_shared_child_prefix(self) -> None:
        plan, target_super, _parent_source, child_results = (
            self.make_grouped_recursive_plan(
                "checkout-shared-prefix",
                ("group/left", "group/right"),
            )
        )

        self.assertEqual(
            set(plan.shared_missing_ancestors),
            {("parent", "group")},
        )
        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        for child_path, (_remote, child_sha) in zip(
            ("group/left", "group/right"),
            child_results,
        ):
            with self.subTest(child_path=child_path):
                self.assertEqual(
                    run_git(target_super / "parent" / child_path, "rev-parse", "HEAD"),
                    child_sha,
                )
        shared = plan.shared_missing_ancestors[("parent", "group")]
        self.assertIsNotNone(shared.materialized_node)
        assert shared.materialized_node is not None
        self.assertEqual(
            shared.materialized_node.path,
            (target_super / "parent/group").resolve(),
        )
        self.assertEqual(
            shared.materialized_node.fingerprint,
            MODULE.filesystem_fingerprint(target_super / "parent/group"),
        )

    def test_recursive_parent_checkout_binds_each_deep_shared_prefix(self) -> None:
        plan, target_super, _parent_source, child_results = (
            self.make_grouped_recursive_plan(
                "checkout-deep-prefix",
                ("group/deep/left", "group/deep/right"),
            )
        )

        self.assertEqual(
            set(plan.shared_missing_ancestors),
            {
                ("parent", "group"),
                ("parent", "group", "deep"),
            },
        )
        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        for relative_parts in (
            ("parent", "group"),
            ("parent", "group", "deep"),
        ):
            shared = plan.shared_missing_ancestors[relative_parts]
            self.assertIsNotNone(shared.materialized_node)
            assert shared.materialized_node is not None
            self.assertEqual(
                shared.materialized_node.fingerprint,
                MODULE.filesystem_fingerprint(target_super.joinpath(*relative_parts)),
            )
        for child_path, (_remote, child_sha) in zip(
            ("group/deep/left", "group/deep/right"),
            child_results,
        ):
            self.assertEqual(
                run_git(target_super / "parent" / child_path, "rev-parse", "HEAD"),
                child_sha,
            )

    def test_shared_prefix_revalidation_requires_owner_mode_under_dac_override(
        self,
    ) -> None:
        shared = self.root / "dac-override-shared-prefix"
        shared.mkdir()
        shared.chmod(0o555)
        node = MODULE.BoundNode(
            shared,
            MODULE.filesystem_fingerprint(shared),
        )
        try:
            with mock.patch.object(MODULE, "probe_access", return_value=True):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "no longer permits materialization",
                ):
                    MODULE.revalidate_materialized_shared_node(node)
        finally:
            shared.chmod(0o755)

    def test_recursive_parent_checkout_does_not_bless_unrelated_sibling(
        self,
    ) -> None:
        plan, target_super, _parent_source, _child_results = (
            self.make_grouped_recursive_plan(
                "checkout-unrelated-sibling",
                ("group/left", "group/right"),
                regular_paths=("unrelated/nested.txt",),
            )
        )
        self.assertNotIn(
            ("parent", "unrelated"),
            plan.shared_missing_ancestors,
        )

        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        self.assertEqual(
            (target_super / "parent/unrelated/nested.txt").read_text(encoding="utf-8"),
            "checkout-unrelated-sibling\n",
        )
        self.assertEqual(
            set(plan.shared_missing_ancestors),
            {("parent", "group")},
        )

    def test_recursive_parent_checkout_rejects_shared_prefix_replacement(
        self,
    ) -> None:
        for boundary in ("group", "deep"):
            with self.subTest(boundary=boundary):
                scenario = f"checkout-replace-{boundary}"
                plan, target_super, parent_source, _child_results = (
                    self.make_grouped_recursive_plan(
                        scenario,
                        ("group/deep/left", "group/deep/right"),
                    )
                )
                registry_before = run_git(
                    self.root,
                    f"--git-dir={parent_source}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                original_capture = MODULE.capture_checkout_materialized_shared_ancestors
                original_open = os.open
                replaced = False
                quarantine = self.root / f"{scenario}-quarantine"

                def capture_with_replacement(
                    current_plan: object,
                    owner_index: int,
                    entry: object,
                    lease: object,
                ) -> object:
                    def replace_before_open(
                        path: object,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal replaced
                        if (
                            not replaced
                            and path == boundary
                            and dir_fd is not None
                            and flags & os.O_DIRECTORY
                        ):
                            replaced = True
                            prefix = (
                                target_super / "parent/group"
                                if boundary == "group"
                                else target_super / "parent/group/deep"
                            )
                            prefix.rename(quarantine)
                            prefix.mkdir()
                        return original_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )

                    with mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=replace_before_open,
                    ):
                        return original_capture(
                            current_plan,
                            owner_index,
                            entry,
                            lease,
                        )

                with mock.patch.object(
                    MODULE,
                    "capture_checkout_materialized_shared_ancestors",
                    side_effect=capture_with_replacement,
                ):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            "changed during descriptor binding",
                        ):
                            MODULE.apply_sync_plan(plan)

                self.assertTrue(replaced)
                self.assertFalse((target_super / "parent").exists())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={parent_source}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    registry_before,
                )
                self.assertFalse(plan.applied_target_roots)
                self.assertTrue(
                    all(
                        ancestor.materialized_node is None
                        for ancestor in plan.shared_missing_ancestors.values()
                    )
                )

    def test_recursive_parent_checkout_rejects_unsafe_shared_prefix_policy(
        self,
    ) -> None:
        for policy in ("wrong-owner", "non-writable", "symlink"):
            with self.subTest(policy=policy):
                scenario = f"checkout-policy-{policy}"
                plan, target_super, parent_source, _child_results = (
                    self.make_grouped_recursive_plan(
                        scenario,
                        ("group/left", "group/right"),
                    )
                )
                registry_before = run_git(
                    self.root,
                    f"--git-dir={parent_source}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                original_capture = MODULE.capture_checkout_materialized_shared_ancestors
                outside = self.root / f"{scenario}-outside"
                outside.mkdir()

                def capture_with_policy_change(
                    current_plan: object,
                    owner_index: int,
                    entry: object,
                    lease: object,
                ) -> object:
                    group = target_super / "parent/group"
                    if policy == "wrong-owner":
                        with mock.patch.object(
                            MODULE.os,
                            "geteuid",
                            return_value=os.geteuid() + 1,
                        ):
                            return original_capture(
                                current_plan,
                                owner_index,
                                entry,
                                lease,
                            )
                    if policy == "non-writable":
                        group.chmod(0o555)
                        with mock.patch.object(
                            MODULE,
                            "probe_access_at",
                            return_value=True,
                        ):
                            return original_capture(
                                current_plan,
                                owner_index,
                                entry,
                                lease,
                            )
                    else:
                        shutil.rmtree(group)
                        group.symlink_to(outside, target_is_directory=True)
                    return original_capture(
                        current_plan,
                        owner_index,
                        entry,
                        lease,
                    )

                expected = {
                    "wrong-owner": "wrong owner",
                    "non-writable": "does not permit descendant materialization",
                    "symlink": "is not a directory",
                }[policy]
                with mock.patch.object(
                    MODULE,
                    "capture_checkout_materialized_shared_ancestors",
                    side_effect=capture_with_policy_change,
                ):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            expected,
                        ) as raised:
                            MODULE.apply_sync_plan(plan)

                self.assertNotIn("worktree rollback failed", str(raised.exception))
                self.assertFalse((target_super / "parent").exists())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={parent_source}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    registry_before,
                )
                self.assertEqual(list(outside.iterdir()), [])

    def test_managed_recursive_parent_failure_preserves_shared_prefix_mode(
        self,
    ) -> None:
        plan, target_super, parent_source, _child_results = (
            self.make_grouped_recursive_plan(
                "managed-checkout-policy",
                ("group/left", "group/right"),
            )
        )
        parent_entry = plan.entries[0]
        target_sha = parent_entry.sha
        parent_remote = Path(parent_entry.submodule.url)
        run_git(
            parent_remote,
            "update-index",
            "--force-remove",
            "--",
            "group/left",
            "group/right",
        )
        run_git(parent_remote, "rm", "-f", ".gitmodules")
        run_git(parent_remote, "commit", "-m", "remove recursive children")
        no_group_sha = run_git(parent_remote, "rev-parse", "HEAD")
        self.fetch_source(parent_source)

        parent_target = target_super / "parent"
        self.add_managed_worktree(
            parent_source,
            parent_target,
            no_group_sha,
        )
        managed_plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=parent_source.parent.parent,
            source_superproject=None,
            planned_modules=[
                (
                    parent_entry.submodule,
                    target_sha,
                )
            ],
            depth=1,
            recursive=True,
            force_replace_empty=False,
            fetch_missing=False,
        )
        self.assertEqual(managed_plan.entries[0].state, "managed")
        self.assertIn(
            ("parent", "group"),
            managed_plan.shared_missing_ancestors,
        )
        registry_paths_before = MODULE.registered_worktree_paths(parent_source)
        original_capture = MODULE.capture_checkout_materialized_shared_ancestors
        made_read_only = False

        def capture_after_policy_change(
            current_plan: object,
            owner_index: int,
            entry: object,
            lease: object,
        ) -> object:
            nonlocal made_read_only
            shared_prefix = target_super / "parent/group"
            self.assertTrue(shared_prefix.is_dir())
            shared_prefix.chmod(0o555)
            made_read_only = True
            return original_capture(
                current_plan,
                owner_index,
                entry,
                lease,
            )

        prior_umask = os.umask(0o077)
        shared_prefix = target_super / "parent/group"
        try:
            with mock.patch.object(
                MODULE,
                "capture_checkout_materialized_shared_ancestors",
                side_effect=capture_after_policy_change,
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "does not permit descendant materialization",
                    ):
                        MODULE.apply_sync_plan(managed_plan)

            self.assertTrue(made_read_only)
            self.assertTrue(parent_target.is_dir())
            self.assertEqual(
                stat.S_IMODE(shared_prefix.stat().st_mode),
                0o555,
            )
            self.assertEqual(
                MODULE.registered_worktree_paths(parent_source),
                registry_paths_before,
            )
            self.assertEqual(
                run_git(parent_target, "rev-parse", "HEAD"),
                target_sha,
            )
        finally:
            os.umask(prior_umask)
            if shared_prefix.exists():
                shared_prefix.chmod(0o755)

    def test_recursive_parent_checkout_binding_failure_rolls_back_registration(
        self,
    ) -> None:
        plan, target_super, parent_source, _child_results = (
            self.make_grouped_recursive_plan(
                "checkout-binding-rollback",
                ("group/left", "group/right"),
            )
        )
        registry_before = run_git(
            self.root,
            f"--git-dir={parent_source}",
            "worktree",
            "list",
            "--porcelain",
        )

        with mock.patch.object(
            MODULE,
            "capture_checkout_materialized_shared_ancestors",
            side_effect=MODULE.PlanError("injected shared-prefix binding failure"),
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "injected shared-prefix binding failure",
                ):
                    MODULE.apply_sync_plan(plan)

        self.assertFalse((target_super / "parent").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={parent_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )
        self.assertFalse(plan.applied_target_roots)
        self.assertTrue(
            all(
                ancestor.materialized_node is None
                for ancestor in plan.shared_missing_ancestors.values()
            )
        )

    def test_recursive_parent_collapses_grandchild_to_direct_child_subtree(
        self,
    ) -> None:
        plan, target_super, _parent_source = (
            self.make_grouped_recursive_plan_with_grandchild(
                "checkout-grandchild-owner",
            )
        )
        group_parts = ("parent", "group")
        self.assertEqual(
            plan.shared_missing_ancestors[group_parts].participant_targets,
            frozenset(
                {
                    ("parent", "group", "left"),
                    (
                        "parent",
                        "group",
                        "left",
                        "nested",
                        "grandchild",
                    ),
                    ("parent", "group", "right"),
                }
            ),
        )
        self.assertEqual(
            MODULE.recursive_parent_target_owners(plan, 0),
            {
                ("parent", "group", "left"): (
                    "parent",
                    "group",
                    "left",
                ),
                (
                    "parent",
                    "group",
                    "left",
                    "nested",
                    "grandchild",
                ): ("parent", "group", "left"),
                ("parent", "group", "right"): (
                    "parent",
                    "group",
                    "right",
                ),
            },
        )

        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        receipt = plan.shared_missing_ancestors[group_parts].materialized_node
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(
            receipt.fingerprint,
            MODULE.filesystem_fingerprint(target_super / "parent/group"),
        )
        for relative_path in (
            "parent/group/left",
            "parent/group/left/nested/grandchild",
            "parent/group/right",
        ):
            self.assertEqual(
                run_git(target_super / relative_path, "status", "--porcelain"),
                "",
            )

    def test_recursive_parent_grandchild_plan_detects_shared_prefix_replacement_at_boundaries(
        self,
    ) -> None:
        triggers = {
            "before-first-subtree": ("parent", "group", "left"),
            "between-direct-subtrees": ("parent", "group", "right"),
        }
        for boundary, trigger_parts in triggers.items():
            with self.subTest(boundary=boundary):
                scenario = f"checkout-grandchild-replace-{boundary}"
                plan, target_super, parent_source = (
                    self.make_grouped_recursive_plan_with_grandchild(
                        scenario,
                    )
                )
                right_source = parent_source / "modules" / "right"
                right_registry_before = run_git(
                    self.root,
                    f"--git-dir={right_source}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                original_revalidate = MODULE.revalidate_planned_entry
                quarantined = self.root / f"{scenario}-quarantined"
                replaced = False

                def replace_group_at_boundary(
                    current_plan: object,
                    current_entry: object,
                    *,
                    allow_parent_materialization: bool = False,
                ) -> object:
                    nonlocal replaced
                    if (
                        not replaced
                        and current_entry.target.relative_parts == trigger_parts
                        and plan.shared_missing_ancestors[
                            ("parent", "group")
                        ].materialized_node
                        is not None
                    ):
                        replaced = True
                        group = target_super / "parent/group"
                        group.rename(quarantined)
                        group.mkdir()
                    return original_revalidate(
                        current_plan,
                        current_entry,
                        allow_parent_materialization=allow_parent_materialization,
                    )

                with mock.patch.object(
                    MODULE,
                    "revalidate_planned_entry",
                    side_effect=replace_group_at_boundary,
                ):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            "plan-owned shared target ancestor changed",
                        ):
                            MODULE.apply_sync_plan(plan)

                self.assertTrue(replaced)
                self.assertTrue((target_super / "parent/group").is_dir())
                self.assertTrue(quarantined.is_dir())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={right_source}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    right_registry_before,
                )

    def test_new_leaf_finalization_failures_restore_target_and_registry(
        self,
    ) -> None:
        stages = (
            "source-access",
            "source-completeness",
            "source-admission",
            "head",
            "common-gitdir",
            "object-closure",
            "shared-receipt",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                target, module, input_receipt = self.make_target_superproject(
                    f"leaf-finalize-{stage}",
                    self.sha,
                )
                plan = MODULE.build_sync_plan(
                    root=target,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[(module, self.sha)],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    fetch_missing=False,
                    input_receipt=input_receipt,
                )
                entry = plan.entries[0]
                registry_before = run_git(
                    self.root,
                    f"--git-dir={entry.source_git_dir}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                final_target = target / module.path

                def registration_exists() -> bool:
                    return (
                        MODULE.registered_target_path(
                            entry.source_git_dir,
                            final_target,
                        )
                        is not None
                    )

                with ExitStack() as patches:
                    if stage == "source-access":
                        original = MODULE.revalidate_access

                        def fail_source_access(binding: object) -> None:
                            if (
                                registration_exists()
                                and binding in entry.source_bindings
                            ):
                                raise MODULE.PlanError(
                                    "injected final source-access failure"
                                )
                            original(binding)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "revalidate_access",
                                side_effect=fail_source_access,
                            )
                        )
                    elif stage == "source-completeness":
                        original = MODULE.revalidate_source_completeness_receipt

                        def fail_source_completeness(*args: object) -> None:
                            if registration_exists():
                                raise MODULE.PlanError(
                                    "injected final source-completeness failure"
                                )
                            original(*args)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "revalidate_source_completeness_receipt",
                                side_effect=fail_source_completeness,
                            )
                        )
                    elif stage == "source-admission":
                        original = MODULE.revalidate_source_object_admission

                        def fail_source_admission(*args: object) -> None:
                            if registration_exists():
                                raise MODULE.PlanError(
                                    "injected final source-admission failure"
                                )
                            original(*args)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "revalidate_source_object_admission",
                                side_effect=fail_source_admission,
                            )
                        )
                    elif stage == "head":
                        original = MODULE.open_bound_regular_file_at

                        def fail_final_head(
                            *args: object,
                            **kwargs: object,
                        ) -> object:
                            if registration_exists() and args[1] == "HEAD":
                                raise MODULE.PlanError("injected final HEAD failure")
                            return original(*args, **kwargs)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "open_bound_regular_file_at",
                                side_effect=fail_final_head,
                            )
                        )
                    elif stage == "common-gitdir":
                        original = MODULE.open_bound_regular_file_at

                        def fail_final_common_gitdir(
                            *args: object,
                            **kwargs: object,
                        ) -> object:
                            if registration_exists() and args[1] == "commondir":
                                raise MODULE.PlanError(
                                    "injected final common-gitdir failure"
                                )
                            return original(*args, **kwargs)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "open_bound_regular_file_at",
                                side_effect=fail_final_common_gitdir,
                            )
                        )
                    elif stage == "object-closure":
                        original = MODULE.target_object_closure

                        def fail_object_closure(*args: object) -> object:
                            if registration_exists():
                                raise MODULE.PlanError(
                                    "injected final object-closure failure"
                                )
                            return original(*args)

                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "target_object_closure",
                                side_effect=fail_object_closure,
                            )
                        )
                    else:
                        patches.enter_context(
                            mock.patch.object(
                                MODULE,
                                "record_materialized_shared_ancestors",
                                side_effect=MODULE.PlanError(
                                    "injected final shared-receipt failure"
                                ),
                            )
                        )

                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            "injected final",
                        ) as raised:
                            MODULE.apply_sync_plan(plan)

                self.assertNotIn(
                    "worktree/materialization rollback failed",
                    str(raised.exception),
                )
                self.assertFalse(final_target.exists())
                self.assertFalse((target / "third_party").exists())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={entry.source_git_dir}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    registry_before,
                )
                self.assertTrue(
                    all(
                        ancestor.materialized_node is None
                        for ancestor in plan.shared_missing_ancestors.values()
                    )
                )

    def test_new_leaf_finalization_rejects_same_source_admin_retarget(
        self,
    ) -> None:
        phases = (
            "before-final-state-capture",
            "during-postvalidation",
            "before-receipt-publication",
            "during-receipt-publication",
            "after-receipt-publication",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                target, module, input_receipt = self.make_target_superproject(
                    f"leaf-control-retarget-{phase}",
                    self.sha,
                )
                plan = MODULE.build_sync_plan(
                    root=target,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[(module, self.sha)],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    fetch_missing=False,
                    input_receipt=input_receipt,
                )
                entry = plan.entries[0]
                peer = self.root / f"leaf-control-peer-{phase}"
                self.add_managed_worktree(
                    entry.source_git_dir,
                    peer,
                    entry.sha,
                )
                peer_admin = MODULE.gitdir_file_target(peer)
                self.assertIsNotNone(peer_admin)
                registry_before = run_git(
                    self.root,
                    f"--git-dir={entry.source_git_dir}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                shared_before = {
                    parts: ancestor.materialized_node
                    for parts, ancestor in plan.shared_missing_ancestors.items()
                }
                roots_before = dict(plan.applied_target_roots)
                final_target = target / module.path
                replaced = False

                def retarget_control_file() -> None:
                    nonlocal replaced
                    if replaced:
                        return
                    replaced = True
                    replacement = final_target / ".git.control-replacement"
                    replacement.write_text(
                        f"gitdir: {peer_admin}\n",
                        encoding="utf-8",
                    )
                    os.replace(replacement, final_target / ".git")

                original_capture = MODULE.capture_managed_final_state_receipt
                original_revalidate = MODULE.revalidate_managed_final_state_receipt
                original_closure = MODULE.target_object_closure
                original_record = MODULE.record_materialized_shared_ancestors
                final_state_captured = False
                final_revalidation_count = 0

                def capture_final_state(*args: object, **kwargs: object) -> object:
                    nonlocal final_state_captured
                    if phase == "before-final-state-capture":
                        retarget_control_file()
                    result = original_capture(*args, **kwargs)
                    final_state_captured = True
                    return result

                def close_over_final_state(
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    result = original_closure(*args, **kwargs)
                    if phase == "during-postvalidation" and final_state_captured:
                        retarget_control_file()
                    return result

                def revalidate_final_state(
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    nonlocal final_revalidation_count
                    final_revalidation_count += 1
                    if (
                        phase == "before-receipt-publication"
                        and final_revalidation_count == 2
                    ) or (
                        phase == "after-receipt-publication"
                        and final_revalidation_count == 3
                    ):
                        retarget_control_file()
                    original_revalidate(*args, **kwargs)

                def publish_shared_receipts(
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    original_record(*args, **kwargs)
                    if phase == "during-receipt-publication":
                        retarget_control_file()

                with (
                    mock.patch.object(
                        MODULE,
                        "capture_managed_final_state_receipt",
                        side_effect=capture_final_state,
                    ),
                    mock.patch.object(
                        MODULE,
                        "target_object_closure",
                        side_effect=close_over_final_state,
                    ),
                    mock.patch.object(
                        MODULE,
                        "revalidate_managed_final_state_receipt",
                        side_effect=revalidate_final_state,
                    ),
                    mock.patch.object(
                        MODULE,
                        "record_materialized_shared_ancestors",
                        side_effect=publish_shared_receipts,
                    ),
                ):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(MODULE.PlanError) as raised:
                            MODULE.apply_sync_plan(plan)

                self.assertTrue(replaced)
                self.assertIn("control file", str(raised.exception))
                self.assertNotIn(
                    "worktree/materialization rollback failed",
                    str(raised.exception),
                )
                self.assertFalse(final_target.exists())
                self.assertFalse((target / "third_party").exists())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={entry.source_git_dir}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    registry_before,
                )
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={peer_admin}",
                        "rev-parse",
                        "HEAD",
                    ),
                    entry.sha,
                )
                self.assertEqual(plan.applied_target_roots, roots_before)
                self.assertEqual(
                    {
                        parts: ancestor.materialized_node
                        for parts, ancestor in plan.shared_missing_ancestors.items()
                    },
                    shared_before,
                )

    def test_new_leaf_finalization_rejects_admin_backlink_retarget(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "leaf-backlink-retarget",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        entry = plan.entries[0]
        peer = self.root / "leaf-backlink-peer"
        self.add_managed_worktree(
            entry.source_git_dir,
            peer,
            entry.sha,
        )
        peer_admin = MODULE.gitdir_file_target(peer)
        self.assertIsNotNone(peer_admin)
        registry_before = run_git(
            self.root,
            f"--git-dir={entry.source_git_dir}",
            "worktree",
            "list",
            "--porcelain",
        )
        final_target = target / module.path
        original_record = MODULE.record_materialized_shared_ancestors
        replaced = False

        def replace_backlink_during_publication(
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            original_record(*args, **kwargs)
            target_admin = MODULE.gitdir_file_target(final_target)
            self.assertIsNotNone(target_admin)
            assert target_admin is not None
            replacement = target_admin / "gitdir.control-replacement"
            replacement.write_text(
                f"{peer / '.git'}\n",
                encoding="utf-8",
            )
            os.replace(replacement, target_admin / "gitdir")
            replaced = True

        with mock.patch.object(
            MODULE,
            "record_materialized_shared_ancestors",
            side_effect=replace_backlink_during_publication,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "admin backlink",
                ) as raised:
                    MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertNotIn(
            "worktree/materialization rollback failed",
            str(raised.exception),
        )
        self.assertFalse(final_target.exists())
        self.assertFalse((target / "third_party").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={entry.source_git_dir}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={peer_admin}",
                "rev-parse",
                "HEAD",
            ),
            entry.sha,
        )
        self.assertEqual(plan.applied_target_roots, {})
        self.assertTrue(
            all(
                ancestor.materialized_node is None
                for ancestor in plan.shared_missing_ancestors.values()
            )
        )

    def test_new_leaf_finalization_binds_each_admin_state_file(
        self,
    ) -> None:
        for admin_name in ("HEAD", "commondir", "index"):
            with self.subTest(admin_name=admin_name):
                target, module, input_receipt = self.make_target_superproject(
                    f"leaf-admin-state-{admin_name}",
                    self.sha,
                )
                plan = MODULE.build_sync_plan(
                    root=target,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[(module, self.sha)],
                    depth=1,
                    recursive=False,
                    force_replace_empty=False,
                    fetch_missing=False,
                    input_receipt=input_receipt,
                )
                entry = plan.entries[0]
                final_target = target / module.path
                registry_before = run_git(
                    self.root,
                    f"--git-dir={entry.source_git_dir}",
                    "worktree",
                    "list",
                    "--porcelain",
                )
                original_record = MODULE.record_materialized_shared_ancestors
                replaced = False

                def replace_admin_state_file(
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    nonlocal replaced
                    original_record(*args, **kwargs)
                    target_admin = MODULE.gitdir_file_target(final_target)
                    self.assertIsNotNone(target_admin)
                    assert target_admin is not None
                    admin_path = target_admin / admin_name
                    admin_stat = admin_path.stat()
                    replacement = target_admin / f"{admin_name}.replacement"
                    replacement.write_bytes(admin_path.read_bytes())
                    replacement.chmod(stat.S_IMODE(admin_stat.st_mode))
                    os.replace(replacement, admin_path)
                    replaced = True

                with mock.patch.object(
                    MODULE,
                    "record_materialized_shared_ancestors",
                    side_effect=replace_admin_state_file,
                ):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(MODULE.PlanError) as raised:
                            MODULE.apply_sync_plan(plan)

                self.assertTrue(replaced)
                self.assertIn(
                    {
                        "HEAD": "final HEAD",
                        "commondir": "common-gitdir pointer",
                        "index": "final index",
                    }[admin_name],
                    str(raised.exception),
                )
                self.assertNotIn(
                    "worktree/materialization rollback failed",
                    str(raised.exception),
                )
                self.assertFalse(final_target.exists())
                self.assertFalse((target / "third_party").exists())
                self.assertEqual(
                    run_git(
                        self.root,
                        f"--git-dir={entry.source_git_dir}",
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    registry_before,
                )
                self.assertTrue(
                    all(
                        ancestor.materialized_node is None
                        for ancestor in plan.shared_missing_ancestors.values()
                    )
                )

    def test_final_pointer_revalidation_rejects_intermediate_symlink_retarget(
        self,
    ) -> None:
        for pointer_kind in ("git-admin", "backlink", "commondir"):
            with self.subTest(pointer_kind=pointer_kind):
                source_name = f"pointer-chain-{pointer_kind}"
                source = self.clone_named_source(source_name)
                run_git(source, "config", "core.untrackedCache", "false")
                target_root = self.root / f"{source_name}-target"
                target_root.mkdir()
                target = target_root / "lib"
                self.add_managed_worktree(source, target, self.sha)
                run_git(target, "update-index", "--no-untracked-cache")
                run_git(target, "update-index", "--no-fsmonitor")
                admin = MODULE.gitdir_file_target(target)
                self.assertIsNotNone(admin)
                assert admin is not None

                if pointer_kind == "git-admin":
                    peer = self.root / f"{source_name}-peer"
                    self.add_managed_worktree(source, peer, self.sha)
                    peer_admin = MODULE.gitdir_file_target(peer)
                    self.assertIsNotNone(peer_admin)
                    assert peer_admin is not None
                    symlink_target = peer_admin / "child"
                    symlink_target.mkdir()
                    route = admin / "route"
                    route.mkdir()
                    (target / ".git").write_text(
                        f"gitdir: {route}/..\n",
                        encoding="utf-8",
                    )
                elif pointer_kind == "backlink":
                    peer = self.root / f"{source_name}-peer"
                    self.add_managed_worktree(source, peer, self.sha)
                    symlink_target = peer / "child"
                    symlink_target.mkdir()
                    route = target / "route"
                    route.mkdir()
                    (admin / "gitdir").write_text(
                        f"{route.resolve(strict=True)}/../.git\n",
                        encoding="utf-8",
                    )
                else:
                    alternate_source = self.clone_named_source(
                        f"{source_name}-alternate"
                    )
                    symlink_target = alternate_source / "a" / "b" / "c"
                    symlink_target.mkdir(parents=True)
                    route = admin / "route"
                    route.mkdir()
                    (admin / "commondir").write_text(
                        "route/../../..\n",
                        encoding="utf-8",
                    )

                plan = MODULE.build_sync_plan(
                    root=target_root,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                source_name,
                                "lib",
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
                entry = plan.entries[0]
                lease = MODULE.materialize_bound_target_directory(entry.target)
                source_lease = MODULE.capture_planned_source_lease(entry)
                control = MODULE.capture_managed_control_receipt(
                    target,
                    source,
                    lease.target_descriptor,
                )
                final_state = MODULE.capture_managed_final_state_receipt(
                    entry,
                    lease,
                    control,
                    source_lease,
                )
                original_route = route.with_name("route.original")
                route.rename(original_route)
                os.symlink(symlink_target, route, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "no-follow descriptor chain|no-follow endpoint",
                    ):
                        MODULE.revalidate_managed_final_state_receipt(
                            entry,
                            final_state,
                            lease,
                            control,
                            source_lease,
                        )
                finally:
                    route.unlink()
                    original_route.rename(route)
                    try:
                        final_state.close()
                    finally:
                        try:
                            control.close()
                        finally:
                            try:
                                source_lease.close()
                            finally:
                                lease.close()

    def test_final_state_capture_and_revalidation_close_index_validation_races(
        self,
    ) -> None:
        for phase in ("capture", "revalidate"):
            for mutation in ("HEAD", "commondir"):
                with self.subTest(phase=phase, mutation=mutation):
                    source_name = f"final-state-{phase}-{mutation.lower()}"
                    source = self.clone_named_source(source_name)
                    run_git(source, "config", "core.untrackedCache", "false")
                    target_root = self.root / f"{source_name}-target"
                    target_root.mkdir()
                    target = target_root / "lib"
                    self.add_managed_worktree(source, target, self.sha)
                    run_git(target, "update-index", "--no-untracked-cache")
                    run_git(target, "update-index", "--no-fsmonitor")
                    admin = MODULE.gitdir_file_target(target)
                    self.assertIsNotNone(admin)
                    assert admin is not None

                    head_content = (admin / "HEAD").read_bytes()
                    route = admin / "route"
                    original_route = route.with_name("route.original")
                    alternate_source = self.clone_named_source(
                        f"{source_name}-alternate"
                    )
                    symlink_target = alternate_source / "a" / "b" / "c"
                    symlink_target.mkdir(parents=True)
                    route.mkdir()
                    (admin / "commondir").write_text(
                        "route/../../..\n",
                        encoding="utf-8",
                    )

                    plan = MODULE.build_sync_plan(
                        root=target_root,
                        common_git_dir=self.named_common_git_dir,
                        source_superproject=None,
                        planned_modules=[
                            (
                                MODULE.Submodule(
                                    source_name,
                                    "lib",
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
                    entry = plan.entries[0]
                    lease = MODULE.materialize_bound_target_directory(entry.target)
                    source_lease = MODULE.capture_planned_source_lease(entry)
                    control = MODULE.capture_managed_control_receipt(
                        target,
                        source,
                        lease.target_descriptor,
                    )
                    final_state = None
                    original_validate = MODULE.validate_captured_index_matches_tree
                    mutated = False

                    def mutate_after_index_validation(
                        current_entry: object,
                        index_content: bytes,
                    ) -> None:
                        nonlocal mutated
                        original_validate(current_entry, index_content)
                        if mutated:
                            return
                        if mutation == "HEAD":
                            (admin / "HEAD").write_text(
                                f"{'f' * 40}\n",
                                encoding="ascii",
                            )
                        else:
                            route.rename(original_route)
                            os.symlink(
                                symlink_target,
                                route,
                                target_is_directory=True,
                            )
                        mutated = True

                    error = (
                        "final HEAD content changed"
                        if mutation == "HEAD"
                        else "no-follow descriptor chain|no-follow endpoint"
                    )
                    try:
                        if phase == "revalidate":
                            final_state = MODULE.capture_managed_final_state_receipt(
                                entry,
                                lease,
                                control,
                                source_lease,
                            )
                        with mock.patch.object(
                            MODULE,
                            "validate_captured_index_matches_tree",
                            side_effect=mutate_after_index_validation,
                        ):
                            with self.assertRaisesRegex(MODULE.PlanError, error):
                                if phase == "capture":
                                    MODULE.capture_managed_final_state_receipt(
                                        entry,
                                        lease,
                                        control,
                                        source_lease,
                                    )
                                else:
                                    assert final_state is not None
                                    MODULE.revalidate_managed_final_state_receipt(
                                        entry,
                                        final_state,
                                        lease,
                                        control,
                                        source_lease,
                                    )
                        self.assertTrue(mutated)
                    finally:
                        if mutation == "HEAD":
                            (admin / "HEAD").write_bytes(head_content)
                        elif mutated:
                            route.unlink()
                            original_route.rename(route)
                        if final_state is not None:
                            final_state.close()
                        try:
                            control.close()
                        finally:
                            try:
                                source_lease.close()
                            finally:
                                lease.close()

    def test_fresh_source_lease_bridges_preflight_before_managed_and_new_git(
        self,
    ) -> None:
        for state in ("managed", "new"):
            with self.subTest(state=state):
                source_name = f"source-lease-bridge-{state}"
                source = self.clone_named_source(source_name)
                target_root = self.root / f"{source_name}-target"
                target_root.mkdir()
                target = target_root / "lib"
                if state == "managed":
                    self.add_managed_worktree(source, target, self.sha)
                plan = MODULE.build_sync_plan(
                    root=target_root,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                source_name,
                                "lib",
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
                original_source = source.with_name(f"{source.name}.preflight")
                original_capture = MODULE.capture_planned_source_lease
                replaced = False

                def replace_source_before_fresh_capture(
                    current_entry: object,
                ) -> object:
                    nonlocal replaced
                    source.rename(original_source)
                    shutil.copytree(original_source, source)
                    replaced = True
                    return original_capture(current_entry)

                try:
                    with (
                        mock.patch.object(
                            MODULE,
                            "capture_planned_source_lease",
                            side_effect=replace_source_before_fresh_capture,
                        ),
                        mock.patch.object(
                            MODULE,
                            "run_git_at_directory_descriptor",
                        ) as git_mutation,
                    ):
                        with self.assertRaisesRegex(
                            MODULE.PlanError,
                            "fresh source gitdir lease does not match the "
                            "preflight source access binding",
                        ):
                            MODULE.apply_sync_plan(plan)
                    self.assertTrue(replaced)
                    git_mutation.assert_not_called()
                    if state == "new":
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(
                            run_git(target, "rev-parse", "HEAD"),
                            self.sha,
                        )
                finally:
                    if replaced:
                        shutil.rmtree(source)
                        original_source.rename(source)

    def test_partial_registration_source_replacement_preserves_target(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "partial-registration-source-replacement",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        entry = plan.entries[0]
        final_target = target / module.path
        replacement_source = self.root / "partial-registration-replacement"
        original_source = self.root / "partial-registration-original"
        shutil.copytree(entry.source_git_dir, replacement_source)
        original_run = MODULE.run_git_at_directory_descriptor
        replaced = False

        def fail_after_registration(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal replaced
            result = original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )
            if not replaced and "worktree" in args and "add" in args:
                entry.source_git_dir.rename(original_source)
                replacement_source.rename(entry.source_git_dir)
                replaced = True
                raise MODULE.GitError(
                    "injected failure after partial worktree registration"
                )
            return result

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=fail_after_registration,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "registration state is uncertain; preserving the target",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertTrue(final_target.is_dir())
        self.assertTrue((final_target / ".git").is_file())
        self.assertEqual(
            MODULE.registered_target_path(
                original_source,
                final_target,
            ),
            final_target.resolve(),
        )
        self.assertIsNone(
            MODULE.registered_target_path(
                entry.source_git_dir,
                final_target,
            )
        )

    def assert_partial_admin_gitdir_preserved(
        self,
        scenario: str,
        gitdir_content: bytes | None,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            f"partial-admin-{scenario}",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        entry = plan.entries[0]
        final_target = target / module.path
        created_parent = final_target.parent
        partial_admin = entry.source_git_dir / "worktrees" / "half-created"
        original_run = MODULE.run_git_at_directory_descriptor

        def interrupt_with_partial_admin(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            if "worktree" in args and "add" in args:
                partial_admin.mkdir(parents=True)
                (partial_admin / "HEAD").write_text(
                    f"{self.sha}\n",
                    encoding="ascii",
                )
                (partial_admin / "commondir").write_text(
                    "../..\n",
                    encoding="ascii",
                )
                if gitdir_content is not None:
                    (partial_admin / "gitdir").write_bytes(gitdir_content)
                raise MODULE.GitError(f"injected {scenario} gitdir interruption")
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with mock.patch.object(
            MODULE,
            "run_git_at_directory_descriptor",
            side_effect=interrupt_with_partial_admin,
        ):
            with self.assertRaises(MODULE.PlanError) as raised:
                MODULE.apply_sync_plan(plan)

        error = str(raised.exception)
        self.assertIn(
            "registration state is uncertain; preserving the target",
            error,
        )
        self.assertIn("recovery_status: admin-entry-added", error)
        self.assertIn(f"recovery_location: {partial_admin}", error)
        self.assertTrue(final_target.is_dir())
        self.assertTrue(created_parent.is_dir())
        self.assertTrue(partial_admin.is_dir())
        if gitdir_content is None:
            self.assertFalse((partial_admin / "gitdir").exists())
        else:
            self.assertEqual(
                (partial_admin / "gitdir").read_bytes(),
                gitdir_content,
            )
        self.assertIsNone(
            MODULE.registered_target_path(
                entry.source_git_dir,
                final_target,
            )
        )

    def test_partial_admin_missing_gitdir_preserves_recovery_locator(
        self,
    ) -> None:
        self.assert_partial_admin_gitdir_preserved("missing", None)

    def test_partial_admin_invalid_gitdir_preserves_recovery_locator(
        self,
    ) -> None:
        self.assert_partial_admin_gitdir_preserved("invalid", b"")

    def test_known_admin_rollback_requires_exact_entry_absence(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "rollback-admin-entry-reappears",
            self.sha,
        )
        peer = self.root / "rollback-admin-entry-peer"
        self.add_managed_worktree(
            self.named_source_git_dir,
            peer,
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        final_target = target / module.path
        created_parent = final_target.parent
        original_run = MODULE.run_git_at_directory_descriptor
        recreated_admin: Path | None = None

        def recreate_admin_after_remove(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal recreated_admin
            if "worktree" in args and "remove" in args:
                recreated_admin = MODULE.gitdir_file_target(final_target)
                self.assertIsNotNone(recreated_admin)
                result = original_run(
                    args,
                    directory_descriptor,
                    extra_env=extra_env,
                    directory_identity_leases=directory_identity_leases,
                )
                assert recreated_admin is not None
                recreated_admin.mkdir()
                return result
            return original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )

        with (
            mock.patch.object(
                MODULE,
                "postvalidate_applied_entry",
                side_effect=MODULE.PlanError("injected finalization failure"),
            ),
            mock.patch.object(
                MODULE,
                "run_git_at_directory_descriptor",
                side_effect=recreate_admin_after_remove,
            ),
        ):
            with self.assertRaises(MODULE.PlanError) as raised:
                MODULE.apply_sync_plan(plan)

        self.assertIsNotNone(recreated_admin)
        assert recreated_admin is not None
        error = str(raised.exception)
        self.assertIn("recovery_status: admin-entry-still-present", error)
        self.assertIn(f"recovery_location: {recreated_admin}", error)
        self.assertFalse(final_target.exists())
        self.assertTrue(created_parent.is_dir())
        self.assertTrue(recreated_admin.is_dir())

    def test_rollback_exec_gate_preserves_raced_target_replacement(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "rollback-target-entry-replacement",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        final_target = target / module.path
        held_original = self.root / "rollback-target-entry-original"
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False
        registered_admin: Path | None = None

        def replace_target_before_remove_exec(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed, registered_admin
            if "worktree" in args and "remove" in args:
                self.assertFalse(replacement_performed)
                registered_admin = MODULE.gitdir_file_target(final_target)
                self.assertIsNotNone(registered_admin)
                final_target.rename(held_original)
                final_target.mkdir()
                (final_target / "replacement.txt").write_text(
                    "must survive failed rollback\n",
                    encoding="utf-8",
                )
                replacement_performed = True
            return original_run_bounded(args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "postvalidate_applied_entry",
                side_effect=MODULE.PlanError("injected finalization failure"),
            ),
            mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                side_effect=replace_target_before_remove_exec,
            ),
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(MODULE.PlanError) as raised:
                    MODULE.apply_sync_plan(plan)

        self.assertTrue(replacement_performed)
        self.assertIn("recovery_status: rollback-incomplete", str(raised.exception))
        self.assertEqual(
            (final_target / "replacement.txt").read_text(encoding="utf-8"),
            "must survive failed rollback\n",
        )
        self.assertFalse((final_target / ".git").exists())
        self.assertTrue((held_original / ".git").is_file())
        self.assertIsNotNone(registered_admin)
        assert registered_admin is not None
        self.assertTrue(registered_admin.is_dir())

    def assert_unproven_admin_ownership_preserved(
        self,
        scenario: str,
        *,
        interrupt_after_add: bool,
        expected_status: str,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            f"{scenario}-control-unproven",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        entry = plan.entries[0]
        final_target = target / module.path
        original_run = MODULE.run_git_at_directory_descriptor
        remove_attempted = False

        def interrupt_after_successful_add(
            args: list[str],
            directory_descriptor: int,
            *,
            extra_env: dict[str, str] | None = None,
            directory_identity_leases: tuple[object, ...] = (),
        ) -> subprocess.CompletedProcess[str]:
            nonlocal remove_attempted
            if "worktree" in args and "remove" in args:
                remove_attempted = True
            result = original_run(
                args,
                directory_descriptor,
                extra_env=extra_env,
                directory_identity_leases=directory_identity_leases,
            )
            if interrupt_after_add and "worktree" in args and "add" in args:
                raise MODULE.GitError("injected post-add interruption")
            return result

        with (
            mock.patch.object(
                MODULE,
                "run_git_at_directory_descriptor",
                side_effect=interrupt_after_successful_add,
            ),
            mock.patch.object(
                MODULE,
                "validate_expected_worktree_admin_add",
                side_effect=MODULE.PlanError(
                    "injected administration ownership proof failure"
                ),
            ) as validate_add,
        ):
            with self.assertRaises(MODULE.PlanError) as raised:
                MODULE.apply_sync_plan(plan)

        error = str(raised.exception)
        self.assertIn(
            "registration state is uncertain; preserving the target",
            error,
        )
        self.assertIn(f"recovery_status: {expected_status}", error)
        self.assertEqual(validate_add.call_count, 1)
        self.assertFalse(remove_attempted)
        self.assertTrue(final_target.is_dir())
        self.assertTrue((final_target / ".git").is_file())
        self.assertEqual(
            MODULE.registered_target_path(
                entry.source_git_dir,
                final_target,
            ),
            final_target.resolve(strict=True),
        )

    def test_primary_control_without_admin_ownership_proof_is_preserved(
        self,
    ) -> None:
        self.assert_unproven_admin_ownership_preserved(
            "primary",
            interrupt_after_add=False,
            expected_status="admin-ownership-unproven",
        )

    def test_recovered_control_without_admin_ownership_proof_is_preserved(
        self,
    ) -> None:
        self.assert_unproven_admin_ownership_preserved(
            "recovered",
            interrupt_after_add=True,
            expected_status="admin-entry-added",
        )

    def test_expected_admin_add_binds_inventory_to_control_parent(
        self,
    ) -> None:
        source = self.clone_named_source("admin-parent-binding")
        target = self.root / "admin-parent-binding-target"
        source_lease = MODULE.capture_directory_entry_lease(
            source,
            os.R_OK | os.W_OK | os.X_OK,
            "selected source common gitdir",
        )
        target_descriptor = -1
        control = None
        try:
            before = MODULE.capture_worktree_admin_inventory(
                source,
                source_lease,
            )
            self.add_managed_worktree(source, target, self.sha)
            after = MODULE.capture_worktree_admin_inventory(
                source,
                source_lease,
            )
            target_descriptor = MODULE.open_directory_descriptor(
                target,
                "managed worktree target",
            )
            control = MODULE.capture_managed_control_receipt(
                target,
                source,
                target_descriptor,
            )
            self.assertNotEqual(
                after.root_fingerprint,
                control.admin_lease.binding.fingerprint,
            )
            mismatched_after = MODULE.WorktreeAdminInventory(
                source_fingerprint=after.source_fingerprint,
                root_fingerprint=control.admin_lease.binding.fingerprint,
                entries=after.entries,
            )

            with self.assertRaisesRegex(
                MODULE.PlanError,
                "different parent objects",
            ):
                MODULE.validate_expected_worktree_admin_add(
                    source,
                    before,
                    mismatched_after,
                    control,
                )
        finally:
            if control is not None:
                control.close()
            if target_descriptor >= 0:
                os.close(target_descriptor)
            source_lease.close()

    def test_final_rollback_registry_query_drift_preserves_target_parent(
        self,
    ) -> None:
        target, module, input_receipt = self.make_target_superproject(
            "rollback-registry-query-drift",
            self.sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, self.sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        entry = plan.entries[0]
        final_target = target / module.path
        created_parent = final_target.parent
        replacement_source = self.root / "rollback-query-replacement"
        original_source = self.root / "rollback-query-original"
        shutil.copytree(entry.source_git_dir, replacement_source)
        original_read_git_bounded = MODULE.read_git_bounded
        replaced = False

        def replace_after_final_registry_query(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replaced
            result = original_read_git_bounded(args, **kwargs)
            if (
                not replaced
                and "worktree" in args
                and "list" in args
                and kwargs.get("directory_identity_leases")
                and not final_target.exists()
            ):
                entry.source_git_dir.rename(original_source)
                replacement_source.rename(entry.source_git_dir)
                replaced = True
            return result

        with (
            mock.patch.object(
                MODULE,
                "postvalidate_applied_entry",
                side_effect=MODULE.PlanError("injected finalization failure"),
            ),
            mock.patch.object(
                MODULE,
                "read_git_bounded",
                side_effect=replace_after_final_registry_query,
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "rolled-back worktree registry state is uncertain",
            ):
                MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertFalse(final_target.exists())
        self.assertTrue(created_parent.is_dir())
        self.assertIsNone(
            MODULE.registered_target_path(
                original_source,
                final_target,
            )
        )
        self.assertIsNone(
            MODULE.registered_target_path(
                entry.source_git_dir,
                final_target,
            )
        )

    def test_captured_index_rejects_conflict_and_hidden_entry_flags(self) -> None:
        source = self.clone_named_source("captured-index-flags")
        target = self.root / "captured-index-flags-target"
        self.add_managed_worktree(source, target, self.sha)
        admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(admin)
        assert admin is not None
        original = (admin / "index").read_bytes()
        self.assertEqual(int.from_bytes(original[4:8], "big"), 2)
        flags_offset = 12 + 40 + 20

        for label, flag, error in (
            ("conflict", 0x1000, "unresolved conflict"),
            ("assume-valid", 0x8000, "hidden assume-valid"),
        ):
            with self.subTest(label=label):
                modified = bytearray(original)
                flags = int.from_bytes(
                    modified[flags_offset : flags_offset + 2],
                    "big",
                )
                modified[flags_offset : flags_offset + 2] = (flags | flag).to_bytes(
                    2, "big"
                )
                modified[-20:] = hashlib.sha1(modified[:-20]).digest()
                with self.assertRaisesRegex(MODULE.PlanError, error):
                    MODULE.captured_index_entries(bytes(modified), 20)

    def test_captured_index_parser_accepts_versions_two_through_four(self) -> None:
        (self.remote / "docs").mkdir()
        for name in ("alpha.txt", "alpine.txt", "zebra.txt"):
            (self.remote / "docs" / name).write_text(
                f"{name}\n",
                encoding="utf-8",
            )
        for directory in ("z", "aa"):
            (self.remote / directory).mkdir()
            (self.remote / directory / "file.txt").write_text(
                f"{directory}\n",
                encoding="utf-8",
            )
        run_git(self.remote, "add", "docs", "z", "aa")
        run_git(self.remote, "commit", "-m", "add index version paths")
        version_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("captured-index-versions")
        target = self.root / "captured-index-versions-target"
        run_git(source, "config", "core.untrackedCache", "false")
        self.add_managed_worktree(source, target, version_sha)
        run_git(target, "update-index", "--no-untracked-cache")
        run_git(target, "update-index", "--no-fsmonitor")
        run_git(target, "write-tree")
        admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(admin)
        assert admin is not None
        entry = SimpleNamespace(
            sha=version_sha,
            source_git_dir=source,
            target=SimpleNamespace(path=target),
        )

        version_two = (admin / "index").read_bytes()
        self.assertEqual(int.from_bytes(version_two[4:8], "big"), 2)
        self.assertIn(b"TREE", v2_index_extension_signatures(version_two))
        MODULE.validate_captured_index_matches_tree(entry, version_two)

        chunks, paths, extensions_offset = split_v2_index_entries(version_two)
        first_chunk = bytearray(chunks[0][:62])
        first_flags = int.from_bytes(first_chunk[60:62], "big")
        first_chunk[60:62] = (first_flags | 0x4000).to_bytes(2, "big")
        first_entry = bytes(first_chunk) + b"\0\0" + paths[0] + b"\0"
        first_entry += bytes((-len(first_entry)) % 8)
        version_three_body = (
            version_two[:4]
            + (3).to_bytes(4, "big")
            + version_two[8:12]
            + first_entry
            + b"".join(chunks[1:])
            + version_two[extensions_offset:-20]
        )
        version_three = version_three_body + hashlib.sha1(version_three_body).digest()
        MODULE.validate_captured_index_matches_tree(entry, version_three)

        run_git(target, "update-index", "--index-version", "4")
        version_four = (admin / "index").read_bytes()
        self.assertEqual(int.from_bytes(version_four[4:8], "big"), 4)
        MODULE.validate_captured_index_matches_tree(entry, version_four)

    def test_new_worktree_accepts_real_cache_tree_name_order(self) -> None:
        for directory in ("aa", "b"):
            path = self.remote / directory
            path.mkdir()
            (path / "file.txt").write_text(
                f"{directory}\n",
                encoding="utf-8",
            )
        run_git(self.remote, "add", "aa", "b")
        run_git(self.remote, "commit", "-m", "add cache tree name ordering")
        ordered_sha = run_git(self.remote, "rev-parse", "HEAD")
        self.fetch_source(self.named_source_git_dir)
        target, module, input_receipt = self.make_target_superproject(
            "cache-tree-name-order",
            ordered_sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, ordered_sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )

        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        final_target = target / module.path
        self.assertEqual(
            (final_target / "aa/file.txt").read_text(encoding="utf-8"),
            "aa\n",
        )
        self.assertEqual(
            (final_target / "b/file.txt").read_text(encoding="utf-8"),
            "b\n",
        )
        admin = MODULE.gitdir_file_target(final_target)
        self.assertIsNotNone(admin)
        assert admin is not None
        index_content = (admin / "index").read_bytes()
        self.assertIn(
            b"TREE",
            v2_index_extension_signatures(index_content),
        )
        self.assertEqual(
            cache_tree_root_leaf_child_names(
                v2_index_extension_payload(index_content, b"TREE"),
            ),
            (b"b", b"aa"),
        )
        self.assertEqual(
            MODULE.registered_target_path(
                self.named_source_git_dir,
                final_target,
            ),
            final_target.resolve(strict=True),
        )

    def test_captured_cache_tree_rejects_tree_entry_name_order(self) -> None:
        root_object_id = b"\x01" * 20
        aa_object_id = b"\x02" * 20
        b_object_id = b"\x03" * 20
        expected_nodes = {
            b"": (2, root_object_id),
            b"aa": (1, aa_object_id),
            b"b": (1, b_object_id),
        }
        root = b"\0" + b"2 2\n" + root_object_id
        aa = b"aa\0" + b"1 0\n" + aa_object_id
        b_node = b"b\0" + b"1 0\n" + b_object_id
        canonical = root + b_node + aa
        MODULE.validate_captured_cache_tree(
            canonical,
            0,
            len(canonical),
            20,
            expected_nodes,
        )

        malformed = root + aa + b_node
        with self.assertRaisesRegex(MODULE.PlanError, "canonical order"):
            MODULE.validate_captured_cache_tree(
                malformed,
                0,
                len(malformed),
                20,
                expected_nodes,
            )

    def test_captured_index_rejects_noncanonical_or_hidden_semantics(self) -> None:
        for name in ("alpha.txt", "zebra.txt"):
            (self.remote / name).write_text(f"{name}\n", encoding="utf-8")
        run_git(self.remote, "add", "alpha.txt", "zebra.txt")
        run_git(self.remote, "commit", "-m", "add strict index paths")
        strict_sha = run_git(self.remote, "rev-parse", "HEAD")
        source = self.clone_named_source("captured-index-strict")
        run_git(source, "config", "core.untrackedCache", "false")
        target = self.root / "captured-index-strict-target"
        self.add_managed_worktree(source, target, strict_sha)
        admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(admin)
        assert admin is not None
        entry = SimpleNamespace(
            sha=strict_sha,
            source_git_dir=source,
            target=SimpleNamespace(path=target),
        )

        run_git(target, "config", "index.skipHash", "true")
        run_git(target, "config", "index.recordEndOfIndexEntries", "true")
        run_git(target, "update-index", "--force-write-index")
        skip_hash_index = (admin / "index").read_bytes()
        self.assertEqual(skip_hash_index[-20:], bytes(20))
        self.assertIn(b"EOIE", v2_index_extension_signatures(skip_hash_index))
        with self.assertRaisesRegex(MODULE.PlanError, "skip-hash checksum"):
            MODULE.captured_index_entries(skip_hash_index, 20)
        self.assertIn("index.skipHash=false", MODULE.SAFE_GIT_CONFIG_ARGS)
        self.assertIn(
            "index.recordEndOfIndexEntries=false",
            MODULE.SAFE_GIT_CONFIG_ARGS,
        )
        MODULE.run(
            [
                "git",
                "-C",
                str(target),
                "update-index",
                "--no-untracked-cache",
                "--no-fsmonitor",
                "--force-write-index",
            ]
        )
        strict_index = (admin / "index").read_bytes()
        self.assertNotEqual(strict_index[-20:], bytes(20))
        self.assertNotIn(b"EOIE", v2_index_extension_signatures(strict_index))
        chunks, paths, _extensions_offset = split_v2_index_entries(strict_index)
        self.assertGreaterEqual(len(chunks), 1)
        base_body = strict_index[:12] + b"".join(chunks)

        for signature, error in (
            (b"FSMN", "hidden fsmonitor"),
            (b"UNTR", "hidden untracked-cache"),
            (b"EOIE", "hidden end-of-index-entry"),
            (b"IEOT", "hidden index-entry-offset-table"),
            (b"ABCD", "unsupported optional"),
        ):
            with self.subTest(signature=signature):
                body = base_body + signature + (0).to_bytes(4, "big")
                modified = body + hashlib.sha1(body).digest()
                with self.assertRaisesRegex(MODULE.PlanError, error):
                    MODULE.validate_captured_index_matches_tree(entry, modified)

        cache_tree_payload = (
            b"\0" + str(len(chunks)).encode("ascii") + b" 0\n" + bytes(20)
        )
        cache_tree_body = (
            base_body
            + b"TREE"
            + len(cache_tree_payload).to_bytes(4, "big")
            + cache_tree_payload
        )
        cache_tree_index = cache_tree_body + hashlib.sha1(cache_tree_body).digest()
        with self.assertRaisesRegex(MODULE.PlanError, "cache-tree does not match"):
            MODULE.validate_captured_index_matches_tree(entry, cache_tree_index)

        if len(chunks) >= 2:
            reordered = (chunks[1], chunks[0], *chunks[2:])
            version_two_body = strict_index[:12] + b"".join(reordered)
            version_two = version_two_body + hashlib.sha1(version_two_body).digest()
            with self.assertRaisesRegex(MODULE.PlanError, "canonical order"):
                MODULE.validate_captured_index_matches_tree(entry, version_two)

            version_four_body = (
                strict_index[:4] + (4).to_bytes(4, "big") + strict_index[8:12]
            )
            prior_path = b""
            for chunk, raw_path in zip(reordered, (paths[1], paths[0], *paths[2:])):
                version_four_body += (
                    chunk[:62]
                    + encode_index_v4_strip_count(len(prior_path))
                    + raw_path
                    + b"\0"
                )
                prior_path = raw_path
            version_four = version_four_body + hashlib.sha1(version_four_body).digest()
            with self.assertRaisesRegex(MODULE.PlanError, "canonical order"):
                MODULE.validate_captured_index_matches_tree(entry, version_four)

        strict_index = base_body + hashlib.sha1(base_body).digest()
        with mock.patch.object(MODULE, "GIT_ENUMERATION_OUTPUT_LIMIT_BYTES", 1):
            with self.assertRaisesRegex(MODULE.PlanError, "expanded paths exceed"):
                MODULE.captured_index_entries(strict_index, 20)

        root_object_id = b"\x01" * 20
        empty_object_id = b"\x02" * 20
        expected_empty_tree = {
            b"": (0, root_object_id),
            b"empty": (0, empty_object_id),
        }
        empty_body = b"DIRC" + (2).to_bytes(4, "big") + bytes(4)
        empty_index = empty_body + hashlib.sha1(empty_body).digest()
        with self.assertRaisesRegex(MODULE.PlanError, "requires a cache-tree"):
            MODULE.captured_index_entries(
                empty_index,
                20,
                expected_cache_tree=expected_empty_tree,
                require_cache_tree=True,
            )

        cache_tree_payload = (
            b"\0" + b"0 1\n" + root_object_id + b"empty\0" + b"0 0\n" + empty_object_id
        )
        empty_tree_body = (
            empty_body
            + b"TREE"
            + len(cache_tree_payload).to_bytes(4, "big")
            + cache_tree_payload
        )
        empty_tree_index = empty_tree_body + hashlib.sha1(empty_tree_body).digest()
        self.assertEqual(
            MODULE.captured_index_entries(
                empty_tree_index,
                20,
                expected_cache_tree=expected_empty_tree,
                require_cache_tree=True,
            ),
            (),
        )

    def test_managed_preflight_rejects_split_index_before_checkout(self) -> None:
        source = self.clone_named_source("managed-split-index")
        target_super = self.root / "managed-split-index-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, self.sha)
        run_git(target, "update-index", "--split-index")
        admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(admin)
        assert admin is not None
        self.assertIn(
            b"link",
            v2_index_extension_signatures((admin / "index").read_bytes()),
        )

        with (
            mock.patch.object(MODULE, "probe_managed_checkout") as probe,
            mock.patch.object(MODULE, "checkout_existing_worktree") as checkout,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "hidden split-index state",
            ):
                MODULE.build_sync_plan(
                    root=target_super,
                    common_git_dir=self.named_common_git_dir,
                    source_superproject=None,
                    planned_modules=[
                        (
                            MODULE.Submodule(
                                "managed-split-index",
                                "lib",
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
        probe.assert_not_called()
        checkout.assert_not_called()

    def test_managed_preflight_rejects_unknown_index_extension_before_checkout(
        self,
    ) -> None:
        source = self.clone_named_source("managed-unknown-index-extension")
        target_super = self.root / "managed-unknown-index-extension-target"
        target_super.mkdir()
        target = target_super / "lib"
        self.add_managed_worktree(source, target, self.sha)
        admin = MODULE.gitdir_file_target(target)
        self.assertIsNotNone(admin)
        assert admin is not None
        index_path = admin / "index"
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "managed-unknown-index-extension",
                        "lib",
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
        original = index_path.read_bytes()
        body = original[:-20] + b"ABCD" + bytes(4)
        index_path.write_bytes(body + hashlib.sha1(body).digest())
        self.assertIn(
            b"ABCD",
            v2_index_extension_signatures(index_path.read_bytes()),
        )

        with (
            mock.patch.object(MODULE, "probe_managed_checkout") as probe,
            mock.patch.object(MODULE, "checkout_existing_worktree") as checkout,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "unsupported optional extension: b'ABCD'",
            ):
                MODULE.apply_sync_plan(plan)
        probe.assert_not_called()
        checkout.assert_not_called()

    def test_final_index_rejects_another_valid_checkout_snapshot(self) -> None:
        (self.remote / "SECOND.md").write_text("second\n", encoding="utf-8")
        run_git(self.remote, "add", "SECOND.md")
        run_git(self.remote, "commit", "-m", "second")
        target_sha = run_git(self.remote, "rev-parse", "HEAD")
        self.fetch_source(self.named_source_git_dir)
        peer = self.root / "final-index-peer"
        self.add_managed_worktree(
            self.named_source_git_dir,
            peer,
            self.sha,
        )
        peer_admin = MODULE.gitdir_file_target(peer)
        self.assertIsNotNone(peer_admin)
        assert peer_admin is not None
        peer_index = (peer_admin / "index").read_bytes()
        peer_chunks, _peer_paths, _peer_extensions = split_v2_index_entries(peer_index)
        peer_body = peer_index[:12] + b"".join(peer_chunks)
        peer_index = peer_body + hashlib.sha1(peer_body).digest()

        target, module, input_receipt = self.make_target_superproject(
            "final-index-target",
            target_sha,
        )
        plan = MODULE.build_sync_plan(
            root=target,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[(module, target_sha)],
            depth=1,
            recursive=False,
            force_replace_empty=False,
            fetch_missing=False,
            input_receipt=input_receipt,
        )
        final_target = target / module.path
        original_capture = MODULE.capture_managed_final_state_receipt
        replaced = False

        def replace_index_before_capture(
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal replaced
            control = args[2]
            self.assertIsInstance(control, MODULE.ManagedControlReceipt)
            (control.admin_git_dir / "index").write_bytes(peer_index)
            replaced = True
            return original_capture(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "capture_managed_final_state_receipt",
            side_effect=replace_index_before_capture,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "stage-0 mode/OID/path set does not match",
                ):
                    MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertFalse(final_target.exists())
        self.assertTrue(
            all(
                ancestor.materialized_node is None
                for ancestor in plan.shared_missing_ancestors.values()
            )
        )

    def test_managed_leaf_finalization_retains_original_admin_identity(
        self,
    ) -> None:
        source = self.clone_named_source("managed-final-control")
        target_super = self.root / "managed-final-control-target"
        target_super.mkdir()
        target = target_super / "lib"
        peer = self.root / "managed-final-control-peer"
        self.add_managed_worktree(source, target, self.sha)
        self.add_managed_worktree(source, peer, self.sha)
        original_admin = MODULE.gitdir_file_target(target)
        peer_admin = MODULE.gitdir_file_target(peer)
        self.assertIsNotNone(original_admin)
        self.assertIsNotNone(peer_admin)
        plan = MODULE.build_sync_plan(
            root=target_super,
            common_git_dir=self.named_common_git_dir,
            source_superproject=None,
            planned_modules=[
                (
                    MODULE.Submodule(
                        "managed-final-control",
                        "lib",
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
        registry_before = run_git(
            self.root,
            f"--git-dir={source}",
            "worktree",
            "list",
            "--porcelain",
        )
        original_record = MODULE.record_materialized_shared_ancestors
        replaced = False

        def replace_during_publication(*args: object, **kwargs: object) -> None:
            nonlocal replaced
            original_record(*args, **kwargs)
            replacement = target / ".git.control-replacement"
            replacement.write_text(
                f"gitdir: {peer_admin}\n",
                encoding="utf-8",
            )
            os.replace(replacement, target / ".git")
            replaced = True

        with mock.patch.object(
            MODULE,
            "record_materialized_shared_ancestors",
            side_effect=replace_during_publication,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "control file",
                ):
                    MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={original_admin}",
                "rev-parse",
                "HEAD",
            ),
            self.sha,
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={peer_admin}",
                "rev-parse",
                "HEAD",
            ),
            self.sha,
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )
        self.assertEqual(plan.applied_target_roots, {})

    def test_recursive_parent_control_retarget_restores_all_receipts(
        self,
    ) -> None:
        plan, target_super, parent_source, _children = self.make_grouped_recursive_plan(
            "recursive-final-control",
            ("group/left", "group/right"),
        )
        parent_entry = plan.entries[0]
        peer = self.root / "recursive-final-control-peer"
        self.add_managed_worktree(
            parent_source,
            peer,
            parent_entry.sha,
        )
        peer_admin = MODULE.gitdir_file_target(peer)
        self.assertIsNotNone(peer_admin)
        registry_before = run_git(
            self.root,
            f"--git-dir={parent_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        shared_before = {
            parts: ancestor.materialized_node
            for parts, ancestor in plan.shared_missing_ancestors.items()
        }
        roots_before = dict(plan.applied_target_roots)
        original_record = MODULE.record_applied_target_root
        parent_target = parent_entry.target.path
        replaced = False

        def replace_after_root_receipt(
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            original_record(*args, **kwargs)
            replacement = parent_target / ".git.control-replacement"
            replacement.write_text(
                f"gitdir: {peer_admin}\n",
                encoding="utf-8",
            )
            os.replace(replacement, parent_target / ".git")
            replaced = True

        with mock.patch.object(
            MODULE,
            "record_applied_target_root",
            side_effect=replace_after_root_receipt,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "control file",
                ) as raised:
                    MODULE.apply_sync_plan(plan)

        self.assertTrue(replaced)
        self.assertNotIn(
            "worktree/materialization rollback failed",
            str(raised.exception),
        )
        self.assertFalse(parent_target.exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={parent_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            registry_before,
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={peer_admin}",
                "rev-parse",
                "HEAD",
            ),
            parent_entry.sha,
        )
        self.assertEqual(plan.applied_target_roots, roots_before)
        self.assertEqual(
            {
                parts: ancestor.materialized_node
                for parts, ancestor in plan.shared_missing_ancestors.items()
            },
            shared_before,
        )
        self.assertEqual(list(target_super.iterdir()), [])

    def test_materialization_open_failure_cleans_created_parent_chain(self) -> None:
        target_root = self.root / "materialize-open-cleanup"
        target_root.mkdir()
        target = MODULE.bind_target_path(
            target_root,
            ("one", "two", "three"),
            "materialization cleanup test",
        )
        original_open = MODULE.os.open
        failed = False

        def fail_middle_open(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal failed
            if not failed and path == "two" and dir_fd is not None:
                failed = True
                raise OSError(errno.EIO, "injected directory open failure")
            return original_open(
                path,
                flags,
                mode,
                dir_fd=dir_fd,
            )

        with mock.patch.object(
            MODULE.os,
            "open",
            side_effect=fail_middle_open,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "cannot open descriptor-relative target directory",
            ) as raised:
                MODULE.materialize_bound_target_directory(target)

        self.assertTrue(failed)
        self.assertNotIn("recovery_schema", str(raised.exception))
        self.assertFalse((target_root / "one").exists())

    def test_materialization_final_validation_failure_cleans_all_levels(
        self,
    ) -> None:
        target_root = self.root / "materialize-final-cleanup"
        target_root.mkdir()
        target = MODULE.bind_target_path(
            target_root,
            ("one", "two", "three"),
            "materialization cleanup test",
        )

        with mock.patch.object(
            MODULE,
            "revalidate_materialized_target_lease",
            side_effect=MODULE.PlanError("injected final lease failure"),
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "injected final lease failure",
            ) as raised:
                MODULE.materialize_bound_target_directory(target)

        self.assertNotIn("recovery_schema", str(raised.exception))
        self.assertFalse((target_root / "one").exists())

    def test_materialization_cleanup_preserves_replaced_boundary_with_recovery(
        self,
    ) -> None:
        for boundary in ("one", "two", "three"):
            with self.subTest(boundary=boundary):
                target_root = self.root / f"materialize-replaced-{boundary}"
                target_root.mkdir()
                target = MODULE.bind_target_path(
                    target_root,
                    ("one", "two", "three"),
                    "materialization replacement test",
                )
                quarantine = self.root / f"materialize-original-{boundary}"
                replaced = False

                def replace_boundary(_lease: object) -> None:
                    nonlocal replaced
                    replaced = True
                    boundary_path = target_root.joinpath(
                        *("one", "two", "three")[
                            : ("one", "two", "three").index(boundary) + 1
                        ]
                    )
                    boundary_path.rename(quarantine)
                    boundary_path.mkdir()
                    raise MODULE.PlanError("injected final lease replacement failure")

                with mock.patch.object(
                    MODULE,
                    "revalidate_materialized_target_lease",
                    side_effect=replace_boundary,
                ):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "recovery_schema: target-materialization-cleanup-v1",
                    ) as raised:
                        MODULE.materialize_bound_target_directory(target)

                boundary_path = target_root.joinpath(
                    *("one", "two", "three")[
                        : ("one", "two", "three").index(boundary) + 1
                    ]
                )
                self.assertTrue(replaced)
                self.assertTrue(boundary_path.is_dir())
                self.assertTrue(quarantine.is_dir())
                self.assertIn(
                    f"recovery_location: {boundary_path.resolve()}",
                    str(raised.exception),
                )

    def test_materialization_cleanup_preserves_access_policy_drift(
        self,
    ) -> None:
        target_root = self.root / "materialize-access-drift"
        target_root.mkdir()
        target = MODULE.bind_target_path(
            target_root,
            ("one", "two", "three"),
            "materialization access-policy test",
        )
        drifted_path = target_root / "one/two"

        def change_mode_then_fail(_lease: object) -> None:
            drifted_path.chmod(0o555)
            raise MODULE.PlanError("injected final access-policy failure")

        with mock.patch.object(
            MODULE,
            "revalidate_materialized_target_lease",
            side_effect=change_mode_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "recovery_schema: target-materialization-cleanup-v1",
            ) as raised:
                MODULE.materialize_bound_target_directory(target)

        self.assertTrue(drifted_path.is_dir())
        self.assertEqual(
            stat.S_IMODE(drifted_path.stat().st_mode),
            0o555,
        )
        self.assertIn(
            f"recovery_location: {drifted_path.resolve()}",
            str(raised.exception),
        )

    def test_recursive_apply_two_level_chain_uses_each_parent_receipt(
        self,
    ) -> None:
        grandchild_remote, grandchild_sha = self.create_gitlink_remote(
            "chain-grandchild"
        )
        child_remote, child_sha = self.create_gitlink_remote(
            "chain-child",
            (
                (
                    "grandchild",
                    "grandchild",
                    grandchild_remote,
                    grandchild_sha,
                ),
            ),
        )
        parent_remote, parent_sha = self.create_gitlink_remote(
            "chain-parent",
            (("child", "child", child_remote, child_sha),),
        )

        source_common = self.root / "chain-recursive-source" / ".git"
        parent_source = source_common / "modules" / "parent"
        child_source = parent_source / "modules" / "child"
        self.clone_recursive_source(
            parent_source,
            parent_remote,
            "chain-parent-standard",
        )
        self.clone_recursive_source(
            child_source,
            child_remote,
            "chain-child-standard",
        )
        self.clone_recursive_source(
            child_source / "modules" / "grandchild",
            grandchild_remote,
            "chain-grandchild-standard",
        )
        target_super = self.root / "chain-recursive-target"
        target_super.mkdir()
        plan = MODULE.build_sync_plan(
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
            fetch_missing=False,
        )

        self.assertEqual(
            [entry.parent_index for entry in plan.entries],
            [None, 0, 1],
        )
        final_target_roots = {entry.target.relative_parts for entry in plan.entries}
        self.assertTrue(final_target_roots.isdisjoint(plan.shared_missing_ancestors))
        with redirect_stdout(io.StringIO()):
            MODULE.apply_sync_plan(plan)

        expected_targets = (
            (target_super / "parent", parent_sha),
            (target_super / "parent" / "child", child_sha),
            (
                target_super / "parent" / "child" / "grandchild",
                grandchild_sha,
            ),
        )
        for target, expected_sha in expected_targets:
            with self.subTest(target=target):
                self.assertEqual(run_git(target, "rev-parse", "HEAD"), expected_sha)
        self.assertEqual(set(plan.applied_target_roots), {0, 1})
        for owner_index, relative_parts in (
            (0, ("parent",)),
            (1, ("parent", "child")),
        ):
            receipt = MODULE.revalidate_applied_target_root(plan, owner_index)
            target = plan.entries[owner_index].target.path
            self.assertEqual(receipt.relative_parts, relative_parts)
            self.assertEqual(receipt.node.path, target)
            self.assertEqual(
                receipt.node.fingerprint,
                MODULE.filesystem_fingerprint(target),
            )

    def test_recursive_apply_binds_parent_root_for_descendants(self) -> None:
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

        race_target = self.root / "recursive-parent-replacement-target"
        race_target.mkdir()
        race_plan = MODULE.build_sync_plan(
            root=race_target,
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
            fetch_missing=False,
        )
        self.assertEqual(race_plan.entries[1].parent_index, 0)
        child_registry_before = run_git(
            self.root,
            f"--git-dir={child_source}",
            "worktree",
            "list",
            "--porcelain",
        )
        quarantined_parent = self.root / "recursive-parent-quarantined"
        original_record = MODULE.record_applied_target_root
        replaced = False

        def replace_parent_after_receipt(
            plan: object,
            owner_index: int,
            entry: object,
            lease: object,
        ) -> None:
            nonlocal replaced
            original_record(plan, owner_index, entry, lease)
            if owner_index == 0:
                replaced = True
                parent_target = race_target / "parent"
                parent_target.rename(quarantined_parent)
                parent_target.mkdir()
                (parent_target / "sentinel").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )

        with mock.patch.object(
            MODULE,
            "record_applied_target_root",
            side_effect=replace_parent_after_receipt,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "descriptor-bound target worktree",
                ) as raised:
                    MODULE.apply_sync_plan(race_plan)

        self.assertTrue(replaced)
        self.assertIn(
            "worktree/materialization rollback failed",
            str(raised.exception),
        )
        self.assertEqual(
            (race_target / "parent" / "sentinel").read_text(encoding="utf-8"),
            "replacement\n",
        )
        self.assertFalse((race_target / "parent" / "nested").exists())
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={child_source}",
                "worktree",
                "list",
                "--porcelain",
            ),
            child_registry_before,
        )
        self.assertEqual(race_plan.applied_target_roots, {})

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

    def test_commit_fast_path_rejects_stale_shallow_or_fetch_fence(self) -> None:
        for name in (
            MODULE.SOURCE_SHALLOW_LOCK_NAME,
            MODULE.SOURCE_FETCH_TRANSACTION_NAME,
        ):
            with self.subTest(name=name):
                fence = self.named_source_git_dir / name
                fence.write_text("stale\n", encoding="utf-8")
                with mock.patch.object(MODULE, "read_git") as read_git:
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "objects are unavailable.*recovered",
                    ):
                        MODULE.commit_exists(
                            self.named_source_git_dir,
                            self.root / "unused-target",
                            self.sha,
                        )
                read_git.assert_not_called()
                fence.unlink()

    def test_failed_fetch_retains_persistent_fence_and_blocks_known_commit(
        self,
    ) -> None:
        (self.remote / "FETCH-FAIL.md").write_text(
            "fetch failure\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-FAIL.md")
        run_git(self.remote, "commit", "-m", "fetch failure")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        original_run_bounded = MODULE.run_bounded_bytes

        def fail_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            if "fetch" in args:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout=b"",
                    stderr=b"simulated fetch failure",
                )
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=fail_fetch,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "recovery fence was retained",
            ):
                MODULE.fetch_missing_commit(
                    self.named_source_git_dir,
                    self.root / "unused-target",
                    submodule,
                    missing_sha,
                    1,
                    dry_run=False,
                    transport_receipt=receipt,
                    fetch_missing=True,
                )

        fence = self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME
        self.assertTrue(fence.is_file())
        with self.assertRaisesRegex(MODULE.PlanError, "objects are unavailable"):
            MODULE.commit_exists(
                self.named_source_git_dir,
                self.root / "unused-target",
                self.sha,
            )

    def test_fetch_signal_retains_fence_before_propagating_signal(
        self,
    ) -> None:
        (self.remote / "FETCH-SIGNAL.md").write_text(
            "fetch signal\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-SIGNAL.md")
        run_git(self.remote, "commit", "-m", "fetch signal")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.addCleanup(receipt.fetch_guard.cleanup)
        original_run_bounded = MODULE.run_bounded_bytes

        def interrupt_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            if "fetch" in args:
                raise MODULE.ForwardedProcessSignal(signal.SIGTERM)
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=interrupt_fetch,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(MODULE.ForwardedProcessSignal) as raised:
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "unused-target",
                        submodule,
                        missing_sha,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )
        self.assertTrue(
            any(
                "source-fetch-transaction-v1" in detail
                for detail in raised.exception.recovery_details
            )
        )

    def test_fetch_transport_rejects_loose_object_fanout_symlink(self) -> None:
        source_objects = self.named_source_git_dir / "objects"
        fanout_name = next(
            name
            for name in MODULE.LOOSE_OBJECT_FANOUT_NAMES
            if not (source_objects / name).exists()
        )
        outside = self.root / "outside-loose-object-fanout"
        outside.mkdir()
        (source_objects / fanout_name).symlink_to(
            outside,
            target_is_directory=True,
        )
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )

        with self.assertRaisesRegex(
            MODULE.PlanError,
            "loose-object fanout.*unsafe object type",
        ):
            MODULE.capture_transport_receipt(
                self.named_source_git_dir,
                submodule,
            )

    def test_fetch_rejects_absent_private_shallow_injection_before_git_starts(
        self,
    ) -> None:
        sources = {
            injection_kind: self.clone_named_source(
                f"fetch-control-absent-shallow-{injection_kind}"
            )
            for injection_kind in ("regular", "symlink")
        }
        (self.remote / "FETCH-CONTROL-ABSENT-SHALLOW.md").write_text(
            "fetch control absent shallow\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-CONTROL-ABSENT-SHALLOW.md")
        run_git(self.remote, "commit", "-m", "fetch control absent shallow")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")

        for injection_kind, source_git_dir in sources.items():
            with self.subTest(injection_kind=injection_kind):
                submodule = MODULE.Submodule(
                    f"fetch-control-absent-shallow-{injection_kind}",
                    f"third_party/fetch-control-absent-shallow-{injection_kind}",
                    str(self.remote),
                )
                receipt = MODULE.capture_transport_receipt(
                    source_git_dir,
                    submodule,
                )
                self.addCleanup(receipt.fetch_guard.cleanup)
                injected_path = receipt.fetch_git_dir / MODULE.SOURCE_SHALLOW_NAME
                symlink_target = self.root / f"{injection_kind}-shallow-target"
                symlink_target.write_text("outside\n", encoding="utf-8")
                original_run_bounded = MODULE.run_bounded_bytes
                injection_performed = False

                def inject_absent_shallow_before_fetch(
                    args: list[str],
                    **kwargs: object,
                ) -> subprocess.CompletedProcess[bytes]:
                    nonlocal injection_performed
                    if "fetch" in args:
                        self.assertFalse(injection_performed)
                        injection_performed = True
                        absent_leases = kwargs["directory_absent_entry_leases"]
                        self.assertEqual(len(absent_leases), 1)
                        self.assertEqual(
                            absent_leases[0].entry_names,
                            (
                                MODULE.SOURCE_SHALLOW_NAME,
                                *MODULE.FETCH_CONTROL_LOCK_NAMES,
                            ),
                        )
                        if injection_kind == "regular":
                            injected_path.write_text(
                                "injected\n",
                                encoding="utf-8",
                            )
                        else:
                            injected_path.symlink_to(symlink_target)
                    return original_run_bounded(args, **kwargs)

                try:
                    with mock.patch.object(
                        MODULE,
                        "run_bounded_bytes",
                        side_effect=inject_absent_shallow_before_fetch,
                    ):
                        with redirect_stdout(io.StringIO()):
                            with self.assertRaisesRegex(
                                MODULE.PlanError,
                                "recovery fence was retained",
                            ):
                                MODULE.fetch_missing_commit(
                                    source_git_dir,
                                    self.root / "unused-target",
                                    submodule,
                                    missing_sha,
                                    1,
                                    dry_run=False,
                                    transport_receipt=receipt,
                                    fetch_missing=True,
                                )
                    self.assertTrue(injection_performed)
                    self.assertTrue(
                        (
                            source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME
                        ).is_file()
                    )
                finally:
                    injected_path.unlink(missing_ok=True)

    def test_fetch_rejects_raced_private_gitdir_replacement_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-CONTROL-GITDIR-REPLACEMENT.md").write_text(
            "fetch control gitdir replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-CONTROL-GITDIR-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch control gitdir replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.addCleanup(receipt.fetch_guard.cleanup)
        held_gitdir = receipt.fetch_git_dir.with_name(
            f"{receipt.fetch_git_dir.name}.held"
        )
        self.assertFalse(held_gitdir.exists())
        replacement_content = b"replacement control gitdir\n"
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def replace_control_gitdir_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                directory_leases = kwargs["directory_identity_leases"]
                self.assertTrue(
                    any(
                        lease.path == receipt.fetch_git_dir
                        for lease in directory_leases
                    )
                )
                receipt.fetch_git_dir.rename(held_gitdir)
                receipt.fetch_git_dir.mkdir(mode=0o700)
                (receipt.fetch_git_dir / "sentinel").write_bytes(replacement_content)
            return original_run_bounded(args, **kwargs)

        try:
            with mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                side_effect=replace_control_gitdir_before_fetch,
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "recovery fence was retained",
                    ) as error:
                        MODULE.fetch_missing_commit(
                            self.named_source_git_dir,
                            self.root / "unused-target",
                            submodule,
                            missing_sha,
                            1,
                            dry_run=False,
                            transport_receipt=receipt,
                            fetch_missing=True,
                        )

            self.assertTrue(replacement_performed)
            self.assertEqual(
                list(receipt.fetch_git_dir.iterdir()),
                [receipt.fetch_git_dir / "sentinel"],
            )
            self.assertEqual(
                (receipt.fetch_git_dir / "sentinel").read_bytes(),
                replacement_content,
            )
            self.assertIn(
                '"profile":"source-fetch-transaction-v1"',
                str(error.exception),
            )
            self.assertTrue(
                (
                    self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME
                ).is_file()
            )
        finally:
            if receipt.fetch_git_dir.exists():
                shutil.rmtree(receipt.fetch_git_dir)
            if held_gitdir.exists():
                held_gitdir.rename(receipt.fetch_git_dir)

    def test_fetch_rejects_raced_private_config_replacement_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-CONTROL-CONFIG-REPLACEMENT.md").write_text(
            "fetch control config replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-CONTROL-CONFIG-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch control config replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.addCleanup(receipt.fetch_guard.cleanup)
        config_path = receipt.fetch_git_dir / "config"
        held_config = receipt.fetch_git_dir / "config.receipt-held"
        malicious_content = (
            b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            b"[transfer]\n\tfsckObjects = false\n"
            b'[url "file:///attacker.invalid/"]\n\tinsteadOf = file:///\n'
        )
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def replace_control_config_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                file_leases = kwargs["file_content_leases"]
                self.assertEqual(
                    tuple(lease.entry_name for lease in file_leases),
                    MODULE.FETCH_CONTROL_FILE_NAMES,
                )
                config_path.rename(held_config)
                config_path.write_bytes(malicious_content)
                config_path.chmod(0o600)
            return original_run_bounded(args, **kwargs)

        try:
            with mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                side_effect=replace_control_config_before_fetch,
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "recovery fence was retained",
                    ) as error:
                        MODULE.fetch_missing_commit(
                            self.named_source_git_dir,
                            self.root / "unused-target",
                            submodule,
                            missing_sha,
                            1,
                            dry_run=False,
                            transport_receipt=receipt,
                            fetch_missing=True,
                        )

            self.assertTrue(replacement_performed)
            self.assertEqual(config_path.read_bytes(), malicious_content)
            self.assertIn(
                '"profile":"source-fetch-transaction-v1"',
                str(error.exception),
            )
            self.assertTrue(
                (
                    self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME
                ).is_file()
            )
        finally:
            config_path.unlink(missing_ok=True)
            if held_config.exists():
                held_config.rename(config_path)

    def test_fetch_rejects_raced_private_config_content_rewrite_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-CONTROL-CONFIG-CONTENT-RACE.md").write_text(
            "fetch control config content race\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-CONTROL-CONFIG-CONTENT-RACE.md")
        run_git(self.remote, "commit", "-m", "fetch control config content race")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.addCleanup(receipt.fetch_guard.cleanup)
        config_path = receipt.fetch_git_dir / "config"
        original_content = config_path.read_bytes()
        rewritten_content = original_content.replace(b"bare = true", b"bare = fals", 1)
        self.assertEqual(len(rewritten_content), len(original_content))
        self.assertNotEqual(rewritten_content, original_content)
        original_inode = config_path.stat().st_ino
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def rewrite_control_config_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                config_path.write_bytes(rewritten_content)
                self.assertEqual(config_path.stat().st_ino, original_inode)
            return original_run_bounded(args, **kwargs)

        try:
            with mock.patch.object(
                MODULE,
                "run_bounded_bytes",
                side_effect=rewrite_control_config_before_fetch,
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "recovery fence was retained",
                    ):
                        MODULE.fetch_missing_commit(
                            self.named_source_git_dir,
                            self.root / "unused-target",
                            submodule,
                            missing_sha,
                            1,
                            dry_run=False,
                            transport_receipt=receipt,
                            fetch_missing=True,
                        )

            self.assertTrue(replacement_performed)
            self.assertEqual(config_path.read_bytes(), rewritten_content)
            self.assertTrue(
                (
                    self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME
                ).is_file()
            )
        finally:
            config_path.write_bytes(original_content)

    def test_fetch_rejects_raced_loose_object_fanout_symlink_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-FANOUT-REPLACEMENT.md").write_text(
            "fetch fanout replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-FANOUT-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch fanout replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        source_objects = self.named_source_git_dir / "objects"
        fanout_name = next(
            name
            for name in MODULE.LOOSE_OBJECT_FANOUT_NAMES
            if not (source_objects / name).exists()
        )
        source_fanout = source_objects / fanout_name
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.assertEqual(len(receipt.source_object_write_children), 257)
        self.assertEqual(
            tuple(name for name, _ in receipt.source_object_write_children),
            MODULE.OBJECT_WRITE_CHILD_NAMES,
        )
        self.assertEqual(
            dict(receipt.source_object_write_children)[fanout_name],
            None,
        )
        outside = self.root / "outside-raced-loose-object-fanout"
        outside.mkdir()
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def replace_fanout_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                source_fanout.symlink_to(outside, target_is_directory=True)
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=replace_fanout_before_fetch,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "recovery fence was retained",
                ):
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "unused-target",
                        submodule,
                        missing_sha,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertTrue(replacement_performed)
        self.assertTrue(source_fanout.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_fetch_rejects_raced_pack_directory_replacement_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-PACK-REPLACEMENT.md").write_text(
            "fetch pack replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-PACK-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch pack replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        source_objects = self.named_source_git_dir / "objects"
        source_pack = source_objects / "pack"
        source_pack.mkdir(exist_ok=True)
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.assertEqual(
            dict(receipt.source_object_write_children)["pack"],
            MODULE.filesystem_fingerprint(source_pack),
        )
        outside = self.root / "outside-raced-pack-directory"
        outside.mkdir()
        held_pack = self.root / "held-source-pack-directory"
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def replace_pack_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                source_pack.rename(held_pack)
                source_pack.symlink_to(outside, target_is_directory=True)
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=replace_pack_before_fetch,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "recovery fence was retained",
                ):
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "unused-target",
                        submodule,
                        missing_sha,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertTrue(replacement_performed)
        self.assertTrue(source_pack.is_symlink())
        self.assertTrue(held_pack.is_dir())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_fetch_rejects_raced_absent_pack_symlink_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-ABSENT-PACK-REPLACEMENT.md").write_text(
            "fetch absent pack replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-ABSENT-PACK-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch absent pack replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        source_objects = self.named_source_git_dir / "objects"
        source_pack = source_objects / "pack"
        held_pack = self.root / "preflight-absent-source-pack"
        source_pack.rename(held_pack)
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        self.assertIsNone(
            dict(receipt.source_object_write_children)["pack"],
        )
        outside = self.root / "outside-raced-absent-pack"
        outside.mkdir()
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def publish_pack_symlink_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                source_pack.symlink_to(outside, target_is_directory=True)
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=publish_pack_symlink_before_fetch,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "recovery fence was retained",
                ):
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "unused-target",
                        submodule,
                        missing_sha,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertTrue(replacement_performed)
        self.assertTrue(source_pack.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_fetch_rejects_source_object_directory_replacement_before_git_starts(
        self,
    ) -> None:
        (self.remote / "FETCH-OBJECT-REPLACEMENT.md").write_text(
            "fetch object replacement\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-OBJECT-REPLACEMENT.md")
        run_git(self.remote, "commit", "-m", "fetch object replacement")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        source_objects = receipt.source_object_directory
        outside_objects = self.root / "outside-objects"
        held_source_objects = self.root / "held-source-objects"
        shutil.copytree(source_objects, outside_objects)

        def directory_snapshot(root: Path) -> list[tuple[str, str, bytes]]:
            snapshot: list[tuple[str, str, bytes]] = []
            for path in sorted(root.rglob("*"), key=lambda item: os.fsencode(item)):
                relative = str(path.relative_to(root))
                if path.is_symlink():
                    snapshot.append(
                        (relative, "symlink", os.fsencode(os.readlink(path)))
                    )
                elif path.is_file():
                    snapshot.append((relative, "file", path.read_bytes()))
                else:
                    snapshot.append((relative, "directory", b""))
            return snapshot

        outside_before = directory_snapshot(outside_objects)
        original_run_bounded = MODULE.run_bounded_bytes
        replacement_performed = False

        def replace_objects_before_fetch(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal replacement_performed
            if "fetch" in args:
                self.assertFalse(replacement_performed)
                replacement_performed = True
                os.rename(source_objects, held_source_objects)
                source_objects.symlink_to(outside_objects, target_is_directory=True)
            return original_run_bounded(args, **kwargs)

        with mock.patch.object(
            MODULE,
            "run_bounded_bytes",
            side_effect=replace_objects_before_fetch,
        ):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    MODULE.PlanError,
                    "recovery fence was retained",
                ):
                    MODULE.fetch_missing_commit(
                        self.named_source_git_dir,
                        self.root / "unused-target",
                        submodule,
                        missing_sha,
                        1,
                        dry_run=False,
                        transport_receipt=receipt,
                        fetch_missing=True,
                    )

        self.assertTrue(replacement_performed)
        self.assertTrue(source_objects.is_symlink())
        self.assertTrue(held_source_objects.is_dir())
        self.assertEqual(directory_snapshot(outside_objects), outside_before)
        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_successful_fetch_retains_fence_until_full_closure_is_verified(
        self,
    ) -> None:
        (self.remote / "FETCH-CLOSURE.md").write_text(
            "fetch closure\n",
            encoding="utf-8",
        )
        run_git(self.remote, "add", "FETCH-CLOSURE.md")
        run_git(self.remote, "commit", "-m", "fetch closure")
        missing_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )

        with mock.patch.object(
            MODULE,
            "target_object_closure",
            side_effect=MODULE.PlanError("simulated corrupt fetched blob"),
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "recovery fence was retained",
            ):
                MODULE.fetch_missing_commit(
                    self.named_source_git_dir,
                    self.root / "unused-target",
                    submodule,
                    missing_sha,
                    1,
                    dry_run=False,
                    transport_receipt=receipt,
                    fetch_missing=True,
                )

        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_cleanup_fsync_failure_restores_recovery_fence(self) -> None:
        submodule = MODULE.Submodule(
            name="custom-lib",
            path="third_party/libexample",
            url=str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
        )
        transaction = MODULE.begin_source_fetch_transaction(receipt)
        real_fsync = MODULE.os.fsync
        failed = False

        def fail_first_directory_fsync(descriptor: int) -> None:
            nonlocal failed
            if descriptor == transaction.directory_descriptor and not failed:
                failed = True
                raise OSError("simulated directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(
            MODULE.os,
            "fsync",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "cleanup durability is unverified",
            ):
                MODULE.complete_source_fetch_transaction(transaction)

        self.assertTrue(failed)
        self.assertTrue(
            (self.named_source_git_dir / MODULE.SOURCE_FETCH_TRANSACTION_NAME).is_file()
        )

    def test_owned_descriptor_cleanup_attempts_both_after_first_error(self) -> None:
        binding = mock.MagicMock()
        lease = MODULE.MaterializedTargetLease(
            target=self.root / "target",
            target_binding=binding,
            target_descriptor=101,
            parent_binding=binding,
            parent_descriptor=102,
            entry_name="target",
        )
        with mock.patch.object(
            MODULE.os,
            "close",
            side_effect=[OSError("first close failed"), None],
        ) as close:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "target lease descriptor cleanup failed",
            ):
                lease.close()
        self.assertEqual(
            close.call_args_list,
            [mock.call(101), mock.call(102)],
        )
        self.assertEqual(lease.target_descriptor, -1)
        self.assertEqual(lease.parent_descriptor, -1)

        transaction = MODULE.SourceFetchTransaction(
            source_git_dir=self.named_source_git_dir,
            directory_binding=binding,
            directory_descriptor=201,
            fence_binding=binding,
            fence_descriptor=202,
            transaction_id="test-transaction",
        )
        with mock.patch.object(
            MODULE.os,
            "close",
            side_effect=[OSError("first close failed"), None],
        ) as close:
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "source fetch descriptor cleanup failed",
            ):
                transaction.close_descriptors()
        self.assertEqual(
            close.call_args_list,
            [mock.call(202), mock.call(201)],
        )
        self.assertEqual(transaction.fence_descriptor, -1)
        self.assertEqual(transaction.directory_descriptor, -1)

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
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            submodule,
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
                    transport_receipt=receipt,
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

    def test_authorized_fetch_updates_source_shallow_boundary_before_control_cleanup(
        self,
    ) -> None:
        (self.remote / "SECOND.md").write_text("second\n", encoding="utf-8")
        run_git(self.remote, "add", "SECOND.md")
        run_git(self.remote, "commit", "-m", "second")
        second_sha = run_git(self.remote, "rev-parse", "HEAD")

        shallow_source = self.named_common_git_dir / "modules" / "shallow-custom-lib"
        shallow_source.parent.mkdir(parents=True, exist_ok=True)
        remote_url = self.remote.as_uri()
        run_git(
            self.root,
            "clone",
            "--depth",
            "1",
            "--separate-git-dir",
            str(shallow_source),
            remote_url,
            str(self.root / "shallow-standard"),
        )
        self.assertEqual(
            run_git(
                self.root,
                f"--git-dir={shallow_source}",
                "rev-parse",
                "--is-shallow-repository",
            ),
            "true",
        )
        self.assertIn(second_sha, (shallow_source / "shallow").read_text())

        (self.remote / "THIRD.md").write_text("third\n", encoding="utf-8")
        run_git(self.remote, "add", "THIRD.md")
        run_git(self.remote, "commit", "-m", "third")
        third_sha = run_git(self.remote, "rev-parse", "HEAD")
        submodule = MODULE.Submodule(
            name="shallow-custom-lib",
            path="third_party/shallow-libexample",
            url=remote_url,
        )
        receipt = MODULE.capture_transport_receipt(
            shallow_source,
            submodule,
        )
        self.assertIsNone(receipt.ssh_command)

        with redirect_stdout(io.StringIO()):
            self.assertTrue(
                MODULE.fetch_missing_commit(
                    shallow_source,
                    self.root / "shallow-fetched-worktree",
                    submodule,
                    third_sha,
                    1,
                    dry_run=False,
                    transport_receipt=receipt,
                    fetch_missing=True,
                )
            )
        self.assertIn(third_sha, (shallow_source / "shallow").read_text())

        receipt.fetch_guard.cleanup()
        self.assertFalse(receipt.fetch_git_dir.exists())
        target = MODULE.bind_target_path(
            self.root,
            ("shallow-fetched-worktree",),
            "test shallow fetched worktree",
        )
        lease = MODULE.materialize_bound_target_directory(target)
        source_lease = MODULE.capture_directory_entry_lease(
            shallow_source,
            os.R_OK | os.W_OK | os.X_OK,
            "selected source common gitdir",
        )
        try:
            MODULE.add_worktree(
                shallow_source,
                target.path,
                third_sha,
                dry_run=False,
                lease=lease,
                source_lease=source_lease,
            )
        finally:
            try:
                source_lease.close()
            finally:
                lease.close()
        run_git(
            self.root,
            f"--git-dir={shallow_source}",
            "fsck",
            "--full",
        )

    def test_root_commit_fetch_keeps_complete_source_without_shallow_boundary(
        self,
    ) -> None:
        source = self.named_common_git_dir / "modules" / "root-fetch"
        source.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.root, "init", "--bare", str(source))
        run_git(
            self.root,
            f"--git-dir={source}",
            "config",
            "remote.origin.url",
            str(self.remote),
        )
        submodule = MODULE.Submodule(
            "root-fetch",
            "third_party/root-fetch",
            str(self.remote),
        )
        receipt = MODULE.capture_transport_receipt(source, submodule)
        self.addCleanup(receipt.fetch_guard.cleanup)

        with redirect_stdout(io.StringIO()):
            self.assertTrue(
                MODULE.fetch_missing_commit(
                    source,
                    self.root / "root-fetch-target",
                    submodule,
                    self.sha,
                    2,
                    dry_run=False,
                    transport_receipt=receipt,
                    fetch_missing=True,
                )
            )

        self.assertFalse((source / MODULE.SOURCE_SHALLOW_NAME).exists())
        self.assertFalse((source / MODULE.SOURCE_SHALLOW_LOCK_NAME).exists())
        run_git(
            self.root,
            f"--git-dir={source}",
            "fsck",
            "--full",
        )

    def test_shallow_install_absent_to_absent_is_a_true_noop(self) -> None:
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=None,
        )

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_noreplace",
            side_effect=AssertionError("no publication expected"),
        ):
            with mock.patch.object(
                MODULE,
                "descriptor_atomic_rename_exchange",
                side_effect=AssertionError("no exchange expected"),
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
        )

    def test_shallow_install_absent_to_present_uses_group_policy(self) -> None:
        fetched = b"g" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
            shared_repository="group",
        )
        creation = receipt.source_shallow_creation_policy
        self.assertIsNotNone(creation)

        MODULE.install_post_fetch_shallow_state(receipt)

        observed = receipt.source_shallow_path.stat()
        self.assertEqual(stat.S_IMODE(observed.st_mode), creation.permissions)
        self.assertEqual(observed.st_uid, creation.owner)
        self.assertEqual(observed.st_gid, creation.group)
        self.assertEqual(receipt.source_shallow_path.read_bytes(), fetched)

    def test_shallow_install_absent_to_present_uses_numeric_policy(self) -> None:
        fetched = b"n" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
            shared_repository="0640",
        )
        creation = receipt.source_shallow_creation_policy
        self.assertIsNotNone(creation)

        MODULE.install_post_fetch_shallow_state(receipt)

        observed = receipt.source_shallow_path.stat()
        self.assertEqual(stat.S_IMODE(observed.st_mode), 0o640)
        self.assertEqual(observed.st_uid, creation.owner)
        self.assertEqual(observed.st_gid, creation.group)

    def test_shallow_install_preserves_existing_owner_group_and_mode(self) -> None:
        initial = b"o" * 40 + b"\n"
        fetched = b"p" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
            shared_repository="group",
        )
        receipt.source_shallow_path.chmod(0o660)
        receipt = MODULE.capture_transport_receipt(
            self.named_source_git_dir,
            MODULE.Submodule(
                name="custom-lib",
                path="third_party/libexample",
                url=str(self.remote),
            ),
        )
        self.addCleanup(receipt.fetch_guard.cleanup)
        (receipt.fetch_git_dir / MODULE.SOURCE_SHALLOW_NAME).write_bytes(fetched)
        expected = receipt.source_shallow_binding
        self.assertIsNotNone(expected)
        assert expected is not None

        MODULE.install_post_fetch_shallow_state(receipt)

        observed = MODULE.filesystem_fingerprint(receipt.source_shallow_path)
        self.assertEqual(observed.owner, expected.fingerprint.owner)
        self.assertEqual(observed.group, expected.fingerprint.group)
        self.assertEqual(
            observed.permissions,
            expected.fingerprint.permissions,
        )
        self.assertEqual(receipt.source_shallow_path.read_bytes(), fetched)

    def test_shallow_install_rejects_replacement_lock_policy_drift(self) -> None:
        for drift in ("owner", "group", "mode"):
            with self.subTest(drift=drift):
                initial = b"q" * 40 + b"\n"
                fetched = b"r" * 40 + b"\n"
                receipt = self.make_shallow_install_receipt(
                    initial_content=initial,
                    fetched_content=fetched,
                    shared_repository="group",
                )
                original_fingerprint = MODULE.fingerprint_from_stat
                lock_inode: int | None = None
                known_regular_inodes = {
                    receipt.source_shallow_path.stat().st_ino,
                    (receipt.fetch_git_dir / MODULE.SOURCE_SHALLOW_NAME).stat().st_ino,
                }

                def report_policy_drift(
                    observed: os.stat_result,
                ) -> MODULE.FsFingerprint:
                    nonlocal lock_inode
                    fingerprint = original_fingerprint(observed)
                    if (
                        lock_inode is None
                        and fingerprint.kind == stat.S_IFREG
                        and fingerprint.inode not in known_regular_inodes
                    ):
                        lock_inode = fingerprint.inode
                    if lock_inode is None or fingerprint.inode != lock_inode:
                        return fingerprint
                    return MODULE.FsFingerprint(
                        device=fingerprint.device,
                        inode=fingerprint.inode,
                        kind=fingerprint.kind,
                        owner=(
                            fingerprint.owner + 1
                            if drift == "owner"
                            else fingerprint.owner
                        ),
                        group=(
                            fingerprint.group + 1
                            if drift == "group"
                            else fingerprint.group
                        ),
                        permissions=(
                            fingerprint.permissions ^ stat.S_IXUSR
                            if drift == "mode"
                            else fingerprint.permissions
                        ),
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "fingerprint_from_stat",
                        side_effect=report_policy_drift,
                    ),
                    mock.patch.object(
                        MODULE,
                        "descriptor_atomic_rename_exchange",
                        side_effect=AssertionError(
                            "replacement policy drift must block exchange"
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        MODULE.PlanError,
                        "frozen boundary policy",
                    ):
                        MODULE.install_post_fetch_shallow_state(receipt)

                self.assertEqual(receipt.source_shallow_path.read_bytes(), initial)
                self.assertFalse(
                    receipt.source_shallow_path.with_name(
                        MODULE.SOURCE_SHALLOW_LOCK_NAME
                    ).exists()
                )

    def test_shallow_install_rejects_umask_drift_before_source_mutation(
        self,
    ) -> None:
        fetched = b"u" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
            shared_repository="group",
        )
        creation = receipt.source_shallow_creation_policy
        self.assertIsNotNone(creation)
        drifted_umask = creation.process_umask ^ 0o002

        with mock.patch.object(
            MODULE,
            "capture_process_umask",
            return_value=drifted_umask,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                "mode or ownership policy changed",
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
        )

    def test_shallow_install_present_to_absent_deletes_bound_boundary(self) -> None:
        initial = b"a" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=None,
        )

        MODULE.install_post_fetch_shallow_state(receipt)

        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
        )

    def test_shallow_install_final_fsync_failure_returns_recovery_receipt(
        self,
    ) -> None:
        fetched = b"f" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
        )
        original_fsync = MODULE.os.fsync
        source_directory_fsyncs = 0

        def fail_final_publish_fsync(descriptor: int) -> None:
            nonlocal source_directory_fsyncs
            observed = MODULE.fingerprint_from_stat(os.fstat(descriptor))
            if observed == receipt.source_shallow_parent_binding.fingerprint:
                source_directory_fsyncs += 1
                if source_directory_fsyncs == 2:
                    raise OSError(errno.EIO, "injected final directory fsync failure")
            original_fsync(descriptor)

        with mock.patch.object(
            MODULE.os,
            "fsync",
            side_effect=fail_final_publish_fsync,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"durability-unverified-rolled-back-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertGreaterEqual(source_directory_fsyncs, 3)
        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            fetched,
        )

    def test_shallow_install_post_publish_recheck_returns_recovery_receipt(
        self,
    ) -> None:
        fetched = b"e" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
        )
        original_bind = MODULE.bind_regular_file_descriptor_at
        injected = False

        def fail_installed_recheck(
            descriptor: int,
            directory_descriptor: int,
            name: str,
            display_path: Path,
            **kwargs: object,
        ) -> tuple[object, object]:
            nonlocal injected
            if (
                not injected
                and name == MODULE.SOURCE_SHALLOW_NAME
                and kwargs.get("purpose") == "installed source shallow boundary"
            ):
                injected = True
                raise MODULE.PlanError("injected post-publication recheck failure")
            return original_bind(
                descriptor,
                directory_descriptor,
                name,
                display_path,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "bind_regular_file_descriptor_at",
            side_effect=fail_installed_recheck,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"rolled-back-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertTrue(injected)
        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            fetched,
        )

    def test_shallow_delete_fsync_failure_retains_old_boundary_fence(self) -> None:
        initial = b"d" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=None,
        )
        original_fsync = MODULE.os.fsync
        source_directory_fsyncs = 0

        def fail_delete_fsync(descriptor: int) -> None:
            nonlocal source_directory_fsyncs
            observed = MODULE.fingerprint_from_stat(os.fstat(descriptor))
            if observed == receipt.source_shallow_parent_binding.fingerprint:
                source_directory_fsyncs += 1
                if source_directory_fsyncs == 2:
                    raise OSError(
                        errno.EIO, "injected deletion directory fsync failure"
                    )
            original_fsync(descriptor)

        with mock.patch.object(
            MODULE.os,
            "fsync",
            side_effect=fail_delete_fsync,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"delete-durability-unverified-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(source_directory_fsyncs, 2)
        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            initial,
        )

    def test_shallow_install_holds_parent_descriptor_across_path_replace_restore(
        self,
    ) -> None:
        fetched = b"f" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
        )
        source_git_dir = receipt.source_shallow_parent_binding.path
        moved_git_dir = source_git_dir.with_name(f"{source_git_dir.name}-moved")
        substitute_entries: set[str] = set()
        original_noreplace = MODULE.descriptor_atomic_rename_noreplace

        def replace_restore_then_publish(
            directory_descriptor: int,
            source_name: str,
            target_name: str,
        ) -> None:
            nonlocal substitute_entries
            os.rename(source_git_dir, moved_git_dir)
            source_git_dir.mkdir()
            (source_git_dir / "substitute-sentinel").write_text(
                "substitute\n",
                encoding="utf-8",
            )
            try:
                original_noreplace(
                    directory_descriptor,
                    source_name,
                    target_name,
                )
                substitute_entries = {entry.name for entry in source_git_dir.iterdir()}
            finally:
                shutil.rmtree(source_git_dir)
                os.rename(moved_git_dir, source_git_dir)

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_noreplace",
            side_effect=replace_restore_then_publish,
        ):
            MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(substitute_entries, {"substitute-sentinel"})
        self.assertEqual(
            (source_git_dir / MODULE.SOURCE_SHALLOW_NAME).read_bytes(),
            fetched,
        )
        self.assertFalse((source_git_dir / MODULE.SOURCE_SHALLOW_LOCK_NAME).exists())

    def test_shallow_install_existing_racer_is_swapped_back_with_fence(
        self,
    ) -> None:
        initial = b"1" * 40 + b"\n"
        fetched = b"2" * 40 + b"\n"
        racer = b"3" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
        )
        original_exchange = MODULE.descriptor_atomic_rename_exchange
        exchange_calls = 0

        def race_then_exchange(
            directory_descriptor: int,
            first_name: str,
            second_name: str,
        ) -> None:
            nonlocal exchange_calls
            exchange_calls += 1
            if exchange_calls == 1:
                self.write_descriptor_relative_file(
                    directory_descriptor,
                    "racing-shallow",
                    racer,
                )
                os.rename(
                    "racing-shallow",
                    MODULE.SOURCE_SHALLOW_NAME,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
            original_exchange(
                directory_descriptor,
                first_name,
                second_name,
            )

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_exchange",
            side_effect=race_then_exchange,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"rolled-back-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(exchange_calls, 2)
        self.assertEqual(receipt.source_shallow_path.read_bytes(), racer)
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            fetched,
        )

    def test_shallow_install_absent_racer_uses_no_replace_and_retains_fence(
        self,
    ) -> None:
        fetched = b"4" * 40 + b"\n"
        racer = b"5" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
        )
        original_noreplace = MODULE.descriptor_atomic_rename_noreplace

        def race_then_publish(
            directory_descriptor: int,
            source_name: str,
            target_name: str,
        ) -> None:
            self.write_descriptor_relative_file(
                directory_descriptor,
                MODULE.SOURCE_SHALLOW_NAME,
                racer,
            )
            original_noreplace(
                directory_descriptor,
                source_name,
                target_name,
            )

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_noreplace",
            side_effect=race_then_publish,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"cas-conflict-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(receipt.source_shallow_path.read_bytes(), racer)
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            fetched,
        )

    def test_shallow_install_rollback_failure_retains_both_recovery_objects(
        self,
    ) -> None:
        initial = b"6" * 40 + b"\n"
        fetched = b"7" * 40 + b"\n"
        racer = b"8" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
        )
        original_exchange = MODULE.descriptor_atomic_rename_exchange
        exchange_calls = 0

        def fail_rollback(
            directory_descriptor: int,
            first_name: str,
            second_name: str,
        ) -> None:
            nonlocal exchange_calls
            exchange_calls += 1
            if exchange_calls == 1:
                self.write_descriptor_relative_file(
                    directory_descriptor,
                    "racing-shallow",
                    racer,
                )
                os.rename(
                    "racing-shallow",
                    MODULE.SOURCE_SHALLOW_NAME,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                original_exchange(
                    directory_descriptor,
                    first_name,
                    second_name,
                )
                return
            raise MODULE.AtomicRenameError("exchange", errno.EIO)

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_exchange",
            side_effect=fail_rollback,
        ):
            with self.assertRaisesRegex(
                MODULE.PlanError,
                '"state":"rollback-failed-fence-retained"',
            ):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(exchange_calls, 2)
        self.assertEqual(receipt.source_shallow_path.read_bytes(), fetched)
        self.assertEqual(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).read_bytes(),
            racer,
        )

    def test_shallow_install_unsupported_atomic_primitive_fails_closed(
        self,
    ) -> None:
        fetched = b"9" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=None,
            fetched_content=fetched,
        )

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_noreplace",
            side_effect=MODULE.AtomicRenameError("noreplace", errno.ENOSYS),
        ):
            with self.assertRaisesRegex(MODULE.PlanError, "unsupported"):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertFalse(receipt.source_shallow_path.exists())
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
        )

    def test_shallow_install_unsupported_exchange_preserves_original(self) -> None:
        initial = b"0" * 40 + b"\n"
        fetched = b"e" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
        )

        with mock.patch.object(
            MODULE,
            "descriptor_atomic_rename_exchange",
            side_effect=MODULE.AtomicRenameError(
                "exchange",
                errno.EOPNOTSUPP,
            ),
        ):
            with self.assertRaisesRegex(MODULE.PlanError, "unsupported"):
                MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(receipt.source_shallow_path.read_bytes(), initial)
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
        )

    def test_shallow_install_preexisting_lock_blocks_without_mutation(self) -> None:
        initial = b"a" * 40 + b"\n"
        fetched = b"b" * 40 + b"\n"
        preexisting_lock = b"existing-lock\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
        )
        lock_path = receipt.source_shallow_path.with_name(
            MODULE.SOURCE_SHALLOW_LOCK_NAME
        )
        lock_path.write_bytes(preexisting_lock)

        with self.assertRaisesRegex(MODULE.PlanError, "lock already exists"):
            MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(receipt.source_shallow_path.read_bytes(), initial)
        self.assertEqual(lock_path.read_bytes(), preexisting_lock)

    def test_shallow_install_accepts_mtime_only_source_change(self) -> None:
        initial = b"c" * 40 + b"\n"
        fetched = b"d" * 40 + b"\n"
        receipt = self.make_shallow_install_receipt(
            initial_content=initial,
            fetched_content=fetched,
        )
        before = receipt.source_shallow_path.stat()
        os.utime(
            receipt.source_shallow_path,
            ns=(
                before.st_atime_ns,
                before.st_mtime_ns + 1_000_000_000,
            ),
        )

        MODULE.install_post_fetch_shallow_state(receipt)

        self.assertEqual(receipt.source_shallow_path.read_bytes(), fetched)
        self.assertFalse(
            receipt.source_shallow_path.with_name(
                MODULE.SOURCE_SHALLOW_LOCK_NAME
            ).exists()
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

    def test_no_fetch_apply_skips_redundant_full_plan_validation(self) -> None:
        target = SimpleNamespace(path=self.root / "no-fetch-target")
        entry = SimpleNamespace(
            needs_fetch=False,
            checkout_preflight=mock.sentinel.checkout_receipt,
            parent_index=None,
            target=target,
            source_git_dir=self.named_source_git_dir,
            submodule=MODULE.Submodule(
                "no-fetch",
                "no-fetch",
                str(self.remote),
            ),
            sha=self.sha,
            state="missing",
        )
        plan = SimpleNamespace(entries=[entry], depth=1)
        lease = mock.MagicMock()
        lease.created_nodes = ()

        with (
            mock.patch.object(MODULE, "validate_sync_plan") as validate,
            mock.patch.object(
                MODULE,
                "revalidate_planned_entry",
                return_value=target,
            ),
            mock.patch.object(
                MODULE,
                "materialize_bound_target_directory",
                return_value=lease,
            ),
            mock.patch.object(
                MODULE,
                "capture_planned_source_lease",
                return_value=mock.MagicMock(),
            ),
            mock.patch.object(MODULE, "revalidate_planned_source_lease"),
            mock.patch.object(MODULE, "revalidate_materialized_target_lease"),
            mock.patch.object(MODULE, "revalidate_source_object_admission"),
            mock.patch.object(MODULE, "revalidate_checkout_preflight"),
            mock.patch.object(MODULE, "add_worktree"),
            mock.patch.object(MODULE, "postvalidate_applied_entry"),
        ):
            MODULE.apply_sync_plan(plan)

        validate.assert_called_once_with(plan)

    def test_apply_revalidates_only_each_fetch_source_before_one_full_pass(
        self,
    ) -> None:
        entries = []
        for index in range(3):
            entries.append(
                SimpleNamespace(
                    needs_fetch=True,
                    source_git_dir=self.root / f"source-{index}",
                    source_completeness=mock.sentinel.source_completeness,
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
            *,
            lease: object | None = None,
            source_lease: object | None = None,
            finalize_checkout: object | None = None,
        ) -> None:
            self.assertFalse(dry_run)
            self.assertIsNotNone(lease)
            self.assertIsNotNone(source_lease)
            events.append(f"add:{target.name}")
            if callable(finalize_checkout):
                finalize_checkout(
                    mock.sentinel.control,
                    mock.sentinel.source_lease,
                )

        lease = mock.MagicMock()
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
                                "materialize_bound_target_directory",
                                return_value=lease,
                            ):
                                with mock.patch.object(
                                    MODULE,
                                    "capture_planned_source_lease",
                                    side_effect=lambda _entry: mock.MagicMock(),
                                ):
                                    with mock.patch.object(
                                        MODULE,
                                        "revalidate_planned_source_lease",
                                    ):
                                        with mock.patch.object(
                                            MODULE,
                                            "revalidate_materialized_target_lease",
                                        ):
                                            with mock.patch.object(
                                                MODULE,
                                                "revalidate_source_object_admission",
                                            ):
                                                with mock.patch.object(
                                                    MODULE,
                                                    "revalidate_checkout_preflight",
                                                ):
                                                    with mock.patch.object(
                                                        MODULE,
                                                        "postvalidate_applied_entry",
                                                    ):
                                                        with mock.patch.object(
                                                            MODULE,
                                                            "revalidate_managed_final_state_receipt",
                                                        ):
                                                            with mock.patch.object(
                                                                MODULE,
                                                                "add_worktree",
                                                                side_effect=add,
                                                            ):
                                                                MODULE.apply_sync_plan(
                                                                    plan
                                                                )

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
