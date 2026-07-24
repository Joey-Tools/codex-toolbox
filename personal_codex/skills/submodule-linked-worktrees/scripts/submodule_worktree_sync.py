#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import configparser
import ctypes
from dataclasses import dataclass, field as dataclass_field, replace
import errno
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Optional
import unicodedata


GIT_ENUMERATION_TIMEOUT_SECONDS = 120.0
GIT_ENUMERATION_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
GIT_ERROR_OUTPUT_LIMIT_BYTES = 256 * 1024
GIT_INPUT_LIMIT_BYTES = 64 * 1024 * 1024
GIT_VERSION_TIMEOUT_SECONDS = 5.0
GIT_MINIMUM_VERSION = (2, 45, 0)
MAX_GIT_EXECUTABLE_BYTES = 128 * 1024 * 1024
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
PROCESS_TERM_GRACE_SECONDS = 0.5
MAX_CHECKOUT_PATHS = 250_000
MAX_CHECKOUT_PATH_BYTES = 4096
MAX_CHECKOUT_PATH_COMPONENTS = 1_000_000
MAX_CHECKOUT_ACCESS_BINDINGS = 500_000
MAX_NAME_POLICY_PROBE_ENTRIES = 256
MAX_REGISTERED_WORKTREE_FIELDS = 1_000_000
MAX_PLANNED_WORKTREES = 250_000
MAX_GIT_PATHSPEC_ARG_BYTES = 64 * 1024
MAX_GIT_PATHSPECS_PER_BATCH = 1024
MAX_GIT_PATHSPEC_BATCHES = 4096
MAX_GITMODULES_FILE_BYTES = 4 * 1024 * 1024
MAX_GITMODULES_RETAINED_BYTES = 64 * 1024 * 1024
MAX_SOURCE_CONFIG_BYTES = 4 * 1024 * 1024
MAX_SOURCE_SHALLOW_BYTES = 64 * 1024 * 1024
MAX_SUPERPROJECT_INDEX_BYTES = 512 * 1024 * 1024
MAX_TRANSPORT_EXECUTABLE_BYTES = 128 * 1024 * 1024
MAX_CONFIG_ENTRIES = 100_000
SOURCE_SHALLOW_NAME = "shallow"
SOURCE_SHALLOW_LOCK_NAME = "shallow.lock"
LINUX_RENAME_NOREPLACE = 0x00000001
LINUX_RENAME_EXCHANGE = 0x00000002
DARWIN_RENAME_SWAP = 0x00000002
DARWIN_RENAME_EXCL = 0x00000004
ATOMIC_RENAME_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.ENOSYS,
        errno.EINVAL,
        errno.EOPNOTSUPP,
        errno.EXDEV,
    }
)

LINUX_CASE_SENSITIVE_FILESYSTEM_MAGICS = frozenset(
    {
        0x01021994,  # tmpfs
        0x9123683E,  # Btrfs
        0xEF53,  # ext2/ext3/ext4
    }
)
LINUX_OVERLAYFS_MAGIC = 0x794C7630

GIT_ENV_PASSTHROUGH = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSH_AUTH_SOCK",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
)
SAFE_GIT_ENV = {
    "GIT_ASKPASS": "/usr/bin/false",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
    "SSH_ASKPASS": "/usr/bin/false",
    "SSH_ASKPASS_REQUIRE": "never",
}
SAFE_GIT_CONFIG_ARGS = (
    "--no-pager",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    f"core.excludesFile={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
)


class GitError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


class AtomicRenameError(PlanError):
    def __init__(self, operation: str, error_number: int) -> None:
        self.operation = operation
        self.error_number = error_number
        capability = (
            " is unsupported for this runtime or filesystem"
            if error_number in ATOMIC_RENAME_UNSUPPORTED_ERRNOS
            else " failed"
        )
        super().__init__(
            f"descriptor-relative atomic {operation}{capability}: "
            f"{os.strerror(error_number)}"
        )


class Submodule:
    def __init__(self, name: str, path: str, url: str) -> None:
        self.name = name
        self.path = path
        self.url = url


@dataclass(frozen=True)
class FsFingerprint:
    device: int
    inode: int
    kind: int
    owner: int
    group: int
    permissions: int


@dataclass(frozen=True)
class ExecutableState:
    fingerprint: FsFingerprint
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class GitRuntime:
    source_executable: Path
    source_state: ExecutableState
    executable: Path
    executable_state: ExecutableState
    content_sha256: str
    version: tuple[int, int, int]
    version_text: str
    snapshot_guard: object = dataclass_field(repr=False, compare=False)


@dataclass(frozen=True)
class BoundNode:
    path: Path
    fingerprint: FsFingerprint


@dataclass(frozen=True)
class AccessBinding:
    path: Path
    fingerprint: FsFingerprint
    mode: int
    purpose: str


@dataclass(frozen=True)
class FileContentBinding:
    path: Path
    fingerprint: FsFingerprint
    size: int
    content_sha256: str
    mode: int
    purpose: str
    maximum_bytes: int


@dataclass(frozen=True)
class FilesystemNamePolicy:
    case_sensitive: bool
    normalization: str
    source: str


@dataclass(frozen=True)
class TreeChange:
    relative_parts: tuple[str, ...]
    old_mode: str
    new_mode: str


@dataclass(frozen=True)
class FilterSelection:
    treeish: str
    raw_path: bytes
    driver: str


@dataclass
class GitmodulesReadBudget:
    deadline: float
    retained_limit: int = MAX_GITMODULES_RETAINED_BYTES
    retained_bytes: int = 0

    @classmethod
    def start(cls) -> GitmodulesReadBudget:
        return cls(deadline=time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS)

    def remaining_seconds(self, description: str) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PlanError(
                f"{description} exceeded the shared .gitmodules read deadline"
            )
        return remaining

    def check_capacity(self, byte_count: int, description: str) -> None:
        if byte_count < 0 or byte_count > MAX_GITMODULES_FILE_BYTES:
            raise PlanError(
                f"{description} exceeds the "
                f"{MAX_GITMODULES_FILE_BYTES}-byte per-file limit"
            )
        if self.retained_bytes + byte_count > self.retained_limit:
            raise PlanError(
                f"{description} exceeds the "
                f"{self.retained_limit}-byte shared retained-content limit"
            )

    def retain(self, byte_count: int, description: str) -> None:
        self.check_capacity(byte_count, description)
        self.retained_bytes += byte_count


@dataclass(frozen=True)
class CheckoutPreflight:
    kind: str
    current_head: Optional[str]
    index_digest: Optional[str]
    index_entry_count: Optional[int]
    path_count: int
    path_digest: str
    changes: tuple[TreeChange, ...]


@dataclass(frozen=True)
class SuperprojectIndexReceipt:
    index_bindings: tuple[FileContentBinding, ...]
    selected_gitlinks: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PlanInputReceipt:
    gitmodules_binding: FileContentBinding
    superproject_index: SuperprojectIndexReceipt


@dataclass(frozen=True)
class TransportReceipt:
    config_binding: FileContentBinding
    approved_url: str
    origin_url: str
    ssh_executable_binding: Optional[FileContentBinding]
    ssh_command: Optional[str]
    source_object_directory: Path
    source_shallow_path: Path
    source_shallow_parent_binding: AccessBinding
    source_shallow_binding: Optional[FileContentBinding]
    fetch_git_dir: Path
    fetch_access_bindings: tuple[AccessBinding, ...]
    fetch_file_bindings: tuple[FileContentBinding, ...]
    git_environment: tuple[tuple[str, str], ...]
    fetch_guard: object = dataclass_field(repr=False, compare=False)

    def __del__(self) -> None:
        guard = getattr(self, "fetch_guard", None)
        cleanup = getattr(guard, "cleanup", None)
        if cleanup is not None:
            cleanup()


@dataclass(frozen=True)
class BoundTarget:
    path: Path
    relative_parts: tuple[str, ...]
    existing_nodes: tuple[BoundNode, ...]
    missing_parts: tuple[str, ...]
    name_policy: FilesystemNamePolicy
    name_policy_anchor: BoundNode
    collision_tokens: tuple[tuple[object, ...], ...]


@dataclass
class PlannedWorktree:
    submodule: Submodule
    sha: str
    target: BoundTarget
    source_git_dir: Path
    parent_source_git_dir: Optional[Path]
    parent_index: Optional[int]
    state: str
    source_bindings: tuple[AccessBinding, ...]
    target_bindings: tuple[AccessBinding, ...]
    checkout_preflight: Optional[CheckoutPreflight]
    transport_receipt: Optional[TransportReceipt]
    needs_fetch: bool


@dataclass
class SyncPlan:
    root: Path
    display_root: Path
    entries: list[PlannedWorktree]
    depth: int
    force_replace_empty: bool
    fetch_missing: bool
    input_receipt: Optional[PlanInputReceipt]


class TargetCollisionNode:
    def __init__(self) -> None:
        self.children: dict[tuple[object, ...], TargetCollisionNode] = {}
        self.terminal_index: Optional[int] = None
        self.first_descendant_index: Optional[int] = None


class TargetCollisionIndex:
    def __init__(self) -> None:
        self.root = TargetCollisionNode()

    def add(
        self,
        entries: list[PlannedWorktree],
        candidate: PlannedWorktree,
        active_ancestor_indexes: Optional[set[int]] = None,
    ) -> None:
        active_ancestors = (
            active_ancestor_indexes if active_ancestor_indexes is not None else set()
        )
        node = self.root
        visited = [node]
        for token in candidate.target.collision_tokens:
            if (
                node.terminal_index is not None
                and node.terminal_index not in active_ancestors
            ):
                prior = entries[node.terminal_index]
                raise PlanError(
                    "planned worktree targets overlap or alias each other\n"
                    f"  first: {prior.target.path}\n"
                    f"  second: {candidate.target.path}"
                )
            node = node.children.setdefault(token, TargetCollisionNode())
            visited.append(node)

        if node.terminal_index is not None:
            prior = entries[node.terminal_index]
            raise PlanError(
                "planned worktree target collision\n"
                f"  first: {prior.target.path}\n"
                f"  second: {candidate.target.path}"
            )
        if node.first_descendant_index is not None:
            prior = entries[node.first_descendant_index]
            raise PlanError(
                "planned worktree targets overlap or alias each other\n"
                f"  first: {prior.target.path}\n"
                f"  second: {candidate.target.path}"
            )

        candidate_index = len(entries)
        node.terminal_index = candidate_index
        for visited_node in visited:
            if visited_node.first_descendant_index is None:
                visited_node.first_descendant_index = candidate_index


_GIT_RUNTIME: Optional[GitRuntime] = None


def git_environment(
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in GIT_ENV_PASSTHROUGH if key in os.environ
    }
    environment.update(SAFE_GIT_ENV)
    if extra_env:
        unsupported = set(extra_env) - {
            "GIT_ATTR_SOURCE",
            "GIT_LITERAL_PATHSPECS",
        }
        if unsupported:
            raise PlanError(
                "unsupported Git environment override: "
                + ", ".join(sorted(unsupported))
            )
        environment.update(extra_env)
    return environment


def executable_state_from_stat(path_stat: os.stat_result) -> ExecutableState:
    return ExecutableState(
        fingerprint=fingerprint_from_stat(path_stat),
        size=path_stat.st_size,
        modified_ns=path_stat.st_mtime_ns,
        changed_ns=path_stat.st_ctime_ns,
    )


def read_fd_digest(
    descriptor: int,
    expected_size: int,
    *,
    deadline: float,
    description: str,
) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    retained = 0
    while retained < expected_size:
        if time.monotonic() >= deadline:
            raise PlanError(f"{description} exceeded the content-copy deadline")
        chunk = os.read(descriptor, min(64 * 1024, expected_size - retained))
        if not chunk:
            raise PlanError(f"{description} changed size during content binding")
        retained += len(chunk)
        digest.update(chunk)
    if os.read(descriptor, 1):
        raise PlanError(f"{description} changed size during content binding")
    return digest.hexdigest()


def copy_git_executable_snapshot(
    source: Path,
    expected_fingerprint: FsFingerprint,
) -> tuple[
    object,
    Path,
    ExecutableState,
    ExecutableState,
    str,
]:
    snapshot_guard = tempfile.TemporaryDirectory(prefix="submodule-worktree-git.")
    snapshot_root = Path(snapshot_guard.name)
    snapshot = snapshot_root / "git"
    source_descriptor = -1
    snapshot_descriptor = -1
    try:
        root_state = filesystem_fingerprint(snapshot_root)
        if (
            root_state.kind != stat.S_IFDIR
            or root_state.owner != os.geteuid()
            or root_state.permissions & 0o077
        ):
            raise PlanError(
                "Git executable snapshot directory is not owner-private\n"
                f"  directory: {snapshot_root}"
            )
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        source_before = executable_state_from_stat(os.fstat(source_descriptor))
        if source_before.fingerprint != expected_fingerprint:
            raise PlanError(
                "the resolved Git executable changed before content binding\n"
                f"  executable: {source}"
            )
        if source_before.fingerprint.kind != stat.S_IFREG:
            raise PlanError(f"resolved Git executable is not a regular file: {source}")
        if source_before.size <= 0 or source_before.size > MAX_GIT_EXECUTABLE_BYTES:
            raise PlanError(
                "resolved Git executable exceeds the content-binding size limit\n"
                f"  executable: {source}\n"
                f"  size: {source_before.size}\n"
                f"  limit: {MAX_GIT_EXECUTABLE_BYTES}"
            )
        snapshot_descriptor = os.open(
            snapshot,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        deadline = time.monotonic() + GIT_VERSION_TIMEOUT_SECONDS
        digest = hashlib.sha256()
        copied = 0
        while copied < source_before.size:
            if time.monotonic() >= deadline:
                raise PlanError(
                    "Git executable snapshot exceeded the content-copy deadline"
                )
            chunk = os.read(
                source_descriptor,
                min(64 * 1024, source_before.size - copied),
            )
            if not chunk:
                raise PlanError(
                    "the resolved Git executable changed size during content binding"
                )
            copied += len(chunk)
            digest.update(chunk)
            pending = memoryview(chunk)
            while pending:
                written = os.write(snapshot_descriptor, pending)
                if written <= 0:
                    raise PlanError("failed to write the Git executable snapshot")
                pending = pending[written:]
        if os.read(source_descriptor, 1):
            raise PlanError(
                "the resolved Git executable changed size during content binding"
            )
        source_after = executable_state_from_stat(os.fstat(source_descriptor))
        if source_after != source_before:
            raise PlanError(
                "the resolved Git executable changed during content binding\n"
                f"  executable: {source}"
            )
        os.fchmod(snapshot_descriptor, 0o500)
        os.fsync(snapshot_descriptor)
        snapshot_digest = read_fd_digest(
            snapshot_descriptor,
            copied,
            deadline=deadline,
            description="Git executable snapshot",
        )
        if snapshot_digest != digest.hexdigest():
            raise PlanError("Git executable snapshot content verification failed")
        snapshot_state = executable_state_from_stat(os.fstat(snapshot_descriptor))
        if (
            snapshot_state.fingerprint.kind != stat.S_IFREG
            or snapshot_state.fingerprint.owner != os.geteuid()
            or snapshot_state.fingerprint.permissions != 0o500
            or snapshot_state.size != source_before.size
        ):
            raise PlanError(
                "Git executable snapshot does not satisfy its owner-private "
                "regular-file policy"
            )
        snapshot = snapshot.resolve(strict=True)
        return (
            snapshot_guard,
            snapshot,
            source_before,
            snapshot_state,
            snapshot_digest,
        )
    except OSError as exc:
        snapshot_guard.cleanup()
        raise PlanError(
            f"cannot create a verified Git executable snapshot: {exc}"
        ) from exc
    except BaseException:
        snapshot_guard.cleanup()
        raise
    finally:
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def revalidate_executable_content(
    path: Path,
    recorded_state: ExecutableState,
    expected_digest: str,
    description: str,
) -> ExecutableState:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        current_state = executable_state_from_stat(os.fstat(descriptor))
        if current_state.fingerprint != recorded_state.fingerprint:
            raise PlanError(
                f"{description} object or access policy changed\n  executable: {path}"
            )
        if current_state == recorded_state:
            return recorded_state
        if current_state.size <= 0 or current_state.size > MAX_GIT_EXECUTABLE_BYTES:
            raise PlanError(
                f"{description} content changed after version preflight\n"
                f"  executable: {path}"
            )
        deadline = time.monotonic() + GIT_VERSION_TIMEOUT_SECONDS
        digest = read_fd_digest(
            descriptor,
            current_state.size,
            deadline=deadline,
            description=description,
        )
        final_state = executable_state_from_stat(os.fstat(descriptor))
        if final_state != current_state or digest != expected_digest:
            raise PlanError(
                f"{description} content changed after version preflight\n"
                f"  executable: {path}"
            )
        return current_state
    except OSError as exc:
        raise PlanError(f"cannot revalidate {description}: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def discover_git_runtime() -> GitRuntime:
    if os.name != "posix":
        raise PlanError(
            "submodule-linked-worktrees supports only POSIX hosts; "
            "native Windows path and process semantics are unsupported"
        )
    candidate = shutil.which("git")
    if not candidate:
        raise PlanError("cannot resolve the required Git executable from PATH")
    executable = Path(candidate).resolve(strict=True)
    fingerprint = filesystem_fingerprint(executable)
    if fingerprint.kind != stat.S_IFREG or not probe_access(executable, os.X_OK):
        raise PlanError(
            f"resolved Git executable is not an executable file: {executable}"
        )
    (
        snapshot_guard,
        snapshot,
        source_state,
        snapshot_state,
        content_sha256,
    ) = copy_git_executable_snapshot(executable, fingerprint)
    try:
        result = run_bounded_bytes(
            [str(snapshot), "--version"],
            timeout_seconds=GIT_VERSION_TIMEOUT_SECONDS,
            stdout_limit=256,
            stderr_limit=256,
            prepare_git_command=False,
        )
        version_text = os.fsdecode(result.stdout).strip()
        match = re.fullmatch(
            r"git version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:[^\r\n]*)",
            version_text,
        )
        if not match:
            raise PlanError(
                f"cannot parse the fixed Git executable version: {version_text!r}"
            )
        version = tuple(int(value or "0") for value in match.groups())
        if version < GIT_MINIMUM_VERSION:
            minimum = ".".join(str(value) for value in GIT_MINIMUM_VERSION)
            raise PlanError(
                f"Git {minimum} or newer is required before repository access\n"
                f"  executable: {executable}\n"
                f"  actual: {version_text}\n"
                "  older Git versions cannot prove the no-lazy-fetch checkout contract"
            )
        return GitRuntime(
            source_executable=executable,
            source_state=source_state,
            executable=snapshot,
            executable_state=snapshot_state,
            content_sha256=content_sha256,
            version=version,
            version_text=version_text,
            snapshot_guard=snapshot_guard,
        )
    except BaseException:
        snapshot_guard.cleanup()
        raise


def git_runtime() -> GitRuntime:
    global _GIT_RUNTIME
    if _GIT_RUNTIME is None:
        _GIT_RUNTIME = discover_git_runtime()
    source_state = revalidate_executable_content(
        _GIT_RUNTIME.source_executable,
        _GIT_RUNTIME.source_state,
        _GIT_RUNTIME.content_sha256,
        "fixed source Git executable",
    )
    executable_state = revalidate_executable_content(
        _GIT_RUNTIME.executable,
        _GIT_RUNTIME.executable_state,
        _GIT_RUNTIME.content_sha256,
        "owner-private Git executable snapshot",
    )
    if (
        source_state != _GIT_RUNTIME.source_state
        or executable_state != _GIT_RUNTIME.executable_state
    ):
        _GIT_RUNTIME = replace(
            _GIT_RUNTIME,
            source_state=source_state,
            executable_state=executable_state,
        )
    return _GIT_RUNTIME


def safe_command(args: list[str]) -> list[str]:
    if not args or args[0] != "git":
        return args
    runtime = git_runtime()
    return [str(runtime.executable), *SAFE_GIT_CONFIG_ARGS, *args[1:]]


def run(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = True,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    command = safe_command(args)
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=git_environment(extra_env),
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GitError(
            f"{shell_join(command)} failed with exit code {result.returncode}: {stderr}"
        )
    return result


def wait_for_process_reap(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=max(0.0, timeout_seconds))
        return True
    except subprocess.TimeoutExpired:
        return False


def terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    cleanup_timeout_seconds: float = PROCESS_CLEANUP_TIMEOUT_SECONDS,
    term_grace_seconds: float = PROCESS_TERM_GRACE_SECONDS,
) -> None:
    cleanup_deadline = time.monotonic() + max(0.0, cleanup_timeout_seconds)
    group_cleanup_error: Optional[str] = None
    term_sent = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        term_sent = True
    except ProcessLookupError:
        pass
    except OSError as exc:
        group_cleanup_error = f"cannot signal the process group with TERM: {exc}"
        try:
            process.terminate()
            term_sent = True
        except (OSError, ProcessLookupError):
            pass

    if term_sent:
        remaining = cleanup_deadline - time.monotonic()
        grace = min(max(0.0, term_grace_seconds), max(0.0, remaining))
        if grace:
            # Do not reap the group leader before KILL. Retaining the zombie
            # prevents PID/PGID reuse during the TERM grace window.
            time.sleep(grace)
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        if group_cleanup_error is None:
            group_cleanup_error = f"cannot signal the process group with KILL: {exc}"
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass

    remaining = cleanup_deadline - time.monotonic()
    reaped = wait_for_process_reap(process, max(0.0, remaining))
    if not reaped:
        raise PlanError(
            "process cleanup-incomplete: direct child could not be reaped within "
            f"{cleanup_timeout_seconds:g} seconds"
        )
    if group_cleanup_error is not None:
        raise PlanError(f"process cleanup-incomplete: {group_cleanup_error}")


def run_bounded_bytes(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    input_bytes: Optional[bytes] = None,
    timeout_seconds: float = GIT_ENUMERATION_TIMEOUT_SECONDS,
    stdout_limit: int = GIT_ENUMERATION_OUTPUT_LIMIT_BYTES,
    stderr_limit: int = GIT_ERROR_OUTPUT_LIMIT_BYTES,
    extra_env: Optional[dict[str, str]] = None,
    fixed_env: Optional[dict[str, str]] = None,
    prepare_git_command: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    if input_bytes is not None and len(input_bytes) > GIT_INPUT_LIMIT_BYTES:
        raise PlanError(
            f"Git command input exceeds the {GIT_INPUT_LIMIT_BYTES}-byte safety limit"
        )
    if fixed_env is not None and extra_env is not None:
        raise PlanError(
            "fixed and incremental command environments are mutually exclusive"
        )
    command = safe_command(args) if prepare_git_command else args
    environment = (
        dict(fixed_env) if fixed_env is not None else git_environment(extra_env)
    )
    input_file = tempfile.TemporaryFile()
    if input_bytes is not None:
        input_file.write(input_bytes)
        input_file.seek(0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=environment,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        input_file.close()
        raise GitError(f"failed to start {shell_join(command)}: {exc}") from exc

    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    selector: Optional[selectors.BaseSelector] = None
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    failure: Optional[str] = None
    try:
        if stdout_pipe is None or stderr_pipe is None:
            raise GitError(f"{shell_join(command)} did not provide capture pipes")
        selector = selectors.DefaultSelector()
        selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
        selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"exceeded the {timeout_seconds:g}-second deadline"
                break
            events = selector.select(min(remaining, 0.25))
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                retained = stdout if key.data == "stdout" else stderr
                limit = stdout_limit if key.data == "stdout" else stderr_limit
                if len(retained) + len(chunk) > limit:
                    failure = (
                        f"{key.data} exceeds the {limit}-byte retained-output limit"
                    )
                    break
                retained.extend(chunk)
            if failure is not None:
                break
        if failure is not None:
            raise PlanError(f"{shell_join(command)} {failure}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = f"exceeded the {timeout_seconds:g}-second deadline"
        else:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                failure = f"exceeded the {timeout_seconds:g}-second deadline"
        if failure is not None:
            raise PlanError(f"{shell_join(command)} {failure}")
    except BaseException as exc:
        try:
            terminate_process_group(process)
        except PlanError as cleanup_error:
            raise PlanError(f"{exc}\n{cleanup_error}") from exc
        raise
    finally:
        if selector is not None:
            selector.close()
        if stdout_pipe is not None:
            stdout_pipe.close()
        if stderr_pipe is not None:
            stderr_pipe.close()
        input_file.close()

    result = subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )
    if check and returncode != 0:
        error = os.fsdecode(result.stderr).strip()
        raise GitError(
            f"{shell_join(command)} failed with exit code {returncode}: {error}"
        )
    return result


def read_git_bounded(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    input_bytes: Optional[bytes] = None,
    stdout_limit: int = GIT_ENUMERATION_OUTPUT_LIMIT_BYTES,
    extra_env: Optional[dict[str, str]] = None,
    fixed_env: Optional[dict[str, str]] = None,
    timeout_seconds: float = GIT_ENUMERATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    return run_bounded_bytes(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        check=check,
        input_bytes=input_bytes,
        stdout_limit=stdout_limit,
        extra_env=extra_env,
        fixed_env=fixed_env,
        timeout_seconds=timeout_seconds,
    )


def read_git(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "check": check,
    }
    if extra_env is not None:
        kwargs["extra_env"] = extra_env
    return run(["git", "--no-optional-locks", *args], **kwargs)


def git(args: list[str], *, cwd: Optional[Path] = None, check: bool = True) -> str:
    return read_git(args, cwd=cwd, check=check).stdout.strip()


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def resolved_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def validate_relative_git_path(value: str, field: str, origin: str) -> str:
    if not value:
        raise PlanError(f"{field} in {origin} must not be empty")
    if "\0" in value:
        raise PlanError(f"{field} in {origin} contains a NUL byte")
    if len(os.fsencode(value)) > MAX_CHECKOUT_PATH_BYTES:
        raise PlanError(
            f"{field} in {origin} exceeds the {MAX_CHECKOUT_PATH_BYTES}-byte path limit"
        )
    if "\\" in value:
        raise PlanError(
            f"{field} in {origin} contains an unsupported path separator: {value}"
        )
    windows_path = PureWindowsPath(value)
    if (
        value.startswith("/")
        or Path(value).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    ):
        raise PlanError(f"{field} in {origin} must be relative: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PlanError(f"{field} in {origin} contains an unsafe path segment: {value}")
    return "/".join(parts)


def contained_child_path(base: Path, relative_path: str, label: str) -> Path:
    base_resolved = base.resolve()
    candidate = (base / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise PlanError(f"{label} escapes {base}: {candidate}") from exc
    return candidate


def fingerprint_from_stat(path_stat: os.stat_result) -> FsFingerprint:
    return FsFingerprint(
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        kind=stat.S_IFMT(path_stat.st_mode),
        owner=path_stat.st_uid,
        group=path_stat.st_gid,
        permissions=stat.S_IMODE(path_stat.st_mode),
    )


def filesystem_fingerprint(path: Path) -> FsFingerprint:
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise PlanError(f"required path is missing: {path}") from exc
    except PermissionError as exc:
        raise PlanError(f"required path is unreadable: {path}") from exc
    return fingerprint_from_stat(path_stat)


def path_entry_exists(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except PermissionError as exc:
        raise PlanError(f"required path is unreadable: {path}") from exc
    return True


def probe_access(path: Path, mode: int) -> bool:
    try:
        return os.access(path, mode, effective_ids=True, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        return os.access(path, mode)


def probe_access_at(directory_descriptor: int, name: str, mode: int) -> bool:
    if os.access not in os.supports_dir_fd:
        raise PlanError("descriptor-relative effective-access checks are unavailable")
    try:
        return os.access(
            name,
            mode,
            dir_fd=directory_descriptor,
            effective_ids=True,
            follow_symlinks=False,
        )
    except (NotImplementedError, TypeError) as exc:
        raise PlanError(
            "descriptor-relative effective-access checks are unavailable"
        ) from exc


def access_mode_text(mode: int) -> str:
    labels: list[str] = []
    if mode & os.R_OK:
        labels.append("read")
    if mode & os.W_OK:
        labels.append("write")
    if mode & os.X_OK:
        labels.append("search/execute")
    return "/".join(labels)


def capture_access(path: Path, mode: int, purpose: str) -> AccessBinding:
    fingerprint = filesystem_fingerprint(path)
    if not probe_access(path, mode):
        raise PlanError(
            f"access policy denies {purpose}\n"
            f"  path: {path}\n"
            f"  required: {access_mode_text(mode)}"
        )
    return AccessBinding(path=path, fingerprint=fingerprint, mode=mode, purpose=purpose)


def capture_typed_access(
    path: Path,
    mode: int,
    purpose: str,
    expected_kind: int,
) -> AccessBinding:
    binding = capture_access(path, mode, purpose)
    if binding.fingerprint.kind != expected_kind:
        raise PlanError(f"{purpose} path has an unsafe object type\n  path: {path}")
    return binding


def revalidate_access(binding: AccessBinding) -> None:
    # Protected property: permission to perform the planned operation on the same object.
    # dev/ino bind identity; kind prevents substitution; uid/gid/mode bind POSIX policy;
    # effective access rechecks ACLs and current credentials. Timestamps and directory
    # entry churn are intentionally excluded because they do not change that property.
    try:
        current = filesystem_fingerprint(binding.path)
    except PlanError as exc:
        raise PlanError(
            f"access-policy revalidation failed for {binding.purpose}: {exc}"
        ) from exc
    if current != binding.fingerprint:
        raise PlanError(
            f"access-policy object or policy changed for {binding.purpose}\n"
            f"  path: {binding.path}"
        )
    if not probe_access(binding.path, binding.mode):
        raise PlanError(
            f"access policy now denies {binding.purpose}\n"
            f"  path: {binding.path}\n"
            f"  required: {access_mode_text(binding.mode)}"
        )


def read_bound_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    mode: int,
    purpose: str,
    retain_content: bool,
    deadline: Optional[float] = None,
) -> tuple[FileContentBinding, Optional[bytes]]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise PlanError(
            f"cannot safely bind {purpose}: O_NOFOLLOW and O_NONBLOCK are required"
        )
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        before_stat = os.fstat(descriptor)
        fingerprint = fingerprint_from_stat(before_stat)
        if fingerprint.kind != stat.S_IFREG:
            raise PlanError(f"{purpose} is not a regular file\n  path: {path}")
        if before_stat.st_size < 0 or before_stat.st_size > maximum_bytes:
            raise PlanError(
                f"{purpose} exceeds the {maximum_bytes}-byte safety limit\n"
                f"  path: {path}\n"
                f"  size: {before_stat.st_size}"
            )
        if not probe_access(path, mode):
            raise PlanError(
                f"access policy denies {purpose}\n"
                f"  path: {path}\n"
                f"  required: {access_mode_text(mode)}"
            )
        effective_deadline = deadline or (
            time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS
        )

        def read_pass(keep: bool) -> tuple[str, Optional[bytes]]:
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            content = bytearray() if keep else None
            consumed = 0
            while consumed < before_stat.st_size:
                if time.monotonic() >= effective_deadline:
                    raise PlanError(f"{purpose} exceeded the content-read deadline")
                try:
                    chunk = os.read(
                        descriptor,
                        min(64 * 1024, before_stat.st_size - consumed),
                    )
                except BlockingIOError as exc:
                    raise PlanError(
                        f"{purpose} did not provide bounded regular-file I/O"
                    ) from exc
                if not chunk:
                    raise PlanError(f"{purpose} changed size during content binding")
                consumed += len(chunk)
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
            if os.read(descriptor, 1):
                raise PlanError(f"{purpose} changed size during content binding")
            return digest.hexdigest(), bytes(content) if content is not None else None

        first_digest, first_content = read_pass(retain_content)
        middle_stat = os.fstat(descriptor)
        second_digest, _ = read_pass(False)
        after_stat = os.fstat(descriptor)
        path_fingerprint = filesystem_fingerprint(path)
        for observed in (middle_stat, after_stat):
            if (
                fingerprint_from_stat(observed) != fingerprint
                or observed.st_size != before_stat.st_size
            ):
                raise PlanError(f"{purpose} object or size changed during binding")
        if path_fingerprint != fingerprint:
            raise PlanError(
                f"{purpose} path object changed during binding\n  path: {path}"
            )
        if first_digest != second_digest:
            raise PlanError(f"{purpose} content changed during binding\n  path: {path}")
        return (
            FileContentBinding(
                path=path,
                fingerprint=fingerprint,
                size=before_stat.st_size,
                content_sha256=first_digest,
                mode=mode,
                purpose=purpose,
                maximum_bytes=maximum_bytes,
            ),
            first_content,
        )
    except FileNotFoundError as exc:
        raise PlanError(f"{purpose} is missing\n  path: {path}") from exc
    except OSError as exc:
        raise PlanError(
            f"cannot bind {purpose}\n  path: {path}\n  error: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bind_regular_file_descriptor_at(
    descriptor: int,
    directory_descriptor: int,
    name: str,
    display_path: Path,
    *,
    maximum_bytes: int,
    mode: int,
    purpose: str,
    retain_content: bool,
    deadline: Optional[float] = None,
) -> tuple[FileContentBinding, Optional[bytes]]:
    before_stat = os.fstat(descriptor)
    fingerprint = fingerprint_from_stat(before_stat)
    if fingerprint.kind != stat.S_IFREG:
        raise PlanError(f"{purpose} is not a regular file\n  path: {display_path}")
    if before_stat.st_size < 0 or before_stat.st_size > maximum_bytes:
        raise PlanError(
            f"{purpose} exceeds the {maximum_bytes}-byte safety limit\n"
            f"  path: {display_path}\n"
            f"  size: {before_stat.st_size}"
        )
    if not probe_access_at(directory_descriptor, name, mode):
        raise PlanError(
            f"access policy denies {purpose}\n"
            f"  path: {display_path}\n"
            f"  required: {access_mode_text(mode)}"
        )
    effective_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS
    )

    def read_pass(keep: bool) -> tuple[str, Optional[bytes]]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        content = bytearray() if keep else None
        consumed = 0
        while consumed < before_stat.st_size:
            if time.monotonic() >= effective_deadline:
                raise PlanError(f"{purpose} exceeded the content-read deadline")
            try:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, before_stat.st_size - consumed),
                )
            except BlockingIOError as exc:
                raise PlanError(
                    f"{purpose} did not provide bounded regular-file I/O"
                ) from exc
            if not chunk:
                raise PlanError(f"{purpose} changed size during content binding")
            consumed += len(chunk)
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        if os.read(descriptor, 1):
            raise PlanError(f"{purpose} changed size during content binding")
        return digest.hexdigest(), bytes(content) if content is not None else None

    first_digest, first_content = read_pass(retain_content)
    middle_stat = os.fstat(descriptor)
    second_digest, _ = read_pass(False)
    after_stat = os.fstat(descriptor)
    try:
        path_stat = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise PlanError(f"{purpose} path disappeared during binding") from exc
    except PermissionError as exc:
        raise PlanError(f"{purpose} path became unreadable during binding") from exc
    path_fingerprint = fingerprint_from_stat(path_stat)
    for observed in (middle_stat, after_stat):
        if (
            fingerprint_from_stat(observed) != fingerprint
            or observed.st_size != before_stat.st_size
        ):
            raise PlanError(f"{purpose} object or size changed during binding")
    if path_fingerprint != fingerprint:
        raise PlanError(
            f"{purpose} path object changed during binding\n  path: {display_path}"
        )
    if first_digest != second_digest:
        raise PlanError(
            f"{purpose} content changed during binding\n  path: {display_path}"
        )
    return (
        FileContentBinding(
            path=display_path,
            fingerprint=fingerprint,
            size=before_stat.st_size,
            content_sha256=first_digest,
            mode=mode,
            purpose=purpose,
            maximum_bytes=maximum_bytes,
        ),
        first_content,
    )


def bind_regular_file_descriptor_at(
    descriptor: int,
    directory_descriptor: int,
    name: str,
    display_path: Path,
    *,
    maximum_bytes: int,
    mode: int,
    purpose: str,
    retain_content: bool,
    deadline: Optional[float] = None,
) -> tuple[FileContentBinding, Optional[bytes]]:
    try:
        return _bind_regular_file_descriptor_at(
            descriptor,
            directory_descriptor,
            name,
            display_path,
            maximum_bytes=maximum_bytes,
            mode=mode,
            purpose=purpose,
            retain_content=retain_content,
            deadline=deadline,
        )
    except OSError as exc:
        raise PlanError(
            f"cannot bind descriptor-relative {purpose}\n"
            f"  path: {display_path}\n"
            f"  error: {exc}"
        ) from exc


def open_bound_regular_file_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    *,
    maximum_bytes: int,
    mode: int,
    purpose: str,
    retain_content: bool,
) -> tuple[int, FileContentBinding, Optional[bytes]]:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_NONBLOCK")
        or os.open not in os.supports_dir_fd
    ):
        raise PlanError(
            f"cannot safely bind {purpose}: descriptor-relative O_NOFOLLOW and "
            "O_NONBLOCK are required"
        )
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_descriptor,
        )
        binding, content = bind_regular_file_descriptor_at(
            descriptor,
            directory_descriptor,
            name,
            display_path,
            maximum_bytes=maximum_bytes,
            mode=mode,
            purpose=purpose,
            retain_content=retain_content,
        )
        return descriptor, binding, content
    except FileNotFoundError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise PlanError(f"{purpose} is missing\n  path: {display_path}") from exc
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise PlanError(
            f"cannot bind {purpose}\n  path: {display_path}\n  error: {exc}"
        ) from exc
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def revalidate_file_content_binding(binding: FileContentBinding) -> None:
    # Protected properties are object identity, byte content, and effective access.
    # dev/ino/type/uid/gid/mode bind identity and POSIX policy; the digest binds
    # content; effective access rechecks ACL/current-credential effects. Timestamps
    # are deliberately excluded, so mtime-only bookkeeping does not cause a mismatch.
    current, _ = read_bound_regular_file(
        binding.path,
        maximum_bytes=binding.maximum_bytes,
        mode=binding.mode,
        purpose=binding.purpose,
        retain_content=False,
    )
    if current.fingerprint != binding.fingerprint:
        raise PlanError(
            f"{binding.purpose} object or access policy changed after preflight\n"
            f"  path: {binding.path}"
        )
    if current.size != binding.size or current.content_sha256 != binding.content_sha256:
        raise PlanError(
            f"{binding.purpose} content changed after preflight\n  path: {binding.path}"
        )


def local_git_bool(root: Path, key: str) -> Optional[bool]:
    result = read_git_bounded(
        [
            "-C",
            str(root),
            "config",
            "--local",
            "--no-includes",
            "--type=bool",
            "--get",
            key,
        ],
        check=False,
        stdout_limit=16,
    )
    if result.returncode != 0:
        return None
    value = os.fsdecode(result.stdout).strip()
    if value == "true":
        return True
    if value == "false":
        return False
    raise PlanError(f"unexpected boolean value for {key}: {value!r}")


def darwin_volume_case_sensitive(path: Path) -> bool:
    class AttrList(ctypes.Structure):
        _fields_ = [
            ("bitmapcount", ctypes.c_ushort),
            ("reserved", ctypes.c_uint16),
            ("commonattr", ctypes.c_uint32),
            ("volattr", ctypes.c_uint32),
            ("dirattr", ctypes.c_uint32),
            ("fileattr", ctypes.c_uint32),
            ("forkattr", ctypes.c_uint32),
        ]

    class VolumeCapabilities(ctypes.Structure):
        _fields_ = [
            ("capabilities", ctypes.c_uint32 * 4),
            ("valid", ctypes.c_uint32 * 4),
        ]

    class AttributeBuffer(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("capabilities", VolumeCapabilities),
        ]

    attr_vol_capabilities = 0x00020000
    attr_vol_info = 0x80000000
    vol_cap_fmt_case_sensitive = 0x00000100
    attributes = AttrList()
    attributes.bitmapcount = 5
    attributes.volattr = attr_vol_info | attr_vol_capabilities
    output = AttributeBuffer()
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.getattrlist(
        os.fsencode(path),
        ctypes.byref(attributes),
        ctypes.byref(output),
        ctypes.sizeof(output),
        0,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise PlanError(
            f"cannot determine target volume case semantics for {path}: "
            f"{os.strerror(error)}"
        )
    valid = output.capabilities.valid[0]
    if not valid & vol_cap_fmt_case_sensitive:
        raise PlanError(
            f"target volume does not report valid case semantics for {path}"
        )
    return bool(output.capabilities.capabilities[0] & vol_cap_fmt_case_sensitive)


def alternate_case_name(name: str) -> Optional[str]:
    for index, character in enumerate(name):
        alternate = character.swapcase()
        if alternate != character and len(alternate) == 1:
            return f"{name[:index]}{alternate}{name[index + 1 :]}"
    return None


def open_directory_descriptor(path: Path, purpose: str) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PlanError(
            f"cannot safely bind {purpose}: O_NOFOLLOW and O_DIRECTORY are required"
        )
    try:
        return os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
    except OSError as exc:
        raise PlanError(f"cannot open {purpose}: {path}\n  error: {exc}") from exc


def revalidate_directory_descriptor(
    binding: AccessBinding,
    directory_descriptor: int,
) -> None:
    # Protected property: all later relative operations stay anchored to the
    # exact directory object and access policy captured by the receipt. The
    # pathname check detects replacement, while fstat keeps a rename from
    # redirecting operations to a different object.
    current_descriptor = fingerprint_from_stat(os.fstat(directory_descriptor))
    if current_descriptor != binding.fingerprint:
        raise PlanError(
            f"descriptor object or access policy changed for {binding.purpose}\n"
            f"  path: {binding.path}"
        )
    if not probe_access_at(directory_descriptor, ".", binding.mode):
        raise PlanError(
            f"descriptor access policy now denies {binding.purpose}\n"
            f"  path: {binding.path}\n"
            f"  required: {access_mode_text(binding.mode)}"
        )
    try:
        current_path = filesystem_fingerprint(binding.path)
    except PlanError as exc:
        raise PlanError(
            f"path revalidation failed for descriptor-bound {binding.purpose}: {exc}"
        ) from exc
    if current_path != binding.fingerprint:
        raise PlanError(
            f"path object or access policy changed for descriptor-bound "
            f"{binding.purpose}\n"
            f"  path: {binding.path}"
        )


def validate_descriptor_entry_name(name: str) -> bytes:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
        or os.fsdecode(os.fsencode(name)) != name
    ):
        raise PlanError(f"unsafe descriptor-relative entry name: {name!r}")
    return os.fsencode(name)


def descriptor_atomic_rename(
    source_directory_descriptor: int,
    source_name: str,
    target_directory_descriptor: int,
    target_name: str,
    *,
    operation: str,
) -> None:
    source_bytes = validate_descriptor_entry_name(source_name)
    target_bytes = validate_descriptor_entry_name(target_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        flags = (
            DARWIN_RENAME_EXCL
            if operation == "noreplace"
            else DARWIN_RENAME_SWAP
            if operation == "exchange"
            else None
        )
        if flags is None:
            raise PlanError(f"unknown atomic rename operation: {operation}")
        try:
            rename_function = libc.renameatx_np
        except AttributeError as exc:
            raise AtomicRenameError(operation, errno.ENOSYS) from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
    elif sys.platform.startswith("linux"):
        flags = (
            LINUX_RENAME_NOREPLACE
            if operation == "noreplace"
            else LINUX_RENAME_EXCHANGE
            if operation == "exchange"
            else None
        )
        if flags is None:
            raise PlanError(f"unknown atomic rename operation: {operation}")
        try:
            rename_function = libc.renameat2
        except AttributeError as exc:
            raise AtomicRenameError(operation, errno.ENOSYS) from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
    else:
        raise AtomicRenameError(operation, errno.ENOSYS)

    ctypes.set_errno(0)
    result = rename_function(
        source_directory_descriptor,
        source_bytes,
        target_directory_descriptor,
        target_bytes,
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise AtomicRenameError(operation, error_number)


def descriptor_atomic_rename_noreplace(
    directory_descriptor: int,
    source_name: str,
    target_name: str,
) -> None:
    descriptor_atomic_rename(
        directory_descriptor,
        source_name,
        directory_descriptor,
        target_name,
        operation="noreplace",
    )


def descriptor_atomic_rename_exchange(
    directory_descriptor: int,
    first_name: str,
    second_name: str,
) -> None:
    descriptor_atomic_rename(
        directory_descriptor,
        first_name,
        directory_descriptor,
        second_name,
        operation="exchange",
    )


def probe_directory_case_sensitive(
    path: Path,
    descriptor: Optional[int] = None,
) -> Optional[bool]:
    owned_descriptor = descriptor is None
    directory_descriptor = (
        open_directory_descriptor(path, "target name-policy directory")
        if descriptor is None
        else descriptor
    )
    try:
        with os.scandir(directory_descriptor) as entries:
            entry_names: list[str] = []
            enumeration_truncated = False
            for index, entry in enumerate(entries):
                if index >= MAX_NAME_POLICY_PROBE_ENTRIES:
                    enumeration_truncated = True
                    break
                entry_names.append(entry.name)
            exact_entry_names = set(entry_names)
            for entry_name in entry_names:
                alternate_name = alternate_case_name(entry_name)
                if alternate_name is None:
                    continue
                try:
                    original_before_stat = os.stat(
                        entry_name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    # Entry churn before the proof starts is benign. Try another
                    # bounded entry rather than treating disappearance as case evidence.
                    continue
                except PermissionError as exc:
                    raise PlanError(
                        f"cannot inspect target name semantics: {path / entry_name}"
                    ) from exc
                original_before = fingerprint_from_stat(original_before_stat)
                alternate: Optional[FsFingerprint]
                try:
                    alternate_stat = os.stat(
                        alternate_name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    alternate = None
                except PermissionError as exc:
                    raise PlanError(
                        f"cannot inspect target name semantics: {path / alternate_name}"
                    ) from exc
                else:
                    alternate = fingerprint_from_stat(alternate_stat)
                try:
                    original_after = fingerprint_from_stat(
                        os.stat(
                            entry_name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except (FileNotFoundError, PermissionError) as exc:
                    raise PlanError(
                        "target name-policy probe entry became unavailable during "
                        f"revalidation: {path / entry_name}"
                    ) from exc
                if original_after != original_before:
                    raise PlanError(
                        "target name-policy probe entry changed identity or access "
                        f"policy during revalidation: {path / entry_name}"
                    )
                if alternate is None:
                    return True
                if (
                    original_before.device == alternate.device
                    and original_before.inode == alternate.inode
                    and original_before.kind == alternate.kind
                ):
                    if alternate_name in exact_entry_names:
                        # Two separately enumerated spellings that resolve to the
                        # same inode are hardlinks on a case-sensitive directory,
                        # not evidence of case-insensitive lookup.
                        return True
                    if enumeration_truncated:
                        # The alternate spelling may be a distinct dirent outside
                        # the bounded inventory, so this probe is inconclusive.
                        continue
                    return False
                return True
    except PermissionError as exc:
        raise PlanError(f"cannot inspect target name semantics: {path}") from exc
    finally:
        if owned_descriptor:
            os.close(directory_descriptor)
    return None


def linux_directory_casefold(
    path: Path,
    descriptor: Optional[int] = None,
) -> Optional[bool]:
    if not sys.platform.startswith("linux"):
        return None
    import array
    import fcntl

    fs_ioc_getflags = 0x80086601
    fs_casefold_fl = 0x40000000
    flags = array.array("I", [0])
    owned_descriptor = descriptor is None
    directory_descriptor = (
        open_directory_descriptor(path, "target directory casefold probe")
        if descriptor is None
        else descriptor
    )
    try:
        try:
            fcntl.ioctl(directory_descriptor, fs_ioc_getflags, flags, True)
        except OSError:
            return None
    finally:
        if owned_descriptor:
            os.close(directory_descriptor)
    return bool(flags[0] & fs_casefold_fl)


def linux_filesystem_magic(
    path: Path,
    descriptor: Optional[int] = None,
) -> Optional[int]:
    if not sys.platform.startswith("linux"):
        return None
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    owned_descriptor = descriptor is None
    directory_descriptor = (
        open_directory_descriptor(path, "target filesystem-type probe")
        if descriptor is None
        else descriptor
    )
    try:
        libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
        libc.fstatfs.restype = ctypes.c_int
        result = libc.fstatfs(directory_descriptor, ctypes.byref(buffer))
        if result != 0:
            error = ctypes.get_errno()
            raise PlanError(
                f"cannot determine target filesystem type for {path}: "
                f"{os.strerror(error)}"
            )
    finally:
        if owned_descriptor:
            os.close(directory_descriptor)
    value = ctypes.c_long.from_buffer(buffer).value
    return value & ((1 << (ctypes.sizeof(ctypes.c_long) * 8)) - 1)


def filesystem_name_policy(root: Path) -> FilesystemNamePolicy:
    root = root.resolve(strict=True)
    configured_precompose = local_git_bool(root, "core.precomposeUnicode")

    if sys.platform == "darwin":
        case_sensitive = darwin_volume_case_sensitive(root)
        return FilesystemNamePolicy(
            case_sensitive=case_sensitive,
            normalization="NFD",
            source="darwin-volume-capabilities",
        )

    if os.name == "nt":
        raise PlanError("native Windows filesystem name semantics are unsupported")

    directory_descriptor: Optional[int] = None
    directory_fingerprint: Optional[FsFingerprint] = None
    if sys.platform.startswith("linux"):
        directory_descriptor = open_directory_descriptor(
            root,
            "target name-policy directory",
        )
        directory_fingerprint = fingerprint_from_stat(os.fstat(directory_descriptor))
        if directory_fingerprint.kind != stat.S_IFDIR:
            os.close(directory_descriptor)
            raise PlanError(f"target name-policy path is not a directory: {root}")
        if filesystem_fingerprint(root) != directory_fingerprint:
            os.close(directory_descriptor)
            raise PlanError(
                f"target name-policy directory changed during descriptor binding: {root}"
            )
    try:
        probed_case_sensitive = probe_directory_case_sensitive(
            root,
            descriptor=directory_descriptor,
        )
        directory_casefold = linux_directory_casefold(
            root,
            descriptor=directory_descriptor,
        )
        filesystem_magic = (
            linux_filesystem_magic(root, descriptor=directory_descriptor)
            if sys.platform.startswith("linux")
            else None
        )
        if directory_descriptor is not None:
            current_descriptor_fingerprint = fingerprint_from_stat(
                os.fstat(directory_descriptor)
            )
            current_path_fingerprint = filesystem_fingerprint(root)
            if (
                current_descriptor_fingerprint != directory_fingerprint
                or current_path_fingerprint != directory_fingerprint
            ):
                raise PlanError(
                    "target name-policy directory changed identity or access policy "
                    f"during revalidation: {root}"
                )
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    if filesystem_magic == LINUX_OVERLAYFS_MAGIC:
        raise PlanError(
            "cannot determine target directory case semantics without mutation\n"
            f"  directory: {root}\n"
            "  OverlayFS merged-directory lookup is not authoritative for every layer\n"
            "  no target worktree or source object was changed"
        )
    if directory_casefold is True:
        case_sensitive = False
        source = "linux-directory-casefold-flag"
    elif probed_case_sensitive is not None:
        case_sensitive = probed_case_sensitive
        source = "directory-lookup-probe"
    elif sys.platform.startswith("linux"):
        if (
            directory_casefold is False
            and filesystem_magic in LINUX_CASE_SENSITIVE_FILESYSTEM_MAGICS
        ):
            case_sensitive = True
            source = f"linux-statfs-and-directory-flags:{filesystem_magic:#x}"
        else:
            raise PlanError(
                "cannot determine target directory case semantics without mutation\n"
                f"  directory: {root}\n"
                "  no existing entry supports an authoritative lookup probe, and "
                "the filesystem/flags combination is not a known case-sensitive policy\n"
                "  no target worktree or source object was changed"
            )
    else:
        raise PlanError(
            "cannot determine target directory case semantics without mutation\n"
            f"  directory: {root}\n"
            "  no existing entry supports an authoritative lookup probe\n"
            "  no target worktree or source object was changed"
        )
    normalization = "NFD" if directory_casefold or configured_precompose else "exact"
    return FilesystemNamePolicy(
        case_sensitive=case_sensitive,
        normalization=normalization,
        source=source,
    )


def normalized_path_parts(
    parts: Iterable[str],
    policy: FilesystemNamePolicy,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for part in parts:
        value = (
            unicodedata.normalize(policy.normalization, part)
            if policy.normalization != "exact"
            else part
        )
        normalized.append(value if policy.case_sensitive else value.casefold())
    return tuple(normalized)


def lexical_relative_parts(root: Path, path: Path, label: str) -> tuple[str, ...]:
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise PlanError(f"{label} escapes {root_absolute}: {path_absolute}") from exc
    if relative == Path("."):
        return ()
    parts = tuple(relative.parts)
    for part in parts:
        validate_relative_git_path(part, label, str(path))
    return parts


def bind_target_path(
    root: Path,
    relative_parts: tuple[str, ...],
    label: str,
    name_policy: Optional[FilesystemNamePolicy] = None,
) -> BoundTarget:
    root = root.resolve(strict=True)
    validated_parts: list[str] = []
    for part in relative_parts:
        normalized = validate_relative_git_path(part, label, str(root))
        if "/" in normalized:
            raise PlanError(f"{label} path component contains a separator: {part}")
        validated_parts.append(normalized)
    relative_parts = tuple(validated_parts)
    relative_path_bytes = len(os.fsencode("/".join(relative_parts)))
    if relative_path_bytes > MAX_CHECKOUT_PATH_BYTES:
        raise PlanError(
            f"{label} exceeds the {MAX_CHECKOUT_PATH_BYTES}-byte target path limit"
        )
    path = root.joinpath(*relative_parts)
    if lexical_relative_parts(root, path, label) != relative_parts:
        raise PlanError(f"{label} escapes its lexical target root: {path}")

    nodes: list[BoundNode] = []
    current = root
    root_fingerprint = filesystem_fingerprint(root)
    if root_fingerprint.kind != stat.S_IFDIR:
        raise PlanError(f"{label} root is not a directory: {root}")
    nodes.append(BoundNode(root, root_fingerprint))

    missing_parts: tuple[str, ...] = ()
    for index, part in enumerate(relative_parts):
        current = current / part
        try:
            current_fingerprint = filesystem_fingerprint(current)
        except PlanError as exc:
            if "is missing:" in str(exc):
                missing_parts = relative_parts[index:]
                break
            raise
        if current_fingerprint.kind == stat.S_IFLNK:
            raise PlanError(
                f"{label} escapes the no-follow target policy through a symlink "
                f"alias/collision: {current}"
            )
        if current_fingerprint.kind != stat.S_IFDIR:
            if index < len(relative_parts) - 1:
                raise PlanError(
                    f"cannot create worktree path {root.joinpath(*relative_parts)}\n"
                    f"  existing parent is not a directory: {current}"
                )
            raise PlanError(f"{label} already exists and is not a directory: {current}")
        nodes.append(BoundNode(current, current_fingerprint))

    name_policy_anchor = nodes[-1]
    if name_policy is None:
        name_policy = filesystem_name_policy(name_policy_anchor.path)
    collision_tokens: list[tuple[object, ...]] = [
        ("existing", node.fingerprint.device, node.fingerprint.inode)
        for node in nodes[1:]
    ]
    collision_tokens.extend(
        ("missing", normalized)
        for normalized in normalized_path_parts(missing_parts, name_policy)
    )
    return BoundTarget(
        path=path,
        relative_parts=relative_parts,
        existing_nodes=tuple(nodes),
        missing_parts=missing_parts,
        name_policy=name_policy,
        name_policy_anchor=name_policy_anchor,
        collision_tokens=tuple(collision_tokens),
    )


def revalidate_bound_target(target: BoundTarget) -> None:
    for node in target.existing_nodes:
        try:
            current = filesystem_fingerprint(node.path)
        except PlanError as exc:
            raise PlanError(f"target-path revalidation failed: {exc}") from exc
        if current != node.fingerprint:
            raise PlanError(f"target-path object or policy changed: {node.path}")
        if current.kind == stat.S_IFLNK:
            raise PlanError(
                f"target path became a symlink alias/collision: {node.path}"
            )
    current_policy = filesystem_name_policy(target.name_policy_anchor.path)
    if (
        current_policy.case_sensitive != target.name_policy.case_sensitive
        or current_policy.normalization != target.name_policy.normalization
    ):
        raise PlanError(
            "target directory name semantics changed after preflight\n"
            f"  anchor: {target.name_policy_anchor.path}"
        )
    if target.missing_parts:
        first_missing = target.existing_nodes[-1].path / target.missing_parts[0]
        try:
            os.stat(first_missing, follow_symlinks=False)
        except FileNotFoundError:
            return
        except PermissionError as exc:
            raise PlanError(f"target path became unreadable: {first_missing}") from exc
        raise PlanError(
            f"target path changed after preflight: {first_missing} now exists"
        )


def source_repo_args(source_git_dir: Path, work_tree: Path) -> list[str]:
    return [f"--git-dir={source_git_dir}", f"--work-tree={work_tree}"]


def source_object_repo_args(source_git_dir: Path) -> list[str]:
    return [f"--git-dir={source_git_dir}", f"--work-tree={source_git_dir}"]


def repo_paths(repo: Path) -> tuple[Path, Path, Path]:
    root = Path(git(["rev-parse", "--show-toplevel"], cwd=repo)).resolve()
    git_dir = Path(git(["rev-parse", "--git-dir"], cwd=root))
    common_git_dir = Path(git(["rev-parse", "--git-common-dir"], cwd=root))
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    if not common_git_dir.is_absolute():
        common_git_dir = (root / common_git_dir).resolve()
    return root, git_dir, common_git_dir


def parse_gitmodules(content: str, origin: str) -> list[Submodule]:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise PlanError(f"failed to parse {origin}: {exc}") from exc

    modules: list[Submodule] = []
    seen_paths: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith("submodule "):
            continue
        try:
            path = validate_relative_git_path(
                parser.get(section, "path").strip(),
                f"path for [{section}]",
                origin,
            )
            url = parser.get(section, "url").strip()
        except configparser.Error as exc:
            raise PlanError(
                f"section [{section}] in {origin} is missing required keys: {exc}"
            ) from exc
        name = validate_relative_git_path(
            section[len("submodule ") :].strip().strip('"'),
            f"name for [{section}]",
            origin,
        )
        if path in seen_paths:
            raise PlanError(
                f"duplicate submodule path in {origin}: {path} "
                f"is used by {seen_paths[path]} and {name}"
            )
        seen_paths[path] = name
        modules.append(Submodule(name=name, path=path, url=url))
    return modules


def decode_gitmodules(content: bytes, origin: str) -> list[Submodule]:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError(f"{origin} is not valid UTF-8") from exc
    return parse_gitmodules(decoded, origin)


def capture_worktree_gitmodules(
    root: Path,
    budget: Optional[GitmodulesReadBudget] = None,
) -> tuple[list[Submodule], Optional[FileContentBinding]]:
    budget = budget or GitmodulesReadBudget.start()
    path = root.resolve(strict=True) / ".gitmodules"
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return [], None
        raise PlanError(f"cannot inspect {path} safely: {exc}") from exc
    budget.check_capacity(path_stat.st_size, str(path))
    binding, content = read_bound_regular_file(
        path,
        maximum_bytes=MAX_GITMODULES_FILE_BYTES,
        mode=os.R_OK,
        purpose="top-level .gitmodules",
        retain_content=True,
        deadline=time.monotonic() + budget.remaining_seconds("top-level .gitmodules"),
    )
    if content is None:
        raise PlanError("top-level .gitmodules content binding returned no content")
    budget.retain(len(content), str(path))
    return decode_gitmodules(content, str(path)), binding


def read_worktree_gitmodules(
    root: Path,
    budget: Optional[GitmodulesReadBudget] = None,
) -> list[Submodule]:
    modules, _ = capture_worktree_gitmodules(root, budget)
    return modules


def read_commit_gitmodules(
    source_git_dir: Path,
    work_tree: Path,
    commit: str,
    budget: Optional[GitmodulesReadBudget] = None,
) -> list[Submodule]:
    del work_tree
    budget = budget or GitmodulesReadBudget.start()
    tree_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            "-z",
            commit,
            "--",
            ".gitmodules",
        ],
        stdout_limit=MAX_CHECKOUT_PATH_BYTES + 512,
        timeout_seconds=budget.remaining_seconds(f"{commit}:.gitmodules tree entry"),
    )
    records = bounded_records(
        tree_result.stdout,
        f"{commit}:.gitmodules tree entry",
        maximum_records=1,
    )
    if not records:
        return []
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
    except ValueError as exc:
        raise PlanError(f"{commit}:.gitmodules has an invalid tree entry") from exc
    fields = metadata.split()
    if (
        len(fields) != 3
        or not fields[0].startswith(b"100")
        or fields[1] != b"blob"
        or raw_path != b".gitmodules"
    ):
        raise PlanError(f"{commit}:.gitmodules is not a regular Git blob")
    object_id = os.fsdecode(fields[2])
    if not re.fullmatch(r"[0-9a-f]+", object_id):
        raise PlanError(f"{commit}:.gitmodules returned an invalid object id")
    size_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "cat-file",
            "-s",
            object_id,
        ],
        stdout_limit=64,
        timeout_seconds=budget.remaining_seconds(f"{commit}:.gitmodules size"),
    )
    raw_size = size_result.stdout.strip()
    if not raw_size.isdigit():
        raise PlanError(f"{commit}:.gitmodules returned an invalid blob size")
    blob_size = int(raw_size)
    budget.check_capacity(blob_size, f"{commit}:.gitmodules")
    content_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "cat-file",
            "blob",
            object_id,
        ],
        stdout_limit=blob_size,
        timeout_seconds=budget.remaining_seconds(f"{commit}:.gitmodules content"),
    )
    if len(content_result.stdout) != blob_size:
        raise PlanError(f"{commit}:.gitmodules changed size during bounded read")
    budget.retain(blob_size, f"{commit}:.gitmodules")
    return decode_gitmodules(content_result.stdout, f"{commit}:.gitmodules")


def expected_sha(root: Path, rel_path: str) -> str:
    output = git(["ls-files", "-s", "--", rel_path], cwd=root)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise PlanError(f"{rel_path} is not a gitlink in the current index")
    if len(lines) != 1:
        raise PlanError(
            f"{rel_path} has unresolved index entries; resolve conflicts before syncing"
        )
    fields = lines[0].split()
    if len(fields) < 4 or fields[0] != "160000":
        raise PlanError(f"{rel_path} is not a gitlink in the current index")
    if fields[2] != "0":
        raise PlanError(
            f"{rel_path} has unresolved index stage {fields[2]}; resolve conflicts before syncing"
        )
    return fields[1]


def superproject_index_paths(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    index_result = read_git_bounded(
        [
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
        ],
        stdout_limit=MAX_CHECKOUT_PATH_BYTES + 1,
    )
    index_output = os.fsdecode(index_result.stdout).strip()
    index_path = Path(index_output)
    if not index_path.is_absolute():
        index_path = (root / index_path).absolute()
    shared_result = read_git_bounded(
        [
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--shared-index-path",
        ],
        stdout_limit=MAX_CHECKOUT_PATH_BYTES + 1,
    )
    shared_text = os.fsdecode(shared_result.stdout).strip()
    if not shared_text:
        return (index_path,)
    shared_path = Path(shared_text)
    if not shared_path.is_absolute():
        shared_path = (root / shared_path).absolute()
    if shared_path == index_path:
        raise PlanError("superproject shared index aliases the primary index")
    return index_path, shared_path


def selected_gitlink_rows(
    root: Path,
    selected_paths: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    if not selected_paths:
        raise PlanError("cannot bind an empty selected-gitlink set")
    expected_raw: dict[bytes, str] = {}
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = 0
    for path in selected_paths:
        raw_path = os.fsencode(path)
        if raw_path in expected_raw:
            raise PlanError(f"duplicate selected gitlink path: {path}")
        expected_raw[raw_path] = path
        path_bytes = len(raw_path) + 1
        if path_bytes > MAX_GIT_PATHSPEC_ARG_BYTES:
            raise PlanError(f"selected gitlink pathspec is too large: {path}")
        if batch and (
            batch_bytes + path_bytes > MAX_GIT_PATHSPEC_ARG_BYTES
            or len(batch) >= MAX_GIT_PATHSPECS_PER_BATCH
        ):
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(path)
        batch_bytes += path_bytes
    if batch:
        batches.append(batch)
    if len(batches) > MAX_GIT_PATHSPEC_BATCHES:
        raise PlanError(
            "selected gitlink lookup exceeds the "
            f"{MAX_GIT_PATHSPEC_BATCHES}-batch safety limit"
        )
    rows: dict[str, str] = {}
    retained_bytes = 0
    deadline = time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS
    for batch_paths in batches:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlanError("selected gitlink lookup exceeded its deadline")
        retained_limit = GIT_ENUMERATION_OUTPUT_LIMIT_BYTES - retained_bytes
        if retained_limit <= 0:
            raise PlanError(
                "selected gitlink rows exceed the "
                f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
            )
        result = read_git_bounded(
            [
                "-C",
                str(root),
                "ls-files",
                "-s",
                "--full-name",
                "-z",
                "--",
                *batch_paths,
            ],
            stdout_limit=retained_limit,
            timeout_seconds=remaining,
        )
        retained_bytes += len(result.stdout)
        for record in bounded_records(
            result.stdout,
            "selected superproject gitlink rows",
            maximum_records=len(batch_paths) * 4,
        ):
            try:
                metadata, raw_path = record.split(b"\t", 1)
            except ValueError as exc:
                raise PlanError(
                    "superproject index returned an invalid gitlink row"
                ) from exc
            fields = metadata.split()
            if len(fields) != 3:
                raise PlanError("superproject index returned invalid gitlink metadata")
            mode, raw_object_id, raw_stage = fields
            selected_path = expected_raw.get(raw_path)
            if selected_path is None:
                raise PlanError(
                    "superproject index returned an unrequested gitlink path"
                )
            if selected_path in rows:
                raise PlanError(
                    f"{selected_path} has unresolved index entries; "
                    "resolve conflicts before syncing"
                )
            object_id = os.fsdecode(raw_object_id)
            stage = os.fsdecode(raw_stage)
            if mode != b"160000":
                raise PlanError(
                    f"{selected_path} is not a gitlink in the current index"
                )
            if stage != "0":
                raise PlanError(
                    f"{selected_path} has unresolved index stage {stage}; "
                    "resolve conflicts before syncing"
                )
            if not re.fullmatch(r"[0-9a-f]+", object_id):
                raise PlanError(
                    f"{selected_path} has an invalid gitlink object id in the index"
                )
            rows[selected_path] = object_id
    missing = [path for path in selected_paths if path not in rows]
    if missing:
        raise PlanError(f"{missing[0]} is not a gitlink in the current index")
    return tuple((path, rows[path]) for path in selected_paths)


def capture_superproject_index_receipt(
    root: Path,
    selected_paths: tuple[str, ...],
) -> SuperprojectIndexReceipt:
    bindings = tuple(
        read_bound_regular_file(
            path,
            maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
            mode=os.R_OK,
            purpose="superproject index",
            retain_content=False,
        )[0]
        for path in superproject_index_paths(root)
    )
    selected_rows = selected_gitlink_rows(root, selected_paths)
    for binding in bindings:
        revalidate_file_content_binding(binding)
    if superproject_index_paths(root) != tuple(binding.path for binding in bindings):
        raise PlanError("superproject index path set changed during preflight")
    repeated_rows = selected_gitlink_rows(root, selected_paths)
    if repeated_rows != selected_rows:
        raise PlanError("selected superproject gitlink rows changed during preflight")
    for binding in bindings:
        revalidate_file_content_binding(binding)
    return SuperprojectIndexReceipt(
        index_bindings=bindings,
        selected_gitlinks=selected_rows,
    )


def revalidate_superproject_index_receipt(
    root: Path,
    receipt: SuperprojectIndexReceipt,
) -> None:
    for binding in receipt.index_bindings:
        revalidate_file_content_binding(binding)
    current_paths = superproject_index_paths(root)
    expected_paths = tuple(binding.path for binding in receipt.index_bindings)
    if current_paths != expected_paths:
        raise PlanError("superproject index path set changed after preflight")
    selected_paths = tuple(path for path, _ in receipt.selected_gitlinks)
    current_rows = selected_gitlink_rows(root, selected_paths)
    if current_rows != receipt.selected_gitlinks:
        raise PlanError("selected superproject gitlink rows changed after preflight")
    for binding in receipt.index_bindings:
        revalidate_file_content_binding(binding)


def expected_sha_from_tree(
    source_git_dir: Path, work_tree: Path, treeish: str, rel_path: str
) -> str:
    output = git(
        [*source_object_repo_args(source_git_dir), "ls-tree", treeish, "--", rel_path]
    )
    fields = output.split()
    if len(fields) < 4 or fields[0] != "160000":
        raise PlanError(f"{rel_path} is not a gitlink in {treeish}")
    return fields[2]


def source_git_dir_for(common_git_dir: Path, submodule_name: str) -> Path:
    return contained_child_path(
        common_git_dir / "modules",
        submodule_name,
        f"source gitdir for submodule {submodule_name}",
    )


def nested_source_git_dir_for(parent_source_git_dir: Path, submodule_name: str) -> Path:
    return contained_child_path(
        parent_source_git_dir / "modules",
        submodule_name,
        f"nested source gitdir for submodule {submodule_name}",
    )


def is_valid_git_dir(source_git_dir: Path, work_tree: Path) -> bool:
    if not source_git_dir.exists():
        return False
    result = read_git(
        [*source_object_repo_args(source_git_dir), "rev-parse", "--git-dir"],
        check=False,
    )
    return result.returncode == 0


def ensure_source_repo(
    source_git_dir: Path,
    work_tree: Path,
    submodule: Submodule,
    source_superproject: Optional[Path],
    parent_source_git_dir: Optional[Path],
) -> None:
    if is_valid_git_dir(source_git_dir, work_tree):
        return

    lines = [
        f"source repo is missing or invalid for {submodule.path}",
        f"  url: {submodule.url}",
        f"  source gitdir: {source_git_dir}",
    ]
    if source_superproject and parent_source_git_dir is None:
        fix_command = [
            "git",
            "-C",
            str(source_superproject),
            "submodule",
            "update",
            "--init",
            "--depth",
            "1",
            "--",
            submodule.path,
        ]
        lines.extend(["  fix:", f"    {shell_join(fix_command)}"])
    elif parent_source_git_dir:
        lines.extend(
            [
                f"  parent source gitdir: {parent_source_git_dir}",
                "  fix:",
                "    initialize this nested submodule in the source checkout that owns the parent repo",
            ]
        )
    else:
        lines.extend(
            [
                "  fix:",
                "    provide --source-superproject, or initialize this source repo under "
                "the selected .git/modules tree",
            ]
        )
    raise PlanError("\n".join(lines))


def commit_exists(source_git_dir: Path, work_tree: Path, sha: str) -> bool:
    result = read_git(
        [
            *source_object_repo_args(source_git_dir),
            "cat-file",
            "-e",
            f"{sha}^{{commit}}",
        ],
        check=False,
    )
    return result.returncode == 0


def parse_bound_git_config(
    content: bytes,
    source: Path,
) -> tuple[tuple[str, str], ...]:
    result = read_git_bounded(
        [
            "config",
            "--file",
            "-",
            "--no-includes",
            "--null",
            "--list",
        ],
        input_bytes=content,
        stdout_limit=MAX_SOURCE_CONFIG_BYTES * 2,
    )
    entries: list[tuple[str, str]] = []
    for record in bounded_records(
        result.stdout,
        f"source Git config entries from {source}",
        maximum_records=MAX_CONFIG_ENTRIES,
    ):
        try:
            raw_key, raw_value = record.split(b"\n", 1)
        except ValueError as exc:
            raise PlanError(
                f"source Git config returned an invalid entry\n  path: {source}"
            ) from exc
        key = os.fsdecode(raw_key)
        value = os.fsdecode(raw_value)
        entries.append((key, value))
    return tuple(entries)


def reject_unsafe_fetch_config(
    entries: tuple[tuple[str, str], ...],
    source: Path,
) -> str:
    origin_urls: list[str] = []
    allowed_origin_keys = {"remote.origin.url", "remote.origin.fetch"}
    for key, value in entries:
        lowered = key.casefold()
        if lowered == "remote.origin.url":
            origin_urls.append(value)
            continue
        unsafe = (
            lowered.startswith("include.")
            or lowered.startswith("includeif.")
            or lowered
            in {
                "core.alternaterefscommand",
                "core.sshcommand",
                "core.gitproxy",
                "extensions.worktreeconfig",
                "ssh.variant",
            }
            or lowered.startswith("fetch.bundle")
            or lowered.startswith("transfer.bundle")
            or lowered.startswith("credential.")
            or lowered.startswith("http.")
            or lowered.startswith("url.")
            or lowered.startswith("protocol.")
            or (
                lowered.startswith("remote.origin.")
                and lowered not in allowed_origin_keys
            )
        )
        if unsafe:
            raise PlanError(
                "source Git config contains fetch-executable, credential, proxy, "
                "include, or URL-redirection policy\n"
                f"  path: {source}\n"
                f"  key: {key}"
            )
    if len(origin_urls) != 1:
        raise PlanError(
            "source Git config must contain exactly one remote.origin.url\n"
            f"  path: {source}\n"
            f"  count: {len(origin_urls)}"
        )
    return origin_urls[0]


def source_object_format(
    entries: tuple[tuple[str, str], ...],
    source: Path,
) -> str:
    values = [
        value.casefold()
        for key, value in entries
        if key.casefold() == "extensions.objectformat"
    ]
    if len(values) > 1:
        raise PlanError(
            "source Git config contains duplicate object-format declarations\n"
            f"  path: {source}"
        )
    object_format = values[0] if values else "sha1"
    if object_format not in {"sha1", "sha256"}:
        raise PlanError(
            "source Git config contains an unsupported object format\n"
            f"  path: {source}\n"
            f"  format: {object_format}"
        )
    return object_format


def require_absent_entry_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    purpose: str,
) -> None:
    try:
        os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except PermissionError as exc:
        raise PlanError(f"cannot verify absent {purpose}: {display_path}") from exc
    except OSError as exc:
        raise PlanError(
            f"cannot verify absent {purpose}: {display_path}\n  error: {exc}"
        ) from exc
    raise PlanError(f"{purpose} appeared after preflight\n  path: {display_path}")


def require_matching_file_binding(
    expected: FileContentBinding,
    observed: FileContentBinding,
    description: str,
) -> None:
    if observed.fingerprint != expected.fingerprint:
        raise PlanError(
            f"{description} object or access policy changed after preflight"
        )
    if (
        observed.size != expected.size
        or observed.content_sha256 != expected.content_sha256
    ):
        raise PlanError(f"{description} content changed after preflight")


def access_binding_for_path(
    bindings: tuple[AccessBinding, ...],
    path: Path,
    purpose: str,
) -> AccessBinding:
    matches = [binding for binding in bindings if binding.path == path]
    if len(matches) != 1:
        raise PlanError(
            f"{purpose} lacks one exact access binding\n"
            f"  path: {path}\n"
            f"  matches: {len(matches)}"
        )
    return matches[0]


def fingerprint_recovery_payload(
    fingerprint: FsFingerprint,
) -> dict[str, object]:
    return {
        "device": fingerprint.device,
        "group": fingerprint.group,
        "inode": fingerprint.inode,
        "kind": fingerprint.kind,
        "owner": fingerprint.owner,
        "permissions": fingerprint.permissions,
    }


def file_binding_recovery_payload(
    binding: Optional[FileContentBinding],
) -> dict[str, object]:
    if binding is None:
        return {"state": "absent"}
    return {
        "content_sha256": binding.content_sha256,
        "fingerprint": fingerprint_recovery_payload(binding.fingerprint),
        "size": binding.size,
        "state": "present",
    }


def inspect_shallow_entry_for_recovery(
    directory_descriptor: int,
    name: str,
    display_path: Path,
) -> dict[str, object]:
    try:
        path_stat = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "state": "unavailable",
        }
    fingerprint = fingerprint_from_stat(path_stat)
    payload: dict[str, object] = {
        "fingerprint": fingerprint_recovery_payload(fingerprint),
        "size": path_stat.st_size,
        "state": "present",
    }
    if fingerprint.kind != stat.S_IFREG:
        return payload
    descriptor = -1
    try:
        descriptor, binding, _ = open_bound_regular_file_at(
            directory_descriptor,
            name,
            display_path,
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK,
            purpose=f"recovery inspection for {display_path.name}",
            retain_content=False,
        )
        payload["content_sha256"] = binding.content_sha256
    except PlanError as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload


def shallow_recovery_identity(
    receipt: TransportReceipt,
    source_directory_descriptor: int,
    state: str,
    detail: str,
) -> str:
    descriptor_fingerprint = fingerprint_from_stat(
        os.fstat(source_directory_descriptor)
    )
    try:
        path_fingerprint = fingerprint_recovery_payload(
            filesystem_fingerprint(receipt.source_shallow_parent_binding.path)
        )
    except PlanError as exc:
        path_fingerprint = {
            "error": f"{type(exc).__name__}: {exc}",
            "state": "unavailable",
        }
    payload = {
        "detail": detail,
        "expected_shallow": file_binding_recovery_payload(
            receipt.source_shallow_binding
        ),
        "profile": "source-shallow-cas-v1",
        "shallow": inspect_shallow_entry_for_recovery(
            source_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            receipt.source_shallow_path,
        ),
        "shallow_lock": inspect_shallow_entry_for_recovery(
            source_directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
        ),
        "source_git_dir": str(receipt.source_shallow_parent_binding.path),
        "source_git_dir_descriptor": fingerprint_recovery_payload(
            descriptor_fingerprint
        ),
        "source_git_dir_path": path_fingerprint,
        "state": state,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def source_shallow_recovery_error(
    receipt: TransportReceipt,
    source_directory_descriptor: int,
    state: str,
    detail: str,
) -> PlanError:
    return PlanError(
        f"source shallow CAS did not reach a clean terminal state: {detail}\n"
        f"  recovery_identity: "
        f"{shallow_recovery_identity(receipt, source_directory_descriptor, state, detail)}"
    )


def open_revalidated_source_shallow_at(
    receipt: TransportReceipt,
    source_directory_descriptor: int,
) -> int:
    revalidate_directory_descriptor(
        receipt.source_shallow_parent_binding,
        source_directory_descriptor,
    )
    if receipt.source_shallow_binding is None:
        require_absent_entry_at(
            source_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            receipt.source_shallow_path,
            "source shallow boundary",
        )
        return -1
    descriptor = -1
    try:
        descriptor, observed, _ = open_bound_regular_file_at(
            source_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            receipt.source_shallow_path,
            maximum_bytes=receipt.source_shallow_binding.maximum_bytes,
            mode=receipt.source_shallow_binding.mode,
            purpose=receipt.source_shallow_binding.purpose,
            retain_content=False,
        )
        require_matching_file_binding(
            receipt.source_shallow_binding,
            observed,
            "source shallow boundary",
        )
        revalidate_directory_descriptor(
            receipt.source_shallow_parent_binding,
            source_directory_descriptor,
        )
        return descriptor
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def capture_source_shallow_state(
    source_git_dir: Path,
    submodule_path: str,
) -> tuple[
    Path,
    AccessBinding,
    Optional[FileContentBinding],
    Optional[bytes],
]:
    shallow_path = source_git_dir / SOURCE_SHALLOW_NAME
    parent_binding = capture_typed_access(
        source_git_dir,
        os.R_OK | os.W_OK | os.X_OK,
        f"source shallow-file parent for {submodule_path}",
        stat.S_IFDIR,
    )
    shallow_binding: Optional[FileContentBinding] = None
    shallow_content: Optional[bytes] = None
    source_directory_descriptor = open_directory_descriptor(
        source_git_dir,
        f"source shallow-file parent for {submodule_path}",
    )
    shallow_descriptor = -1
    try:
        revalidate_directory_descriptor(parent_binding, source_directory_descriptor)
        try:
            os.stat(
                SOURCE_SHALLOW_NAME,
                dir_fd=source_directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            require_absent_entry_at(
                source_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                shallow_path,
                f"source shallow boundary for {submodule_path}",
            )
        except OSError as exc:
            raise PlanError(
                f"cannot inspect source shallow boundary for {submodule_path}\n"
                f"  path: {shallow_path}\n"
                f"  error: {exc}"
            ) from exc
        else:
            (
                shallow_descriptor,
                shallow_binding,
                shallow_content,
            ) = open_bound_regular_file_at(
                source_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                shallow_path,
                maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
                mode=os.R_OK | os.W_OK,
                purpose=f"source shallow boundary for {submodule_path}",
                retain_content=True,
            )
            observed, _ = bind_regular_file_descriptor_at(
                shallow_descriptor,
                source_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                shallow_path,
                maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
                mode=os.R_OK | os.W_OK,
                purpose=f"source shallow boundary for {submodule_path}",
                retain_content=False,
            )
            require_matching_file_binding(
                shallow_binding,
                observed,
                "source shallow boundary",
            )
        revalidate_directory_descriptor(parent_binding, source_directory_descriptor)
    finally:
        if shallow_descriptor >= 0:
            os.close(shallow_descriptor)
        os.close(source_directory_descriptor)
    return shallow_path, parent_binding, shallow_binding, shallow_content


def revalidate_source_shallow_state(receipt: TransportReceipt) -> None:
    source_directory_descriptor = open_directory_descriptor(
        receipt.source_shallow_parent_binding.path,
        "source shallow-file parent",
    )
    shallow_descriptor = -1
    try:
        shallow_descriptor = open_revalidated_source_shallow_at(
            receipt,
            source_directory_descriptor,
        )
    finally:
        if shallow_descriptor >= 0:
            os.close(shallow_descriptor)
        os.close(source_directory_descriptor)


def _cleanup_owned_source_shallow_lock(
    source_directory_descriptor: int,
    lock_descriptor: int,
    lock_fingerprint: FsFingerprint,
) -> None:
    descriptor_fingerprint = fingerprint_from_stat(os.fstat(lock_descriptor))
    try:
        path_fingerprint = fingerprint_from_stat(
            os.stat(
                SOURCE_SHALLOW_LOCK_NAME,
                dir_fd=source_directory_descriptor,
                follow_symlinks=False,
            )
        )
    except FileNotFoundError as exc:
        raise PlanError("owned source shallow lock disappeared before cleanup") from exc
    if (
        descriptor_fingerprint != lock_fingerprint
        or path_fingerprint != lock_fingerprint
    ):
        raise PlanError(
            "source shallow lock ownership changed before cleanup; "
            "the stale fence was retained"
        )
    os.unlink(
        SOURCE_SHALLOW_LOCK_NAME,
        dir_fd=source_directory_descriptor,
    )
    os.fsync(source_directory_descriptor)


def cleanup_owned_source_shallow_lock(
    source_directory_descriptor: int,
    lock_descriptor: int,
    lock_fingerprint: FsFingerprint,
) -> None:
    try:
        _cleanup_owned_source_shallow_lock(
            source_directory_descriptor,
            lock_descriptor,
            lock_fingerprint,
        )
    except OSError as exc:
        raise PlanError(
            "cannot safely clean the owned source shallow lock; "
            f"the stale fence was retained: {exc}"
        ) from exc


def rollback_source_shallow_exchange(
    receipt: TransportReceipt,
    source_directory_descriptor: int,
    new_shallow_descriptor: int,
    new_shallow_binding: FileContentBinding,
) -> tuple[str, Optional[str]]:
    try:
        descriptor_atomic_rename_exchange(
            source_directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            SOURCE_SHALLOW_NAME,
        )
    except PlanError as exc:
        return "rollback-failed-fence-retained", str(exc)
    try:
        restored_lock, _ = bind_regular_file_descriptor_at(
            new_shallow_descriptor,
            source_directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="rolled-back source shallow stale fence",
            retain_content=False,
        )
        require_matching_file_binding(
            new_shallow_binding,
            restored_lock,
            "rolled-back source shallow stale fence",
        )
        revalidate_directory_descriptor(
            receipt.source_shallow_parent_binding,
            source_directory_descriptor,
        )
        os.fsync(source_directory_descriptor)
    except (OSError, PlanError) as exc:
        return "rollback-unverified-fence-retained", str(exc)
    return "rolled-back-fence-retained", None


def rollback_absent_source_shallow_publish(
    receipt: TransportReceipt,
    source_directory_descriptor: int,
    new_shallow_descriptor: int,
    new_shallow_binding: FileContentBinding,
) -> tuple[str, Optional[str]]:
    try:
        descriptor_atomic_rename_noreplace(
            source_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            SOURCE_SHALLOW_LOCK_NAME,
        )
    except PlanError as exc:
        return "rollback-failed", str(exc)
    try:
        restored_lock, _ = bind_regular_file_descriptor_at(
            new_shallow_descriptor,
            source_directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="rolled-back source shallow stale fence",
            retain_content=False,
        )
        require_matching_file_binding(
            new_shallow_binding,
            restored_lock,
            "rolled-back source shallow stale fence",
        )
        revalidate_directory_descriptor(
            receipt.source_shallow_parent_binding,
            source_directory_descriptor,
        )
        os.fsync(source_directory_descriptor)
    except (OSError, PlanError) as exc:
        return "rollback-unverified-fence-retained", str(exc)
    return "rolled-back-fence-retained", None


def install_post_fetch_shallow_state(receipt: TransportReceipt) -> None:
    private_shallow_path = receipt.fetch_git_dir / SOURCE_SHALLOW_NAME
    private_directory_binding = access_binding_for_path(
        receipt.fetch_access_bindings,
        receipt.fetch_git_dir,
        "post-fetch private shallow-file parent",
    )
    private_directory_descriptor = open_directory_descriptor(
        receipt.fetch_git_dir,
        "post-fetch private shallow-file parent",
    )
    private_descriptor = -1
    source_directory_descriptor = -1
    lock_descriptor = -1
    expected_shallow_descriptor = -1
    lock_binding: Optional[FileContentBinding] = None
    lock_fingerprint: Optional[FsFingerprint] = None
    cleanup_owned_lock = False
    preserve_fence = False
    try:
        revalidate_directory_descriptor(
            private_directory_binding,
            private_directory_descriptor,
        )
        (
            private_descriptor,
            private_binding,
            private_content,
        ) = open_bound_regular_file_at(
            private_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            private_shallow_path,
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="post-fetch private shallow boundary",
            retain_content=True,
        )
        if private_content is None:
            raise PlanError("post-fetch private shallow boundary returned no content")
        revalidate_directory_descriptor(
            private_directory_binding,
            private_directory_descriptor,
        )

        source_directory_descriptor = open_directory_descriptor(
            receipt.source_shallow_parent_binding.path,
            "source shallow-file parent",
        )
        revalidate_directory_descriptor(
            receipt.source_shallow_parent_binding,
            source_directory_descriptor,
        )
        mode = (
            receipt.source_shallow_binding.fingerprint.permissions
            if receipt.source_shallow_binding is not None
            else 0o600
        )
        try:
            lock_descriptor = os.open(
                SOURCE_SHALLOW_LOCK_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | os.O_NOFOLLOW,
                mode,
                dir_fd=source_directory_descriptor,
            )
        except FileExistsError as exc:
            raise PlanError(
                "source shallow lock already exists; refusing to overlap another "
                "Git shallow-boundary writer\n"
                f"  path: "
                f"{receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME)}"
            ) from exc
        except OSError as exc:
            raise PlanError(
                "cannot create descriptor-relative source shallow lock\n"
                f"  path: "
                f"{receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME)}\n"
                f"  error: {exc}"
            ) from exc
        lock_fingerprint = fingerprint_from_stat(os.fstat(lock_descriptor))
        cleanup_owned_lock = True
        os.fchmod(lock_descriptor, mode)
        lock_fingerprint = fingerprint_from_stat(os.fstat(lock_descriptor))
        pending = memoryview(private_content)
        while pending:
            written = os.write(lock_descriptor, pending)
            if written <= 0:
                raise PlanError(
                    "cannot write descriptor-relative source shallow lock file"
                )
            pending = pending[written:]
        os.fsync(lock_descriptor)
        lock_binding, _ = bind_regular_file_descriptor_at(
            lock_descriptor,
            source_directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="pending source shallow boundary",
            retain_content=False,
        )
        os.fsync(source_directory_descriptor)

        private_observed, _ = bind_regular_file_descriptor_at(
            private_descriptor,
            private_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            private_shallow_path,
            maximum_bytes=private_binding.maximum_bytes,
            mode=private_binding.mode,
            purpose=private_binding.purpose,
            retain_content=False,
        )
        require_matching_file_binding(
            private_binding,
            private_observed,
            "post-fetch private shallow boundary",
        )
        expected_shallow_descriptor = open_revalidated_source_shallow_at(
            receipt,
            source_directory_descriptor,
        )

        if receipt.source_shallow_binding is None:
            try:
                descriptor_atomic_rename_noreplace(
                    source_directory_descriptor,
                    SOURCE_SHALLOW_LOCK_NAME,
                    SOURCE_SHALLOW_NAME,
                )
            except AtomicRenameError as exc:
                if exc.error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                    cleanup_owned_lock = False
                    preserve_fence = True
                    raise source_shallow_recovery_error(
                        receipt,
                        source_directory_descriptor,
                        "cas-conflict-fence-retained",
                        "source shallow boundary appeared before no-replace publish",
                    ) from exc
                raise
            cleanup_owned_lock = False
            try:
                installed, _ = bind_regular_file_descriptor_at(
                    lock_descriptor,
                    source_directory_descriptor,
                    SOURCE_SHALLOW_NAME,
                    receipt.source_shallow_path,
                    maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
                    mode=os.R_OK | os.W_OK,
                    purpose="installed source shallow boundary",
                    retain_content=False,
                )
                require_matching_file_binding(
                    lock_binding,
                    installed,
                    "installed source shallow boundary",
                )
                if (
                    installed.size != private_binding.size
                    or installed.content_sha256 != private_binding.content_sha256
                ):
                    raise PlanError(
                        "installed source shallow boundary does not match the "
                        "fetched receipt"
                    )
                revalidate_directory_descriptor(
                    receipt.source_shallow_parent_binding,
                    source_directory_descriptor,
                )
                os.fsync(source_directory_descriptor)
            except PlanError as exc:
                rollback_state, rollback_error = rollback_absent_source_shallow_publish(
                    receipt,
                    source_directory_descriptor,
                    lock_descriptor,
                    lock_binding,
                )
                preserve_fence = rollback_state.endswith("fence-retained")
                detail = str(exc)
                if rollback_error is not None:
                    detail += f"; rollback: {rollback_error}"
                raise source_shallow_recovery_error(
                    receipt,
                    source_directory_descriptor,
                    rollback_state,
                    detail,
                ) from exc
            return

        try:
            descriptor_atomic_rename_exchange(
                source_directory_descriptor,
                SOURCE_SHALLOW_LOCK_NAME,
                SOURCE_SHALLOW_NAME,
            )
        except AtomicRenameError as exc:
            if exc.error_number not in ATOMIC_RENAME_UNSUPPORTED_ERRNOS:
                cleanup_owned_lock = False
                preserve_fence = True
                raise source_shallow_recovery_error(
                    receipt,
                    source_directory_descriptor,
                    "exchange-failed-fence-retained",
                    str(exc),
                ) from exc
            raise
        cleanup_owned_lock = False
        try:
            observed_old, _ = bind_regular_file_descriptor_at(
                expected_shallow_descriptor,
                source_directory_descriptor,
                SOURCE_SHALLOW_LOCK_NAME,
                receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
                maximum_bytes=receipt.source_shallow_binding.maximum_bytes,
                mode=receipt.source_shallow_binding.mode,
                purpose="exchanged source shallow receipt boundary",
                retain_content=False,
            )
            require_matching_file_binding(
                receipt.source_shallow_binding,
                observed_old,
                "exchanged source shallow receipt boundary",
            )
            installed, _ = bind_regular_file_descriptor_at(
                lock_descriptor,
                source_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                receipt.source_shallow_path,
                maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
                mode=os.R_OK | os.W_OK,
                purpose="installed source shallow boundary",
                retain_content=False,
            )
            require_matching_file_binding(
                lock_binding,
                installed,
                "installed source shallow boundary",
            )
            if (
                installed.size != private_binding.size
                or installed.content_sha256 != private_binding.content_sha256
            ):
                raise PlanError(
                    "installed source shallow boundary does not match the fetched "
                    "receipt"
                )
            revalidate_directory_descriptor(
                receipt.source_shallow_parent_binding,
                source_directory_descriptor,
            )
        except PlanError as exc:
            rollback_state, rollback_error = rollback_source_shallow_exchange(
                receipt,
                source_directory_descriptor,
                lock_descriptor,
                lock_binding,
            )
            preserve_fence = True
            detail = str(exc)
            if rollback_error is not None:
                detail += f"; rollback: {rollback_error}"
            raise source_shallow_recovery_error(
                receipt,
                source_directory_descriptor,
                rollback_state,
                detail,
            ) from exc

        try:
            old_path_fingerprint = fingerprint_from_stat(
                os.stat(
                    SOURCE_SHALLOW_LOCK_NAME,
                    dir_fd=source_directory_descriptor,
                    follow_symlinks=False,
                )
            )
            if old_path_fingerprint != receipt.source_shallow_binding.fingerprint:
                raise PlanError(
                    "exchanged source shallow lock identity changed before unlink"
                )
            os.unlink(
                SOURCE_SHALLOW_LOCK_NAME,
                dir_fd=source_directory_descriptor,
            )
            os.fsync(source_directory_descriptor)
        except (OSError, PlanError) as exc:
            try:
                os.stat(
                    SOURCE_SHALLOW_LOCK_NAME,
                    dir_fd=source_directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                rollback_state = "commit-durability-unverified"
                rollback_error = "exchanged old shallow was already unlinked"
            else:
                rollback_state, rollback_error = rollback_source_shallow_exchange(
                    receipt,
                    source_directory_descriptor,
                    lock_descriptor,
                    lock_binding,
                )
                preserve_fence = True
            detail = str(exc)
            if rollback_error is not None:
                detail += f"; rollback: {rollback_error}"
            raise source_shallow_recovery_error(
                receipt,
                source_directory_descriptor,
                rollback_state,
                detail,
            ) from exc
    except Exception as exc:
        if (
            cleanup_owned_lock
            and not preserve_fence
            and lock_fingerprint is not None
            and source_directory_descriptor >= 0
            and lock_descriptor >= 0
        ):
            try:
                cleanup_owned_source_shallow_lock(
                    source_directory_descriptor,
                    lock_descriptor,
                    lock_fingerprint,
                )
            except PlanError as cleanup_exc:
                raise source_shallow_recovery_error(
                    receipt,
                    source_directory_descriptor,
                    "cleanup-unverified-fence-retained",
                    f"{exc}; cleanup: {cleanup_exc}",
                ) from exc
        if isinstance(exc, OSError):
            raise PlanError(
                f"source shallow installation failed before a clean terminal state: "
                f"{exc}"
            ) from exc
        raise
    finally:
        if expected_shallow_descriptor >= 0:
            os.close(expected_shallow_descriptor)
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if source_directory_descriptor >= 0:
            os.close(source_directory_descriptor)
        if private_descriptor >= 0:
            os.close(private_descriptor)
        os.close(private_directory_descriptor)


def write_owner_private_file(path: Path, content: bytes, purpose: str) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise PlanError(f"cannot write {purpose}: {path}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise PlanError(f"cannot create {purpose}: {path}\n  error: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def capture_owner_private_directory(path: Path, purpose: str) -> AccessBinding:
    binding = capture_typed_access(
        path,
        os.R_OK | os.W_OK | os.X_OK,
        purpose,
        stat.S_IFDIR,
    )
    if binding.fingerprint.owner != os.geteuid() or (
        binding.fingerprint.permissions & 0o077
    ):
        raise PlanError(
            f"{purpose} is not owner-private\n"
            f"  path: {path}\n"
            f"  mode: {binding.fingerprint.permissions:#o}"
        )
    return binding


class OwnerPrivateTemporaryDirectory:
    def __init__(self, prefix: str) -> None:
        self.name = tempfile.mkdtemp(prefix=prefix)
        self._path = Path(self.name)
        self._fingerprint = filesystem_fingerprint(self._path)
        self._active = True

    def cleanup(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            current = filesystem_fingerprint(self._path)
        except PlanError:
            return
        if current != self._fingerprint:
            # Fail safe on replacement: never recursively remove an object that
            # is no longer the owner-private directory created by this guard.
            return
        shutil.rmtree(self._path)

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass


def capture_fetch_control_gitdir(
    object_format: str,
    submodule_path: str,
    initial_shallow_content: Optional[bytes],
) -> tuple[
    object,
    Path,
    tuple[AccessBinding, ...],
    tuple[FileContentBinding, ...],
]:
    guard = OwnerPrivateTemporaryDirectory(prefix="submodule-worktree-fetch.")
    fetch_git_dir = Path(guard.name)
    try:
        objects_dir = fetch_git_dir / "objects"
        refs_dir = fetch_git_dir / "refs"
        heads_dir = refs_dir / "heads"
        for directory in (objects_dir, refs_dir, heads_dir):
            os.mkdir(directory, mode=0o700)

        repository_version = "1" if object_format == "sha256" else "0"
        config_content = (
            f"[core]\n\trepositoryformatversion = {repository_version}\n\tbare = true\n"
        )
        if object_format == "sha256":
            config_content += "[extensions]\n\tobjectformat = sha256\n"
        config_path = fetch_git_dir / "config"
        head_path = fetch_git_dir / "HEAD"
        write_owner_private_file(
            config_path,
            config_content.encode("ascii"),
            f"isolated fetch config for {submodule_path}",
        )
        write_owner_private_file(
            head_path,
            b"ref: refs/heads/fetch-isolated\n",
            f"isolated fetch HEAD for {submodule_path}",
        )
        private_shallow_path = fetch_git_dir / "shallow"
        if initial_shallow_content is not None:
            write_owner_private_file(
                private_shallow_path,
                initial_shallow_content,
                f"isolated fetch shallow boundary for {submodule_path}",
            )

        access_bindings = tuple(
            capture_owner_private_directory(path, purpose)
            for path, purpose in (
                (
                    fetch_git_dir,
                    f"isolated fetch gitdir for {submodule_path}",
                ),
                (
                    objects_dir,
                    f"isolated fetch placeholder object directory for {submodule_path}",
                ),
                (
                    refs_dir,
                    f"isolated fetch refs directory for {submodule_path}",
                ),
                (
                    heads_dir,
                    f"isolated fetch heads directory for {submodule_path}",
                ),
            )
        )
        file_specs = [
            (
                config_path,
                f"isolated fetch config for {submodule_path}",
            ),
            (
                head_path,
                f"isolated fetch HEAD for {submodule_path}",
            ),
        ]
        if initial_shallow_content is not None:
            file_specs.append(
                (
                    private_shallow_path,
                    f"isolated fetch shallow boundary for {submodule_path}",
                )
            )
        file_bindings = tuple(
            read_bound_regular_file(
                path,
                maximum_bytes=MAX_SOURCE_CONFIG_BYTES,
                mode=os.R_OK,
                purpose=purpose,
                retain_content=False,
            )[0]
            for path, purpose in file_specs
        )
        return guard, fetch_git_dir, access_bindings, file_bindings
    except Exception:
        guard.cleanup()
        raise


def transport_uses_ssh(url: str) -> bool:
    scheme_match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", url)
    if scheme_match:
        return scheme_match.group(1).casefold() == "ssh"
    return bool(re.fullmatch(r"(?:[^@/:]+@)?[^/:]+:.+", url))


def validate_approved_fetch_url(url: str, submodule_path: str) -> None:
    if not url or "\x00" in url or "\n" in url or "\r" in url:
        raise PlanError(f"submodule {submodule_path} has an invalid approved fetch URL")
    if url.startswith("-") or "::" in url:
        raise PlanError(
            f"submodule {submodule_path} uses an unsupported fetch transport: {url}"
        )
    if Path(url).is_absolute():
        return
    scheme_match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", url)
    if scheme_match:
        if scheme_match.group(1).casefold() not in {
            "file",
            "git",
            "http",
            "https",
            "ssh",
        }:
            raise PlanError(
                f"submodule {submodule_path} uses an unsupported fetch URL scheme: "
                f"{scheme_match.group(1)}"
            )
        return
    if transport_uses_ssh(url):
        return
    raise PlanError(
        f"submodule {submodule_path} uses a relative or ambiguous fetch URL\n"
        f"  url: {url}\n"
        "  bind the source repository and .gitmodules to one exact absolute or "
        "standard-protocol URL before using --fetch-missing"
    )


def capture_transport_receipt(
    source_git_dir: Path,
    submodule: Submodule,
) -> TransportReceipt:
    config_path = source_git_dir / "config"
    config_binding, config_content = read_bound_regular_file(
        config_path,
        maximum_bytes=MAX_SOURCE_CONFIG_BYTES,
        mode=os.R_OK,
        purpose=f"source Git config for {submodule.path}",
        retain_content=True,
    )
    if config_content is None:
        raise PlanError("source Git config content binding returned no content")
    entries = parse_bound_git_config(config_content, config_path)
    origin_url = reject_unsafe_fetch_config(entries, config_path)
    object_format = source_object_format(entries, config_path)
    validate_approved_fetch_url(submodule.url, submodule.path)
    if origin_url != submodule.url:
        raise PlanError(
            "source remote.origin.url does not match the task-approved "
            ".gitmodules URL\n"
            f"  submodule: {submodule.path}\n"
            f"  .gitmodules: {submodule.url}\n"
            f"  remote.origin.url: {origin_url}"
        )
    ssh_binding: Optional[FileContentBinding] = None
    ssh_command: Optional[str] = None
    if transport_uses_ssh(origin_url):
        candidate = shutil.which("ssh")
        if not candidate:
            raise PlanError(
                f"cannot resolve SSH for the approved fetch transport: {origin_url}"
            )
        ssh_path = Path(candidate).resolve(strict=True)
        ssh_binding, _ = read_bound_regular_file(
            ssh_path,
            maximum_bytes=MAX_TRANSPORT_EXECUTABLE_BYTES,
            mode=os.R_OK | os.X_OK,
            purpose=f"SSH executable for {submodule.path}",
            retain_content=False,
        )
        ssh_command = " ".join(
            [
                shlex.quote(str(ssh_path)),
                "-F",
                shlex.quote(os.devnull),
                "-o",
                "CanonicalizeHostname=no",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ProxyCommand=none",
                "-o",
                "ProxyJump=none",
            ]
        )
    source_object_directory = source_git_dir / "objects"
    source_object_bindings = [
        capture_typed_access(
            source_object_directory,
            os.R_OK | os.W_OK | os.X_OK,
            f"authorized fetch object database for {submodule.path}",
            stat.S_IFDIR,
        )
    ]
    source_pack_directory = source_object_directory / "pack"
    if path_entry_exists(source_pack_directory):
        source_object_bindings.append(
            capture_typed_access(
                source_pack_directory,
                os.R_OK | os.W_OK | os.X_OK,
                f"authorized fetch pack directory for {submodule.path}",
                stat.S_IFDIR,
            )
        )
    (
        source_shallow_path,
        source_shallow_parent_binding,
        source_shallow_binding,
        source_shallow_content,
    ) = capture_source_shallow_state(source_git_dir, submodule.path)
    (
        fetch_guard,
        fetch_git_dir,
        private_access_bindings,
        fetch_file_bindings,
    ) = capture_fetch_control_gitdir(
        object_format,
        submodule.path,
        source_shallow_content,
    )
    environment = git_environment()
    environment["GIT_OBJECT_DIRECTORY"] = str(source_object_directory)
    revalidate_file_content_binding(config_binding)
    return TransportReceipt(
        config_binding=config_binding,
        approved_url=submodule.url,
        origin_url=origin_url,
        ssh_executable_binding=ssh_binding,
        ssh_command=ssh_command,
        source_object_directory=source_object_directory,
        source_shallow_path=source_shallow_path,
        source_shallow_parent_binding=source_shallow_parent_binding,
        source_shallow_binding=source_shallow_binding,
        fetch_git_dir=fetch_git_dir,
        fetch_access_bindings=(
            *source_object_bindings,
            *private_access_bindings,
        ),
        fetch_file_bindings=fetch_file_bindings,
        git_environment=tuple(sorted(environment.items())),
        fetch_guard=fetch_guard,
    )


def validate_frozen_git_environment(
    environment_items: tuple[tuple[str, str], ...],
    expected_object_directory: Path,
) -> None:
    environment = dict(environment_items)
    if len(environment) != len(environment_items):
        raise PlanError("fetch transport environment contains duplicate keys")
    allowed = (
        set(GIT_ENV_PASSTHROUGH)
        | set(SAFE_GIT_ENV)
        | {
            "GIT_OBJECT_DIRECTORY",
        }
    )
    unexpected = set(environment) - allowed
    if unexpected:
        raise PlanError(
            "fetch transport environment contains unapproved keys: "
            + ", ".join(sorted(unexpected))
        )
    for key, expected in SAFE_GIT_ENV.items():
        if environment.get(key) != expected:
            raise PlanError(
                f"fetch transport environment changed required policy: {key}"
            )
    if environment.get("GIT_OBJECT_DIRECTORY") != str(expected_object_directory):
        raise PlanError(
            "fetch transport environment changed the bound source object directory"
        )
    if not expected_object_directory.is_absolute():
        raise PlanError("fetch transport object directory is not absolute")


def revalidate_transport_receipt(
    receipt: TransportReceipt,
    submodule: Submodule,
) -> None:
    if (
        receipt.approved_url != submodule.url
        or receipt.origin_url != receipt.approved_url
    ):
        raise PlanError(f"fetch transport receipt no longer matches {submodule.path}")
    revalidate_file_content_binding(receipt.config_binding)
    if receipt.ssh_executable_binding is not None:
        revalidate_file_content_binding(receipt.ssh_executable_binding)
    revalidate_source_shallow_state(receipt)
    for binding in receipt.fetch_access_bindings:
        revalidate_access(binding)
    for binding in receipt.fetch_file_bindings:
        revalidate_file_content_binding(binding)
    validate_frozen_git_environment(
        receipt.git_environment,
        receipt.source_object_directory,
    )


def transport_fetch_command(
    source_git_dir: Path,
    receipt: TransportReceipt,
    sha: str,
    depth: int,
) -> list[str]:
    if source_git_dir != receipt.config_binding.path.parent:
        raise PlanError("fetch transport receipt does not match the source gitdir")
    config: list[str] = [
        "-c",
        "http.proxy=",
        "-c",
        "http.extraHeader=",
        "-c",
        "http.followRedirects=false",
        "-c",
        "core.gitProxy=",
    ]
    if receipt.ssh_command is not None:
        config.extend(["-c", f"core.sshCommand={receipt.ssh_command}"])
    return [
        "git",
        *config,
        f"--git-dir={receipt.fetch_git_dir}",
        "fetch",
        "--depth",
        str(depth),
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-auto-maintenance",
        "--no-write-commit-graph",
        "--",
        receipt.origin_url,
        sha,
    ]


def fetch_missing_commit(
    source_git_dir: Path,
    work_tree: Path,
    submodule: Submodule,
    sha: str,
    depth: int,
    dry_run: bool,
    transport_receipt: Optional[TransportReceipt] = None,
    fetch_missing: bool = False,
) -> bool:
    if commit_exists(source_git_dir, work_tree, sha):
        return True
    receipt = transport_receipt
    if receipt is None and fetch_missing:
        raise PlanError(
            f"authorized fetch for {submodule.path} lacks a bound transport receipt"
        )
    command = (
        transport_fetch_command(source_git_dir, receipt, sha, depth)
        if receipt is not None
        else [
            "git",
            *source_object_repo_args(source_git_dir),
            "fetch",
            "--depth",
            str(depth),
            "--",
            submodule.url,
            sha,
        ]
    )
    if not fetch_missing:
        raise PlanError(
            "\n".join(
                [
                    f"target commit is missing for {submodule.path}",
                    f"  url: {submodule.url}",
                    f"  sha: {sha}",
                    f"  source gitdir: {source_git_dir}",
                    "  network fetch is disabled by default",
                    "  fix:",
                    "    fetch the commit manually, or pass --fetch-missing only when the task "
                    "explicitly authorizes fetching missing commits",
                    f"    planned command: {shell_join(command)}",
                ]
            )
        )
    if dry_run:
        print(f"would fetch missing commit for {submodule.path}: {shell_join(command)}")
        return False
    if receipt is None:
        raise PlanError(
            f"authorized fetch for {submodule.path} lacks a bound transport receipt"
        )
    revalidate_transport_receipt(receipt, submodule)
    print(
        f"fetch missing commit for {submodule.path}: {shell_join(command)}", flush=True
    )
    bounded_result = run_bounded_bytes(
        command,
        check=False,
        timeout_seconds=GIT_ENUMERATION_TIMEOUT_SECONDS,
        stdout_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
        stderr_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
        fixed_env=dict(receipt.git_environment),
    )
    result = subprocess.CompletedProcess(
        args=bounded_result.args,
        returncode=bounded_result.returncode,
        stdout=os.fsdecode(bounded_result.stdout),
        stderr=os.fsdecode(bounded_result.stderr),
    )
    if result.returncode == 0 and commit_exists(source_git_dir, work_tree, sha):
        install_post_fetch_shallow_state(receipt)
        return True
    stderr = (result.stderr or "").strip()
    branch_fetch_command = transport_fetch_command(
        source_git_dir,
        receipt,
        "<branch-or-tag>",
        100,
    )
    raise PlanError(
        "\n".join(
            [
                f"failed to shallow-fetch target commit for {submodule.path}",
                f"  url: {submodule.url}",
                f"  sha: {sha}",
                f"  source gitdir: {source_git_dir}",
                f"  command: {shell_join(command)}",
                f"  error: {stderr or 'target commit is still missing after fetch'}",
                "  fixes:",
                "    - check VPN/SSH/auth, then rerun this script",
                "    - if the server rejects raw SHA fetch, fetch a containing branch/tag manually, then rerun:",
                f"      {shell_join(branch_fetch_command)}",
            ]
        )
    )


def gitdir_file_target(worktree_path: Path) -> Optional[Path]:
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return None
    content = git_file.read_text(encoding="utf-8").strip()
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    target = Path(content[len(prefix) :].strip())
    if not target.is_absolute():
        target = (worktree_path / target).resolve()
    return target


def worktree_common_git_dir(worktree_path: Path) -> Optional[Path]:
    if not (worktree_path / ".git").exists():
        return None
    result = read_git(
        ["-C", str(worktree_path), "rev-parse", "--git-common-dir"], check=False
    )
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (worktree_path / common).resolve()
    return common


def is_managed_linked_worktree(worktree_path: Path, source_git_dir: Path) -> bool:
    common_git_dir = worktree_common_git_dir(worktree_path)
    source_git_dir = source_git_dir.resolve()
    if not common_git_dir or common_git_dir != source_git_dir:
        return False

    gitdir_target = gitdir_file_target(worktree_path)
    if not gitdir_target:
        return False

    try:
        gitdir_target.resolve().relative_to(source_git_dir / "worktrees")
    except ValueError:
        return False
    return True


def is_empty_dir(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is None


def has_local_changes(worktree_path: Path, current_head: str) -> bool:
    result = read_git_bounded(
        [
            "-C",
            str(worktree_path),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
            "--no-renames",
        ],
        extra_env={"GIT_ATTR_SOURCE": current_head},
    )
    records = bounded_records(
        result.stdout,
        "managed worktree tracked-status inventory",
    )
    return bool(records)


def registered_worktree_paths(source_git_dir: Path) -> list[Path]:
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "worktree",
            "list",
            "--porcelain",
            "-z",
        ]
    )
    paths: list[Path] = []
    for field in bounded_records(
        result.stdout,
        "source worktree registry",
        maximum_records=MAX_REGISTERED_WORKTREE_FIELDS,
    ):
        if not field.startswith(b"worktree "):
            continue
        raw_path = field[len(b"worktree ") :]
        if not raw_path or len(raw_path) > MAX_CHECKOUT_PATH_BYTES:
            raise PlanError(
                "source worktree registry contains an empty or oversized path"
            )
        paths.append(Path(os.fsdecode(raw_path)).resolve(strict=False))
    return paths


def registered_target_path(source_git_dir: Path, target_path: Path) -> Optional[Path]:
    resolved_target = target_path.resolve(strict=False)
    for registered_path in registered_worktree_paths(source_git_dir):
        if registered_path == resolved_target:
            return registered_path
    return None


def ensure_target_parent_is_creatable(path: Path) -> None:
    ancestor = path.parent
    while not ancestor.exists():
        if ancestor == ancestor.parent:
            raise PlanError(f"cannot resolve an existing parent directory for {path}")
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise PlanError(
            f"cannot create worktree path {path}\n"
            f"  existing parent is not a directory: {ancestor}"
        )


def prepare_target_path(
    path: Path, source_git_dir: Path, force_replace_empty: bool, dry_run: bool
) -> str:
    registered_path = registered_target_path(source_git_dir, path)
    managed = path.exists() and is_managed_linked_worktree(path, source_git_dir)

    if managed:
        if registered_path is None:
            raise PlanError(
                f"{path} looks like a managed linked worktree but is absent from the source registry\n"
                f"  source gitdir: {source_git_dir}"
            )
        return "managed"

    if registered_path is not None:
        raise PlanError(
            f"{path} is registered in the source repository but is not a usable managed linked worktree\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  registered path: {registered_path}\n"
            "  inspect the stale worktree record and prune it manually before rerunning"
        )

    if not path.exists():
        ensure_target_parent_is_creatable(path)
        return "missing"

    if is_empty_dir(path):
        if not force_replace_empty:
            raise PlanError(
                f"{path} is an empty directory; pass --force-replace-empty to use it"
            )
        if dry_run:
            print(f"would use empty directory: {path}")
        return "empty"

    target = gitdir_file_target(path)
    if target:
        raise PlanError(
            f"{path} is not a managed linked worktree for the expected source repository\n"
            f"  gitdir: {target}\n"
            "  remove or deinit it manually before rerunning this script"
        )

    raise PlanError(
        f"{path} already exists and is not an empty directory or managed linked worktree\n"
        "  this script will not overwrite it"
    )


def checkout_existing_worktree(worktree_path: Path, sha: str, dry_run: bool) -> None:
    command = [
        "git",
        "-C",
        str(worktree_path),
        "checkout",
        "--no-overwrite-ignore",
        "--no-recurse-submodules",
        "--detach",
        sha,
    ]
    if dry_run:
        print(f"would checkout existing worktree: {shell_join(command)}")
        return
    run(command, extra_env={"GIT_ATTR_SOURCE": sha})


def add_worktree(
    source_git_dir: Path, worktree_path: Path, sha: str, dry_run: bool
) -> None:
    command = [
        "git",
        *source_repo_args(source_git_dir, worktree_path),
        "worktree",
        "add",
        "--detach",
        str(worktree_path),
        sha,
    ]
    if dry_run:
        print(f"would add worktree: {shell_join(command)}")
        return
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    run(command)


def classify_planned_target(
    target: BoundTarget,
    source_git_dir: Path,
    force_replace_empty: bool,
) -> str:
    registered_path = registered_target_path(source_git_dir, target.path)
    target_exists = not target.missing_parts
    managed = target_exists and is_managed_linked_worktree(target.path, source_git_dir)

    if managed:
        if registered_path is None:
            raise PlanError(
                f"{target.path} looks like a managed linked worktree but is absent from "
                f"the source registry\n"
                f"  source gitdir: {source_git_dir}"
            )
        return "managed"

    if registered_path is not None:
        raise PlanError(
            f"{target.path} is registered in the source repository but is not a usable "
            f"managed linked worktree\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  registered path: {registered_path}\n"
            "  inspect the stale worktree record and prune it manually before rerunning"
        )

    if not target_exists:
        return "missing"

    if is_empty_dir(target.path):
        if not force_replace_empty:
            raise PlanError(
                f"{target.path} is an empty directory; pass --force-replace-empty to use it"
            )
        return "empty"

    gitdir_target = gitdir_file_target(target.path)
    if gitdir_target:
        raise PlanError(
            f"{target.path} is not a managed linked worktree for the expected source repository\n"
            f"  gitdir: {gitdir_target}\n"
            "  remove or deinit it manually before rerunning this script"
        )
    raise PlanError(
        f"{target.path} already exists and is not an empty directory or managed linked worktree\n"
        "  this script will not overwrite it"
    )


def source_access_bindings(
    source_git_dir: Path, needs_fetch: bool
) -> list[AccessBinding]:
    bindings = [
        capture_typed_access(
            source_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "source gitdir administration and registry writes",
            stat.S_IFDIR,
        )
    ]
    objects_dir = source_git_dir / "objects"
    objects_mode = os.R_OK | os.X_OK | (os.W_OK if needs_fetch else 0)
    bindings.append(
        capture_typed_access(
            objects_dir,
            objects_mode,
            "source object database access",
            stat.S_IFDIR,
        )
    )
    pack_dir = objects_dir / "pack"
    if needs_fetch and path_entry_exists(pack_dir):
        bindings.append(
            capture_typed_access(
                pack_dir,
                os.R_OK | os.W_OK | os.X_OK,
                "source pack-object updates for an authorized fetch",
                stat.S_IFDIR,
            )
        )
    worktrees_dir = source_git_dir / "worktrees"
    if path_entry_exists(worktrees_dir):
        bindings.append(
            capture_typed_access(
                worktrees_dir,
                os.R_OK | os.W_OK | os.X_OK,
                "source worktree registry writes",
                stat.S_IFDIR,
            )
        )
    if needs_fetch:
        refs_dir = source_git_dir / "refs"
        if path_entry_exists(refs_dir):
            bindings.append(
                capture_typed_access(
                    refs_dir,
                    os.R_OK | os.W_OK | os.X_OK,
                    "source ref updates for an authorized fetch",
                    stat.S_IFDIR,
                )
            )
    return bindings


def target_access_bindings(
    target: BoundTarget,
    state: str,
    source_git_dir: Path,
) -> list[AccessBinding]:
    bindings = [
        capture_access(node.path, os.X_OK, "target ancestor search")
        for node in target.existing_nodes
    ]
    if state == "missing":
        bindings.append(
            capture_access(
                target.existing_nodes[-1].path,
                os.W_OK | os.X_OK,
                "target parent creation",
            )
        )
    else:
        bindings.append(
            capture_access(
                target.path,
                os.R_OK | os.W_OK | os.X_OK,
                "target worktree update",
            )
        )

    if state != "managed":
        return bindings

    git_file = target.path / ".git"
    bindings.append(
        capture_typed_access(
            git_file,
            os.R_OK,
            "managed worktree gitdir read",
            stat.S_IFREG,
        )
    )
    admin_dir = gitdir_file_target(target.path)
    if not admin_dir:
        raise PlanError(
            f"cannot resolve managed worktree admin directory: {target.path}"
        )
    bindings.append(
        capture_typed_access(
            admin_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "managed worktree administration",
            stat.S_IFDIR,
        )
    )
    index_output = git(["-C", str(target.path), "rev-parse", "--git-path", "index"])
    index_path = Path(index_output)
    if not index_path.is_absolute():
        index_path = target.path / index_path
    index_path = index_path.resolve(strict=False)
    bindings.append(
        capture_typed_access(
            index_path.parent,
            os.W_OK | os.X_OK,
            "managed worktree index parent update",
            stat.S_IFDIR,
        )
    )
    if path_entry_exists(index_path):
        bindings.append(
            capture_typed_access(
                index_path,
                os.R_OK | os.W_OK,
                "managed worktree index update",
                stat.S_IFREG,
            )
        )
    common_dir = worktree_common_git_dir(target.path)
    if not common_dir or common_dir.resolve() != source_git_dir.resolve():
        raise PlanError(f"managed worktree common gitdir changed: {target.path}")
    return bindings


def validate_checkout_path(raw_path: bytes, description: str) -> tuple[str, ...]:
    if not raw_path:
        raise PlanError(f"{description} contains an empty Git path")
    if len(raw_path) > MAX_CHECKOUT_PATH_BYTES:
        raise PlanError(
            f"{description} path exceeds the {MAX_CHECKOUT_PATH_BYTES}-byte limit"
        )
    path = os.fsdecode(raw_path)
    return tuple(validate_relative_git_path(path, description, "Git tree").split("/"))


def bounded_records(
    payload: bytes,
    description: str,
    *,
    maximum_records: int = MAX_CHECKOUT_PATHS,
) -> list[bytes]:
    if not payload:
        return []
    records = payload.split(b"\0")
    if records[-1] != b"":
        raise PlanError(f"{description} is not NUL terminated")
    records.pop()
    if len(records) > maximum_records:
        raise PlanError(
            f"{description} exceeds the {maximum_records}-record safety limit"
        )
    return records


def managed_head(worktree_path: Path) -> str:
    result = read_git_bounded(
        ["-C", str(worktree_path), "rev-parse", "--verify", "HEAD^{commit}"],
        stdout_limit=256,
    )
    value = os.fsdecode(result.stdout).strip()
    if not value or any(character not in "0123456789abcdef" for character in value):
        raise PlanError(f"managed worktree returned an invalid HEAD: {worktree_path}")
    return value


def managed_index_snapshot(
    worktree_path: Path,
) -> tuple[str, int, tuple[tuple[str, ...], ...]]:
    result = read_git_bounded(
        ["-C", str(worktree_path), "ls-files", "--stage", "-v", "-z"]
    )
    records = bounded_records(result.stdout, "managed worktree index")
    regular_paths: list[tuple[str, ...]] = []
    for record in records:
        if not record.startswith(b"H "):
            tag = os.fsdecode(record[:1]) if record else "<empty>"
            raise PlanError(
                "managed worktree index has unsupported sparse or hidden state\n"
                f"  worktree: {worktree_path}\n"
                f"  tag: {tag}"
            )
        try:
            metadata, raw_path = record[2:].split(b"\t", 1)
        except ValueError as exc:
            raise PlanError(
                "managed worktree index has an invalid stage record"
            ) from exc
        fields = metadata.split()
        if len(fields) != 3:
            raise PlanError("managed worktree index has an invalid stage header")
        mode, _object_id, stage_value = fields
        if stage_value != b"0":
            raise PlanError(
                "managed worktree index has unresolved entries; resolve conflicts "
                "before syncing"
            )
        if mode.startswith(b"100"):
            regular_paths.append(
                validate_checkout_path(raw_path, "managed worktree index")
            )
    return (
        hashlib.sha256(result.stdout).hexdigest(),
        len(records),
        tuple(regular_paths),
    )


def parse_managed_tree_changes(
    worktree_path: Path,
    current_head: str,
    target_sha: str,
) -> tuple[TreeChange, ...]:
    result = read_git_bounded(
        [
            "-C",
            str(worktree_path),
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "-r",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            current_head,
            target_sha,
        ]
    )
    records = bounded_records(
        result.stdout,
        "managed checkout write set",
        maximum_records=MAX_CHECKOUT_PATHS * 2,
    )
    if len(records) % 2 != 0:
        raise PlanError("managed checkout write set has a truncated raw record")
    changes: list[TreeChange] = []
    for offset in range(0, len(records), 2):
        header = records[offset]
        raw_path = records[offset + 1]
        fields = header.split()
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise PlanError("managed checkout write set has an invalid raw header")
        old_mode = os.fsdecode(fields[0][1:])
        new_mode = os.fsdecode(fields[1])
        status_code = fields[4]
        if status_code not in {b"A", b"D", b"M", b"T", b"U", b"X"}:
            raise PlanError(
                "managed checkout write set contains an unsupported status: "
                f"{os.fsdecode(status_code)}"
            )
        changes.append(
            TreeChange(
                relative_parts=validate_checkout_path(
                    raw_path,
                    "managed checkout write set",
                ),
                old_mode=old_mode,
                new_mode=new_mode,
            )
        )
    return tuple(changes)


def target_tree_blob_paths(
    source_git_dir: Path,
    target_sha: str,
) -> tuple[tuple[str, ...], ...]:
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            target_sha,
        ]
    )
    paths: list[tuple[str, ...]] = []
    for record in bounded_records(result.stdout, "target checkout tree"):
        try:
            header, raw_path = record.split(b"\t", 1)
        except ValueError as exc:
            raise PlanError("target checkout tree has an invalid record") from exc
        fields = header.split()
        if len(fields) != 3:
            raise PlanError("target checkout tree has an invalid header")
        mode, object_type, _object_id = fields
        if object_type == b"blob" and mode.startswith(b"100"):
            paths.append(validate_checkout_path(raw_path, "target checkout tree"))
    return tuple(paths)


def selected_checkout_filters(
    source_git_dir: Path,
    treeish: str,
    paths: tuple[tuple[str, ...], ...],
) -> tuple[FilterSelection, ...]:
    if not paths:
        return ()
    encoded_paths = bytearray()
    expected_paths: list[bytes] = []
    for parts in paths:
        raw_path = os.fsencode("/".join(parts))
        if len(encoded_paths) + len(raw_path) + 1 > GIT_INPUT_LIMIT_BYTES:
            raise PlanError(
                "checkout attribute query exceeds the "
                f"{GIT_INPUT_LIMIT_BYTES}-byte input limit"
            )
        encoded_paths.extend(raw_path)
        encoded_paths.append(0)
        expected_paths.append(raw_path)
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "check-attr",
            f"--source={treeish}",
            "-z",
            "--stdin",
            "filter",
        ],
        input_bytes=bytes(encoded_paths),
    )
    records = bounded_records(
        result.stdout,
        "checkout filter attributes",
        maximum_records=MAX_CHECKOUT_PATHS * 3,
    )
    if len(records) != len(paths) * 3:
        raise PlanError("checkout filter attribute result has an invalid shape")
    selections: list[FilterSelection] = []
    for offset in range(0, len(records), 3):
        raw_path, attribute, value = records[offset : offset + 3]
        if raw_path != expected_paths[offset // 3]:
            raise PlanError("checkout filter attribute result changed path order")
        if attribute != b"filter":
            raise PlanError(
                "checkout filter attribute result named the wrong attribute"
            )
        if value in {b"unspecified", b"unset"}:
            continue
        selections.append(
            FilterSelection(
                treeish=treeish,
                raw_path=raw_path,
                driver=os.fsdecode(value),
            )
        )
    return tuple(selections)


def selected_index_filters(
    worktree_path: Path,
    paths: tuple[tuple[str, ...], ...],
) -> tuple[FilterSelection, ...]:
    if not paths:
        return ()
    encoded_paths = bytearray()
    expected_paths: list[bytes] = []
    for parts in paths:
        raw_path = os.fsencode("/".join(parts))
        if len(encoded_paths) + len(raw_path) + 1 > GIT_INPUT_LIMIT_BYTES:
            raise PlanError(
                "index attribute query exceeds the "
                f"{GIT_INPUT_LIMIT_BYTES}-byte input limit"
            )
        encoded_paths.extend(raw_path)
        encoded_paths.append(0)
        expected_paths.append(raw_path)
    result = read_git_bounded(
        [
            "-C",
            str(worktree_path),
            "check-attr",
            "--cached",
            "-z",
            "--stdin",
            "filter",
        ],
        input_bytes=bytes(encoded_paths),
    )
    records = bounded_records(
        result.stdout,
        "index filter attributes",
        maximum_records=MAX_CHECKOUT_PATHS * 3,
    )
    if len(records) != len(paths) * 3:
        raise PlanError("index filter attribute result has an invalid shape")
    selections: list[FilterSelection] = []
    for offset in range(0, len(records), 3):
        raw_path, attribute, value = records[offset : offset + 3]
        if raw_path != expected_paths[offset // 3]:
            raise PlanError("index filter attribute result changed path order")
        if attribute != b"filter":
            raise PlanError("index filter attribute result named the wrong attribute")
        if value in {b"unspecified", b"unset"}:
            continue
        selections.append(
            FilterSelection(
                treeish="index",
                raw_path=raw_path,
                driver=os.fsdecode(value),
            )
        )
    return tuple(selections)


def configured_filter_commands(
    source_git_dir: Path,
) -> dict[str, set[str]]:
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "config",
            "--local",
            "--no-includes",
            "--name-only",
            "-z",
            "--get-regexp",
            r"^filter\..*\.(clean|process)$",
        ],
        check=False,
    )
    if result.returncode == 1:
        return {}
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        raise PlanError(
            "cannot inspect local clean/process filter configuration\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  error: {detail or 'git config failed'}"
        )
    commands: dict[str, set[str]] = {}
    for raw_key in bounded_records(
        result.stdout,
        "local clean/process filter configuration",
    ):
        key = os.fsdecode(raw_key)
        match = re.fullmatch(r"filter\.(.+)\.(clean|process)", key, re.IGNORECASE)
        if not match:
            raise PlanError(
                f"local filter configuration returned an invalid key: {key!r}"
            )
        commands.setdefault(match.group(1).casefold(), set()).add(
            match.group(2).lower()
        )
    return commands


def reject_checkout_filters(
    source_git_dir: Path,
    worktree_path: Path,
    tree_paths: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...],
    index_paths: tuple[tuple[str, ...], ...] = (),
) -> None:
    selections: list[FilterSelection] = []
    selections.extend(selected_index_filters(worktree_path, index_paths))
    for treeish, paths in tree_paths:
        selections.extend(
            selected_checkout_filters(
                source_git_dir,
                treeish,
                paths,
            )
        )
    configured_commands = configured_filter_commands(source_git_dir)
    if not selections:
        return
    selection = selections[0]
    command_kinds = sorted(configured_commands.get(selection.driver.casefold(), set()))
    configured_text = ", ".join(command_kinds) if command_kinds else "none"
    raise PlanError(
        "checkout requires an untrusted content filter and is blocked before "
        "tracked-status inspection or mutation\n"
        f"  worktree: {worktree_path}\n"
        f"  tree: {selection.treeish}\n"
        f"  path: {os.fsdecode(selection.raw_path)}\n"
        f"  filter: {selection.driver}\n"
        f"  configured clean/process commands: {configured_text}\n"
        "  this helper does not execute repository-defined clean, process, "
        "or smudge filters"
    )


def checkout_write_access_bindings(
    worktree_path: Path,
    changes: tuple[TreeChange, ...],
) -> tuple[AccessBinding, ...]:
    requested_modes: dict[Path, int] = {}
    requested_purposes: dict[Path, set[str]] = {}

    def request(path: Path, mode: int, purpose: str) -> None:
        requested_modes[path] = requested_modes.get(path, 0) | mode
        requested_purposes.setdefault(path, set()).add(purpose)
        if len(requested_modes) > MAX_CHECKOUT_ACCESS_BINDINGS:
            raise PlanError(
                "managed checkout access plan exceeds the "
                f"{MAX_CHECKOUT_ACCESS_BINDINGS}-binding safety limit"
            )

    root = worktree_path.resolve(strict=True)
    request(root, os.R_OK | os.W_OK | os.X_OK, "managed checkout root update")
    component_count = 0
    for change in changes:
        component_count += len(change.relative_parts)
        if component_count > MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "managed checkout write set exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
            )
        current = root
        first_missing = False
        for index, part in enumerate(change.relative_parts):
            candidate = current / part
            if first_missing or not path_entry_exists(candidate):
                if not first_missing:
                    request(
                        current,
                        os.W_OK | os.X_OK,
                        "managed checkout path creation",
                    )
                first_missing = True
                current = candidate
                continue
            fingerprint = filesystem_fingerprint(candidate)
            final = index == len(change.relative_parts) - 1
            if not final:
                if fingerprint.kind != stat.S_IFDIR:
                    raise PlanError(
                        "managed checkout path has a non-directory ancestor\n"
                        f"  path: {candidate}"
                    )
                request(candidate, os.X_OK, "managed checkout ancestor search")
            else:
                request(
                    current,
                    os.W_OK | os.X_OK,
                    "managed checkout parent update",
                )
                request(candidate, 0, "managed checkout existing object identity")
            current = candidate

    bindings: list[AccessBinding] = []
    for path in sorted(requested_modes, key=lambda value: os.fsencode(value)):
        purpose = ", ".join(sorted(requested_purposes[path]))
        bindings.append(capture_access(path, requested_modes[path], purpose))
    return tuple(bindings)


def ignored_worktree_paths(
    worktree_path: Path,
    changes: tuple[TreeChange, ...],
) -> tuple[tuple[str, ...], ...]:
    write_paths = sorted(
        {change.relative_parts for change in changes if change.new_mode != "000000"}
    )
    if not write_paths:
        return ()

    deadline = time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS
    ignored: dict[tuple[str, ...], None] = {}
    existing_prefixes: set[tuple[str, ...]] = set()
    component_count = 0
    root = worktree_path.resolve(strict=True)
    for parts in write_paths:
        for length in range(1, len(parts) + 1):
            prefix = parts[:length]
            component_count += 1
            if component_count > MAX_CHECKOUT_PATH_COMPONENTS:
                raise PlanError(
                    "managed ignored-path probe exceeds the "
                    f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
                )
            if path_entry_exists(root.joinpath(*prefix)):
                existing_prefixes.add(prefix)

    if existing_prefixes:
        check_input = bytearray()
        for parts in sorted(existing_prefixes):
            raw_path = b"./" + os.fsencode("/".join(parts))
            if len(check_input) + len(raw_path) + 1 > GIT_INPUT_LIMIT_BYTES:
                raise PlanError(
                    "managed ignored-prefix query exceeds the "
                    f"{GIT_INPUT_LIMIT_BYTES}-byte input limit"
                )
            check_input.extend(raw_path)
            check_input.append(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlanError("managed ignored-path probe exceeded its deadline")
        result = read_git_bounded(
            [
                "-C",
                str(worktree_path),
                "check-ignore",
                "-z",
                "--stdin",
            ],
            check=False,
            input_bytes=bytes(check_input),
            extra_env={"GIT_LITERAL_PATHSPECS": "0"},
            timeout_seconds=remaining,
        )
        if result.returncode not in {0, 1}:
            detail = os.fsdecode(result.stderr).strip()
            raise PlanError(
                "cannot inspect ignored target prefixes\n"
                f"  worktree: {worktree_path}\n"
                f"  error: {detail or 'git check-ignore failed'}"
            )
        for raw_path in bounded_records(
            result.stdout,
            "ignored target-prefix inventory",
        ):
            if not raw_path.startswith(b"./"):
                raise PlanError(
                    "ignored target-prefix inventory returned an unbound path"
                )
            raw_path = raw_path[2:]
            ignored[validate_checkout_path(raw_path, "ignored target prefix")] = None

    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = 0
    for parts in write_paths:
        path = "/".join(parts)
        path_bytes = len(os.fsencode(path)) + 1
        if path_bytes > MAX_GIT_PATHSPEC_ARG_BYTES:
            raise PlanError("managed ignored-path pathspec is too large")
        if batch and (
            batch_bytes + path_bytes > MAX_GIT_PATHSPEC_ARG_BYTES
            or len(batch) >= MAX_GIT_PATHSPECS_PER_BATCH
        ):
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(path)
        batch_bytes += path_bytes
    if batch:
        batches.append(batch)
    if len(batches) > MAX_GIT_PATHSPEC_BATCHES:
        raise PlanError(
            "managed ignored-path probe exceeds the "
            f"{MAX_GIT_PATHSPEC_BATCHES}-batch safety limit"
        )

    retained_bytes = 0
    for batch_paths in batches:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlanError("managed ignored-path probe exceeded its deadline")
        retained_limit = GIT_ENUMERATION_OUTPUT_LIMIT_BYTES - retained_bytes
        if retained_limit <= 0:
            raise PlanError(
                "managed ignored-path inventory exceeds the "
                f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
            )
        result = read_git_bounded(
            [
                "-C",
                str(worktree_path),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--full-name",
                "-z",
                "--",
                *batch_paths,
            ],
            stdout_limit=retained_limit,
            timeout_seconds=remaining,
        )
        retained_bytes += len(result.stdout)
        for raw_path in bounded_records(
            result.stdout,
            "ignored target-descendant inventory",
        ):
            ignored[validate_checkout_path(raw_path, "ignored target descendant")] = (
                None
            )
            if len(ignored) > MAX_CHECKOUT_PATHS:
                raise PlanError(
                    "managed ignored-path inventory exceeds the "
                    f"{MAX_CHECKOUT_PATHS}-entry safety limit"
                )
    return tuple(ignored)


def reject_managed_ignored_conflicts(
    worktree_path: Path,
    changes: tuple[TreeChange, ...],
) -> None:
    policy = filesystem_name_policy(worktree_path)
    changes_by_key: dict[tuple[str, ...], TreeChange] = {}
    component_count = 0
    for change in changes:
        if change.new_mode == "000000":
            continue
        key = normalized_path_parts(change.relative_parts, policy)
        component_count += len(key)
        if component_count > MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "managed checkout conflict plan exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
            )
        prior = changes_by_key.get(key)
        if prior is not None:
            raise PlanError(
                "managed checkout target paths alias on the target filesystem\n"
                f"  first: {'/'.join(prior.relative_parts)}\n"
                f"  second: {'/'.join(change.relative_parts)}"
            )
        changes_by_key[key] = change
    if not changes_by_key:
        return
    sorted_change_keys = sorted(changes_by_key)
    for ignored_parts in ignored_worktree_paths(worktree_path, changes):
        normalized_ignored = normalized_path_parts(ignored_parts, policy)
        component_count += len(normalized_ignored)
        if component_count > MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "managed ignored-path comparison exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
            )
        conflicting_change: Optional[TreeChange] = None
        for length in range(1, len(normalized_ignored) + 1):
            conflicting_change = changes_by_key.get(normalized_ignored[:length])
            if conflicting_change is not None:
                break
        if conflicting_change is None:
            candidate_index = bisect.bisect_left(
                sorted_change_keys,
                normalized_ignored,
            )
            if candidate_index < len(sorted_change_keys):
                candidate = sorted_change_keys[candidate_index]
                if candidate[: len(normalized_ignored)] == normalized_ignored:
                    conflicting_change = changes_by_key[candidate]
        if conflicting_change is None:
            continue
        raise PlanError(
            "managed checkout has an ignored-file conflict and is "
            "blocked before mutation\n"
            f"  worktree: {worktree_path}\n"
            f"  ignored: {'/'.join(ignored_parts)}\n"
            f"  target write: {'/'.join(conflicting_change.relative_parts)}"
        )


def probe_managed_checkout(worktree_path: Path, target_sha: str) -> None:
    result = read_git_bounded(
        [
            "-C",
            str(worktree_path),
            "read-tree",
            "--dry-run",
            "-m",
            "-u",
            "--no-recurse-submodules",
            target_sha,
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        raise PlanError(
            "managed checkout dry-run rejected the target, including possible "
            "ignored-file conflicts\n"
            f"  worktree: {worktree_path}\n"
            f"  target: {target_sha}\n"
            f"  error: {detail or 'Git read-tree preflight failed'}"
        )


def checkout_path_digest(paths: Iterable[tuple[str, ...]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for parts in paths:
        count += 1
        if count > MAX_CHECKOUT_PATHS:
            raise PlanError(
                f"checkout path set exceeds the {MAX_CHECKOUT_PATHS}-entry safety limit"
            )
        digest.update(os.fsencode("/".join(parts)))
        digest.update(b"\0")
    return count, digest.hexdigest()


def capture_checkout_preflight(
    entry: PlannedWorktree,
) -> tuple[CheckoutPreflight, tuple[AccessBinding, ...]]:
    changes: Optional[tuple[TreeChange, ...]] = None
    current_head: Optional[str] = None
    index_digest: Optional[str] = None
    index_entry_count: Optional[int] = None
    write_bindings: tuple[AccessBinding, ...] = ()
    target_blob_paths = target_tree_blob_paths(
        entry.source_git_dir,
        entry.sha,
    )
    if entry.state == "managed":
        current_head = managed_head(entry.target.path)
        (
            index_digest,
            index_entry_count,
            index_blob_paths,
        ) = managed_index_snapshot(entry.target.path)
        changes = parse_managed_tree_changes(
            entry.target.path,
            current_head,
            entry.sha,
        )
        current_blob_paths = target_tree_blob_paths(
            entry.source_git_dir,
            current_head,
        )
        reject_checkout_filters(
            entry.source_git_dir,
            entry.target.path,
            (
                (current_head, current_blob_paths),
                (entry.sha, target_blob_paths),
            ),
            index_blob_paths,
        )
        if has_local_changes(entry.target.path, current_head):
            raise PlanError(
                f"{entry.target.path} has local changes; clean it before syncing"
            )
        write_bindings = checkout_write_access_bindings(
            entry.target.path,
            changes,
        )
        reject_managed_ignored_conflicts(entry.target.path, changes)
        probe_managed_checkout(entry.target.path, entry.sha)
    else:
        reject_checkout_filters(
            entry.source_git_dir,
            entry.target.path,
            ((entry.sha, target_blob_paths),),
        )
    digest_paths: Iterable[tuple[str, ...]]
    if changes is None:
        digest_paths = target_blob_paths
        kind = "new"
    else:
        digest_paths = (change.relative_parts for change in changes)
        kind = "managed"
    path_count, path_digest = checkout_path_digest(digest_paths)
    return (
        CheckoutPreflight(
            kind=kind,
            current_head=current_head,
            index_digest=index_digest,
            index_entry_count=index_entry_count,
            path_count=path_count,
            path_digest=path_digest,
            changes=changes or (),
        ),
        write_bindings,
    )


def revalidate_checkout_preflight(entry: PlannedWorktree) -> None:
    receipt = entry.checkout_preflight
    if receipt is None:
        raise PlanError(f"checkout preflight is incomplete for {entry.submodule.path}")
    if receipt.kind != "managed":
        return
    current_head = managed_head(entry.target.path)
    if current_head != receipt.current_head:
        raise PlanError(
            f"managed worktree HEAD changed after preflight: {entry.target.path}"
        )
    current_digest, current_count, index_blob_paths = managed_index_snapshot(
        entry.target.path
    )
    if (
        current_digest != receipt.index_digest
        or current_count != receipt.index_entry_count
    ):
        raise PlanError(
            f"managed worktree index changed after preflight: {entry.target.path}"
        )
    current_blob_paths = target_tree_blob_paths(
        entry.source_git_dir,
        current_head,
    )
    target_blob_paths = target_tree_blob_paths(
        entry.source_git_dir,
        entry.sha,
    )
    reject_checkout_filters(
        entry.source_git_dir,
        entry.target.path,
        (
            (current_head, current_blob_paths),
            (entry.sha, target_blob_paths),
        ),
        index_blob_paths,
    )
    if has_local_changes(entry.target.path, current_head):
        raise PlanError(
            f"{entry.target.path} has local changes; clean it before syncing"
        )
    reject_managed_ignored_conflicts(entry.target.path, receipt.changes)
    probe_managed_checkout(entry.target.path, entry.sha)


def missing_commit_error(
    source_git_dir: Path,
    submodule: Submodule,
    sha: str,
    depth: int,
) -> PlanError:
    command = [
        "git",
        *source_object_repo_args(source_git_dir),
        "fetch",
        "--depth",
        str(depth),
        "--",
        submodule.url,
        sha,
    ]
    return PlanError(
        "\n".join(
            [
                f"target commit is missing for {submodule.path}",
                f"  url: {submodule.url}",
                f"  sha: {sha}",
                f"  source gitdir: {source_git_dir}",
                "  network fetch is disabled by default",
                "  fix:",
                "    fetch the commit manually, or pass --fetch-missing only when the task "
                "explicitly authorizes fetching missing commits",
                f"    planned command: {shell_join(command)}",
            ]
        )
    )


def build_sync_plan(
    *,
    root: Path,
    common_git_dir: Path,
    source_superproject: Optional[Path],
    planned_modules: list[tuple[Submodule, str]],
    depth: int,
    recursive: bool,
    force_replace_empty: bool,
    fetch_missing: bool,
    base_relative_parts: tuple[str, ...] = (),
    parent_source_git_dir: Optional[Path] = None,
    display_root: Optional[Path] = None,
    gitmodules_budget: Optional[GitmodulesReadBudget] = None,
    input_receipt: Optional[PlanInputReceipt] = None,
) -> SyncPlan:
    root = root.resolve(strict=True)
    gitmodules_budget = gitmodules_budget or GitmodulesReadBudget.start()
    entries: list[PlannedWorktree] = []
    source_identities: dict[tuple[int, int], tuple[Submodule, Path]] = {}
    collision_index = TargetCollisionIndex()
    active_ancestor_indexes: set[int] = set()
    planned_path_components = 0

    def add_entry(
        submodule: Submodule,
        sha: str,
        parent_target_parts: tuple[str, ...],
        parent_source: Optional[Path],
        parent_index: Optional[int],
    ) -> None:
        nonlocal planned_path_components
        module_parts = tuple(
            validate_relative_git_path(
                submodule.path,
                f"worktree path for submodule {submodule.path}",
                ".gitmodules",
            ).split("/")
        )
        candidate_component_count = len(parent_target_parts) + len(module_parts)
        if (
            planned_path_components + candidate_component_count
            > MAX_CHECKOUT_PATH_COMPONENTS
        ):
            raise PlanError(
                "sync plan exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
            )
        target = bind_target_path(
            root,
            parent_target_parts + module_parts,
            f"worktree path for submodule {submodule.path}",
        )
        source_git_dir = (
            nested_source_git_dir_for(parent_source, submodule.name)
            if parent_source
            else source_git_dir_for(common_git_dir, submodule.name)
        )
        ensure_source_repo(
            source_git_dir,
            target.path,
            submodule,
            source_superproject,
            parent_source,
        )
        source_fingerprint = filesystem_fingerprint(source_git_dir)
        source_identity = (
            source_fingerprint.device,
            source_fingerprint.inode,
        )
        prior_source = source_identities.get(source_identity)
        if prior_source is not None:
            prior_module, prior_path = prior_source
            raise PlanError(
                "planned source gitdir collision or filesystem alias\n"
                f"  first submodule: {prior_module.path}\n"
                f"  first source: {prior_path}\n"
                f"  second submodule: {submodule.path}\n"
                f"  second source: {source_git_dir}"
            )
        source_identities[source_identity] = (submodule, source_git_dir)
        state = classify_planned_target(target, source_git_dir, force_replace_empty)
        commit_available = commit_exists(source_git_dir, target.path, sha)
        if not commit_available and not fetch_missing:
            raise missing_commit_error(source_git_dir, submodule, sha, depth)
        if not commit_available and recursive:
            raise PlanError(
                f"cannot complete the recursive plan for {submodule.path} because {sha} "
                "is missing locally\n"
                "  no fetch was attempted because every recursive target and access policy "
                "must be known before mutation\n"
                "  fetch the commit manually and rerun, or pass --no-recursive with "
                "--fetch-missing when non-recursive syncing is intended"
            )

        needs_fetch = not commit_available
        transport_receipt = (
            capture_transport_receipt(source_git_dir, submodule)
            if needs_fetch
            else None
        )
        source_bindings = tuple(source_access_bindings(source_git_dir, needs_fetch))
        target_bindings = tuple(target_access_bindings(target, state, source_git_dir))
        entry = PlannedWorktree(
            submodule=submodule,
            sha=sha,
            target=target,
            source_git_dir=source_git_dir,
            parent_source_git_dir=parent_source,
            parent_index=parent_index,
            state=state,
            source_bindings=source_bindings,
            target_bindings=target_bindings,
            checkout_preflight=None,
            transport_receipt=transport_receipt,
            needs_fetch=needs_fetch,
        )
        if commit_available:
            checkout_preflight, write_bindings = capture_checkout_preflight(entry)
            entry.checkout_preflight = checkout_preflight
            entry.target_bindings = (*entry.target_bindings, *write_bindings)
        if len(entries) >= MAX_PLANNED_WORKTREES:
            raise PlanError(
                f"sync plan exceeds the {MAX_PLANNED_WORKTREES}-worktree safety limit"
            )
        collision_index.add(entries, entry, active_ancestor_indexes)
        current_index = len(entries)
        entries.append(entry)
        planned_path_components += candidate_component_count

        if not recursive:
            return
        active_ancestor_indexes.add(current_index)
        try:
            for nested in read_commit_gitmodules(
                source_git_dir,
                target.path,
                sha,
                gitmodules_budget,
            ):
                nested_sha = expected_sha_from_tree(
                    source_git_dir,
                    target.path,
                    sha,
                    nested.path,
                )
                add_entry(
                    nested,
                    nested_sha,
                    target.relative_parts,
                    source_git_dir,
                    current_index,
                )
        finally:
            active_ancestor_indexes.remove(current_index)

    for module, sha in planned_modules:
        add_entry(
            module,
            sha,
            base_relative_parts,
            parent_source_git_dir,
            None,
        )
    return SyncPlan(
        root=root,
        display_root=display_root or root,
        entries=entries,
        depth=depth,
        force_replace_empty=force_replace_empty,
        fetch_missing=fetch_missing,
        input_receipt=input_receipt,
    )


def fetch_command(entry: PlannedWorktree, depth: int) -> list[str]:
    if entry.transport_receipt is None:
        raise PlanError(
            f"planned fetch for {entry.submodule.path} lacks a transport receipt"
        )
    return transport_fetch_command(
        entry.source_git_dir,
        entry.transport_receipt,
        entry.sha,
        depth,
    )


def print_sync_plan(plan: SyncPlan) -> None:
    for entry in plan.entries:
        display_path = relative_display_path(plan.display_root, entry.target.path)
        print(f"sync {display_path} -> {entry.sha}", flush=True)
        if entry.state == "managed":
            checkout_existing_worktree(entry.target.path, entry.sha, dry_run=True)
        else:
            if entry.state == "empty":
                print(f"would use empty directory: {entry.target.path}")
            add_worktree(
                entry.source_git_dir, entry.target.path, entry.sha, dry_run=True
            )
        if entry.needs_fetch:
            print(
                f"would fetch missing commit for {entry.submodule.path}: "
                f"{shell_join(fetch_command(entry, plan.depth))}"
            )


def revalidate_runtime_source_access(entry: PlannedWorktree) -> None:
    for binding in entry.source_bindings:
        revalidate_access(binding)
    source_access_bindings(entry.source_git_dir, entry.needs_fetch)
    if entry.needs_fetch:
        if entry.transport_receipt is None:
            raise PlanError(
                f"planned fetch for {entry.submodule.path} lacks a transport receipt"
            )
        revalidate_transport_receipt(entry.transport_receipt, entry.submodule)


def revalidate_plan_input_receipt(plan: SyncPlan) -> None:
    receipt = getattr(plan, "input_receipt", None)
    if receipt is None:
        return
    revalidate_file_content_binding(receipt.gitmodules_binding)
    revalidate_superproject_index_receipt(
        plan.root,
        receipt.superproject_index,
    )


def revalidate_planned_entry(
    plan: SyncPlan,
    entry: PlannedWorktree,
    *,
    allow_parent_materialization: bool = False,
) -> BoundTarget:
    for binding in entry.source_bindings:
        revalidate_access(binding)
    for binding in entry.target_bindings:
        revalidate_access(binding)
    source_access_bindings(entry.source_git_dir, entry.needs_fetch)

    if allow_parent_materialization and entry.parent_index is not None:
        for node in entry.target.existing_nodes:
            current = filesystem_fingerprint(node.path)
            if current != node.fingerprint:
                raise PlanError(f"target-path object or policy changed: {node.path}")
        current_target = bind_target_path(
            plan.root,
            entry.target.relative_parts,
            f"worktree path for submodule {entry.submodule.path}",
        )
        current_state = classify_planned_target(
            current_target,
            entry.source_git_dir,
            force_replace_empty=True,
        )
        if entry.state == "missing" and current_state not in {"missing", "empty"}:
            raise PlanError(
                f"nested target changed after preflight: {entry.target.path} "
                f"is now {current_state}"
            )
        target_access_bindings(current_target, current_state, entry.source_git_dir)
        target = current_target
    else:
        revalidate_bound_target(entry.target)
        current_state = classify_planned_target(
            entry.target,
            entry.source_git_dir,
            plan.force_replace_empty,
        )
        if current_state != entry.state:
            raise PlanError(
                f"target state changed after preflight for {entry.target.path}: "
                f"{entry.state} -> {current_state}"
            )
        target = entry.target

    if not entry.needs_fetch and not commit_exists(
        entry.source_git_dir,
        target.path,
        entry.sha,
    ):
        raise PlanError(
            f"target commit disappeared after preflight for {entry.submodule.path}: "
            f"{entry.sha}"
        )
    if not entry.needs_fetch:
        revalidate_checkout_preflight(entry)
    return target


def validate_sync_plan(plan: SyncPlan) -> None:
    revalidate_plan_input_receipt(plan)
    for entry in plan.entries:
        revalidate_planned_entry(plan, entry)


def apply_sync_plan(plan: SyncPlan) -> None:
    validate_sync_plan(plan)
    for entry in plan.entries:
        if not entry.needs_fetch:
            continue
        revalidate_plan_input_receipt(plan)
        revalidate_runtime_source_access(entry)
        fetch_missing_commit(
            entry.source_git_dir,
            entry.target.path,
            entry.submodule,
            entry.sha,
            plan.depth,
            dry_run=False,
            transport_receipt=getattr(entry, "transport_receipt", None),
            fetch_missing=True,
        )
        entry.needs_fetch = False

    for entry in plan.entries:
        if entry.checkout_preflight is not None:
            continue
        checkout_preflight, write_bindings = capture_checkout_preflight(entry)
        entry.checkout_preflight = checkout_preflight
        entry.target_bindings = (*entry.target_bindings, *write_bindings)

    validate_sync_plan(plan)
    applied_indexes: set[int] = set()
    first_mutation = True
    for index, entry in enumerate(plan.entries):
        target = revalidate_planned_entry(
            plan,
            entry,
            allow_parent_materialization=(
                entry.parent_index is not None and entry.parent_index in applied_indexes
            ),
        )
        if first_mutation:
            revalidate_plan_input_receipt(plan)
            first_mutation = False
        if entry.state == "managed":
            checkout_existing_worktree(target.path, entry.sha, dry_run=False)
        else:
            add_worktree(entry.source_git_dir, target.path, entry.sha, dry_run=False)
        applied_indexes.add(index)


def sync_one(
    *,
    root: Path,
    common_git_dir: Path,
    source_superproject: Optional[Path],
    parent_source_git_dir: Optional[Path],
    parent_root: Path,
    submodule: Submodule,
    sha: str,
    depth: int,
    recursive: bool,
    force_replace_empty: bool,
    dry_run: bool,
    fetch_missing: bool = False,
    fetch_during_preflight: bool = False,
) -> None:
    del fetch_during_preflight
    base_relative_parts = lexical_relative_parts(
        root,
        parent_root,
        f"worktree parent for submodule {submodule.path}",
    )
    plan = build_sync_plan(
        root=root,
        common_git_dir=common_git_dir,
        source_superproject=source_superproject,
        planned_modules=[(submodule, sha)],
        depth=depth,
        recursive=recursive,
        force_replace_empty=force_replace_empty,
        fetch_missing=fetch_missing,
        base_relative_parts=base_relative_parts,
        parent_source_git_dir=parent_source_git_dir,
        display_root=root,
    )
    print_sync_plan(plan)
    if not dry_run:
        apply_sync_plan(plan)


def execute_sync_plan(
    *,
    root: Path,
    common_git_dir: Path,
    source_superproject: Optional[Path],
    planned_modules: list[tuple[Submodule, str]],
    depth: int,
    recursive: bool,
    force_replace_empty: bool,
    dry_run: bool,
    fetch_missing: bool,
    gitmodules_budget: Optional[GitmodulesReadBudget] = None,
    input_receipt: Optional[PlanInputReceipt] = None,
) -> None:
    print(f"preflight {len(planned_modules)} top-level submodule path(s)", flush=True)
    plan = build_sync_plan(
        root=root,
        common_git_dir=common_git_dir,
        source_superproject=source_superproject,
        planned_modules=planned_modules,
        depth=depth,
        recursive=recursive,
        force_replace_empty=force_replace_empty,
        fetch_missing=fetch_missing,
        display_root=root,
        gitmodules_budget=gitmodules_budget,
        input_receipt=input_receipt,
    )
    print_sync_plan(plan)

    if dry_run:
        print("preflight complete; no worktrees changed", flush=True)
        return

    print("preflight complete; applying plan", flush=True)
    apply_sync_plan(plan)


def normalize_requested_paths(
    requested_paths: list[str],
    *,
    all_paths: bool = False,
) -> Optional[list[str]]:
    if all_paths and requested_paths:
        raise PlanError(
            "use either explicit top-level submodule paths or --all, not both"
        )
    if all_paths:
        return None
    if not requested_paths:
        raise PlanError(
            "no submodule paths selected; pass explicit top-level paths or --all"
        )

    normalized_paths = [
        validate_relative_git_path(path.rstrip("/"), "requested path", "command line")
        for path in requested_paths
    ]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise PlanError("duplicate top-level submodule paths are not allowed")
    return normalized_paths


def filter_submodules(
    modules: list[Submodule],
    requested_paths: list[str],
    *,
    all_paths: bool = False,
) -> list[Submodule]:
    normalized_paths = normalize_requested_paths(requested_paths, all_paths=all_paths)
    if normalized_paths is None:
        if not modules:
            raise PlanError("--all selected no top-level submodules")
        return modules

    wanted = set(normalized_paths)
    by_path = {module.path: module for module in modules}
    missing = sorted(wanted - set(by_path))
    if missing:
        raise PlanError(f"unknown top-level submodule path(s): {', '.join(missing)}")
    return [by_path[path] for path in normalized_paths]


def relative_display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def choose_source_common_git_dir(
    args: argparse.Namespace, target_root: Path
) -> tuple[Path, Optional[Path]]:
    if args.source_common_git_dir and args.source_superproject:
        raise PlanError(
            "use only one of --source-common-git-dir or --source-superproject"
        )
    if args.source_common_git_dir:
        return resolved_path(args.source_common_git_dir), None
    if args.source_superproject:
        source_root, _, source_common_git_dir = repo_paths(
            resolved_path(args.source_superproject)
        )
        return source_common_git_dir, source_root
    _, _, target_common_git_dir = repo_paths(target_root)
    return target_common_git_dir, None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight, then sync explicitly selected submodules as detached linked worktrees "
            "that reuse .git/modules source repositories."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="explicit top-level submodule paths to sync; required unless --all is authorized",
    )
    parser.add_argument(
        "--all",
        dest="all_paths",
        action="store_true",
        help="sync every top-level submodule; use only when the task explicitly authorizes all paths",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="target superproject worktree; defaults to current directory",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="depth used when fetching a missing target commit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the preflight and print its plan without changing worktrees or fetching commits",
    )
    parser.add_argument(
        "--fetch-missing",
        action="store_true",
        help=(
            "allow shallow-fetching missing target commits during apply; "
            "use only when the task explicitly authorizes network fetches"
        ),
    )
    parser.add_argument(
        "--force-replace-empty",
        action="store_true",
        help="allow using existing empty directories",
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="do not sync nested submodules"
    )
    parser.add_argument(
        "--source-superproject",
        help="source checkout whose .git/modules tree should provide submodule repositories",
    )
    parser.add_argument(
        "--source-common-git-dir",
        help=(
            "explicit common gitdir containing modules/<submodule-path>; "
            "mutually exclusive with --source-superproject"
        ),
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    git_runtime()
    if args.depth < 1:
        raise PlanError("--depth must be greater than zero")
    normalize_requested_paths(args.paths, all_paths=args.all_paths)

    root, _, _ = repo_paths(resolved_path(args.repo))
    source_common_git_dir, source_superproject = choose_source_common_git_dir(
        args, root
    )
    gitmodules_budget = GitmodulesReadBudget.start()
    all_modules, gitmodules_binding = capture_worktree_gitmodules(
        root,
        gitmodules_budget,
    )
    modules = filter_submodules(
        all_modules,
        args.paths,
        all_paths=args.all_paths,
    )
    if gitmodules_binding is None:
        raise PlanError("selected submodules require a bound top-level .gitmodules")
    index_receipt = capture_superproject_index_receipt(
        root,
        tuple(module.path for module in modules),
    )
    sha_by_path = dict(index_receipt.selected_gitlinks)
    planned_modules = [(module, sha_by_path[module.path]) for module in modules]
    input_receipt = PlanInputReceipt(
        gitmodules_binding=gitmodules_binding,
        superproject_index=index_receipt,
    )
    revalidate_file_content_binding(gitmodules_binding)
    execute_sync_plan(
        root=root,
        common_git_dir=source_common_git_dir,
        source_superproject=source_superproject,
        planned_modules=planned_modules,
        depth=args.depth,
        recursive=not args.no_recursive,
        force_replace_empty=args.force_replace_empty,
        dry_run=args.dry_run,
        fetch_missing=args.fetch_missing,
        gitmodules_budget=gitmodules_budget,
        input_receipt=input_receipt,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
