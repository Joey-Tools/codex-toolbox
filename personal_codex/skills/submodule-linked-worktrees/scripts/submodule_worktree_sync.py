#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import configparser
from contextlib import ExitStack
import ctypes
from dataclasses import dataclass, field as dataclass_field, replace
import errno
import functools
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on non-POSIX runtimes
    resource = None  # type: ignore[assignment]
import secrets
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, Callable, Iterable, NoReturn, Optional
import unicodedata


GIT_ENUMERATION_TIMEOUT_SECONDS = 120.0
GIT_ENUMERATION_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
GIT_ERROR_OUTPUT_LIMIT_BYTES = 256 * 1024
GIT_INPUT_LIMIT_BYTES = 64 * 1024 * 1024
GIT_VERSION_TIMEOUT_SECONDS = 5.0
GIT_MINIMUM_VERSION = (2, 45, 0)
MAX_GIT_EXECUTABLE_BYTES = 128 * 1024 * 1024
GIT_TRANSPORT_HELPER_NAMES = (
    "git-remote-http",
    "git-remote-https",
    "git-upload-pack",
)
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
PROCESS_TERM_GRACE_SECONDS = 0.5
UMASK_CAPTURE_TIMEOUT_SECONDS = 5.0
MAX_CHECKOUT_PATHS = 250_000
MAX_CHECKOUT_PATH_BYTES = 4096
MAX_CHECKOUT_PATH_COMPONENTS = 1_000_000
MAX_CHECKOUT_ACCESS_BINDINGS = 500_000
MAX_CHECKOUT_OBJECTS = 500_000
MAX_CHECKOUT_LOGICAL_BYTES = 64 * 1024 * 1024 * 1024
MAX_CHECKOUT_OBJECT_ROOTS = 2
MAX_CHECKOUT_PACK_INDEX_OVERHEAD_BYTES = 1024 * 1024
MAX_SIGNED_FILE_SIZE_BYTES = (1 << 63) - 1
GIT_PACK_HEADER_BYTES = 12
ZLIB_COMPRESS_BOUND_FIXED_BYTES = 13
MAX_NAME_POLICY_PROBE_ENTRIES = 256
MAX_REGISTERED_WORKTREE_FIELDS = 1_000_000
MAX_WORKTREE_ADMIN_ENTRIES = 250_000
MAX_WORKTREE_ADMIN_NAME_BYTES = 4096
MAX_WORKTREE_ADMIN_NAMES_BYTES = 64 * 1024 * 1024
MAX_PLANNED_WORKTREES = 250_000
MAX_GIT_PATHSPEC_ARG_BYTES = 64 * 1024
MAX_GIT_PATHSPECS_PER_BATCH = 1024
MAX_GIT_PATHSPEC_BATCHES = 4096
MAX_GITMODULES_FILE_BYTES = 4 * 1024 * 1024
MAX_GITMODULES_RETAINED_BYTES = 64 * 1024 * 1024
MAX_SOURCE_CONFIG_BYTES = 4 * 1024 * 1024
MAX_CHECKOUT_ATTRIBUTES_BYTES = 4 * 1024 * 1024
MAX_GITDIR_FILE_BYTES = 64 * 1024
MAX_SOURCE_SHALLOW_BYTES = 64 * 1024 * 1024
MAX_SUPERPROJECT_INDEX_BYTES = 512 * 1024 * 1024
MAX_TRANSPORT_EXECUTABLE_BYTES = 128 * 1024 * 1024
MAX_CONFIG_ENTRIES = 100_000
SOURCE_SHALLOW_NAME = "shallow"
SOURCE_SHALLOW_LOCK_NAME = "shallow.lock"
SOURCE_FETCH_TRANSACTION_NAME = "codex-submodule-fetch.pending"
LOOSE_OBJECT_FANOUT_NAMES = tuple(f"{value:02x}" for value in range(256))
OBJECT_WRITE_CHILD_NAMES = (*LOOSE_OBJECT_FANOUT_NAMES, "pack")
FETCH_CONTROL_DIRECTORY_PARTS = (
    (),
    ("objects",),
    ("refs",),
    ("refs", "heads"),
)
FETCH_CONTROL_FILE_NAMES = ("config", "HEAD")
FETCH_CONTROL_LOCK_NAMES = ("config.lock", "HEAD.lock", SOURCE_SHALLOW_LOCK_NAME)
MAX_SOURCE_FETCH_TRANSACTION_BYTES = 64 * 1024
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
# Keep the Unicode-name property independent from the case-sensitivity property.
# These filesystems preserve literal directory-entry bytes when per-directory
# casefolding is known to be disabled.
LINUX_EXACT_NAME_FILESYSTEM_MAGICS = frozenset(
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
    "core.commitGraph=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.multiPackIndex=false",
    "-c",
    "core.splitIndex=false",
    "-c",
    "core.sparseCheckout=false",
    "-c",
    "core.sparseCheckoutCone=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=never",
    "-c",
    "index.skipHash=false",
    "-c",
    "index.recordEndOfIndexEntries=false",
    "-c",
    "index.sparse=false",
    "-c",
    "index.threads=1",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
)


class GitError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


class ForwardedProcessSignal(Exception):
    """Carry one parent-owned termination signal through recovery publication."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum
        self.recovery_details: list[str] = []
        self.cleanup_errors: list[str] = []

    def add_recovery_detail(self, detail: str) -> None:
        if detail:
            self.recovery_details.append(detail)

    def add_cleanup_error(self, detail: str) -> None:
        if detail:
            self.cleanup_errors.append(detail)


class TemporaryDirectoryCleanupError(PlanError):
    def __init__(
        self,
        *,
        status: str,
        location: Path,
        expected: FsFingerprint,
        detail: str,
    ) -> None:
        self.status = status
        self.location = location
        self.expected = expected
        super().__init__(
            "owner-private temporary directory cleanup is incomplete\n"
            "  recovery_schema: owner-private-temporary-cleanup-v1\n"
            f"  recovery_status: {status}\n"
            f"  recovery_location: {location}\n"
            "  expected_identity: "
            f"dev={expected.device},ino={expected.inode},kind={expected.kind},"
            f"uid={expected.owner},gid={expected.group},"
            f"mode={expected.permissions:04o}\n"
            f"  recovery_detail: {detail}"
        )


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
class FsObjectIdentity:
    device: int
    inode: int
    kind: int


@dataclass(frozen=True)
class PosixAccessPolicy:
    owner: int
    group: int
    permissions: int


class TargetMaterializationCleanupError(PlanError):
    def __init__(
        self,
        *,
        status: str,
        location: Path,
        expected: Optional[FsFingerprint],
        detail: str,
    ) -> None:
        self.status = status
        self.location = location
        self.expected = expected
        expected_text = (
            "unbound"
            if expected is None
            else (
                f"dev={expected.device},ino={expected.inode},"
                f"kind={expected.kind},uid={expected.owner},"
                f"gid={expected.group},mode={expected.permissions:04o}"
            )
        )
        super().__init__(
            "target materialization cleanup is incomplete\n"
            "  recovery_schema: target-materialization-cleanup-v1\n"
            f"  recovery_status: {status}\n"
            f"  recovery_location: {location}\n"
            f"  expected_identity: {expected_text}\n"
            f"  recovery_detail: {detail}"
        )


class WorktreeRegistrationRecoveryError(PlanError):
    def __init__(
        self,
        *,
        status: str,
        location: Path,
        preserved_scope: str,
        detail: str,
    ) -> None:
        self.status = status
        self.location = location
        super().__init__(
            "worktree registration state is uncertain; preserving "
            f"{preserved_scope} for recovery\n"
            "  recovery_schema: worktree-registration-recovery-v1\n"
            f"  recovery_status: {status}\n"
            f"  recovery_location: {location}\n"
            f"  recovery_detail: {detail}"
        )


@dataclass(frozen=True)
class ActiveGuardRecord:
    token: str
    purpose: str
    recovery_identity: str
    guard: object = dataclass_field(repr=False, compare=False)


class ActiveGuardRegistry:
    """Retain active cleanup guards until their explicit owner releases them."""

    def __init__(self) -> None:
        self._records: dict[str, ActiveGuardRecord] = {}

    def register(
        self,
        guard: object,
        *,
        purpose: str,
        recovery_identity: str,
    ) -> str:
        token = secrets.token_hex(16)
        self._records[token] = ActiveGuardRecord(
            token=token,
            purpose=purpose,
            recovery_identity=recovery_identity,
            guard=guard,
        )
        return token

    def unregister(self, token: Optional[str]) -> None:
        if token is not None:
            self._records.pop(token, None)

    def cleanup_all(self) -> list[str]:
        cleanup_errors: list[str] = []
        for token in reversed(tuple(self._records)):
            record = self._records.pop(token, None)
            if record is None:
                continue
            cleanup = getattr(record.guard, "cleanup", None)
            if cleanup is None:
                cleanup_errors.append(
                    "active guard cleanup-incomplete\n"
                    f"  purpose: {record.purpose}\n"
                    f"  recovery_identity: {record.recovery_identity}\n"
                    "  detail: registered guard has no cleanup method"
                )
                continue
            try:
                cleanup()
            except BaseException as exc:
                cleanup_errors.append(
                    "active guard cleanup-incomplete\n"
                    f"  purpose: {record.purpose}\n"
                    f"  recovery_identity: {record.recovery_identity}\n"
                    f"  detail: {type(exc).__name__}: {exc}"
                )
        return cleanup_errors

    def active_count(self) -> int:
        return len(self._records)


_ACTIVE_GUARDS = ActiveGuardRegistry()


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
    source_exec_path_binding: AccessBinding
    exec_path: Path
    exec_path_binding: AccessBinding
    helper_snapshots: tuple[ExecutableSnapshotReceipt, ...]
    version: tuple[int, int, int]
    version_text: str
    snapshot_guard: object = dataclass_field(repr=False, compare=False)


@dataclass(frozen=True)
class ExecutableSnapshotReceipt:
    source_executable: Path
    source_state: ExecutableState
    executable: Path
    executable_state: ExecutableState
    content_sha256: str


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
class LooseFanoutBinding:
    name: str
    identity: FsObjectIdentity
    access_policy: PosixAccessPolicy
    required_mode: int


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


@dataclass(frozen=True)
class CheckoutAttributesReceipt:
    info_binding: AccessBinding
    attributes_binding: Optional[FileContentBinding]


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
    target_blob_paths: tuple[tuple[str, ...], ...]
    attributes_receipt: CheckoutAttributesReceipt
    filter_selections: tuple[FilterSelection, ...]
    path_count: int
    path_digest: str
    object_count: int
    object_logical_bytes: int
    object_digest: str
    changes: tuple[TreeChange, ...]


@dataclass(frozen=True)
class ObjectClosureReceipt:
    object_count: int
    logical_bytes: int
    digest: str


@dataclass(frozen=True)
class ObjectClosureManifest:
    root: str
    receipt: ObjectClosureReceipt
    objects: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class CommitGitmodulesReceipt:
    commit: str
    entry_mode: Optional[str]
    blob_id: Optional[str]
    content_size: int
    content_sha256: Optional[str]
    modules: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class RecursiveSubmoduleSelection:
    name: str
    path: str
    url: str
    mode: str
    sha: str


@dataclass(frozen=True)
class RecursiveMetadataReceipt:
    root_tree_id: str
    gitmodules: CommitGitmodulesReceipt
    selections: tuple[RecursiveSubmoduleSelection, ...]


@dataclass(frozen=True)
class SourceCompletenessReceipt:
    gitdir_binding: AccessBinding
    config_binding: FileContentBinding
    objects_binding: AccessBinding
    loose_fanout_inventory: tuple[
        tuple[str, Optional[LooseFanoutBinding]],
        ...,
    ]
    alternates_parent_binding: AccessBinding
    pack_binding: Optional[AccessBinding]


@dataclass
class DirectoryEntryLease:
    path: Path
    binding: AccessBinding
    descriptor: int
    parent_binding: AccessBinding
    parent_descriptor: int
    entry_name: str

    def close(self) -> None:
        descriptors = (self.descriptor, self.parent_descriptor)
        self.descriptor = -1
        self.parent_descriptor = -1
        first_error: Optional[OSError] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"directory-entry lease cleanup failed: {first_error}"
            ) from first_error


@dataclass(frozen=True)
class DirectoryChildInventoryLease:
    directory_binding: AccessBinding
    directory_descriptor: int
    entries: tuple[tuple[str, Optional[FsFingerprint]], ...]
    purpose: str


@dataclass(frozen=True)
class DirectoryExactInventoryLease:
    directory_binding: AccessBinding
    directory_descriptor: int
    entries: tuple[tuple[str, FsFingerprint], ...]
    purpose: str


@dataclass(frozen=True)
class DescriptorBoundFileLease:
    directory_binding: AccessBinding
    directory_descriptor: int
    entry_name: str
    descriptor: int
    binding: FileContentBinding
    content: bytes


@dataclass(frozen=True)
class DescriptorBoundDigestFileLease:
    directory_binding: AccessBinding
    directory_descriptor: int
    entry_name: str
    descriptor: int
    binding: FileContentBinding


@dataclass(frozen=True)
class DirectoryAbsentEntryLease:
    directory_binding: AccessBinding
    directory_descriptor: int
    entry_names: tuple[str, ...]
    purpose: str


@dataclass
class ExecutableExecutionLease:
    path: Path
    parent_binding: AccessBinding
    parent_descriptor: int
    entry_name: str
    descriptor: int
    state: ExecutableState
    content_sha256: str
    description: str
    active: bool = True

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        descriptors = (self.descriptor, self.parent_descriptor)
        self.descriptor = -1
        self.parent_descriptor = -1
        first_error: Optional[OSError] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"executable execution-lease cleanup failed: {first_error}"
            ) from first_error


@dataclass
class FetchControlLease:
    directory_leases: tuple[DirectoryEntryLease, ...]
    file_leases: tuple[DescriptorBoundFileLease, ...]
    absent_entry_leases: tuple[DirectoryAbsentEntryLease, ...]
    active: bool = True

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        first_error: Optional[BaseException] = None
        for file_lease in reversed(self.file_leases):
            try:
                os.close(file_lease.descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        for directory_lease in reversed(self.directory_leases):
            try:
                directory_lease.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"fetch control-plane lease cleanup failed: {first_error}"
            ) from first_error


@dataclass
class CheckoutExecutionView:
    common_git_dir: Path
    object_directory: Path
    private_index_path: Optional[Path]
    directory_leases: tuple[DirectoryEntryLease, ...]
    exact_inventory_leases: tuple[DirectoryExactInventoryLease, ...]
    file_leases: tuple[DescriptorBoundFileLease, ...]
    digest_file_leases: tuple[DescriptorBoundDigestFileLease, ...]
    absent_entry_leases: tuple[DirectoryAbsentEntryLease, ...]
    guard: object = dataclass_field(repr=False, compare=False)
    active: bool = dataclass_field(default=True, repr=False, compare=False)

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        cleanup_errors: list[str] = []
        for file_lease in reversed(self.digest_file_leases):
            try:
                os.close(file_lease.descriptor)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        for file_lease in reversed(self.file_leases):
            try:
                os.close(file_lease.descriptor)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        for directory_lease in reversed(self.directory_leases):
            try:
                directory_lease.close()
            except BaseException as exc:
                cleanup_errors.append(str(exc))
        cleanup = getattr(self.guard, "cleanup", None)
        if cleanup is None:
            cleanup_errors.append("owner-private checkout view has no cleanup method")
        else:
            try:
                cleanup()
            except BaseException as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise PlanError(
                "checkout execution-view cleanup failed: " + "; ".join(cleanup_errors)
            )


@dataclass(frozen=True)
class CapturedSuperprojectIndexEntry:
    """One entry decoded only from captured Git index bytes."""

    raw_path: bytes
    mode: int
    object_id: bytes
    stage: int


@dataclass(frozen=True)
class ParsedCapturedSuperprojectIndex:
    """Checksum-verified entries and extensions from one captured index file."""

    object_id_bytes: int
    entries: tuple[CapturedSuperprojectIndexEntry, ...]
    extensions: tuple[tuple[bytes, bytes], ...]


def close_fetch_execution_leases(
    source_object_lease: DirectoryEntryLease,
    fetch_control_lease: Optional[FetchControlLease],
) -> None:
    cleanup_errors: list[str] = []
    if fetch_control_lease is not None:
        try:
            fetch_control_lease.close()
        except BaseException as exc:
            cleanup_errors.append(f"fetch control-plane lease cleanup failed: {exc}")
    try:
        source_object_lease.close()
    except BaseException as exc:
        cleanup_errors.append(f"source object-directory lease cleanup failed: {exc}")
    if cleanup_errors:
        raise PlanError("; ".join(cleanup_errors))


@dataclass(frozen=True)
class WorktreeAdminInventory:
    source_fingerprint: FsFingerprint
    root_fingerprint: Optional[FsFingerprint]
    entries: tuple[tuple[bytes, FsFingerprint], ...]


@dataclass
class ManagedControlReceipt:
    git_file_descriptor: int
    git_file_binding: FileContentBinding
    git_file_content: bytes
    admin_git_dir: Path
    admin_lease: DirectoryEntryLease
    admin_gitdir_descriptor: int
    admin_gitdir_binding: FileContentBinding
    admin_gitdir_content: bytes

    def close(self) -> None:
        descriptors = (
            self.git_file_descriptor,
            self.admin_gitdir_descriptor,
        )
        self.git_file_descriptor = -1
        self.admin_gitdir_descriptor = -1
        first_error: Optional[BaseException] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        try:
            self.admin_lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise PlanError(
                f"managed control receipt cleanup failed: {first_error}"
            ) from first_error


@dataclass
class ManagedPreflightReceipt:
    target_descriptor: int
    control: ManagedControlReceipt
    index_lease: DescriptorBoundFileLease
    current_head: str
    index_digest: str
    index_entry_count: int
    index_blob_paths: tuple[tuple[str, ...], ...]
    active: bool = True

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        first_error: Optional[BaseException] = None
        try:
            os.close(self.index_lease.descriptor)
        except OSError as exc:
            first_error = exc
        try:
            self.control.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            os.close(self.target_descriptor)
        except OSError as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise PlanError(
                f"managed preflight receipt cleanup failed: {first_error}"
            ) from first_error


@dataclass
class ManagedFinalStateReceipt:
    head_descriptor: int
    head_binding: FileContentBinding
    common_descriptor: int
    common_binding: FileContentBinding
    common_content: bytes
    index_descriptor: int
    index_binding: FileContentBinding
    index_content: bytes

    def close(self) -> None:
        descriptors = (
            self.head_descriptor,
            self.common_descriptor,
            self.index_descriptor,
        )
        self.head_descriptor = -1
        self.common_descriptor = -1
        self.index_descriptor = -1
        first_error: Optional[OSError] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"managed final-state receipt cleanup failed: {first_error}"
            ) from first_error


@dataclass
class ManagedControlRollbackSwap:
    directory_descriptor: int
    entry_name: str
    display_path: Path
    temporary_name: str
    unexpected_descriptor: int
    unexpected_binding: FileContentBinding

    def close(self) -> None:
        descriptor = self.unexpected_descriptor
        self.unexpected_descriptor = -1
        if descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError as exc:
            raise PlanError(
                f"managed control rollback receipt cleanup failed: {exc}"
            ) from exc


@dataclass
class SourceFetchTransaction:
    source_git_dir: Path
    directory_binding: AccessBinding
    directory_descriptor: int
    fence_binding: FileContentBinding
    fence_descriptor: int
    transaction_id: str
    active: bool = True
    guard_registration: Optional[str] = dataclass_field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        recovery_identity = json.dumps(
            {
                "fence": str(self.source_git_dir / SOURCE_FETCH_TRANSACTION_NAME),
                "profile": "source-fetch-transaction-descriptor-guard-v1",
                "source_git_dir": str(self.source_git_dir),
                "transaction_id": self.transaction_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.guard_registration = _ACTIVE_GUARDS.register(
            self,
            purpose="source fetch transaction descriptors",
            recovery_identity=recovery_identity,
        )

    def close_descriptors(self) -> None:
        descriptors = (self.fence_descriptor, self.directory_descriptor)
        self.fence_descriptor = -1
        self.directory_descriptor = -1
        first_error: Optional[OSError] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        _ACTIVE_GUARDS.unregister(self.guard_registration)
        self.guard_registration = None
        if first_error is not None:
            raise PlanError(
                f"source fetch descriptor cleanup failed: {first_error}"
            ) from first_error

    def cleanup(self) -> None:
        """Close transaction descriptors while retaining any active fence."""

        was_active = self.active
        self.close_descriptors()
        if was_active:
            raise PlanError(
                "active source fetch transaction retained its recovery fence\n"
                "  recovery_identity: "
                + json.dumps(
                    {
                        "fence": str(
                            self.source_git_dir / SOURCE_FETCH_TRANSACTION_NAME
                        ),
                        "profile": ("source-fetch-transaction-descriptor-guard-v1"),
                        "source_git_dir": str(self.source_git_dir),
                        "transaction_id": self.transaction_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )


@dataclass(frozen=True)
class SuperprojectIndexReceipt:
    index_bindings: tuple[FileContentBinding, ...]
    selected_gitlinks: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PlanInputReceipt:
    gitmodules_binding: FileContentBinding
    superproject_index: SuperprojectIndexReceipt


@dataclass(frozen=True)
class SourceShallowCreationPolicy:
    shared_repository: str
    process_umask: int
    owner: int
    group: int
    permissions: int


@dataclass
class TransportReceipt:
    config_binding: FileContentBinding
    fetch_object_policy: tuple[tuple[str, str], ...]
    approved_url: str
    origin_url: str
    ssh_executable_snapshot: Optional[ExecutableSnapshotReceipt]
    ssh_command: Optional[str]
    source_object_directory: Path
    source_shallow_path: Path
    source_shallow_parent_binding: AccessBinding
    source_shallow_binding: Optional[FileContentBinding]
    source_shallow_creation_policy: Optional[SourceShallowCreationPolicy]
    source_object_write_children: tuple[
        tuple[str, Optional[FsFingerprint]],
        ...,
    ]
    fetch_git_dir: Path
    fetch_access_bindings: tuple[AccessBinding, ...]
    fetch_file_bindings: tuple[FileContentBinding, ...]
    git_runtime_receipt: GitRuntime = dataclass_field(repr=False, compare=False)
    git_environment: tuple[tuple[str, str], ...]
    fetch_guard: object = dataclass_field(repr=False, compare=False)
    active: bool = dataclass_field(default=True, repr=False, compare=False)

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        cleanup = getattr(self.fetch_guard, "cleanup", None)
        if cleanup is None:
            raise PlanError(
                "transport receipt guard has no explicit cleanup method\n"
                f"  recovery_identity: {self.fetch_git_dir}"
            )
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


@dataclass(frozen=True)
class CreatedTargetNode:
    relative_parts: tuple[str, ...]
    node: BoundNode


@dataclass
class SharedMissingAncestor:
    relative_parts: tuple[str, ...]
    participant_targets: frozenset[tuple[str, ...]]
    materialized_node: Optional[BoundNode] = None


@dataclass(frozen=True)
class AppliedTargetRoot:
    owner_index: int
    relative_parts: tuple[str, ...]
    node: BoundNode


@dataclass
class MaterializedTargetLease:
    target: Path
    target_binding: AccessBinding
    target_descriptor: int
    parent_binding: AccessBinding
    parent_descriptor: int
    entry_name: str
    created_nodes: tuple[CreatedTargetNode, ...] = ()
    materialization_target: Optional[BoundTarget] = None

    def close(self) -> None:
        descriptors = (self.target_descriptor, self.parent_descriptor)
        self.target_descriptor = -1
        self.parent_descriptor = -1
        first_error: Optional[OSError] = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"target lease descriptor cleanup failed: {first_error}"
            ) from first_error

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


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
    source_completeness: SourceCompletenessReceipt
    target_bindings: tuple[AccessBinding, ...]
    checkout_preflight: Optional[CheckoutPreflight]
    recursive_metadata: Optional[RecursiveMetadataReceipt]
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
    shared_missing_ancestors: dict[
        tuple[str, ...],
        SharedMissingAncestor,
    ] = dataclass_field(default_factory=dict)
    applied_target_roots: dict[int, AppliedTargetRoot] = dataclass_field(
        default_factory=dict
    )
    active: bool = dataclass_field(default=True, repr=False)

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        cleanup_errors: list[str] = []
        seen: set[int] = set()
        for entry in reversed(self.entries):
            receipt = entry.transport_receipt
            if receipt is None or id(receipt) in seen:
                continue
            seen.add(id(receipt))
            try:
                receipt.close()
            except BaseException as exc:
                cleanup_errors.append(
                    "transport receipt cleanup failed\n"
                    f"  source_git_dir: {entry.source_git_dir}\n"
                    f"  recovery_identity: {receipt.fetch_git_dir}\n"
                    f"  detail: {exc}"
                )
        if cleanup_errors:
            raise PlanError("\n".join(cleanup_errors))


class TargetCollisionNode:
    def __init__(self) -> None:
        self.children: dict[tuple[object, ...], TargetCollisionNode] = {}
        self.terminal_index: Optional[int] = None
        self.first_descendant_index: Optional[int] = None
        self.original_component: Optional[str] = None


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
        if len(candidate.target.relative_parts) != len(
            candidate.target.collision_tokens
        ):
            raise PlanError("planned target collision receipt has invalid depth")
        for component, token in zip(
            candidate.target.relative_parts,
            candidate.target.collision_tokens,
        ):
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
            if node.original_component is None:
                node.original_component = component
            elif node.original_component != component:
                prior = entries[node.first_descendant_index]
                raise PlanError(
                    "planned worktree target components alias on the target "
                    "filesystem\n"
                    f"  first: {prior.target.path}\n"
                    f"  second: {candidate.target.path}"
                )
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
    *,
    runtime: Optional[GitRuntime] = None,
    include_git_exec_path: bool = True,
) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in GIT_ENV_PASSTHROUGH if key in os.environ
    }
    environment.update(SAFE_GIT_ENV)
    selected_runtime = runtime if runtime is not None else _GIT_RUNTIME
    if selected_runtime is not None and include_git_exec_path:
        environment["GIT_EXEC_PATH"] = str(selected_runtime.exec_path)
    if extra_env:
        unsupported = set(extra_env) - {
            "GIT_ATTR_SOURCE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_LITERAL_PATHSPECS",
            "GIT_OBJECT_DIRECTORY",
        }
        if unsupported:
            raise PlanError(
                "unsupported Git environment override: "
                + ", ".join(sorted(unsupported))
            )
        common_git_dir = extra_env.get("GIT_COMMON_DIR")
        if common_git_dir is not None and not Path(common_git_dir).is_absolute():
            raise PlanError("GIT_COMMON_DIR override must be an absolute path")
        object_directory = extra_env.get("GIT_OBJECT_DIRECTORY")
        if object_directory is not None and not Path(object_directory).is_absolute():
            raise PlanError("GIT_OBJECT_DIRECTORY override must be an absolute path")
        index_file = extra_env.get("GIT_INDEX_FILE")
        if index_file is not None and not Path(index_file).is_absolute():
            raise PlanError("GIT_INDEX_FILE override must be an absolute path")
        environment.update(extra_env)
    return environment


def executable_state_from_stat(path_stat: os.stat_result) -> ExecutableState:
    return ExecutableState(
        fingerprint=fingerprint_from_stat(path_stat),
        size=path_stat.st_size,
        modified_ns=path_stat.st_mtime_ns,
        changed_ns=path_stat.st_ctime_ns,
    )


def fingerprint_stat_values(path_stat: os.stat_result) -> tuple[int, ...]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        stat.S_IFMT(path_stat.st_mode),
        path_stat.st_uid,
        path_stat.st_gid,
        stat.S_IMODE(path_stat.st_mode),
    )


def fingerprint_values(fingerprint: FsFingerprint) -> tuple[int, ...]:
    return (
        fingerprint.device,
        fingerprint.inode,
        fingerprint.kind,
        fingerprint.owner,
        fingerprint.group,
        fingerprint.permissions,
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


def capture_executable_execution_lease(
    path: Path,
    recorded_state: ExecutableState,
    expected_digest: str,
    description: str,
) -> ExecutableExecutionLease:
    if not path.is_absolute():
        raise PlanError(f"{description} path must be absolute: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise PlanError(f"{description} has an invalid content digest receipt")
    entry_name = os.fsdecode(validate_descriptor_entry_name(path.name))
    parent = path.parent
    parent_binding = capture_typed_access(
        parent,
        os.R_OK | os.X_OK,
        f"{description} parent",
        stat.S_IFDIR,
    )
    parent_descriptor = open_directory_descriptor(
        parent,
        f"{description} parent",
    )
    descriptor = -1
    lease_created = False
    try:
        revalidate_directory_descriptor(parent_binding, parent_descriptor)
        descriptor = os.open(
            entry_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        descriptor_state = executable_state_from_stat(os.fstat(descriptor))
        entry_state = executable_state_from_stat(
            os.stat(
                entry_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        if (
            descriptor_state != entry_state
            or descriptor_state.fingerprint != recorded_state.fingerprint
            or descriptor_state.fingerprint.kind != stat.S_IFREG
            or descriptor_state.size != recorded_state.size
            or descriptor_state.size <= 0
            or descriptor_state.size > MAX_GIT_EXECUTABLE_BYTES
        ):
            raise PlanError(
                f"{description} object, size, or access policy changed\n"
                f"  executable: {path}"
            )
        if not probe_access_at(
            parent_descriptor,
            entry_name,
            os.R_OK | os.X_OK,
        ):
            raise PlanError(f"access policy denies {description}\n  executable: {path}")
        digest = read_fd_digest(
            descriptor,
            descriptor_state.size,
            deadline=time.monotonic() + GIT_VERSION_TIMEOUT_SECONDS,
            description=description,
        )
        final_descriptor_state = executable_state_from_stat(os.fstat(descriptor))
        final_entry_state = executable_state_from_stat(
            os.stat(
                entry_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        if (
            final_descriptor_state != descriptor_state
            or final_entry_state != descriptor_state
            or digest != expected_digest
        ):
            raise PlanError(
                f"{description} content changed after version preflight\n"
                f"  executable: {path}"
            )
        revalidate_directory_descriptor(parent_binding, parent_descriptor)
        lease = ExecutableExecutionLease(
            path=path,
            parent_binding=parent_binding,
            parent_descriptor=parent_descriptor,
            entry_name=entry_name,
            descriptor=descriptor,
            state=descriptor_state,
            content_sha256=expected_digest,
            description=description,
        )
        lease_created = True
        return lease
    except OSError as exc:
        raise PlanError(f"cannot bind {description}: {path}: {exc}") from exc
    finally:
        if not lease_created:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_descriptor)


def executable_subprocess_path(lease: ExecutableExecutionLease) -> str:
    if sys.platform.startswith("linux"):
        descriptor_path = Path("/proc/self/fd") / str(lease.descriptor)
        try:
            descriptor_state = fingerprint_from_stat(os.fstat(lease.descriptor))
            path_state = fingerprint_from_stat(os.stat(descriptor_path))
        except OSError:
            pass
        else:
            if descriptor_state == path_state and os.access(
                descriptor_path,
                os.X_OK,
            ):
                return str(descriptor_path)
    return str(lease.path)


def revalidate_executable_execution_lease_in_child(
    lease: ExecutableExecutionLease,
) -> None:
    # Protected property: exec must select the receipt-bound regular-file object,
    # exact bytes, and effective access through the same parent dirent. Receipt
    # timestamps are not compared; current mtime/ctime only sandwich the full read
    # so benign pre-gate metadata churn is allowed but an in-read rewrite is not.
    if (
        not lease.active
        or lease.descriptor < 0
        or lease.parent_descriptor < 0
        or not hasattr(os, "pread")
    ):
        raise OSError(errno.EBADF, f"{lease.description} lease is inactive")
    expected_parent = fingerprint_values(lease.parent_binding.fingerprint)
    expected_executable = fingerprint_values(lease.state.fingerprint)
    parent_path_stat = os.stat(
        lease.parent_binding.path,
        follow_symlinks=False,
    )
    parent_descriptor_stat = os.fstat(lease.parent_descriptor)
    entry_stat = os.stat(
        lease.entry_name,
        dir_fd=lease.parent_descriptor,
        follow_symlinks=False,
    )
    descriptor_stat = os.fstat(lease.descriptor)
    entry_state = executable_state_from_stat(entry_stat)
    descriptor_state = executable_state_from_stat(descriptor_stat)
    if (
        fingerprint_stat_values(parent_path_stat) != expected_parent
        or fingerprint_stat_values(parent_descriptor_stat) != expected_parent
        or fingerprint_stat_values(entry_stat) != expected_executable
        or fingerprint_stat_values(descriptor_stat) != expected_executable
        or entry_state != descriptor_state
        or descriptor_state.size != lease.state.size
    ):
        raise OSError(
            errno.ESTALE,
            f"{lease.description} changed before exec",
        )
    if not os.access(
        lease.entry_name,
        os.R_OK | os.X_OK,
        dir_fd=lease.parent_descriptor,
        effective_ids=True,
        follow_symlinks=False,
    ) or not os.access(
        lease.parent_binding.path,
        lease.parent_binding.mode,
        effective_ids=True,
        follow_symlinks=False,
    ):
        raise OSError(
            errno.EACCES,
            f"{lease.description} access changed before exec",
        )
    digest = hashlib.sha256()
    offset = 0
    while offset < lease.state.size:
        chunk = os.pread(
            lease.descriptor,
            min(64 * 1024, lease.state.size - offset),
            offset,
        )
        if not chunk:
            raise OSError(
                errno.ESTALE,
                f"{lease.description} changed size before exec",
            )
        digest.update(chunk)
        offset += len(chunk)
    if (
        os.pread(lease.descriptor, 1, lease.state.size)
        or digest.hexdigest() != lease.content_sha256
    ):
        raise OSError(
            errno.ESTALE,
            f"{lease.description} content changed before exec",
        )
    final_parent_path_stat = os.stat(
        lease.parent_binding.path,
        follow_symlinks=False,
    )
    final_parent_descriptor_stat = os.fstat(lease.parent_descriptor)
    final_entry_stat = os.stat(
        lease.entry_name,
        dir_fd=lease.parent_descriptor,
        follow_symlinks=False,
    )
    final_descriptor_stat = os.fstat(lease.descriptor)
    if (
        fingerprint_stat_values(final_parent_path_stat) != expected_parent
        or fingerprint_stat_values(final_parent_descriptor_stat) != expected_parent
        or executable_state_from_stat(final_entry_stat) != entry_state
        or executable_state_from_stat(final_descriptor_stat) != descriptor_state
        or not os.access(
            lease.entry_name,
            os.R_OK | os.X_OK,
            dir_fd=lease.parent_descriptor,
            effective_ids=True,
            follow_symlinks=False,
        )
        or not os.access(
            lease.parent_binding.path,
            lease.parent_binding.mode,
            effective_ids=True,
            follow_symlinks=False,
        )
    ):
        raise OSError(
            errno.ESTALE,
            f"{lease.description} changed during the final exec gate",
        )


def copy_executable_snapshot_to_directory(
    source: Path,
    expected_fingerprint: FsFingerprint,
    *,
    snapshot_root: Path,
    filename: str,
    maximum_bytes: int,
    description: str,
) -> tuple[Path, ExecutableState, ExecutableState, str]:
    snapshot = snapshot_root / filename
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
                f"{description} snapshot directory is not owner-private\n"
                f"  directory: {snapshot_root}"
            )
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        source_before = executable_state_from_stat(os.fstat(source_descriptor))
        if source_before.fingerprint != expected_fingerprint:
            raise PlanError(
                f"the resolved {description} changed before content binding\n"
                f"  executable: {source}"
            )
        if source_before.fingerprint.kind != stat.S_IFREG:
            raise PlanError(f"resolved {description} is not a regular file: {source}")
        if source_before.size <= 0 or source_before.size > maximum_bytes:
            raise PlanError(
                f"resolved {description} exceeds the content-binding size limit\n"
                f"  executable: {source}\n"
                f"  size: {source_before.size}\n"
                f"  limit: {maximum_bytes}"
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
                    f"{description} snapshot exceeded the content-copy deadline"
                )
            chunk = os.read(
                source_descriptor,
                min(64 * 1024, source_before.size - copied),
            )
            if not chunk:
                raise PlanError(
                    f"the resolved {description} changed size during content binding"
                )
            copied += len(chunk)
            digest.update(chunk)
            pending = memoryview(chunk)
            while pending:
                written = os.write(snapshot_descriptor, pending)
                if written <= 0:
                    raise PlanError(f"failed to write the {description} snapshot")
                pending = pending[written:]
        if os.read(source_descriptor, 1):
            raise PlanError(
                f"the resolved {description} changed size during content binding"
            )
        source_after = executable_state_from_stat(os.fstat(source_descriptor))
        if source_after != source_before:
            raise PlanError(
                f"the resolved {description} changed during content binding\n"
                f"  executable: {source}"
            )
        os.fchmod(snapshot_descriptor, 0o500)
        os.fsync(snapshot_descriptor)
        snapshot_digest = read_fd_digest(
            snapshot_descriptor,
            copied,
            deadline=deadline,
            description=f"{description} snapshot",
        )
        if snapshot_digest != digest.hexdigest():
            raise PlanError(f"{description} snapshot content verification failed")
        snapshot_state = executable_state_from_stat(os.fstat(snapshot_descriptor))
        if (
            snapshot_state.fingerprint.kind != stat.S_IFREG
            or snapshot_state.fingerprint.owner != os.geteuid()
            or snapshot_state.fingerprint.permissions != 0o500
            or snapshot_state.size != source_before.size
        ):
            raise PlanError(
                f"{description} snapshot does not satisfy its owner-private "
                "regular-file policy"
            )
        snapshot = snapshot.resolve(strict=True)
        return (
            snapshot,
            source_before,
            snapshot_state,
            snapshot_digest,
        )
    except OSError as exc:
        raise PlanError(
            f"cannot create a verified {description} snapshot: {exc}"
        ) from exc
    finally:
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def copy_executable_snapshot(
    source: Path,
    expected_fingerprint: FsFingerprint,
    *,
    prefix: str,
    filename: str,
    maximum_bytes: int,
    description: str,
) -> tuple[
    object,
    Path,
    ExecutableState,
    ExecutableState,
    str,
]:
    snapshot_guard = OwnerPrivateTemporaryDirectory(prefix=prefix)
    snapshot_root = Path(snapshot_guard.name)
    try:
        (
            snapshot,
            source_state,
            snapshot_state,
            snapshot_digest,
        ) = copy_executable_snapshot_to_directory(
            source,
            expected_fingerprint,
            snapshot_root=snapshot_root,
            filename=filename,
            maximum_bytes=maximum_bytes,
            description=description,
        )
        return (
            snapshot_guard,
            snapshot,
            source_state,
            snapshot_state,
            snapshot_digest,
        )
    except BaseException:
        snapshot_guard.cleanup()
        raise


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
    return copy_executable_snapshot(
        source,
        expected_fingerprint,
        prefix="submodule-worktree-git.",
        filename="git",
        maximum_bytes=MAX_GIT_EXECUTABLE_BYTES,
        description="Git executable",
    )


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
        if (
            final_state != current_state
            or current_state.size != recorded_state.size
            or digest != expected_digest
        ):
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


def capture_git_transport_helper_closure(
    git_snapshot: Path,
    git_receipt: ExecutableSnapshotReceipt,
) -> tuple[
    object,
    Path,
    AccessBinding,
    AccessBinding,
    tuple[ExecutableSnapshotReceipt, ...],
]:
    # Protected property: Git's later helper lookup is restricted to exact
    # receipt-bound bytes under one owner-private GIT_EXEC_PATH. Identity and
    # owner/group/mode changes matter; timestamp-only churn does not.
    result = run_bounded_bytes(
        [str(git_snapshot), "--exec-path"],
        timeout_seconds=GIT_VERSION_TIMEOUT_SECONDS,
        stdout_limit=MAX_CHECKOUT_PATH_BYTES,
        stderr_limit=256,
        prepare_git_command=False,
        executable_snapshot_receipt=git_receipt,
    )
    raw_exec_path = os.fsdecode(result.stdout).strip()
    if (
        not raw_exec_path
        or "\x00" in raw_exec_path
        or "\n" in raw_exec_path
        or "\r" in raw_exec_path
    ):
        raise PlanError(f"fixed Git returned an invalid exec path: {raw_exec_path!r}")
    source_exec_path = Path(raw_exec_path)
    if not source_exec_path.is_absolute():
        raise PlanError(
            f"fixed Git returned a non-absolute exec path: {source_exec_path}"
        )
    try:
        source_exec_path = source_exec_path.resolve(strict=True)
    except OSError as exc:
        raise PlanError(
            f"cannot resolve the fixed Git transport-helper directory: {exc}"
        ) from exc
    source_exec_binding = capture_typed_access(
        source_exec_path,
        os.R_OK | os.X_OK,
        "fixed Git transport-helper source directory",
        stat.S_IFDIR,
    )

    helper_guard = OwnerPrivateTemporaryDirectory(prefix="submodule-worktree-git-exec.")
    helper_exec_path = Path(helper_guard.name).resolve(strict=True)
    helper_receipts: list[ExecutableSnapshotReceipt] = []
    try:
        for helper_name in GIT_TRANSPORT_HELPER_NAMES:
            helper_source_entry = source_exec_path / helper_name
            try:
                helper_source = helper_source_entry.resolve(strict=True)
            except OSError as exc:
                raise PlanError(
                    "fixed Git transport-helper closure is incomplete\n"
                    f"  helper: {helper_name}\n"
                    f"  source: {helper_source_entry}\n"
                    f"  error: {exc}"
                ) from exc
            helper_fingerprint = filesystem_fingerprint(helper_source)
            if helper_fingerprint.kind != stat.S_IFREG or not probe_access(
                helper_source,
                os.R_OK | os.X_OK,
            ):
                raise PlanError(
                    "fixed Git transport helper is not an executable regular file\n"
                    f"  helper: {helper_name}\n"
                    f"  source: {helper_source}"
                )
            (
                helper_snapshot,
                helper_source_state,
                helper_snapshot_state,
                helper_digest,
            ) = copy_executable_snapshot_to_directory(
                helper_source,
                helper_fingerprint,
                snapshot_root=helper_exec_path,
                filename=helper_name,
                maximum_bytes=MAX_TRANSPORT_EXECUTABLE_BYTES,
                description=f"Git transport helper {helper_name}",
            )
            helper_receipts.append(
                ExecutableSnapshotReceipt(
                    source_executable=helper_source,
                    source_state=helper_source_state,
                    executable=helper_snapshot,
                    executable_state=helper_snapshot_state,
                    content_sha256=helper_digest,
                )
            )
        revalidate_access(source_exec_binding)
        helper_exec_binding = capture_owner_private_directory(
            helper_exec_path,
            "owner-private Git transport-helper closure",
        )
        return (
            helper_guard,
            helper_exec_path,
            source_exec_binding,
            helper_exec_binding,
            tuple(helper_receipts),
        )
    except BaseException:
        helper_guard.cleanup()
        raise


def revalidate_git_helper_directory_binding(binding: AccessBinding) -> None:
    try:
        current = filesystem_fingerprint(binding.path)
    except PlanError as exc:
        raise PlanError(
            f"Git transport-helper directory became unavailable: {binding.path}"
        ) from exc
    if current != binding.fingerprint:
        raise PlanError(
            "Git transport-helper directory object or access policy changed\n"
            f"  path: {binding.path}"
        )
    if not probe_access(binding.path, binding.mode):
        raise PlanError(
            "Git transport-helper directory no longer provides required access\n"
            f"  path: {binding.path}\n"
            f"  required: {access_mode_text(binding.mode)}"
        )


def revalidate_git_runtime(
    runtime: GitRuntime,
    *,
    require_transport_helpers: bool = True,
) -> GitRuntime:
    source_state = revalidate_executable_content(
        runtime.source_executable,
        runtime.source_state,
        runtime.content_sha256,
        "fixed source Git executable",
    )
    executable_state = revalidate_executable_content(
        runtime.executable,
        runtime.executable_state,
        runtime.content_sha256,
        "owner-private Git executable snapshot",
    )
    if not require_transport_helpers:
        return replace(
            runtime,
            source_state=source_state,
            executable_state=executable_state,
        )
    revalidate_git_helper_directory_binding(runtime.exec_path_binding)
    revalidate_git_helper_directory_binding(runtime.source_exec_path_binding)
    if runtime.exec_path_binding.path != runtime.exec_path:
        raise PlanError("Git transport-helper closure has an inconsistent exec path")
    if tuple(receipt.executable.name for receipt in runtime.helper_snapshots) != (
        GIT_TRANSPORT_HELPER_NAMES
    ):
        raise PlanError("Git transport-helper closure has an invalid helper inventory")
    helper_snapshots: list[ExecutableSnapshotReceipt] = []
    for helper_name, receipt in zip(
        GIT_TRANSPORT_HELPER_NAMES,
        runtime.helper_snapshots,
    ):
        if receipt.executable.parent != runtime.exec_path:
            raise PlanError(
                f"Git transport helper escaped the owner-private exec path: {helper_name}"
            )
        helper_source_state = revalidate_executable_content(
            receipt.source_executable,
            receipt.source_state,
            receipt.content_sha256,
            f"source Git transport helper {helper_name}",
        )
        helper_snapshot_state = revalidate_executable_content(
            receipt.executable,
            receipt.executable_state,
            receipt.content_sha256,
            f"owner-private Git transport helper {helper_name}",
        )
        helper_snapshots.append(
            replace(
                receipt,
                source_state=helper_source_state,
                executable_state=helper_snapshot_state,
            )
        )
    return replace(
        runtime,
        source_state=source_state,
        executable_state=executable_state,
        helper_snapshots=tuple(helper_snapshots),
    )


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
    main_receipt = ExecutableSnapshotReceipt(
        source_executable=executable,
        source_state=source_state,
        executable=snapshot,
        executable_state=snapshot_state,
        content_sha256=content_sha256,
    )
    try:
        result = run_bounded_bytes(
            [str(snapshot), "--version"],
            timeout_seconds=GIT_VERSION_TIMEOUT_SECONDS,
            stdout_limit=256,
            stderr_limit=256,
            prepare_git_command=False,
            executable_snapshot_receipt=main_receipt,
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
        (
            helper_guard,
            helper_exec_path,
            source_exec_path_binding,
            helper_exec_binding,
            helper_snapshots,
        ) = capture_git_transport_helper_closure(snapshot, main_receipt)
        combined_guard = CleanupGuardGroup(snapshot_guard, helper_guard)
        try:
            return GitRuntime(
                source_executable=executable,
                source_state=source_state,
                executable=snapshot,
                executable_state=snapshot_state,
                content_sha256=content_sha256,
                source_exec_path_binding=source_exec_path_binding,
                exec_path=helper_exec_path,
                exec_path_binding=helper_exec_binding,
                helper_snapshots=helper_snapshots,
                version=version,
                version_text=version_text,
                snapshot_guard=combined_guard,
            )
        except BaseException:
            combined_guard.cleanup()
            raise
    except BaseException:
        snapshot_guard.cleanup()
        raise


def git_runtime(*, require_transport_helpers: bool = False) -> GitRuntime:
    global _GIT_RUNTIME
    if _GIT_RUNTIME is None:
        _GIT_RUNTIME = discover_git_runtime()
    _GIT_RUNTIME = revalidate_git_runtime(
        _GIT_RUNTIME,
        require_transport_helpers=require_transport_helpers,
    )
    return _GIT_RUNTIME


def cleanup_cached_git_runtime() -> None:
    """Explicitly release the cached executable and helper snapshots."""

    global _GIT_RUNTIME
    runtime = _GIT_RUNTIME
    _GIT_RUNTIME = None
    if runtime is None:
        return
    cleanup = getattr(runtime.snapshot_guard, "cleanup", None)
    if cleanup is None:
        raise PlanError(
            "cached Git runtime has no explicit cleanup method\n"
            f"  recovery_identity: {runtime.executable.parent}"
        )
    cleanup()


def cleanup_cli_resources() -> None:
    """Release every runtime/guard resource before the CLI returns."""

    cleanup_errors: list[str] = []
    try:
        cleanup_cached_git_runtime()
    except BaseException as exc:
        cleanup_errors.append(
            "cached Git runtime cleanup-incomplete\n"
            f"  detail: {type(exc).__name__}: {exc}"
        )
    cleanup_errors.extend(_ACTIVE_GUARDS.cleanup_all())
    if cleanup_errors:
        raise PlanError("\n".join(cleanup_errors))


def safe_git_command_for_runtime(
    runtime: GitRuntime,
    args: list[str],
    *,
    preserve_split_index: bool = False,
) -> list[str]:
    config_args = list(SAFE_GIT_CONFIG_ARGS)
    if preserve_split_index:
        try:
            split_index_value = config_args.index("core.splitIndex=false")
        except ValueError as exc:
            raise PlanError("safe Git config lacks the split-index override") from exc
        if split_index_value == 0 or config_args[split_index_value - 1] != "-c":
            raise PlanError("safe Git split-index override has an invalid shape")
        del config_args[split_index_value - 1 : split_index_value + 1]
    return [str(runtime.executable), *config_args, *args[1:]]


def safe_command(
    args: list[str],
    *,
    preserve_split_index: bool = False,
) -> list[str]:
    if not args or args[0] != "git":
        return args
    return safe_git_command_for_runtime(
        git_runtime(),
        args,
        preserve_split_index=preserve_split_index,
    )


def prepare_command_execution(
    args: list[str],
    *,
    preserve_split_index: bool = False,
    executable_snapshot_receipt: Optional[ExecutableSnapshotReceipt] = None,
    git_runtime_receipt: Optional[GitRuntime] = None,
) -> tuple[
    list[str],
    Optional[ExecutableExecutionLease],
    tuple[ExecutableExecutionLease, ...],
    Optional[GitRuntime],
]:
    if executable_snapshot_receipt is not None:
        if git_runtime_receipt is not None:
            raise PlanError(
                "explicit executable and Git runtime receipts are mutually exclusive"
            )
        if preserve_split_index:
            raise PlanError(
                "an explicit executable snapshot cannot preserve split-index config"
            )
        if not args or Path(args[0]) != executable_snapshot_receipt.executable:
            raise PlanError(
                "explicit executable snapshot receipt does not match the command"
            )
        lease = capture_executable_execution_lease(
            executable_snapshot_receipt.executable,
            executable_snapshot_receipt.executable_state,
            executable_snapshot_receipt.content_sha256,
            "owner-private executable snapshot",
        )
        return list(args), lease, (), None
    if not args or args[0] != "git":
        if git_runtime_receipt is not None:
            raise PlanError("a Git runtime receipt requires a Git command")
        return args, None, (), None
    require_transport_helpers = git_runtime_receipt is not None
    runtime = (
        revalidate_git_runtime(
            git_runtime_receipt,
            require_transport_helpers=True,
        )
        if git_runtime_receipt is not None
        else git_runtime()
    )
    command = safe_git_command_for_runtime(
        runtime,
        args,
        preserve_split_index=preserve_split_index,
    )
    lease = capture_executable_execution_lease(
        runtime.executable,
        runtime.executable_state,
        runtime.content_sha256,
        "owner-private Git executable snapshot",
    )
    helper_leases: list[ExecutableExecutionLease] = []
    try:
        if require_transport_helpers:
            for helper_name, receipt in zip(
                GIT_TRANSPORT_HELPER_NAMES,
                runtime.helper_snapshots,
            ):
                helper_leases.append(
                    capture_executable_execution_lease(
                        receipt.executable,
                        receipt.executable_state,
                        receipt.content_sha256,
                        f"owner-private Git transport helper {helper_name}",
                    )
                )
    except BaseException:
        cleanup_errors: list[str] = []
        for helper_lease in reversed(helper_leases):
            try:
                helper_lease.close()
            except BaseException as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
        try:
            lease.close()
        except BaseException as cleanup_exc:
            cleanup_errors.append(str(cleanup_exc))
        if cleanup_errors:
            raise PlanError(
                "Git helper execution-lease capture failed and cleanup was "
                "incomplete: " + "; ".join(cleanup_errors)
            )
        raise
    return command, lease, tuple(helper_leases), runtime


def close_executable_execution_leases(
    main_lease: Optional[ExecutableExecutionLease],
    helper_leases: tuple[ExecutableExecutionLease, ...],
) -> None:
    cleanup_errors: list[str] = []
    for helper_lease in reversed(helper_leases):
        try:
            helper_lease.close()
        except BaseException as exc:
            cleanup_errors.append(str(exc))
    if main_lease is not None:
        try:
            main_lease.close()
        except BaseException as exc:
            cleanup_errors.append(str(exc))
    if cleanup_errors:
        raise PlanError(
            "executable execution-lease cleanup failed: " + "; ".join(cleanup_errors)
        )


def revalidate_helper_execution_leases(
    helper_leases: tuple[ExecutableExecutionLease, ...],
) -> None:
    try:
        for helper_lease in helper_leases:
            revalidate_executable_execution_lease_in_child(helper_lease)
    except OSError as exc:
        raise PlanError(
            f"Git transport-helper closure changed while Git was running: {exc}"
        ) from exc


def run(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = True,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    (
        command,
        executable_lease,
        helper_leases,
        selected_runtime,
    ) = prepare_command_execution(args)

    def executable_gate() -> None:
        for helper_lease in helper_leases:
            revalidate_executable_execution_lease_in_child(helper_lease)
        if executable_lease is not None:
            revalidate_executable_execution_lease_in_child(executable_lease)

    inherited_descriptor_set: set[int] = set()
    for helper_lease in helper_leases:
        inherited_descriptor_set.add(helper_lease.parent_descriptor)
        inherited_descriptor_set.add(helper_lease.descriptor)
    if executable_lease is not None:
        inherited_descriptor_set.add(executable_lease.parent_descriptor)
        inherited_descriptor_set.add(executable_lease.descriptor)
    inherited_descriptors = tuple(sorted(inherited_descriptor_set))
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=git_environment(
                extra_env,
                runtime=selected_runtime,
                include_git_exec_path=bool(helper_leases),
            ),
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            executable=(
                None
                if executable_lease is None
                else executable_subprocess_path(executable_lease)
            ),
            pass_fds=inherited_descriptors,
            preexec_fn=(
                executable_gate
                if executable_lease is not None or helper_leases
                else None
            ),
        )
        revalidate_helper_execution_leases(helper_leases)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise GitError(f"failed to start {shell_join(command)}: {exc}") from exc
    finally:
        close_executable_execution_leases(executable_lease, helper_leases)
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
    if getattr(process, "returncode", None) is not None:
        return True
    try:
        process.wait(timeout=max(0.0, timeout_seconds))
        return True
    except subprocess.TimeoutExpired:
        return False


MANAGED_CHILD_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP")
    if hasattr(signal, name)
)


class ChildSignalGate:
    """Latch the first managed signal and raise only at parent-owned checkpoints."""

    def __init__(self) -> None:
        self._armed = False
        self._raised = False
        self._pending: Optional[int] = None
        self._defer_depth = 0

    def handle(self, signum: int, _frame: object) -> None:
        if self._pending is None:
            self._pending = signum

    def arm(self) -> None:
        self._armed = True

    def defer_delivery(self) -> None:
        self._defer_depth += 1

    def resume_delivery(self) -> None:
        if self._defer_depth <= 0:
            raise PlanError("managed signal delivery deferral is unbalanced")
        self._defer_depth -= 1

    @property
    def pending(self) -> Optional[int]:
        return self._pending

    @property
    def delivery_deferred(self) -> bool:
        return self._defer_depth > 0

    def raise_if_pending(self) -> None:
        if (
            self._armed
            and not self._raised
            and not self.delivery_deferred
            and self._pending is not None
        ):
            self._raised = True
            raise ForwardedProcessSignal(self._pending)

    def claim_pending_at_scope_exit(self) -> Optional[ForwardedProcessSignal]:
        if not self._armed or self._raised or self._pending is None:
            return None
        self._raised = True
        return ForwardedProcessSignal(self._pending)


@dataclass
class ChildSignalOwnership:
    gate: ChildSignalGate
    previous_handlers: dict[int, signal.Handlers]
    inherited_mask: set[signal.Signals]
    supervisor_mask: set[signal.Signals]
    depth: int = 1


_ACTIVE_SIGNAL_OWNERSHIP: Optional[ChildSignalOwnership] = None


def start_child_signal_supervision() -> tuple[
    ChildSignalGate,
    dict[int, signal.Handlers],
    set[signal.Signals],
    set[signal.Signals],
]:
    """Block managed signals before spawn so no mutating child is orphaned."""
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if os.name != "posix" or not callable(pthread_sigmask):
        raise PlanError("bounded child signal forwarding requires POSIX signal masks")
    gate = ChildSignalGate()
    inherited_mask = pthread_sigmask(signal.SIG_BLOCK, MANAGED_CHILD_SIGNALS)
    supervisor_mask = set(inherited_mask).difference(MANAGED_CHILD_SIGNALS)
    previous_handlers: dict[int, signal.Handlers] = {}
    try:
        for signum in MANAGED_CHILD_SIGNALS:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, gate.handle)
    except BaseException:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        pthread_sigmask(signal.SIG_SETMASK, inherited_mask)
        raise
    return gate, previous_handlers, set(inherited_mask), supervisor_mask


def restore_child_signal_supervision(
    previous_handlers: dict[int, signal.Handlers],
    inherited_mask: set[signal.Signals],
) -> None:
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if not callable(pthread_sigmask):
        return
    pthread_sigmask(signal.SIG_BLOCK, MANAGED_CHILD_SIGNALS)
    first_error: Optional[BaseException] = None
    for signum, handler in previous_handlers.items():
        try:
            signal.signal(signum, handler)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    try:
        pthread_sigmask(signal.SIG_SETMASK, inherited_mask)
    except BaseException as exc:
        if first_error is None:
            first_error = exc
    if first_error is not None:
        raise PlanError(
            f"bounded child signal supervision cleanup failed: {first_error}"
        ) from first_error


def finish_child_signal_supervision(
    gate: ChildSignalGate,
    previous_handlers: dict[int, signal.Handlers],
    inherited_mask: set[signal.Signals],
    outcome_exception: Optional[BaseException],
) -> None:
    late_signal: Optional[ForwardedProcessSignal] = None
    cleanup_errors: list[str] = []
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, MANAGED_CHILD_SIGNALS)
        if gate.delivery_deferred:
            cleanup_errors.append(
                "managed signal delivery deferral remained active at scope exit"
            )
            late_signal = gate.claim_pending_at_scope_exit()
        else:
            try:
                gate.raise_if_pending()
            except ForwardedProcessSignal as signal_exc:
                late_signal = signal_exc
    except BaseException as exc:
        cleanup_errors.append(
            "managed signal latch finalization failed before handler restoration: "
            f"{type(exc).__name__}: {exc}"
        )
        if not isinstance(outcome_exception, ForwardedProcessSignal):
            late_signal = gate.claim_pending_at_scope_exit()

    signal_is_terminal = (
        isinstance(outcome_exception, ForwardedProcessSignal) or late_signal is not None
    )
    if signal_is_terminal:
        try:
            cleanup_cached_git_runtime()
        except BaseException as exc:
            cleanup_errors.append(
                "cached Git runtime signal cleanup-incomplete\n"
                "  recovery_identity: Git executable and transport-helper "
                "snapshot roots\n"
                f"  detail: {type(exc).__name__}: {exc}"
            )
        cleanup_errors.extend(_ACTIVE_GUARDS.cleanup_all())
    try:
        restore_child_signal_supervision(
            previous_handlers,
            inherited_mask,
        )
    except BaseException as exc:
        cleanup_errors.append(
            f"managed signal handler restoration failed: {type(exc).__name__}: {exc}"
        )

    active_signal = (
        outcome_exception
        if isinstance(outcome_exception, ForwardedProcessSignal)
        else late_signal
    )
    if late_signal is not None and outcome_exception is not None:
        late_signal.add_recovery_detail(
            "operation outcome completed before latched signal delivery: "
            f"{type(outcome_exception).__name__}: {outcome_exception}"
        )
    if active_signal is not None:
        for cleanup_error in cleanup_errors:
            active_signal.add_cleanup_error(cleanup_error)
    elif cleanup_errors and outcome_exception is not None:
        raise PlanError(
            f"{outcome_exception}\n" + "\n".join(cleanup_errors)
        ) from outcome_exception
    elif cleanup_errors:
        raise PlanError("\n".join(cleanup_errors))
    if late_signal is not None and not isinstance(
        outcome_exception,
        ForwardedProcessSignal,
    ):
        raise late_signal from outcome_exception


class SignalOwnershipLease:
    """Keep one signal latch installed across nested mutation phases."""

    def __init__(self) -> None:
        self.ownership: Optional[ChildSignalOwnership] = None
        self.active = False

    def __enter__(self) -> ChildSignalOwnership:
        global _ACTIVE_SIGNAL_OWNERSHIP
        if self.active:
            raise PlanError("managed signal ownership lease cannot be reused")
        if _ACTIVE_SIGNAL_OWNERSHIP is not None:
            _ACTIVE_SIGNAL_OWNERSHIP.depth += 1
            self.ownership = _ACTIVE_SIGNAL_OWNERSHIP
            self.active = True
            return _ACTIVE_SIGNAL_OWNERSHIP
        (
            gate,
            previous_handlers,
            inherited_mask,
            supervisor_mask,
        ) = start_child_signal_supervision()
        ownership = ChildSignalOwnership(
            gate=gate,
            previous_handlers=previous_handlers,
            inherited_mask=inherited_mask,
            supervisor_mask=supervisor_mask,
        )
        gate.arm()
        _ACTIVE_SIGNAL_OWNERSHIP = ownership
        self.ownership = ownership
        self.active = True
        try:
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                supervisor_mask,
            )
        except BaseException:
            _ACTIVE_SIGNAL_OWNERSHIP = None
            self.active = False
            self.ownership = None
            restore_child_signal_supervision(
                previous_handlers,
                inherited_mask,
            )
            raise
        return ownership

    def finish(self, outcome_exception: Optional[BaseException]) -> None:
        global _ACTIVE_SIGNAL_OWNERSHIP
        if not self.active or self.ownership is None:
            return
        ownership = self.ownership
        self.active = False
        self.ownership = None
        ownership.depth -= 1
        if ownership.depth < 0:
            raise PlanError("managed signal ownership depth became negative")
        if ownership.depth:
            return
        if _ACTIVE_SIGNAL_OWNERSHIP is not ownership:
            raise PlanError("managed signal ownership changed before final release")
        _ACTIVE_SIGNAL_OWNERSHIP = None
        finish_child_signal_supervision(
            ownership.gate,
            ownership.previous_handlers,
            ownership.inherited_mask,
            outcome_exception,
        )

    def __exit__(
        self,
        _exception_type: object,
        exception: Optional[BaseException],
        _traceback: object,
    ) -> bool:
        self.finish(exception)
        return False


def signal_owned_operation(function: Callable[..., object]) -> Callable[..., object]:
    @functools.wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        with SignalOwnershipLease():
            return function(*args, **kwargs)

    return wrapped


def signal_checkpoint(_stage: str) -> None:
    ownership = _ACTIVE_SIGNAL_OWNERSHIP
    if ownership is not None:
        ownership.gate.raise_if_pending()


def defer_managed_signal_delivery() -> bool:
    ownership = _ACTIVE_SIGNAL_OWNERSHIP
    if ownership is None:
        return False
    ownership.gate.defer_delivery()
    return True


def resume_managed_signal_delivery(deferred: bool) -> None:
    if not deferred:
        return
    ownership = _ACTIVE_SIGNAL_OWNERSHIP
    if ownership is None:
        raise PlanError("managed signal ownership ended during deferred recovery")
    ownership.gate.resume_delivery()


def finish_explicit_cleanup(
    cleanup: Callable[[], None],
    *,
    outcome_exception: Optional[BaseException],
    purpose: str,
    recovery_identity: str,
) -> None:
    """Run one authoritative cleanup without masking an active signal."""

    try:
        cleanup()
    except BaseException as cleanup_exc:
        detail = (
            f"{purpose} cleanup-incomplete\n"
            f"  recovery_identity: {recovery_identity}\n"
            f"  detail: {type(cleanup_exc).__name__}: {cleanup_exc}"
        )
        if isinstance(outcome_exception, ForwardedProcessSignal):
            outcome_exception.add_cleanup_error(detail)
            return
        if outcome_exception is not None:
            raise PlanError(f"{outcome_exception}\n{detail}") from outcome_exception
        raise PlanError(detail) from cleanup_exc


def drain_process_pipes_until(
    selector: Optional[selectors.BaseSelector],
    deadline: float,
) -> bool:
    if selector is None:
        return True
    while selector.get_map() and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            events = selector.select(min(0.02, max(0.0, remaining)))
        except (OSError, ValueError):
            return False
        for key, _ in events:
            try:
                chunk = os.read(key.fd, 64 * 1024)
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR}:
                    continue
                return False
            if not chunk:
                try:
                    selector.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                try:
                    key.fileobj.close()
                except OSError:
                    pass
    return not selector.get_map()


def terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    initial_signal: int = signal.SIGTERM,
    selector: Optional[selectors.BaseSelector] = None,
    cleanup_timeout_seconds: float = PROCESS_CLEANUP_TIMEOUT_SECONDS,
    term_grace_seconds: float = PROCESS_TERM_GRACE_SECONDS,
) -> None:
    """Forward the original signal, then bound group cleanup and direct reap."""
    cleanup_deadline = time.monotonic() + max(0.0, cleanup_timeout_seconds)
    cleanup_details: list[str] = []

    def raise_cleanup_incomplete(
        *,
        direct_child_reaped: bool,
        inherited_streams_closed: bool,
    ) -> NoReturn:
        detail = (
            "; ".join(cleanup_details)
            if cleanup_details
            else "the bounded cleanup proof did not reach a complete terminal state"
        )
        raise PlanError(
            "process cleanup-incomplete\n"
            "  recovery_schema: process-group-cleanup-v1\n"
            f"  process_group_id: {process.pid}\n"
            f"  direct_child_reaped: {str(direct_child_reaped).lower()}\n"
            "  inherited_streams_closed: "
            f"{str(inherited_streams_closed).lower()}\n"
            f"  recovery_detail: {detail}"
        )

    # Reading returncode does not reap. If another owner already reaped the
    # leader, its numeric PID/PGID can be reused, so signalling that group is
    # no longer identity-safe. We may still prove inherited pipe EOF; otherwise
    # the caller receives a structured incomplete-cleanup receipt.
    if getattr(process, "returncode", None) is not None:
        streams_closed = drain_process_pipes_until(selector, cleanup_deadline)
        if streams_closed:
            return
        cleanup_details.append(
            "the direct child was already reaped before inherited streams closed; "
            "the original process-group identity can no longer be signalled safely"
        )
        raise_cleanup_incomplete(
            direct_child_reaped=True,
            inherited_streams_closed=False,
        )

    initial_sent = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, initial_signal)
        else:
            process.send_signal(initial_signal)
        initial_sent = True
    except ProcessLookupError:
        pass
    except OSError as exc:
        cleanup_details.append(
            f"cannot forward signal {initial_signal} to the process group: {exc}"
        )
        try:
            process.terminate()
            initial_sent = True
        except (OSError, ProcessLookupError):
            pass

    if initial_sent:
        remaining = cleanup_deadline - time.monotonic()
        grace = min(max(0.0, term_grace_seconds), max(0.0, remaining))
        if grace:
            # Do not reap the group leader before KILL. Retaining the zombie
            # prevents PID/PGID reuse during the TERM grace window.
            first_grace = grace if initial_signal == signal.SIGTERM else grace / 2
            if selector is None:
                time.sleep(first_grace)
            else:
                drain_process_pipes_until(
                    selector,
                    time.monotonic() + first_grace,
                )
            if initial_signal != signal.SIGTERM:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    cleanup_details.append(
                        "cannot signal the process group with TERM after "
                        f"forwarding signal {initial_signal}: {exc}"
                    )
                second_grace = min(
                    max(0.0, grace - first_grace),
                    max(0.0, cleanup_deadline - time.monotonic()),
                )
                if selector is None:
                    time.sleep(second_grace)
                else:
                    drain_process_pipes_until(
                        selector,
                        time.monotonic() + second_grace,
                    )
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        cleanup_details.append(f"cannot signal the process group with KILL: {exc}")
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass

    streams_closed = drain_process_pipes_until(selector, cleanup_deadline)
    remaining = cleanup_deadline - time.monotonic()
    reaped = wait_for_process_reap(process, max(0.0, remaining))
    if not reaped:
        cleanup_details.append(
            "the direct child could not be reaped within "
            f"{cleanup_timeout_seconds:g} seconds"
        )
    if not streams_closed:
        cleanup_details.append(
            "stdout/stderr inherited by process-group descendants did not reach EOF"
        )
    if cleanup_details or not reaped or not streams_closed:
        raise_cleanup_incomplete(
            direct_child_reaped=reaped,
            inherited_streams_closed=streams_closed,
        )


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
    preserve_split_index: bool = False,
    directory_descriptor: Optional[int] = None,
    directory_identity_leases: tuple[DirectoryEntryLease, ...] = (),
    directory_child_inventory_leases: tuple[
        DirectoryChildInventoryLease,
        ...,
    ] = (),
    directory_exact_inventory_leases: tuple[DirectoryExactInventoryLease, ...] = (),
    directory_absent_entry_leases: tuple[DirectoryAbsentEntryLease, ...] = (),
    file_content_leases: tuple[DescriptorBoundFileLease, ...] = (),
    digest_file_leases: tuple[DescriptorBoundDigestFileLease, ...] = (),
    file_size_limit_bytes: Optional[int] = None,
    executable_snapshot_receipt: Optional[ExecutableSnapshotReceipt] = None,
    git_runtime_receipt: Optional[GitRuntime] = None,
) -> subprocess.CompletedProcess[bytes]:
    if input_bytes is not None and len(input_bytes) > GIT_INPUT_LIMIT_BYTES:
        raise PlanError(
            f"Git command input exceeds the {GIT_INPUT_LIMIT_BYTES}-byte safety limit"
        )
    if fixed_env is not None and extra_env is not None:
        raise PlanError(
            "fixed and incremental command environments are mutually exclusive"
        )
    if preserve_split_index and not prepare_git_command:
        raise PlanError("split-index preservation requires a prepared Git command")
    if prepare_git_command and executable_snapshot_receipt is not None:
        raise PlanError(
            "prepared Git commands cannot use an explicit executable snapshot receipt"
        )
    if not prepare_git_command and git_runtime_receipt is not None:
        raise PlanError("a Git runtime receipt requires a prepared Git command")
    if (
        directory_descriptor is not None
        or directory_identity_leases
        or directory_child_inventory_leases
        or directory_exact_inventory_leases
        or directory_absent_entry_leases
        or file_content_leases
        or digest_file_leases
        or file_size_limit_bytes is not None
    ) and os.name != "posix":
        raise PlanError("descriptor-anchored Git writes require a POSIX runtime")
    if (file_content_leases or digest_file_leases) and not hasattr(os, "pread"):
        raise PlanError("descriptor-bound control-file exec gates require POSIX pread")
    if file_size_limit_bytes is not None and (
        file_size_limit_bytes <= 0 or resource is None
    ):
        raise PlanError("file-size-limited Git writes require POSIX resource limits")
    for lease in file_content_leases:
        validate_descriptor_entry_name(lease.entry_name)
        if len(lease.content) != lease.binding.size or (
            hashlib.sha256(lease.content).hexdigest() != lease.binding.content_sha256
        ):
            raise PlanError(
                f"{lease.binding.purpose} has an invalid exec-lease content receipt"
            )
    for lease in digest_file_leases:
        validate_descriptor_entry_name(lease.entry_name)
        if lease.binding.size < 0 or not re.fullmatch(
            r"[0-9a-f]{64}",
            lease.binding.content_sha256,
        ):
            raise PlanError(
                f"{lease.binding.purpose} has an invalid exec-lease digest receipt"
            )
    for lease in directory_absent_entry_leases:
        if not lease.entry_names or len(set(lease.entry_names)) != len(
            lease.entry_names
        ):
            raise PlanError(f"{lease.purpose} has an invalid absent-entry receipt")
        for entry_name in lease.entry_names:
            validate_descriptor_entry_name(entry_name)
        revalidate_directory_absent_entry_lease(lease)
    for lease in directory_exact_inventory_leases:
        entry_names = tuple(name for name, _fingerprint in lease.entries)
        if entry_names != tuple(sorted(set(entry_names))):
            raise PlanError(f"{lease.purpose} has an invalid exact inventory receipt")
        for entry_name in entry_names:
            validate_descriptor_entry_name(entry_name)
        revalidate_directory_exact_inventory_lease(lease)
    descriptor_file_leases = tuple(
        (lease, lease.content) for lease in file_content_leases
    ) + tuple((lease, None) for lease in digest_file_leases)
    file_size_rlimit: Optional[tuple[int, int]] = None
    if file_size_limit_bytes is not None:
        assert resource is not None
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
        finite_inherited_limits = tuple(
            limit
            for limit in (soft_limit, hard_limit)
            if limit != resource.RLIM_INFINITY
        )
        effective_limit = min(
            (file_size_limit_bytes, *finite_inherited_limits),
        )
        if effective_limit <= 0:
            raise PlanError("the inherited file-size resource limit blocks Git output")
        file_size_rlimit = (effective_limit, hard_limit)

    input_file = tempfile.TemporaryFile()
    try:
        if input_bytes is not None:
            input_file.write(input_bytes)
            input_file.seek(0)
        if prepare_git_command:
            (
                command,
                executable_lease,
                helper_leases,
                selected_runtime,
            ) = prepare_command_execution(
                args,
                preserve_split_index=preserve_split_index,
                git_runtime_receipt=git_runtime_receipt,
            )
        elif executable_snapshot_receipt is not None:
            (
                command,
                executable_lease,
                helper_leases,
                selected_runtime,
            ) = prepare_command_execution(
                args,
                executable_snapshot_receipt=executable_snapshot_receipt,
            )
        else:
            command = args
            executable_lease = None
            helper_leases = ()
            selected_runtime = None
        environment = (
            dict(fixed_env)
            if fixed_env is not None
            else git_environment(
                extra_env,
                runtime=selected_runtime,
                include_git_exec_path=bool(helper_leases),
            )
        )
        if helper_leases and environment.get("GIT_EXEC_PATH") != str(
            selected_runtime.exec_path
        ):
            raise PlanError(
                "prepared Git environment does not bind the trusted helper path"
            )
    except BaseException:
        if "executable_lease" in locals():
            close_executable_execution_leases(executable_lease, helper_leases)
        input_file.close()
        raise

    command_deadline = time.monotonic() + timeout_seconds

    def enter_bound_directory() -> None:
        if file_size_rlimit is not None:
            assert resource is not None
            resource.setrlimit(resource.RLIMIT_FSIZE, file_size_rlimit)
            if hasattr(signal, "SIGXFSZ"):
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)

        def require_exact_inventory_in_child(
            lease: DirectoryExactInventoryLease,
            phase: str,
        ) -> None:
            current = directory_exact_inventory_at(
                lease.directory_descriptor,
                lease.directory_binding.path,
                lease.purpose,
            )
            if current != lease.entries:
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} changed {phase}",
                )
            expected_directory = fingerprint_values(lease.directory_binding.fingerprint)
            if (
                fingerprint_stat_values(
                    os.stat(
                        lease.directory_binding.path,
                        follow_symlinks=False,
                    )
                )
                != expected_directory
                or fingerprint_stat_values(os.fstat(lease.directory_descriptor))
                != expected_directory
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} directory changed {phase}",
                )
            if not os.access(
                lease.directory_binding.path,
                lease.directory_binding.mode,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise OSError(
                    errno.EACCES,
                    f"{lease.purpose} access changed {phase}",
                )

        for lease in directory_identity_leases:
            expected_parent = lease.parent_binding.fingerprint
            expected_entry = lease.binding.fingerprint
            parent_stat = os.stat(
                lease.parent_binding.path,
                follow_symlinks=False,
            )
            parent_descriptor_stat = os.fstat(lease.parent_descriptor)
            entry_stat = os.stat(
                lease.entry_name,
                dir_fd=lease.parent_descriptor,
                follow_symlinks=False,
            )
            object_stat = os.fstat(lease.descriptor)
            parent_values = (
                parent_stat.st_dev,
                parent_stat.st_ino,
                stat.S_IFMT(parent_stat.st_mode),
                parent_stat.st_uid,
                parent_stat.st_gid,
                stat.S_IMODE(parent_stat.st_mode),
            )
            parent_descriptor_values = (
                parent_descriptor_stat.st_dev,
                parent_descriptor_stat.st_ino,
                stat.S_IFMT(parent_descriptor_stat.st_mode),
                parent_descriptor_stat.st_uid,
                parent_descriptor_stat.st_gid,
                stat.S_IMODE(parent_descriptor_stat.st_mode),
            )
            expected_parent_values = (
                expected_parent.device,
                expected_parent.inode,
                expected_parent.kind,
                expected_parent.owner,
                expected_parent.group,
                expected_parent.permissions,
            )
            entry_values = (
                entry_stat.st_dev,
                entry_stat.st_ino,
                stat.S_IFMT(entry_stat.st_mode),
                entry_stat.st_uid,
                entry_stat.st_gid,
                stat.S_IMODE(entry_stat.st_mode),
            )
            object_values = (
                object_stat.st_dev,
                object_stat.st_ino,
                stat.S_IFMT(object_stat.st_mode),
                object_stat.st_uid,
                object_stat.st_gid,
                stat.S_IMODE(object_stat.st_mode),
            )
            expected_entry_values = (
                expected_entry.device,
                expected_entry.inode,
                expected_entry.kind,
                expected_entry.owner,
                expected_entry.group,
                expected_entry.permissions,
            )
            if (
                parent_values != expected_parent_values
                or parent_descriptor_values != expected_parent_values
                or entry_values != expected_entry_values
                or object_values != expected_entry_values
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.binding.purpose} changed before exec",
                )
            if not os.access(
                ".",
                lease.parent_binding.mode,
                dir_fd=lease.parent_descriptor,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise OSError(
                    errno.EACCES,
                    f"{lease.binding.purpose} parent access changed before exec",
                )
            if not os.access(
                lease.entry_name,
                lease.binding.mode,
                dir_fd=lease.parent_descriptor,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise OSError(
                    errno.EACCES,
                    f"{lease.binding.purpose} access changed before exec",
                )
        for lease in directory_child_inventory_leases:
            directory_stat = os.fstat(lease.directory_descriptor)
            expected_directory = lease.directory_binding.fingerprint
            directory_values = (
                directory_stat.st_dev,
                directory_stat.st_ino,
                stat.S_IFMT(directory_stat.st_mode),
                directory_stat.st_uid,
                directory_stat.st_gid,
                stat.S_IMODE(directory_stat.st_mode),
            )
            expected_directory_values = (
                expected_directory.device,
                expected_directory.inode,
                expected_directory.kind,
                expected_directory.owner,
                expected_directory.group,
                expected_directory.permissions,
            )
            if directory_values != expected_directory_values:
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} parent changed before exec",
                )
            for name, expected_entry in lease.entries:
                try:
                    entry_stat = os.stat(
                        name,
                        dir_fd=lease.directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if expected_entry is None:
                        continue
                    raise OSError(
                        errno.ESTALE,
                        f"{lease.purpose} entry disappeared before exec",
                    )
                if expected_entry is None:
                    raise OSError(
                        errno.ESTALE,
                        f"{lease.purpose} entry appeared before exec",
                    )
                entry_values = (
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                    stat.S_IFMT(entry_stat.st_mode),
                    entry_stat.st_uid,
                    entry_stat.st_gid,
                    stat.S_IMODE(entry_stat.st_mode),
                )
                expected_entry_values = (
                    expected_entry.device,
                    expected_entry.inode,
                    expected_entry.kind,
                    expected_entry.owner,
                    expected_entry.group,
                    expected_entry.permissions,
                )
                if entry_values != expected_entry_values:
                    raise OSError(
                        errno.ESTALE,
                        f"{lease.purpose} entry changed before exec",
                    )
        for lease in directory_exact_inventory_leases:
            require_exact_inventory_in_child(lease, "before exec")
        for lease in directory_absent_entry_leases:
            expected_directory = fingerprint_values(lease.directory_binding.fingerprint)
            directory_path_stat = os.stat(
                lease.directory_binding.path,
                follow_symlinks=False,
            )
            directory_descriptor_stat = os.fstat(lease.directory_descriptor)
            if (
                fingerprint_stat_values(directory_path_stat) != expected_directory
                or fingerprint_stat_values(directory_descriptor_stat)
                != expected_directory
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} directory changed before exec",
                )
            for entry_name in lease.entry_names:
                try:
                    os.stat(
                        entry_name,
                        dir_fd=lease.directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} entry appeared before exec",
                )
            directory_path_after = os.stat(
                lease.directory_binding.path,
                follow_symlinks=False,
            )
            directory_descriptor_after = os.fstat(lease.directory_descriptor)
            if (
                fingerprint_stat_values(directory_path_after) != expected_directory
                or fingerprint_stat_values(directory_descriptor_after)
                != expected_directory
                or not os.access(
                    lease.directory_binding.path,
                    lease.directory_binding.mode,
                    effective_ids=True,
                    follow_symlinks=False,
                )
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.purpose} directory changed before exec",
                )
        for lease, expected_content in descriptor_file_leases:
            expected_directory = lease.directory_binding.fingerprint
            expected_file = lease.binding.fingerprint

            def file_stat_values(path_stat: os.stat_result) -> tuple[int, ...]:
                return (
                    path_stat.st_dev,
                    path_stat.st_ino,
                    stat.S_IFMT(path_stat.st_mode),
                    path_stat.st_uid,
                    path_stat.st_gid,
                    stat.S_IMODE(path_stat.st_mode),
                )

            expected_directory_values = (
                expected_directory.device,
                expected_directory.inode,
                expected_directory.kind,
                expected_directory.owner,
                expected_directory.group,
                expected_directory.permissions,
            )
            expected_file_values = (
                expected_file.device,
                expected_file.inode,
                expected_file.kind,
                expected_file.owner,
                expected_file.group,
                expected_file.permissions,
            )
            directory_path_stat = os.stat(
                lease.directory_binding.path,
                follow_symlinks=False,
            )
            directory_stat = os.fstat(lease.directory_descriptor)
            entry_stat = os.stat(
                lease.entry_name,
                dir_fd=lease.directory_descriptor,
                follow_symlinks=False,
            )
            descriptor_stat = os.fstat(lease.descriptor)
            if (
                file_stat_values(directory_path_stat) != expected_directory_values
                or file_stat_values(directory_stat) != expected_directory_values
                or file_stat_values(entry_stat) != expected_file_values
                or file_stat_values(descriptor_stat) != expected_file_values
                or entry_stat.st_size != lease.binding.size
                or descriptor_stat.st_size != lease.binding.size
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.binding.purpose} changed before exec",
                )
            if not os.access(
                lease.entry_name,
                lease.binding.mode,
                dir_fd=lease.directory_descriptor,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise OSError(
                    errno.EACCES,
                    f"{lease.binding.purpose} access changed before exec",
                )
            digest = hashlib.sha256() if expected_content is None else None
            offset = 0
            while offset < lease.binding.size:
                if time.monotonic() >= command_deadline:
                    raise OSError(
                        errno.ETIMEDOUT,
                        f"{lease.binding.purpose} digest gate exceeded the "
                        "command deadline",
                    )
                chunk = os.pread(
                    lease.descriptor,
                    min(64 * 1024, lease.binding.size - offset),
                    offset,
                )
                if not chunk:
                    raise OSError(
                        errno.ESTALE,
                        f"{lease.binding.purpose} changed size before exec",
                    )
                if expected_content is None:
                    assert digest is not None
                    digest.update(chunk)
                elif chunk != expected_content[offset : offset + len(chunk)]:
                    raise OSError(
                        errno.ESTALE,
                        f"{lease.binding.purpose} content changed before exec",
                    )
                offset += len(chunk)
            if os.pread(lease.descriptor, 1, lease.binding.size):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.binding.purpose} changed size before exec",
                )
            if digest is not None and (
                digest.hexdigest() != lease.binding.content_sha256
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.binding.purpose} content changed before exec",
                )
            entry_after = os.stat(
                lease.entry_name,
                dir_fd=lease.directory_descriptor,
                follow_symlinks=False,
            )
            descriptor_after = os.fstat(lease.descriptor)
            directory_path_after = os.stat(
                lease.directory_binding.path,
                follow_symlinks=False,
            )
            directory_after = os.fstat(lease.directory_descriptor)
            if (
                file_stat_values(entry_after) != expected_file_values
                or file_stat_values(descriptor_after) != expected_file_values
                or file_stat_values(directory_path_after) != expected_directory_values
                or file_stat_values(directory_after) != expected_directory_values
                or entry_after.st_size != lease.binding.size
                or descriptor_after.st_size != lease.binding.size
            ):
                raise OSError(
                    errno.ESTALE,
                    f"{lease.binding.purpose} content changed before exec",
                )
            if not os.access(
                lease.entry_name,
                lease.binding.mode,
                dir_fd=lease.directory_descriptor,
                effective_ids=True,
                follow_symlinks=False,
            ) or not os.access(
                lease.directory_binding.path,
                lease.directory_binding.mode,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise OSError(
                    errno.EACCES,
                    f"{lease.binding.purpose} access changed before exec",
                )
        for lease in directory_exact_inventory_leases:
            require_exact_inventory_in_child(
                lease,
                "during the final exec gate",
            )
        if directory_descriptor is not None:
            os.fchdir(directory_descriptor)
        for helper_lease in helper_leases:
            revalidate_executable_execution_lease_in_child(helper_lease)
        if executable_lease is not None:
            # Keep executable validation after every other filesystem gate.
            revalidate_executable_execution_lease_in_child(executable_lease)
        if time.monotonic() >= command_deadline:
            raise OSError(errno.ETIMEDOUT, "command launch gate exceeded its deadline")
        for signum in MANAGED_CHILD_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, supervisor_signal_mask)

    inherited_descriptors: set[int] = set()
    if directory_descriptor is not None:
        inherited_descriptors.add(directory_descriptor)
    for lease in directory_identity_leases:
        inherited_descriptors.add(lease.descriptor)
        inherited_descriptors.add(lease.parent_descriptor)
    for lease in directory_child_inventory_leases:
        inherited_descriptors.add(lease.directory_descriptor)
    for lease in directory_exact_inventory_leases:
        inherited_descriptors.add(lease.directory_descriptor)
    for lease in directory_absent_entry_leases:
        inherited_descriptors.add(lease.directory_descriptor)
    for lease, _expected_content in descriptor_file_leases:
        inherited_descriptors.add(lease.directory_descriptor)
        inherited_descriptors.add(lease.descriptor)
    for helper_lease in helper_leases:
        inherited_descriptors.add(helper_lease.parent_descriptor)
        inherited_descriptors.add(helper_lease.descriptor)
    if executable_lease is not None:
        inherited_descriptors.add(executable_lease.parent_descriptor)
        inherited_descriptors.add(executable_lease.descriptor)

    signal_lease = SignalOwnershipLease()
    try:
        signal_ownership = signal_lease.__enter__()
    except BaseException:
        close_executable_execution_leases(executable_lease, helper_leases)
        input_file.close()
        raise
    signal_gate = signal_ownership.gate
    supervisor_signal_mask = signal_ownership.supervisor_mask
    spawn_signals_blocked = False
    process: Optional[subprocess.Popen[bytes]] = None
    stdout_pipe: Optional[BinaryIO] = None
    stderr_pipe: Optional[BinaryIO] = None
    selector: Optional[selectors.BaseSelector] = None
    pipe_tracking_complete = False
    main_lease_open = executable_lease is not None
    helper_leases_open = bool(helper_leases)
    outcome_exception: Optional[BaseException] = None
    stdout = bytearray()
    stderr = bytearray()
    try:
        try:
            signal_checkpoint("bounded-command-before-spawn")
            signal.pthread_sigmask(signal.SIG_BLOCK, MANAGED_CHILD_SIGNALS)
            spawn_signals_blocked = True
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd) if cwd else None,
                    env=environment,
                    stdin=input_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=tuple(sorted(inherited_descriptors)),
                    executable=(
                        None
                        if executable_lease is None
                        else executable_subprocess_path(executable_lease)
                    ),
                    preexec_fn=enter_bound_directory,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise GitError(f"failed to start {shell_join(command)}: {exc}") from exc

            stdout_pipe = process.stdout
            stderr_pipe = process.stderr
            if stdout_pipe is None or stderr_pipe is None:
                raise GitError(f"{shell_join(command)} did not provide capture pipes")
            selector = selectors.DefaultSelector()
            selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
            selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
            pipe_tracking_complete = True
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                supervisor_signal_mask,
            )
            spawn_signals_blocked = False
            signal_gate.raise_if_pending()
            close_executable_execution_leases(
                executable_lease,
                (),
            )
            main_lease_open = False
            signal_gate.raise_if_pending()

            deadline = command_deadline
            failure: Optional[str] = None
            while selector.get_map():
                signal_gate.raise_if_pending()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = f"exceeded the {timeout_seconds:g}-second deadline"
                    break
                events = selector.select(min(remaining, 0.25))
                signal_gate.raise_if_pending()
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
            signal_gate.raise_if_pending()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"exceeded the {timeout_seconds:g}-second deadline"
            else:
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = f"exceeded the {timeout_seconds:g}-second deadline"
            signal_gate.raise_if_pending()
            if failure is not None:
                raise PlanError(f"{shell_join(command)} {failure}")
            revalidate_helper_execution_leases(helper_leases)
            try:
                close_executable_execution_leases(None, helper_leases)
            finally:
                helper_leases_open = False
        except BaseException as exc:
            effective_exception = exc
            cleanup_errors: list[str] = []
            if spawn_signals_blocked:
                try:
                    signal.pthread_sigmask(
                        signal.SIG_SETMASK,
                        supervisor_signal_mask,
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        "cannot restore the supervisor signal mask after child "
                        f"spawn: {cleanup_exc}"
                    )
                spawn_signals_blocked = False
            try:
                signal_gate.raise_if_pending()
            except ForwardedProcessSignal as signal_exc:
                effective_exception = signal_exc

            if main_lease_open:
                try:
                    close_executable_execution_leases(
                        executable_lease,
                        (),
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
                main_lease_open = False
            if process is not None:
                if not pipe_tracking_complete:
                    cleanup_errors.append(
                        "process cleanup-incomplete: stdout/stderr pipe tracking "
                        "was not established before supervisor unblocking"
                    )
                try:
                    terminate_process_group(
                        process,
                        initial_signal=(
                            effective_exception.signum
                            if isinstance(
                                effective_exception,
                                ForwardedProcessSignal,
                            )
                            else signal.SIGTERM
                        ),
                        selector=selector,
                    )
                except PlanError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            if helper_leases_open:
                try:
                    revalidate_helper_execution_leases(helper_leases)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
                try:
                    close_executable_execution_leases(
                        None,
                        helper_leases,
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
                helper_leases_open = False
            try:
                signal_gate.raise_if_pending()
            except ForwardedProcessSignal as signal_exc:
                effective_exception = signal_exc

            if isinstance(effective_exception, ForwardedProcessSignal):
                for cleanup_error in cleanup_errors:
                    effective_exception.add_cleanup_error(cleanup_error)
                raise effective_exception from (
                    None if effective_exception is exc else exc
                )
            if cleanup_errors:
                raise PlanError(
                    f"{effective_exception}\n" + "\n".join(cleanup_errors)
                ) from effective_exception
            if effective_exception is exc:
                raise
            raise effective_exception from exc
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
        signal_gate.raise_if_pending()
        return result
    except BaseException as exc:
        outcome_exception = exc
        raise
    finally:
        if spawn_signals_blocked:
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                supervisor_signal_mask,
            )
        signal_lease.finish(outcome_exception)


def run_git_at_directory_descriptor(
    args: list[str],
    directory_descriptor: int,
    *,
    extra_env: Optional[dict[str, str]] = None,
    directory_identity_leases: tuple[DirectoryEntryLease, ...] = (),
    directory_exact_inventory_leases: tuple[DirectoryExactInventoryLease, ...] = (),
    directory_absent_entry_leases: tuple[DirectoryAbsentEntryLease, ...] = (),
    file_content_leases: tuple[DescriptorBoundFileLease, ...] = (),
    digest_file_leases: tuple[DescriptorBoundDigestFileLease, ...] = (),
) -> subprocess.CompletedProcess[str]:
    result = run_bounded_bytes(
        args,
        timeout_seconds=GIT_ENUMERATION_TIMEOUT_SECONDS,
        stdout_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
        stderr_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
        extra_env=extra_env,
        directory_descriptor=directory_descriptor,
        directory_identity_leases=directory_identity_leases,
        directory_exact_inventory_leases=directory_exact_inventory_leases,
        directory_absent_entry_leases=directory_absent_entry_leases,
        file_content_leases=file_content_leases,
        digest_file_leases=digest_file_leases,
    )
    decoded = subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=os.fsdecode(result.stdout),
        stderr=os.fsdecode(result.stderr),
    )
    if decoded.returncode != 0:
        detail = (decoded.stderr or "").strip()
        raise GitError(
            f"{shell_join(list(decoded.args))} failed with exit code "
            f"{decoded.returncode}: {detail}"
        )
    return decoded


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
    preserve_split_index: bool = False,
    directory_descriptor: Optional[int] = None,
    directory_identity_leases: tuple[DirectoryEntryLease, ...] = (),
    directory_exact_inventory_leases: tuple[DirectoryExactInventoryLease, ...] = (),
    directory_absent_entry_leases: tuple[DirectoryAbsentEntryLease, ...] = (),
    file_content_leases: tuple[DescriptorBoundFileLease, ...] = (),
    digest_file_leases: tuple[DescriptorBoundDigestFileLease, ...] = (),
    file_size_limit_bytes: Optional[int] = None,
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
        preserve_split_index=preserve_split_index,
        directory_descriptor=directory_descriptor,
        directory_identity_leases=directory_identity_leases,
        directory_exact_inventory_leases=directory_exact_inventory_leases,
        directory_absent_entry_leases=directory_absent_entry_leases,
        file_content_leases=file_content_leases,
        digest_file_leases=digest_file_leases,
        file_size_limit_bytes=file_size_limit_bytes,
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


def object_identity(fingerprint: FsFingerprint) -> FsObjectIdentity:
    return FsObjectIdentity(
        device=fingerprint.device,
        inode=fingerprint.inode,
        kind=fingerprint.kind,
    )


def posix_access_policy(fingerprint: FsFingerprint) -> PosixAccessPolicy:
    return PosixAccessPolicy(
        owner=fingerprint.owner,
        group=fingerprint.group,
        permissions=fingerprint.permissions,
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


def owner_mode_permits_write_search(fingerprint: FsFingerprint) -> bool:
    required = stat.S_IWUSR | stat.S_IXUSR
    return fingerprint.permissions & required == required


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
    literal_unicode_names = (
        sys.platform.startswith("linux")
        and directory_casefold is False
        and filesystem_magic in LINUX_EXACT_NAME_FILESYSTEM_MAGICS
    )
    normalization = (
        "exact"
        if literal_unicode_names and configured_precompose is not True
        else "NFD"
    )
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


def capture_shared_missing_ancestors(
    entries: list[PlannedWorktree],
) -> dict[tuple[str, ...], SharedMissingAncestor]:
    final_target_roots = {entry.target.relative_parts for entry in entries}
    participants: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for entry in entries:
        target = entry.target
        existing_component_count = len(target.relative_parts) - len(
            target.missing_parts
        )
        # Every final worktree root belongs only to its own entry, including
        # when another target sees it as a missing strict ancestor. Recursive
        # parent roots are bound separately through AppliedTargetRoot receipts.
        for component_count in range(
            existing_component_count + 1,
            len(target.relative_parts),
        ):
            prefix = target.relative_parts[:component_count]
            if prefix in final_target_roots:
                continue
            participants.setdefault(prefix, set()).add(target.relative_parts)
    return {
        prefix: SharedMissingAncestor(
            relative_parts=prefix,
            participant_targets=frozenset(targets),
        )
        for prefix, targets in participants.items()
        if len(targets) > 1
    }


def revalidate_materialized_shared_node(node: BoundNode) -> None:
    try:
        current = filesystem_fingerprint(node.path)
    except PlanError as exc:
        raise PlanError(
            f"plan-owned shared target ancestor changed: {node.path}"
        ) from exc
    if current != node.fingerprint or current.kind != stat.S_IFDIR:
        raise PlanError(f"plan-owned shared target ancestor changed: {node.path}")
    if (
        current.owner != os.geteuid()
        or not owner_mode_permits_write_search(current)
        or not probe_access(node.path, os.W_OK | os.X_OK)
    ):
        raise PlanError(
            "plan-owned shared target ancestor no longer permits "
            f"materialization: {node.path}"
        )


def revalidate_shared_missing_ancestors(
    plan: SyncPlan,
    *,
    materialized_overrides: Optional[dict[tuple[str, ...], BoundNode]] = None,
) -> None:
    ancestors = getattr(plan, "shared_missing_ancestors", {})
    overrides = materialized_overrides or {}
    for relative_parts, ancestor in sorted(
        ancestors.items(),
        key=lambda item: (len(item[0]), item[0]),
    ):
        if ancestor.relative_parts != relative_parts:
            raise PlanError("shared target ancestor receipt has an invalid key")
        path = plan.root.joinpath(*relative_parts)
        materialized_node = overrides.get(
            relative_parts,
            ancestor.materialized_node,
        )
        if materialized_node is not None:
            if materialized_node.path != path:
                raise PlanError("shared target ancestor receipt names the wrong path")
            revalidate_materialized_shared_node(materialized_node)
            continue
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise PlanError(
                f"planned shared target ancestor became unreadable: {path}"
            ) from exc
        except OSError as exc:
            raise PlanError(
                f"cannot revalidate planned shared target ancestor: {path}\n"
                f"  error: {exc}"
            ) from exc
        raise PlanError(
            f"planned shared target ancestor changed after preflight: {path}"
        )


def target_with_materialized_shared_ancestors(
    plan: SyncPlan,
    entry: PlannedWorktree,
) -> BoundTarget:
    ancestors = getattr(plan, "shared_missing_ancestors", {})
    if not ancestors or not entry.target.missing_parts:
        return entry.target
    existing_nodes = list(entry.target.existing_nodes)
    existing_component_count = len(entry.target.relative_parts) - len(
        entry.target.missing_parts
    )
    consumed = 0
    for offset, _part in enumerate(entry.target.missing_parts, start=1):
        prefix = entry.target.relative_parts[: existing_component_count + offset]
        ancestor = ancestors.get(prefix)
        if ancestor is None or ancestor.materialized_node is None:
            break
        if entry.target.relative_parts not in ancestor.participant_targets:
            raise PlanError(
                "planned target attempted to consume another target's shared "
                f"ancestor receipt: {entry.target.path}"
            )
        revalidate_materialized_shared_node(ancestor.materialized_node)
        existing_nodes.append(ancestor.materialized_node)
        consumed = offset
    if consumed == 0:
        return entry.target
    anchor = existing_nodes[-1]
    current_policy = filesystem_name_policy(anchor.path)
    if (
        current_policy.case_sensitive != entry.target.name_policy.case_sensitive
        or current_policy.normalization != entry.target.name_policy.normalization
    ):
        raise PlanError(
            "plan-owned shared target ancestor name semantics changed\n"
            f"  anchor: {anchor.path}"
        )
    return replace(
        entry.target,
        existing_nodes=tuple(existing_nodes),
        missing_parts=entry.target.missing_parts[consumed:],
        name_policy_anchor=anchor,
    )


def prepare_materialized_shared_ancestor_updates(
    plan: SyncPlan,
    created_nodes: tuple[CreatedTargetNode, ...],
    *,
    authorized_targets: frozenset[tuple[str, ...]],
    require_all_participants_authorized: bool,
    existing_updates: Optional[dict[tuple[str, ...], BoundNode]] = None,
) -> dict[tuple[str, ...], BoundNode]:
    ancestors = getattr(plan, "shared_missing_ancestors", {})
    updates = dict(existing_updates or {})
    for created in created_nodes:
        ancestor = ancestors.get(created.relative_parts)
        if ancestor is None:
            continue
        if require_all_participants_authorized:
            authorized = (
                bool(ancestor.participant_targets)
                and ancestor.participant_targets <= authorized_targets
            )
        else:
            authorized = bool(ancestor.participant_targets & authorized_targets)
        if not authorized:
            raise PlanError(
                "planned target created an unowned shared target ancestor\n"
                f"  authorized targets: {sorted(authorized_targets)}\n"
                f"  ancestor: {created.node.path}"
            )
        if created.relative_parts in authorized_targets:
            raise PlanError(
                "a final worktree target cannot become a shared ancestor receipt"
            )
        revalidate_materialized_shared_node(created.node)
        prior = updates.get(
            created.relative_parts,
            ancestor.materialized_node,
        )
        if prior is None:
            updates[created.relative_parts] = created.node
        elif prior != created.node:
            raise PlanError(
                "plan-owned shared target ancestor identity changed during "
                f"materialization: {created.node.path}"
            )
    return updates


def commit_materialized_shared_ancestor_updates(
    plan: SyncPlan,
    updates: dict[tuple[str, ...], BoundNode],
) -> None:
    ancestors = getattr(plan, "shared_missing_ancestors", {})
    revalidate_shared_missing_ancestors(
        plan,
        materialized_overrides=updates,
    )
    for relative_parts, node in updates.items():
        ancestors[relative_parts].materialized_node = node


def record_materialized_shared_ancestors(
    plan: SyncPlan,
    entry: PlannedWorktree,
    created_nodes: tuple[CreatedTargetNode, ...],
) -> None:
    ancestors = getattr(plan, "shared_missing_ancestors", {})
    relevant_nodes = tuple(
        created for created in created_nodes if created.relative_parts in ancestors
    )
    if not relevant_nodes:
        revalidate_shared_missing_ancestors(plan)
        return
    updates = prepare_materialized_shared_ancestor_updates(
        plan,
        relevant_nodes,
        authorized_targets=frozenset((entry.target.relative_parts,)),
        require_all_participants_authorized=False,
    )
    commit_materialized_shared_ancestor_updates(plan, updates)


def recursive_parent_target_owners(
    plan: SyncPlan,
    owner_index: int,
) -> dict[tuple[str, ...], tuple[str, ...]]:
    if owner_index < 0 or owner_index >= len(plan.entries):
        raise PlanError("checkout-created shared ancestor has an invalid owner index")
    owner_by_index: dict[int, tuple[str, ...]] = {}
    owner_by_target: dict[tuple[str, ...], tuple[str, ...]] = {}
    for candidate_index, candidate in enumerate(plan.entries):
        parent_index = candidate.parent_index
        if parent_index == owner_index:
            direct_owner = candidate.target.relative_parts
        elif parent_index is not None and parent_index in owner_by_index:
            direct_owner = owner_by_index[parent_index]
        else:
            continue
        prior = owner_by_target.get(candidate.target.relative_parts)
        if prior is not None and prior != direct_owner:
            raise PlanError(
                "recursive target belongs to multiple direct-child subtrees"
            )
        owner_by_index[candidate_index] = direct_owner
        owner_by_target[candidate.target.relative_parts] = direct_owner
    return owner_by_target


def recursive_parent_subtree_target_parts(
    plan: SyncPlan,
    owner_index: int,
) -> frozenset[tuple[str, ...]]:
    return frozenset(recursive_parent_target_owners(plan, owner_index))


def capture_checkout_materialized_shared_ancestors(
    plan: SyncPlan,
    owner_index: int,
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
) -> tuple[CreatedTargetNode, ...]:
    """Bind plan-owned shared prefixes created by a recursive parent checkout.

    Protected properties are the exact directory-entry/object identity, owner,
    group, permission mode, and effective write/search access of every shared
    prefix component. Traversal starts at the held parent-target descriptor and
    never follows a symlink. Path checks only prove that the descriptor-bound
    object is still published at the planned entry; they do not replace the
    descriptor identity checks.
    """

    if plan.entries[owner_index] is not entry:
        raise PlanError("checkout-created shared ancestor has the wrong owner entry")
    revalidate_materialized_target_lease(lease)
    target_fingerprint = fingerprint_from_stat(os.fstat(lease.target_descriptor))
    if (
        lease.target != entry.target.path
        or lease.target_binding.path != entry.target.path
        or target_fingerprint != lease.target_binding.fingerprint
    ):
        raise PlanError(
            "checkout-created shared ancestor lease does not match its parent target"
        )

    target_owners = recursive_parent_target_owners(plan, owner_index)
    parent_parts = entry.target.relative_parts
    eligible: list[tuple[str, ...]] = []
    for relative_parts, ancestor in getattr(
        plan,
        "shared_missing_ancestors",
        {},
    ).items():
        if ancestor.materialized_node is not None:
            continue
        if (
            len(relative_parts) <= len(parent_parts)
            or relative_parts[: len(parent_parts)] != parent_parts
        ):
            continue
        if not ancestor.participant_targets:
            raise PlanError("checkout-created shared ancestor has no plan participants")
        participant_owners = {
            target_owners.get(participant)
            for participant in ancestor.participant_targets
        }
        if None in participant_owners:
            continue
        if any(
            direct_owner is None
            or len(direct_owner) <= len(relative_parts)
            or direct_owner[: len(relative_parts)] != relative_parts
            for direct_owner in participant_owners
        ):
            # A prefix beneath a direct child's final worktree root belongs
            # to that child transaction, not to the recursive parent checkout.
            continue
        eligible.append(relative_parts)

    captured: list[CreatedTargetNode] = []
    for relative_parts in sorted(eligible, key=lambda parts: (len(parts), parts)):
        suffix = relative_parts[len(parent_parts) :]
        current_descriptor = os.dup(lease.target_descriptor)
        current_path = entry.target.path
        current_fingerprint = target_fingerprint
        missing = False
        try:
            for part in suffix:
                validate_descriptor_entry_name(part)
                current_binding = AccessBinding(
                    path=current_path,
                    fingerprint=current_fingerprint,
                    mode=os.W_OK | os.X_OK,
                    purpose="checkout-created shared ancestor parent",
                )
                revalidate_directory_descriptor(
                    current_binding,
                    current_descriptor,
                )
                child_path = current_path / part
                try:
                    entry_before = fingerprint_from_stat(
                        os.stat(
                            part,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except FileNotFoundError:
                    missing = True
                    break
                except OSError as exc:
                    raise PlanError(
                        "cannot inspect checkout-created shared target ancestor\n"
                        f"  path: {child_path}\n"
                        f"  error: {exc}"
                    ) from exc
                if entry_before.kind != stat.S_IFDIR:
                    raise PlanError(
                        "checkout-created shared target ancestor is not a directory\n"
                        f"  path: {child_path}"
                    )
                try:
                    child_descriptor = os.open(
                        part,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                        dir_fd=current_descriptor,
                    )
                except OSError as exc:
                    raise PlanError(
                        "cannot bind checkout-created shared target ancestor\n"
                        f"  path: {child_path}\n"
                        f"  error: {exc}"
                    ) from exc
                try:
                    child_fingerprint = fingerprint_from_stat(
                        os.fstat(child_descriptor)
                    )
                    entry_after = fingerprint_from_stat(
                        os.stat(
                            part,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                    )
                    path_fingerprint = filesystem_fingerprint(child_path)
                    if (
                        child_fingerprint != entry_before
                        or child_fingerprint != entry_after
                        or child_fingerprint != path_fingerprint
                    ):
                        raise PlanError(
                            "checkout-created shared target ancestor changed "
                            "during descriptor binding\n"
                            f"  path: {child_path}"
                        )
                    if child_fingerprint.owner != os.geteuid():
                        raise PlanError(
                            "checkout-created shared target ancestor has the "
                            "wrong owner\n"
                            f"  path: {child_path}"
                        )
                    if not owner_mode_permits_write_search(
                        child_fingerprint
                    ) or not probe_access_at(
                        child_descriptor,
                        ".",
                        os.W_OK | os.X_OK,
                    ):
                        if entry.state != "managed":
                            # Only a newly registered, transaction-owned parent
                            # is removed by the enclosing rollback. A managed
                            # checkout survives this failure, so changing its
                            # access policy here would be a permanent mutation.
                            try:
                                os.fchmod(
                                    child_descriptor,
                                    child_fingerprint.permissions | 0o700,
                                )
                            except OSError as exc:
                                raise PlanError(
                                    "checkout-created shared target ancestor "
                                    "does not permit descendant materialization "
                                    "and cannot be prepared for rollback\n"
                                    f"  path: {child_path}\n"
                                    f"  error: {exc}"
                                ) from exc
                        raise PlanError(
                            "checkout-created shared target ancestor does not "
                            "permit descendant materialization\n"
                            f"  path: {child_path}"
                        )
                except BaseException:
                    os.close(child_descriptor)
                    raise
                os.close(current_descriptor)
                current_descriptor = child_descriptor
                current_path = child_path
                current_fingerprint = child_fingerprint
            if not missing:
                captured.append(
                    CreatedTargetNode(
                        relative_parts=relative_parts,
                        node=BoundNode(current_path, current_fingerprint),
                    )
                )
        finally:
            os.close(current_descriptor)

    revalidate_materialized_target_lease(lease)
    for created in captured:
        revalidate_materialized_shared_node(created.node)
    return tuple(captured)


def record_checkout_materialized_shared_ancestors(
    plan: SyncPlan,
    owner_index: int,
    created_nodes: tuple[CreatedTargetNode, ...],
) -> None:
    updates = prepare_materialized_shared_ancestor_updates(
        plan,
        created_nodes,
        authorized_targets=recursive_parent_subtree_target_parts(
            plan,
            owner_index,
        ),
        require_all_participants_authorized=True,
    )
    commit_materialized_shared_ancestor_updates(plan, updates)


def revalidate_applied_target_root(
    plan: SyncPlan,
    owner_index: int,
) -> AppliedTargetRoot:
    receipts = getattr(plan, "applied_target_roots", {})
    receipt = receipts.get(owner_index)
    if receipt is None:
        raise PlanError(
            "recursive target lacks its applied parent-root identity receipt"
        )
    if (
        owner_index < 0
        or owner_index >= len(plan.entries)
        or receipt.owner_index != owner_index
    ):
        raise PlanError("applied target-root receipt has an invalid owner index")
    owner = plan.entries[owner_index]
    if (
        receipt.relative_parts != owner.target.relative_parts
        or receipt.node.path != owner.target.path
    ):
        raise PlanError(
            "applied target-root receipt does not match its owning plan entry"
        )
    try:
        current = filesystem_fingerprint(receipt.node.path)
    except PlanError as exc:
        raise PlanError(
            f"applied recursive parent root changed: {receipt.node.path}"
        ) from exc
    if current != receipt.node.fingerprint or current.kind != stat.S_IFDIR:
        raise PlanError(f"applied recursive parent root changed: {receipt.node.path}")
    if not probe_access(receipt.node.path, os.R_OK | os.W_OK | os.X_OK):
        raise PlanError(
            "applied recursive parent root no longer permits descendant "
            f"materialization: {receipt.node.path}"
        )
    return receipt


def record_applied_target_root(
    plan: SyncPlan,
    owner_index: int,
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
) -> None:
    receipts = getattr(plan, "applied_target_roots", None)
    if receipts is None:
        raise PlanError("sync plan cannot retain applied target-root receipts")
    if (
        owner_index < 0
        or owner_index >= len(plan.entries)
        or plan.entries[owner_index] is not entry
    ):
        raise PlanError("applied target-root receipt has an invalid owner entry")
    revalidate_materialized_target_lease(lease)
    target_fingerprint = fingerprint_from_stat(os.fstat(lease.target_descriptor))
    if (
        lease.target != entry.target.path
        or lease.target_binding.path != entry.target.path
        or target_fingerprint != lease.target_binding.fingerprint
    ):
        raise PlanError(
            "applied target-root lease does not match its owning plan entry"
        )
    receipt = AppliedTargetRoot(
        owner_index=owner_index,
        relative_parts=entry.target.relative_parts,
        node=BoundNode(entry.target.path, target_fingerprint),
    )
    prior = receipts.get(owner_index)
    if prior is not None and prior != receipt:
        raise PlanError(f"applied target-root identity changed: {entry.target.path}")
    receipts[owner_index] = receipt
    revalidate_applied_target_root(plan, owner_index)


def remove_created_target_node(
    target: BoundTarget,
    created_nodes: tuple[CreatedTargetNode, ...],
    created: CreatedTargetNode,
) -> None:
    """Remove one transaction-created directory through its bound parent chain."""

    root = target.existing_nodes[0]
    expected_by_path = {node.path: node.fingerprint for node in target.existing_nodes}
    expected_by_path.update(
        {candidate.node.path: candidate.node.fingerprint for candidate in created_nodes}
    )
    current_descriptor = -1
    leaf_descriptor = -1
    try:
        try:
            current_descriptor = open_directory_descriptor(
                root.path,
                "target materialization cleanup root",
            )
        except PlanError as exc:
            raise TargetMaterializationCleanupError(
                status="root-unavailable",
                location=root.path,
                expected=root.fingerprint,
                detail=str(exc),
            ) from exc
        current_path = root.path
        current_fingerprint = fingerprint_from_stat(os.fstat(current_descriptor))
        if current_fingerprint != root.fingerprint:
            raise TargetMaterializationCleanupError(
                status="root-identity-mismatch",
                location=root.path,
                expected=root.fingerprint,
                detail="cleanup root no longer names the preflight object",
            )

        for part in created.relative_parts[:-1]:
            validate_descriptor_entry_name(part)
            child_path = current_path / part
            expected = expected_by_path.get(child_path)
            if expected is None:
                raise TargetMaterializationCleanupError(
                    status="parent-chain-unbound",
                    location=child_path,
                    expected=None,
                    detail="cleanup parent is outside the materialization receipt",
                )
            try:
                revalidate_directory_descriptor(
                    AccessBinding(
                        path=current_path,
                        fingerprint=current_fingerprint,
                        mode=os.X_OK,
                        purpose="target materialization cleanup traversal",
                    ),
                    current_descriptor,
                )
                entry_before = fingerprint_from_stat(
                    os.stat(
                        part,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                )
            except FileNotFoundError as exc:
                raise TargetMaterializationCleanupError(
                    status="parent-chain-missing",
                    location=child_path,
                    expected=expected,
                    detail="a cleanup parent disappeared",
                ) from exc
            except (OSError, PlanError) as exc:
                raise TargetMaterializationCleanupError(
                    status="parent-chain-unreadable",
                    location=child_path,
                    expected=expected,
                    detail=str(exc),
                ) from exc
            if entry_before != expected or entry_before.kind != stat.S_IFDIR:
                raise TargetMaterializationCleanupError(
                    status="parent-identity-mismatch",
                    location=child_path,
                    expected=expected,
                    detail="a cleanup parent was replaced or its policy changed",
                )
            child_descriptor = -1
            try:
                child_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=current_descriptor,
                )
                child_fingerprint = fingerprint_from_stat(os.fstat(child_descriptor))
                entry_after = fingerprint_from_stat(
                    os.stat(
                        part,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                )
            except OSError as exc:
                if child_descriptor >= 0:
                    os.close(child_descriptor)
                raise TargetMaterializationCleanupError(
                    status="parent-chain-unreadable",
                    location=child_path,
                    expected=expected,
                    detail=str(exc),
                ) from exc
            if child_fingerprint != expected or entry_after != expected:
                os.close(child_descriptor)
                raise TargetMaterializationCleanupError(
                    status="parent-identity-mismatch",
                    location=child_path,
                    expected=expected,
                    detail="a cleanup parent changed during descriptor binding",
                )
            os.close(current_descriptor)
            current_descriptor = child_descriptor
            current_path = child_path
            current_fingerprint = child_fingerprint

        leaf_name = created.relative_parts[-1]
        validate_descriptor_entry_name(leaf_name)
        try:
            revalidate_directory_descriptor(
                AccessBinding(
                    path=current_path,
                    fingerprint=current_fingerprint,
                    mode=os.W_OK | os.X_OK,
                    purpose="target materialization cleanup parent",
                ),
                current_descriptor,
            )
        except PlanError as exc:
            raise TargetMaterializationCleanupError(
                status="parent-access-or-identity-drift",
                location=current_path,
                expected=current_fingerprint,
                detail=str(exc),
            ) from exc
        if not probe_access_at(
            current_descriptor,
            ".",
            os.W_OK | os.X_OK,
        ):
            raise TargetMaterializationCleanupError(
                status="parent-access-blocked",
                location=current_path,
                expected=current_fingerprint,
                detail="cleanup parent lacks effective write/search access",
            )
        try:
            entry_before = fingerprint_from_stat(
                os.stat(
                    leaf_name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TargetMaterializationCleanupError(
                status="entry-unreadable",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail=str(exc),
            ) from exc
        if (
            entry_before != created.node.fingerprint
            or entry_before.kind != stat.S_IFDIR
        ):
            raise TargetMaterializationCleanupError(
                status="entry-identity-mismatch",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail="transaction-created directory was replaced or its policy changed",
            )
        try:
            leaf_descriptor = os.open(
                leaf_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=current_descriptor,
            )
            leaf_fingerprint = fingerprint_from_stat(os.fstat(leaf_descriptor))
            entry_after = fingerprint_from_stat(
                os.stat(
                    leaf_name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            )
        except OSError as exc:
            raise TargetMaterializationCleanupError(
                status="entry-unreadable",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail=str(exc),
            ) from exc
        if (
            leaf_fingerprint != created.node.fingerprint
            or entry_after != created.node.fingerprint
        ):
            raise TargetMaterializationCleanupError(
                status="entry-identity-mismatch",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail="transaction-created directory changed during cleanup binding",
            )
        try:
            os.rmdir(leaf_name, dir_fd=current_descriptor)
        except OSError as exc:
            raise TargetMaterializationCleanupError(
                status=(
                    "entry-not-empty"
                    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}
                    else "rmdir-failed"
                ),
                location=created.node.path,
                expected=created.node.fingerprint,
                detail=str(exc),
            ) from exc
        if fingerprint_from_stat(os.fstat(leaf_descriptor)) != created.node.fingerprint:
            raise TargetMaterializationCleanupError(
                status="removed-object-identity-drift",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail="the opened directory identity changed during rmdir",
            )
        try:
            os.stat(
                leaf_name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TargetMaterializationCleanupError(
                status="post-rmdir-unreadable",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail=str(exc),
            ) from exc
        else:
            raise TargetMaterializationCleanupError(
                status="post-rmdir-replacement",
                location=created.node.path,
                expected=created.node.fingerprint,
                detail="an entry appeared at the cleaned name",
            )
        try:
            revalidate_directory_descriptor(
                AccessBinding(
                    path=current_path,
                    fingerprint=current_fingerprint,
                    mode=os.W_OK | os.X_OK,
                    purpose="target materialization cleanup parent",
                ),
                current_descriptor,
            )
        except PlanError as exc:
            raise TargetMaterializationCleanupError(
                status="parent-post-rmdir-drift",
                location=current_path,
                expected=current_fingerprint,
                detail=str(exc),
            ) from exc
    finally:
        if leaf_descriptor >= 0:
            os.close(leaf_descriptor)
        if current_descriptor >= 0:
            os.close(current_descriptor)


def cleanup_materialized_target_nodes(
    target: BoundTarget,
    created_nodes: tuple[CreatedTargetNode, ...],
) -> None:
    for created in reversed(created_nodes):
        remove_created_target_node(
            target,
            created_nodes,
            created,
        )


def materialize_bound_target_directory(
    target: BoundTarget,
) -> MaterializedTargetLease:
    # Protected property: every created component is reached from the exact
    # descriptor-bound root without following symlinks. The final directory
    # object and its direct parent remain held through Git's write and are
    # checked again afterwards.
    if not target.relative_parts:
        raise PlanError("a submodule worktree target cannot be the target root")
    expected_by_path = {node.path: node.fingerprint for node in target.existing_nodes}
    root = target.existing_nodes[0]
    current_descriptor = open_directory_descriptor(
        root.path,
        "target materialization root",
    )
    parent_descriptor = -1
    target_descriptor = -1
    created_nodes: list[CreatedTargetNode] = []
    unbound_created_path: Optional[Path] = None
    lease: Optional[MaterializedTargetLease] = None
    try:
        root_binding = AccessBinding(
            path=root.path,
            fingerprint=root.fingerprint,
            mode=os.X_OK,
            purpose="target materialization root",
        )
        revalidate_directory_descriptor(root_binding, current_descriptor)
        current_path = root.path
        for index, part in enumerate(target.relative_parts):
            child_path = current_path / part
            expected = expected_by_path.get(child_path)
            if expected is None:
                if not probe_access_at(
                    current_descriptor,
                    ".",
                    os.W_OK | os.X_OK,
                ):
                    raise PlanError(
                        "target parent no longer permits descriptor-relative "
                        f"creation: {current_path}"
                    )
                try:
                    os.mkdir(part, mode=0o777, dir_fd=current_descriptor)
                except FileExistsError as exc:
                    raise PlanError(
                        "target path appeared during descriptor-relative "
                        f"materialization: {child_path}"
                    ) from exc
                except OSError as exc:
                    raise PlanError(
                        "cannot create descriptor-relative target directory\n"
                        f"  path: {child_path}\n"
                        f"  error: {exc}"
                    ) from exc
                unbound_created_path = child_path
                # Portable mkdirat does not return a descriptor. The first
                # no-follow entry snapshot plus the immediately following
                # open/fstat/entry recheck defines the created-object receipt.
                # A malicious same-UID replacement before that first snapshot
                # is outside this portable cooperative-race guarantee.
                try:
                    created_fingerprint = fingerprint_from_stat(
                        os.stat(
                            part,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except OSError as exc:
                    raise PlanError(
                        "cannot bind newly created target directory\n"
                        f"  path: {child_path}\n"
                        f"  error: {exc}"
                    ) from exc
                if created_fingerprint.kind != stat.S_IFDIR:
                    raise PlanError(
                        "newly created target entry is not a directory\n"
                        f"  path: {child_path}"
                    )
                created_nodes.append(
                    CreatedTargetNode(
                        relative_parts=target.relative_parts[: index + 1],
                        node=BoundNode(child_path, created_fingerprint),
                    )
                )
                unbound_created_path = None
            try:
                child_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=current_descriptor,
                )
            except OSError as exc:
                raise PlanError(
                    "cannot open descriptor-relative target directory\n"
                    f"  path: {child_path}\n"
                    f"  error: {exc}"
                ) from exc
            child_fingerprint = fingerprint_from_stat(os.fstat(child_descriptor))
            if expected is not None and child_fingerprint != expected:
                os.close(child_descriptor)
                raise PlanError(f"target-path object or policy changed: {child_path}")
            if (
                expected is None
                and child_fingerprint != created_nodes[-1].node.fingerprint
            ):
                os.close(child_descriptor)
                raise PlanError(
                    "newly created target directory changed before descriptor binding\n"
                    f"  path: {child_path}"
                )
            try:
                path_fingerprint = fingerprint_from_stat(
                    os.stat(
                        part,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                )
            except OSError as exc:
                os.close(child_descriptor)
                raise PlanError(
                    "cannot revalidate descriptor-relative target directory\n"
                    f"  path: {child_path}\n"
                    f"  error: {exc}"
                ) from exc
            if path_fingerprint != child_fingerprint:
                os.close(child_descriptor)
                raise PlanError(
                    "target directory entry changed during descriptor binding\n"
                    f"  path: {child_path}"
                )
            if index == len(target.relative_parts) - 1:
                parent_descriptor = current_descriptor
                target_descriptor = child_descriptor
                current_descriptor = -1
                current_path = child_path
                break
            os.close(current_descriptor)
            current_descriptor = child_descriptor
            current_path = child_path

        if parent_descriptor < 0 or target_descriptor < 0:
            raise PlanError("target materialization did not reach the final directory")
        parent_path = target.path.parent
        parent_fingerprint = fingerprint_from_stat(os.fstat(parent_descriptor))
        target_fingerprint = fingerprint_from_stat(os.fstat(target_descriptor))
        parent_binding = AccessBinding(
            path=parent_path,
            fingerprint=parent_fingerprint,
            mode=(os.W_OK | os.X_OK if target.missing_parts else os.X_OK),
            purpose="descriptor-bound target parent",
        )
        target_binding = AccessBinding(
            path=target.path,
            fingerprint=target_fingerprint,
            mode=os.R_OK | os.W_OK | os.X_OK,
            purpose="descriptor-bound target worktree",
        )
        lease = MaterializedTargetLease(
            target=target.path,
            target_binding=target_binding,
            target_descriptor=target_descriptor,
            parent_binding=parent_binding,
            parent_descriptor=parent_descriptor,
            entry_name=target.relative_parts[-1],
            created_nodes=tuple(created_nodes),
            materialization_target=target,
        )
        revalidate_materialized_target_lease(lease)
        return lease
    except BaseException as exc:
        descriptor_cleanup_error: Optional[BaseException] = None
        if lease is not None:
            try:
                lease.close()
            except BaseException as cleanup_exc:
                descriptor_cleanup_error = cleanup_exc
            parent_descriptor = -1
            target_descriptor = -1
        for descriptor in (
            target_descriptor,
            parent_descriptor,
            current_descriptor,
        ):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except BaseException as cleanup_exc:
                if descriptor_cleanup_error is None:
                    descriptor_cleanup_error = cleanup_exc

        recovery_error: Optional[BaseException] = None
        if unbound_created_path is not None:
            recovery_error = TargetMaterializationCleanupError(
                status="created-identity-unbound",
                location=unbound_created_path,
                expected=None,
                detail="mkdir succeeded but the new object could not be safely bound",
            )
        else:
            try:
                cleanup_materialized_target_nodes(
                    target,
                    tuple(created_nodes),
                )
            except BaseException as cleanup_exc:
                recovery_error = cleanup_exc
        if descriptor_cleanup_error is not None and recovery_error is None:
            recovery_error = TargetMaterializationCleanupError(
                status="descriptor-cleanup-failed",
                location=target.path,
                expected=None,
                detail=str(descriptor_cleanup_error),
            )
        if recovery_error is not None:
            raise PlanError(f"{exc}\n{recovery_error}") from exc
        raise


def revalidate_materialized_target_lease(
    lease: MaterializedTargetLease,
) -> None:
    revalidate_directory_descriptor(
        lease.parent_binding,
        lease.parent_descriptor,
    )
    revalidate_directory_descriptor(
        lease.target_binding,
        lease.target_descriptor,
    )
    try:
        entry_fingerprint = fingerprint_from_stat(
            os.stat(
                lease.entry_name,
                dir_fd=lease.parent_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError as exc:
        raise PlanError(
            "cannot revalidate the descriptor-bound target entry\n"
            f"  path: {lease.target}\n"
            f"  error: {exc}"
        ) from exc
    if entry_fingerprint != lease.target_binding.fingerprint:
        raise PlanError(
            "target directory entry changed during the Git write\n"
            f"  path: {lease.target}"
        )


def materialized_target_exec_identity_lease(
    lease: MaterializedTargetLease,
) -> DirectoryEntryLease:
    """Borrow a target lease for the child process identity gate.

    The returned view does not own its descriptors and must not be closed.
    The materialized target lease retains ownership through the subprocess.
    """

    revalidate_materialized_target_lease(lease)
    return DirectoryEntryLease(
        path=lease.target,
        binding=lease.target_binding,
        descriptor=lease.target_descriptor,
        parent_binding=lease.parent_binding,
        parent_descriptor=lease.parent_descriptor,
        entry_name=lease.entry_name,
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


def _capture_commit_gitmodules_with_receipt(
    source_git_dir: Path,
    work_tree: Path,
    commit: str,
    budget: Optional[GitmodulesReadBudget] = None,
) -> tuple[list[Submodule], CommitGitmodulesReceipt]:
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
        return (
            [],
            CommitGitmodulesReceipt(
                commit=commit,
                entry_mode=None,
                blob_id=None,
                content_size=0,
                content_sha256=None,
                modules=(),
            ),
        )
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
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id):
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
    blob_digest = hashlib.sha1() if len(object_id) == 40 else hashlib.sha256()
    blob_digest.update(f"blob {blob_size}\0".encode("ascii"))
    blob_digest.update(content_result.stdout)
    if blob_digest.hexdigest() != object_id:
        raise PlanError(
            f"{commit}:.gitmodules blob content does not match its object id"
        )
    budget.retain(blob_size, f"{commit}:.gitmodules")
    modules = decode_gitmodules(content_result.stdout, f"{commit}:.gitmodules")
    return (
        modules,
        CommitGitmodulesReceipt(
            commit=commit,
            entry_mode=os.fsdecode(fields[0]),
            blob_id=object_id,
            content_size=blob_size,
            content_sha256=hashlib.sha256(content_result.stdout).hexdigest(),
            modules=tuple((module.name, module.path, module.url) for module in modules),
        ),
    )


def read_commit_gitmodules(
    source_git_dir: Path,
    work_tree: Path,
    commit: str,
    budget: Optional[GitmodulesReadBudget] = None,
) -> list[Submodule]:
    modules, _receipt = _capture_commit_gitmodules_with_receipt(
        source_git_dir,
        work_tree,
        commit,
        budget,
    )
    return modules


def expected_sha(root: Path, rel_path: str) -> str:
    receipt = capture_superproject_index_receipt(root, (rel_path,))
    return receipt.selected_gitlinks[0][1]


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
        # This read-only query must observe an existing `link` extension.
        # The ordinary command profile disables new split-index writes, but
        # applying that override here makes Git hide the linked shared index.
        preserve_split_index=True,
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


def capture_superproject_index_snapshot(
    index_paths: tuple[Path, ...],
    *,
    expected_bindings: Optional[tuple[FileContentBinding, ...]] = None,
) -> tuple[tuple[FileContentBinding, ...], tuple[bytes, ...]]:
    """Bind source object identity, bytes, and effective read policy.

    The retained bytes, rather than a later pathname open, become the selected
    gitlink query input. FileContentBinding deliberately excludes timestamps,
    so a restored same-object/same-content ABA is benign at this source layer.
    """

    if len(index_paths) not in {1, 2}:
        raise PlanError("superproject index path set has an invalid shape")
    if expected_bindings is not None and (
        tuple(binding.path for binding in expected_bindings) != index_paths
    ):
        raise PlanError("superproject index path set changed after preflight")
    bindings: list[FileContentBinding] = []
    contents: list[bytes] = []
    for index, path in enumerate(index_paths):
        binding, content = read_bound_regular_file(
            path,
            maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
            mode=os.R_OK,
            purpose="superproject index",
            retain_content=True,
        )
        if content is None:
            raise PlanError("superproject index binding returned no exact bytes")
        if expected_bindings is not None:
            require_matching_file_binding(
                expected_bindings[index],
                binding,
                "superproject index",
            )
        bindings.append(binding)
        contents.append(content)
    for binding in bindings:
        revalidate_file_content_binding(binding)
    return tuple(bindings), tuple(contents)


def superproject_index_snapshot_names(
    bindings: tuple[FileContentBinding, ...],
) -> tuple[str, ...]:
    if len(bindings) not in {1, 2}:
        raise PlanError("superproject index binding set has an invalid shape")
    if len(bindings) == 1:
        return ("index",)
    shared_name = os.fsdecode(validate_descriptor_entry_name(bindings[1].path.name))
    if not re.fullmatch(r"sharedindex\.(?:[0-9a-f]{40}|[0-9a-f]{64})", shared_name):
        raise PlanError("superproject shared index has an invalid filename")
    return "index", shared_name


def captured_superproject_index_object_id_bytes(
    content: bytes,
    purpose: str,
) -> int:
    candidates: list[int] = []
    for object_id_bytes, constructor in ((20, hashlib.sha1), (32, hashlib.sha256)):
        if len(content) < 12 + object_id_bytes:
            continue
        digest = constructor()
        digest.update(memoryview(content)[:-object_id_bytes])
        if digest.digest() == content[-object_id_bytes:]:
            candidates.append(object_id_bytes)
    if len(candidates) != 1:
        raise PlanError(
            f"{purpose} checksum does not identify exactly one supported object format"
        )
    return candidates[0]


def parse_captured_superproject_index(
    content: bytes,
    object_id_bytes: int,
    purpose: str,
) -> ParsedCapturedSuperprojectIndex:
    """Decode one exact v2/v3/v4 index byte string without reopening a path."""

    if object_id_bytes not in {20, 32}:
        raise PlanError(f"{purpose} uses an unsupported object format")
    if len(content) < 12 + object_id_bytes:
        raise PlanError(f"{purpose} is truncated")
    body_end = len(content) - object_id_bytes
    digest = hashlib.sha1() if object_id_bytes == 20 else hashlib.sha256()
    digest.update(memoryview(content)[:body_end])
    if digest.digest() != content[body_end:]:
        raise PlanError(f"{purpose} checksum is invalid")
    if content[:4] != b"DIRC":
        raise PlanError(f"{purpose} has an invalid signature")
    version = int.from_bytes(content[4:8], "big")
    if version not in {2, 3, 4}:
        raise PlanError(f"{purpose} uses an unsupported version: {version}")
    entry_count = int.from_bytes(content[8:12], "big")
    if entry_count > MAX_CHECKOUT_PATHS:
        raise PlanError(
            f"{purpose} exceeds the {MAX_CHECKOUT_PATHS}-entry safety limit"
        )

    offset = 12
    prior_path: Optional[bytes] = None
    expanded_path_bytes = 0
    entries: list[CapturedSuperprojectIndexEntry] = []
    fixed_prefix_bytes = 40 + object_id_bytes + 2
    for _entry_index in range(entry_count):
        entry_start = offset
        if offset + fixed_prefix_bytes > body_end:
            raise PlanError(f"{purpose} has a truncated entry")
        mode = int.from_bytes(content[offset + 24 : offset + 28], "big")
        object_start = offset + 40
        object_end = object_start + object_id_bytes
        object_id = content[object_start:object_end]
        flags = int.from_bytes(content[object_end : object_end + 2], "big")
        offset = object_end + 2
        stage = (flags >> 12) & 0x3
        if flags & 0x4000:
            if version == 2 or offset + 2 > body_end:
                raise PlanError(f"{purpose} has invalid extended entry flags")
            extended_flags = int.from_bytes(content[offset : offset + 2], "big")
            offset += 2
            if extended_flags & ~0x6000:
                raise PlanError(f"{purpose} has unsupported extended entry flags")

        if version == 4:
            strip_count, offset = decode_index_v4_strip_count(
                content,
                offset,
                body_end,
                purpose=purpose,
            )
            retained_path = prior_path or b""
            if strip_count > len(retained_path):
                raise PlanError(f"{purpose} has an invalid v4 path prefix")
            suffix_end = content.find(b"\0", offset, body_end)
            if suffix_end < 0:
                raise PlanError(f"{purpose} has an unterminated v4 path")
            retained_size = len(retained_path) - strip_count
            suffix_size = suffix_end - offset
            if retained_size + suffix_size > MAX_CHECKOUT_PATH_BYTES:
                raise PlanError(f"{purpose} has an oversized v4 path")
            raw_path = retained_path[:retained_size] + content[offset:suffix_end]
            offset = suffix_end + 1
        else:
            path_end = content.find(b"\0", offset, body_end)
            if path_end < 0:
                raise PlanError(f"{purpose} has an unterminated path")
            if path_end - offset > MAX_CHECKOUT_PATH_BYTES:
                raise PlanError(f"{purpose} has an oversized path")
            raw_path = content[offset:path_end]
            relative_size = path_end + 1 - entry_start
            padded_size = (relative_size + 7) & ~7
            offset = entry_start + padded_size
            if offset > body_end or any(content[path_end + 1 : offset]):
                raise PlanError(f"{purpose} has invalid entry padding")

        declared_length = flags & 0x0FFF
        if declared_length != min(len(raw_path), 0x0FFF):
            raise PlanError(f"{purpose} path length does not match its flags")
        if raw_path:
            validate_checkout_path(raw_path, purpose)
            expanded_path_bytes += len(raw_path) + 1
            if expanded_path_bytes > GIT_ENUMERATION_OUTPUT_LIMIT_BYTES:
                raise PlanError(
                    f"{purpose} paths exceed the "
                    f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
                )
        prior_path = raw_path
        entries.append(
            CapturedSuperprojectIndexEntry(
                raw_path=raw_path,
                mode=mode,
                object_id=object_id,
                stage=stage,
            )
        )

    extensions: list[tuple[bytes, bytes]] = []
    extension_count = 0
    while offset < body_end:
        extension_count += 1
        if extension_count > MAX_CHECKOUT_PATHS:
            raise PlanError(
                f"{purpose} exceeds the {MAX_CHECKOUT_PATHS}-extension safety limit"
            )
        if offset + 8 > body_end:
            raise PlanError(f"{purpose} has a truncated extension")
        signature = content[offset : offset + 4]
        extension_size = int.from_bytes(content[offset + 4 : offset + 8], "big")
        offset += 8
        extension_end = offset + extension_size
        if extension_end > body_end:
            raise PlanError(f"{purpose} has an oversized extension")
        if signature == b"sdir":
            raise PlanError(f"{purpose} uses unsupported sparse-index entry semantics")
        if signature != b"link" and not 0x41 <= signature[0] <= 0x5A:
            raise PlanError(
                f"{purpose} uses an unsupported mandatory extension: {signature!r}"
            )
        if signature == b"link":
            maximum_ewah_words = ((MAX_CHECKOUT_PATHS + 63) // 64) * 2 + 1
            maximum_link_bytes = object_id_bytes + 2 * (12 + maximum_ewah_words * 8)
            if extension_size > maximum_link_bytes:
                raise PlanError(f"{purpose} link extension exceeds its safety limit")
            extensions.append((signature, content[offset:extension_end]))
        offset = extension_end
    return ParsedCapturedSuperprojectIndex(
        object_id_bytes=object_id_bytes,
        entries=tuple(entries),
        extensions=tuple(extensions),
    )


def require_canonical_captured_index_entries(
    entries: tuple[CapturedSuperprojectIndexEntry, ...],
    purpose: str,
) -> None:
    prior_key: Optional[tuple[bytes, int]] = None
    for entry in entries:
        if not entry.raw_path:
            raise PlanError(
                f"{purpose} contains an empty path outside split replacement"
            )
        key = (entry.raw_path, entry.stage)
        if prior_key is not None and key == prior_key:
            raise PlanError(f"{purpose} contains a duplicate path and stage")
        if prior_key is not None and key < prior_key:
            raise PlanError(f"{purpose} entries are not in canonical order")
        prior_key = key


def decode_captured_ewah_set_bits(
    payload: bytes,
    offset: int,
    maximum_bits: int,
    purpose: str,
) -> tuple[tuple[int, ...], int]:
    if offset + 8 > len(payload):
        raise PlanError(f"{purpose} is truncated before its EWAH header")
    bit_size = int.from_bytes(payload[offset : offset + 4], "big")
    word_count = int.from_bytes(payload[offset + 4 : offset + 8], "big")
    maximum_words = max(1, ((maximum_bits + 63) // 64) * 2 + 1)
    if word_count == 0 or word_count > maximum_words:
        raise PlanError(f"{purpose} has an invalid EWAH word count")
    words_start = offset + 8
    words_end = words_start + word_count * 8
    serialized_end = words_end + 4
    if serialized_end > len(payload):
        raise PlanError(f"{purpose} is truncated in its EWAH words")
    rlw_position = int.from_bytes(payload[words_end:serialized_end], "big")
    if rlw_position >= word_count:
        raise PlanError(f"{purpose} has an invalid EWAH RLW position")
    if bit_size > maximum_bits:
        raise PlanError(f"{purpose} exceeds the shared-index entry count")

    encoded_ceiling = ((bit_size + 63) // 64) * 64
    set_bits: list[int] = []
    word_position = 0
    pointer = 0
    last_rlw_position = -1
    while pointer < word_count:
        last_rlw_position = pointer
        word_offset = words_start + pointer * 8
        rlw = int.from_bytes(payload[word_offset : word_offset + 8], "big")
        run_bit = rlw & 1
        running_words = (rlw >> 1) & 0xFFFFFFFF
        literal_words = rlw >> 33
        if pointer + 1 + literal_words > word_count:
            raise PlanError(f"{purpose} has truncated EWAH literal words")

        run_end = word_position + running_words * 64
        if run_end > encoded_ceiling:
            raise PlanError(f"{purpose} has an oversized EWAH run")
        if run_bit:
            if run_end > bit_size or run_end > maximum_bits:
                raise PlanError(f"{purpose} sets an out-of-range EWAH bit")
            set_bits.extend(range(word_position, run_end))
        word_position = run_end
        pointer += 1

        for _literal_index in range(literal_words):
            literal_offset = words_start + pointer * 8
            literal = int.from_bytes(
                payload[literal_offset : literal_offset + 8],
                "big",
            )
            while literal:
                lowest = literal & -literal
                bit = word_position + lowest.bit_length() - 1
                if bit >= bit_size or bit >= maximum_bits:
                    raise PlanError(f"{purpose} sets an out-of-range EWAH bit")
                set_bits.append(bit)
                literal ^= lowest
            word_position += 64
            if word_position > encoded_ceiling:
                raise PlanError(f"{purpose} has oversized EWAH literal coverage")
            pointer += 1

    if last_rlw_position != rlw_position:
        raise PlanError(f"{purpose} has a mismatched EWAH RLW position")
    if bit_size > word_position:
        raise PlanError(f"{purpose} has incomplete EWAH bit coverage")
    return tuple(set_bits), serialized_end


def parse_captured_split_index_link(
    payload: bytes,
    object_id_bytes: int,
    shared_entry_count: int,
) -> tuple[bytes, tuple[int, ...], tuple[int, ...]]:
    if len(payload) < object_id_bytes:
        raise PlanError("captured superproject split-index link is truncated")
    shared_checksum = payload[:object_id_bytes]
    offset = object_id_bytes
    if offset == len(payload):
        return shared_checksum, (), ()
    delete_bits, offset = decode_captured_ewah_set_bits(
        payload,
        offset,
        shared_entry_count,
        "captured superproject split-index delete bitmap",
    )
    replace_bits, offset = decode_captured_ewah_set_bits(
        payload,
        offset,
        shared_entry_count,
        "captured superproject split-index replace bitmap",
    )
    if offset != len(payload):
        raise PlanError(
            "captured superproject split-index link has trailing bitmap bytes"
        )
    if set(delete_bits).intersection(replace_bits):
        raise PlanError(
            "captured superproject split-index entry is both replaced and deleted"
        )
    return shared_checksum, delete_bits, replace_bits


def captured_superproject_index_entries(
    bindings: tuple[FileContentBinding, ...],
    contents: tuple[bytes, ...],
) -> tuple[CapturedSuperprojectIndexEntry, ...]:
    """Derive final index semantics solely from already-bound exact bytes."""

    names = superproject_index_snapshot_names(bindings)
    if len(contents) != len(bindings):
        raise PlanError("superproject index snapshot has an invalid content set")
    for binding, content in zip(bindings, contents):
        if (
            len(content) != binding.size
            or hashlib.sha256(content).hexdigest() != binding.content_sha256
        ):
            raise PlanError(
                "superproject index snapshot bytes do not match their binding"
            )

    if len(bindings) == 2:
        shared_hex = names[1].removeprefix("sharedindex.")
        object_id_bytes = len(shared_hex) // 2
    else:
        object_id_bytes = captured_superproject_index_object_id_bytes(
            contents[0],
            "captured superproject primary index",
        )
    primary = parse_captured_superproject_index(
        contents[0],
        object_id_bytes,
        "captured superproject primary index",
    )
    link_payloads = tuple(
        payload for signature, payload in primary.extensions if signature == b"link"
    )
    if len(link_payloads) > 1:
        raise PlanError(
            "captured superproject primary index contains duplicate link extensions"
        )
    if not link_payloads:
        if len(bindings) != 1:
            raise PlanError(
                "captured superproject shared-index path lacks a primary link"
            )
        require_canonical_captured_index_entries(
            primary.entries,
            "captured superproject primary index",
        )
        return primary.entries

    raw_shared_checksum = link_payloads[0][:object_id_bytes]
    if len(raw_shared_checksum) != object_id_bytes:
        raise PlanError("captured superproject split-index link is truncated")
    if raw_shared_checksum == bytes(object_id_bytes):
        if len(bindings) != 1:
            raise PlanError(
                "captured superproject null split-index link has a shared path"
            )
        shared_entries: tuple[CapturedSuperprojectIndexEntry, ...] = ()
    else:
        if len(bindings) != 2:
            raise PlanError(
                "captured superproject split-index link requires a shared index"
            )
        expected_shared_name = f"sharedindex.{raw_shared_checksum.hex()}"
        if names[1] != expected_shared_name:
            raise PlanError(
                "captured superproject shared-index name does not match its link"
            )
        shared = parse_captured_superproject_index(
            contents[1],
            object_id_bytes,
            "captured superproject shared index",
        )
        if any(signature == b"link" for signature, _payload in shared.extensions):
            raise PlanError(
                "captured superproject shared index contains a nested link extension"
            )
        if contents[1][-object_id_bytes:] != raw_shared_checksum:
            raise PlanError(
                "captured superproject shared-index checksum does not match its link"
            )
        require_canonical_captured_index_entries(
            shared.entries,
            "captured superproject shared index",
        )
        shared_entries = shared.entries

    shared_checksum, delete_bits, replace_bits = parse_captured_split_index_link(
        link_payloads[0],
        object_id_bytes,
        len(shared_entries),
    )
    if shared_checksum != raw_shared_checksum:
        raise PlanError("captured superproject split-index link changed while parsing")
    if len(replace_bits) > len(primary.entries):
        raise PlanError(
            "captured superproject split-index has too many replacement bits"
        )

    replacements = primary.entries[: len(replace_bits)]
    additions = primary.entries[len(replace_bits) :]
    if any(entry.raw_path for entry in replacements):
        raise PlanError(
            "captured superproject split-index replacement has a nonempty path"
        )
    if any(not entry.raw_path for entry in additions):
        raise PlanError("captured superproject split-index addition has an empty path")
    require_canonical_captured_index_entries(
        additions,
        "captured superproject split-index additions",
    )

    overlaid = list(shared_entries)
    for position, replacement in zip(replace_bits, replacements):
        shared_entry = overlaid[position]
        overlaid[position] = CapturedSuperprojectIndexEntry(
            raw_path=shared_entry.raw_path,
            mode=replacement.mode,
            object_id=replacement.object_id,
            stage=replacement.stage,
        )
    deleted = set(delete_bits)
    merged = [
        entry for position, entry in enumerate(overlaid) if position not in deleted
    ]
    merged.extend(additions)
    if len(merged) > MAX_CHECKOUT_PATHS:
        raise PlanError(
            "captured superproject split index exceeds the "
            f"{MAX_CHECKOUT_PATHS}-entry safety limit"
        )
    merged.sort(key=lambda entry: (entry.raw_path, entry.stage))
    result = tuple(merged)
    require_canonical_captured_index_entries(
        result,
        "captured superproject merged split index",
    )
    if (
        sum(len(entry.raw_path) + 1 for entry in result)
        > GIT_ENUMERATION_OUTPUT_LIMIT_BYTES
    ):
        raise PlanError(
            "captured superproject merged split-index paths exceed the "
            f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
        )
    return result


def selected_gitlink_rows(
    root: Path,
    selected_paths: tuple[str, ...],
    index_bindings: tuple[FileContentBinding, ...],
    index_contents: tuple[bytes, ...],
) -> tuple[tuple[str, str], ...]:
    del root
    if not selected_paths:
        raise PlanError("cannot bind an empty selected-gitlink set")
    if len(selected_paths) > MAX_CHECKOUT_PATHS:
        raise PlanError(
            "selected gitlink lookup exceeds the "
            f"{MAX_CHECKOUT_PATHS}-entry safety limit"
        )
    expected_raw: dict[bytes, str] = {}
    selected_path_bytes = 0
    for path in selected_paths:
        raw_path = os.fsencode(path)
        if raw_path in expected_raw:
            raise PlanError(f"duplicate selected gitlink path: {path}")
        expected_raw[raw_path] = path
        if len(raw_path) + 1 > MAX_GIT_PATHSPEC_ARG_BYTES:
            raise PlanError(f"selected gitlink pathspec is too large: {path}")
        selected_path_bytes += len(raw_path) + 1
        if selected_path_bytes > GIT_ENUMERATION_OUTPUT_LIMIT_BYTES:
            raise PlanError(
                "selected gitlink paths exceed the "
                f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
            )
    matches: dict[bytes, list[CapturedSuperprojectIndexEntry]] = {
        raw_path: [] for raw_path in expected_raw
    }
    for entry in captured_superproject_index_entries(
        index_bindings,
        index_contents,
    ):
        selected = matches.get(entry.raw_path)
        if selected is not None:
            selected.append(entry)

    rows: list[tuple[str, str]] = []
    for path in selected_paths:
        raw_path = os.fsencode(path)
        selected = matches[raw_path]
        if not selected:
            raise PlanError(f"{path} is not a gitlink in the current index")
        if len(selected) != 1:
            raise PlanError(
                f"{path} has unresolved index entries; resolve conflicts before syncing"
            )
        entry = selected[0]
        if entry.mode != 0o160000:
            raise PlanError(f"{path} is not a gitlink in the current index")
        if entry.stage != 0:
            raise PlanError(
                f"{path} has unresolved index stage {entry.stage}; "
                "resolve conflicts before syncing"
            )
        rows.append((path, entry.object_id.hex()))
    return tuple(rows)


def capture_superproject_index_receipt(
    root: Path,
    selected_paths: tuple[str, ...],
) -> SuperprojectIndexReceipt:
    index_paths = superproject_index_paths(root)
    bindings, contents = capture_superproject_index_snapshot(index_paths)
    selected_rows = selected_gitlink_rows(
        root,
        selected_paths,
        bindings,
        contents,
    )
    for binding in bindings:
        revalidate_file_content_binding(binding)
    if superproject_index_paths(root) != tuple(binding.path for binding in bindings):
        raise PlanError("superproject index path set changed during preflight")
    repeated_rows = selected_gitlink_rows(
        root,
        selected_paths,
        bindings,
        contents,
    )
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
    _bindings, contents = capture_superproject_index_snapshot(
        current_paths,
        expected_bindings=receipt.index_bindings,
    )
    selected_paths = tuple(path for path, _ in receipt.selected_gitlinks)
    current_rows = selected_gitlink_rows(
        root,
        selected_paths,
        receipt.index_bindings,
        contents,
    )
    if current_rows != receipt.selected_gitlinks:
        raise PlanError("selected superproject gitlink rows changed after preflight")
    for binding in receipt.index_bindings:
        revalidate_file_content_binding(binding)


def _read_tree_gitlink(
    source_git_dir: Path, work_tree: Path, treeish: str, rel_path: str
) -> tuple[str, str]:
    del work_tree
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            "-z",
            treeish,
            "--",
            rel_path,
        ],
        stdout_limit=MAX_CHECKOUT_PATH_BYTES + 512,
    )
    records = bounded_records(
        result.stdout,
        f"{treeish}:{rel_path} gitlink",
        maximum_records=1,
    )
    if len(records) != 1:
        raise PlanError(f"{rel_path} is not a gitlink in {treeish}")
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
    except ValueError as exc:
        raise PlanError(f"{rel_path} has an invalid gitlink in {treeish}") from exc
    fields = metadata.split()
    if (
        len(fields) != 3
        or fields[0] != b"160000"
        or fields[1] != b"commit"
        or raw_path != os.fsencode(rel_path)
    ):
        raise PlanError(f"{rel_path} is not a gitlink in {treeish}")
    object_id = os.fsdecode(fields[2])
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id):
        raise PlanError(f"{rel_path} has an invalid gitlink object id in {treeish}")
    return os.fsdecode(fields[0]), object_id


def expected_sha_from_tree(
    source_git_dir: Path, work_tree: Path, treeish: str, rel_path: str
) -> str:
    _mode, object_id = _read_tree_gitlink(
        source_git_dir,
        work_tree,
        treeish,
        rel_path,
    )
    return object_id


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


def reject_incomplete_source_config(
    entries: tuple[tuple[str, str], ...],
    source: Path,
) -> None:
    for key, value in entries:
        lowered = key.casefold()
        if (
            lowered == "include.path"
            or lowered.startswith("include.")
            or lowered.startswith("includeif.")
            or lowered == "extensions.partialclone"
            or lowered == "extensions.worktreeconfig"
            or (
                lowered.startswith("remote.")
                and (
                    lowered.endswith(".promisor")
                    or lowered.endswith(".partialclonefilter")
                )
            )
        ):
            raise PlanError(
                "source repository completeness depends on unsupported include or "
                "promisor policy\n"
                f"  path: {source}\n"
                f"  key: {key}\n"
                f"  value: {value}"
            )
        if lowered.startswith("extensions.") and lowered != "extensions.objectformat":
            raise PlanError(
                "source repository uses an unsupported repository-format extension\n"
                f"  path: {source}\n"
                f"  key: {key}\n"
                f"  value: {value}"
            )


def reject_source_object_alternates(source_git_dir: Path) -> None:
    for name in ("alternates", "http-alternates"):
        path = source_git_dir / "objects" / "info" / name
        if path_entry_exists(path):
            raise PlanError(
                "source repository completeness depends on an alternate object "
                "database\n"
                f"  path: {path}\n"
                "  materialize the required objects in the source repository before "
                "using linked submodule worktrees"
            )


def reject_source_commondir(source_git_dir: Path) -> None:
    commondir = source_git_dir / "commondir"
    if path_entry_exists(commondir):
        raise PlanError(
            "source repository uses an unsupported commondir indirection\n"
            f"  path: {commondir}\n"
            "  use the canonical common gitdir as the source repository"
        )


def reject_source_promisor_markers(source_git_dir: Path) -> None:
    pack_dir = source_git_dir / "objects" / "pack"
    if not path_entry_exists(pack_dir):
        return
    descriptor = open_directory_descriptor(
        pack_dir,
        "source pack directory",
    )
    try:
        names = os.listdir(descriptor)
        if len(names) > MAX_CHECKOUT_OBJECTS:
            raise PlanError(
                "source pack directory exceeds the "
                f"{MAX_CHECKOUT_OBJECTS}-entry safety limit"
            )
        marker = next(
            (
                name
                for name in names
                if isinstance(name, str) and name.casefold().endswith(".promisor")
            ),
            None,
        )
        if marker is not None:
            raise PlanError(
                "source repository completeness depends on a promisor pack\n"
                f"  path: {pack_dir / marker}\n"
                "  materialize the required objects in a non-promisor source "
                "repository before using linked submodule worktrees"
            )
    finally:
        os.close(descriptor)


def capture_source_loose_fanout_entry_at(
    directory_descriptor: int,
    object_directory: Path,
    name: str,
) -> Optional[LooseFanoutBinding]:
    purpose = "source loose-object fanout completeness"
    required_mode = os.R_OK | os.X_OK
    if name not in LOOSE_OBJECT_FANOUT_NAMES:
        raise PlanError(f"{purpose} has an invalid entry name: {name!r}")
    try:
        before = fingerprint_from_stat(
            os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanError(
            f"cannot inspect {purpose}\n"
            f"  path: {object_directory / name}\n"
            f"  error: {exc}"
        ) from exc
    if before.kind != stat.S_IFDIR:
        raise PlanError(
            f"{purpose} has an unsafe object type\n  path: {object_directory / name}"
        )
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise PlanError(
            f"cannot safely bind {purpose}: descriptor-relative "
            "O_NOFOLLOW and O_DIRECTORY are required"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise PlanError(
            f"cannot open {purpose} without following links\n"
            f"  path: {object_directory / name}\n"
            f"  error: {exc}"
        ) from exc
    try:
        opened = fingerprint_from_stat(os.fstat(descriptor))
        try:
            after = fingerprint_from_stat(
                os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            )
        except OSError as exc:
            raise PlanError(
                f"{purpose} name became unavailable during descriptor binding\n"
                f"  path: {object_directory / name}\n"
                f"  error: {exc}"
            ) from exc
        identities = tuple(object_identity(item) for item in (before, opened, after))
        if identities[1:] != identities[:-1]:
            raise PlanError(
                f"{purpose} object identity changed during descriptor binding\n"
                f"  path: {object_directory / name}"
            )
        access_policies = tuple(
            posix_access_policy(item) for item in (before, opened, after)
        )
        if access_policies[1:] != access_policies[:-1]:
            raise PlanError(
                f"{purpose} access policy changed during descriptor binding\n"
                f"  path: {object_directory / name}"
            )
        if not probe_access_at(descriptor, ".", required_mode):
            raise PlanError(
                f"{purpose} denies object reads\n  path: {object_directory / name}"
            )
        return LooseFanoutBinding(
            name=name,
            identity=identities[0],
            access_policy=access_policies[0],
            required_mode=required_mode,
        )
    finally:
        os.close(descriptor)


def source_loose_fanout_inventory_at(
    directory_descriptor: int,
    object_directory: Path,
) -> tuple[tuple[str, Optional[LooseFanoutBinding]], ...]:
    inventory: list[tuple[str, Optional[LooseFanoutBinding]]] = []
    for name in LOOSE_OBJECT_FANOUT_NAMES:
        inventory.append(
            (
                name,
                capture_source_loose_fanout_entry_at(
                    directory_descriptor,
                    object_directory,
                    name,
                ),
            )
        )
    return tuple(inventory)


def capture_source_loose_fanout_inventory(
    objects_binding: AccessBinding,
) -> tuple[tuple[str, Optional[LooseFanoutBinding]], ...]:
    object_descriptor = open_directory_descriptor(
        objects_binding.path,
        "source loose-object fanout completeness",
    )
    try:
        revalidate_directory_descriptor(objects_binding, object_descriptor)
        first = source_loose_fanout_inventory_at(
            object_descriptor,
            objects_binding.path,
        )
        second = source_loose_fanout_inventory_at(
            object_descriptor,
            objects_binding.path,
        )
        revalidate_directory_descriptor(objects_binding, object_descriptor)
        if first != second:
            raise PlanError(
                "source loose-object fanout completeness changed during binding"
            )
        return first
    finally:
        os.close(object_descriptor)


def revalidate_source_loose_fanout_inventory(
    objects_binding: AccessBinding,
    expected: tuple[tuple[str, Optional[LooseFanoutBinding]], ...],
) -> None:
    if tuple(name for name, _ in expected) != LOOSE_OBJECT_FANOUT_NAMES:
        raise PlanError(
            "source loose-object fanout completeness has an invalid receipt shape"
        )
    if any(binding is not None and binding.name != name for name, binding in expected):
        raise PlanError(
            "source loose-object fanout completeness has a mismatched entry binding"
        )
    current = capture_source_loose_fanout_inventory(objects_binding)
    for (name, expected_binding), (_, current_binding) in zip(expected, current):
        if expected_binding is None or current_binding is None:
            if expected_binding != current_binding:
                raise PlanError(
                    "source loose-object fanout presence changed after preflight\n"
                    f"  path: {objects_binding.path / name}"
                )
            continue
        if current_binding.identity != expected_binding.identity:
            raise PlanError(
                "source loose-object fanout object identity changed after preflight\n"
                f"  path: {objects_binding.path / name}"
            )
        if (
            current_binding.access_policy != expected_binding.access_policy
            or current_binding.required_mode != expected_binding.required_mode
        ):
            raise PlanError(
                "source loose-object fanout access policy changed after preflight\n"
                f"  path: {objects_binding.path / name}"
            )


def require_source_completeness_fetch_transition(
    prior: SourceCompletenessReceipt,
    terminal: SourceCompletenessReceipt,
) -> None:
    unchanged_bindings = (
        (
            "source gitdir",
            prior.gitdir_binding,
            terminal.gitdir_binding,
        ),
        (
            "source config",
            prior.config_binding,
            terminal.config_binding,
        ),
        (
            "source object directory",
            prior.objects_binding,
            terminal.objects_binding,
        ),
        (
            "source alternate-object policy parent",
            prior.alternates_parent_binding,
            terminal.alternates_parent_binding,
        ),
    )
    for purpose, expected, current in unchanged_bindings:
        if current != expected:
            raise PlanError(f"{purpose} changed during authorized fetch")
    if prior.pack_binding is not None and terminal.pack_binding != prior.pack_binding:
        raise PlanError("source pack directory changed during authorized fetch")
    prior_fanouts = dict(prior.loose_fanout_inventory)
    terminal_fanouts = dict(terminal.loose_fanout_inventory)
    if (
        tuple(prior_fanouts) != LOOSE_OBJECT_FANOUT_NAMES
        or tuple(terminal_fanouts) != LOOSE_OBJECT_FANOUT_NAMES
        or any(
            binding is not None and binding.name != name
            for name, binding in prior.loose_fanout_inventory
        )
        or any(
            binding is not None and binding.name != name
            for name, binding in terminal.loose_fanout_inventory
        )
    ):
        raise PlanError(
            "source loose-object fanout completeness has an invalid receipt shape"
        )
    for name in LOOSE_OBJECT_FANOUT_NAMES:
        expected = prior_fanouts[name]
        current = terminal_fanouts[name]
        if expected is not None and current is None:
            raise PlanError(
                "source loose-object fanout disappeared during authorized fetch\n"
                f"  path: {prior.objects_binding.path / name}"
            )
        if expected is None:
            continue
        if current is None:
            raise AssertionError("present fanout was checked above")
        if current.identity != expected.identity:
            raise PlanError(
                "source loose-object fanout object identity changed during "
                "authorized fetch\n"
                f"  path: {prior.objects_binding.path / name}"
            )
        if (
            current.access_policy != expected.access_policy
            or current.required_mode != expected.required_mode
        ):
            raise PlanError(
                "source loose-object fanout access policy changed during "
                "authorized fetch\n"
                f"  path: {prior.objects_binding.path / name}"
            )


def capture_source_completeness_receipt(
    source_git_dir: Path,
) -> SourceCompletenessReceipt:
    gitdir_binding = capture_typed_access(
        source_git_dir,
        os.R_OK | os.X_OK,
        "source completeness gitdir",
        stat.S_IFDIR,
    )
    reject_source_commondir(source_git_dir)
    config_path = source_git_dir / "config"
    config_binding, config_content = read_bound_regular_file(
        config_path,
        maximum_bytes=MAX_SOURCE_CONFIG_BYTES,
        mode=os.R_OK,
        purpose="source completeness config",
        retain_content=True,
    )
    if config_content is None:
        raise PlanError("source completeness config returned no content")
    reject_incomplete_source_config(
        parse_bound_git_config(config_content, config_path),
        config_path,
    )
    objects_dir = source_git_dir / "objects"
    objects_binding = capture_typed_access(
        objects_dir,
        os.R_OK | os.X_OK,
        "source completeness object directory",
        stat.S_IFDIR,
    )
    loose_fanout_inventory = capture_source_loose_fanout_inventory(
        objects_binding,
    )
    info_dir = source_git_dir / "objects" / "info"
    alternates_parent = (
        info_dir if path_entry_exists(info_dir) else source_git_dir / "objects"
    )
    alternates_parent_binding = capture_typed_access(
        alternates_parent,
        os.R_OK | os.X_OK,
        "source alternate-object policy parent",
        stat.S_IFDIR,
    )
    pack_dir = objects_dir / "pack"
    pack_binding = (
        capture_typed_access(
            pack_dir,
            os.R_OK | os.X_OK,
            "source promisor-pack policy directory",
            stat.S_IFDIR,
        )
        if path_entry_exists(pack_dir)
        else None
    )
    reject_source_object_alternates(source_git_dir)
    reject_source_promisor_markers(source_git_dir)
    reject_source_commondir(source_git_dir)
    revalidate_access(gitdir_binding)
    revalidate_file_content_binding(config_binding)
    revalidate_access(objects_binding)
    revalidate_source_loose_fanout_inventory(
        objects_binding,
        loose_fanout_inventory,
    )
    revalidate_access(alternates_parent_binding)
    if pack_binding is not None:
        revalidate_access(pack_binding)
    reject_source_object_alternates(source_git_dir)
    reject_source_promisor_markers(source_git_dir)
    reject_source_commondir(source_git_dir)
    return SourceCompletenessReceipt(
        gitdir_binding=gitdir_binding,
        config_binding=config_binding,
        objects_binding=objects_binding,
        loose_fanout_inventory=loose_fanout_inventory,
        alternates_parent_binding=alternates_parent_binding,
        pack_binding=pack_binding,
    )


def revalidate_source_completeness_receipt(
    source_git_dir: Path,
    receipt: SourceCompletenessReceipt,
) -> None:
    if receipt.config_binding.path != source_git_dir / "config":
        raise PlanError("source completeness receipt does not match the source gitdir")
    if receipt.gitdir_binding.path != source_git_dir:
        raise PlanError("source completeness receipt names the wrong source gitdir")
    if receipt.objects_binding.path != source_git_dir / "objects":
        raise PlanError("source completeness receipt names the wrong object directory")
    revalidate_access(receipt.gitdir_binding)
    reject_source_commondir(source_git_dir)
    revalidate_file_content_binding(receipt.config_binding)
    revalidate_access(receipt.objects_binding)
    revalidate_source_loose_fanout_inventory(
        receipt.objects_binding,
        receipt.loose_fanout_inventory,
    )
    revalidate_access(receipt.alternates_parent_binding)
    current_pack = source_git_dir / "objects" / "pack"
    if receipt.pack_binding is None:
        if path_entry_exists(current_pack):
            current_pack_binding = capture_typed_access(
                current_pack,
                os.R_OK | os.X_OK,
                "source promisor-pack policy directory",
                stat.S_IFDIR,
            )
            reject_source_promisor_markers(source_git_dir)
            revalidate_access(current_pack_binding)
    else:
        revalidate_access(receipt.pack_binding)
    reject_source_object_alternates(source_git_dir)
    reject_source_promisor_markers(source_git_dir)
    reject_source_commondir(source_git_dir)
    revalidate_access(receipt.gitdir_binding)


def unresolved_source_transaction_payload(
    source_git_dir: Path,
    directory_descriptor: int,
    *,
    detail: str,
) -> str:
    payload = {
        "detail": detail,
        "fetch_transaction": inspect_shallow_entry_for_recovery(
            directory_descriptor,
            SOURCE_FETCH_TRANSACTION_NAME,
            source_git_dir / SOURCE_FETCH_TRANSACTION_NAME,
        ),
        "profile": "source-fetch-transaction-v1",
        "shallow": inspect_shallow_entry_for_recovery(
            directory_descriptor,
            SOURCE_SHALLOW_NAME,
            source_git_dir / SOURCE_SHALLOW_NAME,
        ),
        "shallow_lock": inspect_shallow_entry_for_recovery(
            directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            source_git_dir / SOURCE_SHALLOW_LOCK_NAME,
        ),
        "source_git_dir": str(source_git_dir),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def revalidate_source_object_admission(
    source_git_dir: Path,
    transaction: Optional[SourceFetchTransaction] = None,
) -> None:
    if transaction is not None:
        if transaction.source_git_dir != source_git_dir or not transaction.active:
            raise PlanError("source fetch transaction does not match the source gitdir")
        revalidate_directory_descriptor(
            transaction.directory_binding,
            transaction.directory_descriptor,
        )
        observed, _ = bind_regular_file_descriptor_at(
            transaction.fence_descriptor,
            transaction.directory_descriptor,
            SOURCE_FETCH_TRANSACTION_NAME,
            source_git_dir / SOURCE_FETCH_TRANSACTION_NAME,
            maximum_bytes=MAX_SOURCE_FETCH_TRANSACTION_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="active source fetch recovery fence",
            retain_content=False,
        )
        require_matching_file_binding(
            transaction.fence_binding,
            observed,
            "active source fetch recovery fence",
        )
        require_absent_entry_at(
            transaction.directory_descriptor,
            SOURCE_SHALLOW_LOCK_NAME,
            source_git_dir / SOURCE_SHALLOW_LOCK_NAME,
            "source shallow lock",
        )
        return

    directory_binding = capture_typed_access(
        source_git_dir,
        os.R_OK | os.X_OK,
        "source object-admission directory",
        stat.S_IFDIR,
    )
    directory_descriptor = open_directory_descriptor(
        source_git_dir,
        "source object-admission directory",
    )
    try:
        revalidate_directory_descriptor(directory_binding, directory_descriptor)
        present = []
        for name in (
            SOURCE_SHALLOW_LOCK_NAME,
            SOURCE_FETCH_TRANSACTION_NAME,
        ):
            try:
                os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PlanError(
                    f"cannot inspect source recovery fence: "
                    f"{source_git_dir / name}\n  error: {exc}"
                ) from exc
            present.append(name)
        if present:
            detail = "unresolved source recovery fence(s): " + ", ".join(present)
            raise PlanError(
                "source objects are unavailable until the shallow/fetch transaction "
                "is recovered\n"
                f"  recovery_identity: "
                f"{unresolved_source_transaction_payload(source_git_dir, directory_descriptor, detail=detail)}"
            )
        revalidate_directory_descriptor(directory_binding, directory_descriptor)
    finally:
        os.close(directory_descriptor)


def begin_source_fetch_transaction(
    receipt: TransportReceipt,
) -> SourceFetchTransaction:
    source_git_dir = receipt.source_shallow_parent_binding.path
    revalidate_source_object_admission(source_git_dir)
    directory_descriptor = open_directory_descriptor(
        source_git_dir,
        "source fetch transaction directory",
    )
    fence_descriptor = -1
    transaction: Optional[SourceFetchTransaction] = None
    try:
        revalidate_directory_descriptor(
            receipt.source_shallow_parent_binding,
            directory_descriptor,
        )
        transaction_id = secrets.token_hex(16)
        payload = json.dumps(
            {
                "expected_shallow": file_binding_recovery_payload(
                    receipt.source_shallow_binding
                ),
                "profile": "source-fetch-transaction-v1",
                "source_git_dir": str(source_git_dir),
                "transaction_id": transaction_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_SOURCE_FETCH_TRANSACTION_BYTES:
            raise PlanError("source fetch recovery receipt exceeds its byte limit")
        fence_descriptor = os.open(
            SOURCE_FETCH_TRANSACTION_NAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NONBLOCK
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        pending = memoryview(payload)
        while pending:
            written = os.write(fence_descriptor, pending)
            if written <= 0:
                raise PlanError("cannot persist the source fetch recovery receipt")
            pending = pending[written:]
        os.fsync(fence_descriptor)
        fence_binding, _ = bind_regular_file_descriptor_at(
            fence_descriptor,
            directory_descriptor,
            SOURCE_FETCH_TRANSACTION_NAME,
            source_git_dir / SOURCE_FETCH_TRANSACTION_NAME,
            maximum_bytes=MAX_SOURCE_FETCH_TRANSACTION_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="source fetch recovery fence",
            retain_content=False,
        )
        os.fsync(directory_descriptor)
        transaction = SourceFetchTransaction(
            source_git_dir=source_git_dir,
            directory_binding=receipt.source_shallow_parent_binding,
            directory_descriptor=directory_descriptor,
            fence_binding=fence_binding,
            fence_descriptor=fence_descriptor,
            transaction_id=transaction_id,
        )
        directory_descriptor = -1
        fence_descriptor = -1
        revalidate_source_object_admission(source_git_dir, transaction)
        return transaction
    except BaseException as exc:
        descriptor_cleanup_error: Optional[str] = None
        recovery_descriptor = (
            transaction.directory_descriptor
            if transaction is not None
            else directory_descriptor
        )
        try:
            recovery_identity = unresolved_source_transaction_payload(
                source_git_dir,
                recovery_descriptor,
                detail=f"source fetch transaction creation failed: {exc}",
            )
        except BaseException as inspection_exc:
            recovery_identity = json.dumps(
                {
                    "detail": f"source fetch transaction creation failed: {exc}",
                    "inspection_error": (
                        f"{type(inspection_exc).__name__}: {inspection_exc}"
                    ),
                    "profile": "source-fetch-transaction-v1",
                    "source_git_dir": str(source_git_dir),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        finally:
            try:
                if transaction is not None:
                    transaction.close_descriptors()
                else:
                    if fence_descriptor >= 0:
                        os.close(fence_descriptor)
                    if directory_descriptor >= 0:
                        os.close(directory_descriptor)
            except BaseException as cleanup_exc:
                descriptor_cleanup_error = (
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        raise PlanError(
            "source fetch transaction could not be established cleanly\n"
            f"  recovery_identity: {recovery_identity}"
            + (
                f"\n  descriptor_cleanup_error: {descriptor_cleanup_error}"
                if descriptor_cleanup_error is not None
                else ""
            )
        ) from exc


def retain_source_fetch_transaction(
    transaction: SourceFetchTransaction,
    detail: str,
) -> PlanError:
    try:
        recovery_identity = unresolved_source_transaction_payload(
            transaction.source_git_dir,
            transaction.directory_descriptor,
            detail=detail,
        )
    except BaseException as exc:
        recovery_identity = json.dumps(
            {
                "detail": detail,
                "inspection_error": f"{type(exc).__name__}: {exc}",
                "profile": "source-fetch-transaction-v1",
                "source_git_dir": str(transaction.source_git_dir),
                "transaction_id": transaction.transaction_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    finally:
        transaction.close_descriptors()
    return PlanError(
        "source fetch did not reach a clean boundary/object terminal state; "
        "the recovery fence was retained\n"
        f"  recovery_identity: {recovery_identity}"
    )


def complete_source_fetch_transaction(
    transaction: SourceFetchTransaction,
) -> None:
    revalidate_source_object_admission(
        transaction.source_git_dir,
        transaction,
    )
    os.unlink(
        SOURCE_FETCH_TRANSACTION_NAME,
        dir_fd=transaction.directory_descriptor,
    )
    try:
        os.fsync(transaction.directory_descriptor)
    except OSError as exc:
        recovery_detail = (
            "fetch transaction fence was unlinked but directory durability "
            "is unverified"
        )
        replacement_descriptor = -1
        replacement_error: Optional[str] = None
        try:
            payload = json.dumps(
                {
                    "detail": recovery_detail,
                    "profile": "source-fetch-transaction-v1",
                    "source_git_dir": str(transaction.source_git_dir),
                    "transaction_id": transaction.transaction_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            replacement_descriptor = os.open(
                SOURCE_FETCH_TRANSACTION_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=transaction.directory_descriptor,
            )
            pending = memoryview(payload)
            while pending:
                written = os.write(replacement_descriptor, pending)
                if written <= 0:
                    raise PlanError("cannot restore the source fetch recovery fence")
                pending = pending[written:]
            os.fsync(replacement_descriptor)
            try:
                os.fsync(transaction.directory_descriptor)
            except OSError as restore_exc:
                replacement_error = (
                    "replacement fence exists but its directory durability is "
                    f"unverified: {restore_exc}"
                )
        except BaseException as restore_exc:
            replacement_error = (
                "could not restore the source fetch recovery fence: "
                f"{type(restore_exc).__name__}: {restore_exc}"
            )
        finally:
            if replacement_descriptor >= 0:
                os.close(replacement_descriptor)
        try:
            recovery_identity = unresolved_source_transaction_payload(
                transaction.source_git_dir,
                transaction.directory_descriptor,
                detail=(
                    recovery_detail
                    if replacement_error is None
                    else f"{recovery_detail}; {replacement_error}"
                ),
            )
        except BaseException as inspection_exc:
            recovery_identity = json.dumps(
                {
                    "detail": recovery_detail,
                    "inspection_error": (
                        f"{type(inspection_exc).__name__}: {inspection_exc}"
                    ),
                    "profile": "source-fetch-transaction-v1",
                    "replacement_error": replacement_error,
                    "source_git_dir": str(transaction.source_git_dir),
                    "transaction_id": transaction.transaction_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        finally:
            transaction.active = False
            transaction.close_descriptors()
        raise PlanError(
            "source fetch transaction cleanup durability is unverified\n"
            f"  recovery_identity: {recovery_identity}"
        ) from exc
    transaction.active = False
    transaction.close_descriptors()


def commit_exists(
    source_git_dir: Path,
    work_tree: Path,
    sha: str,
    *,
    transaction: Optional[SourceFetchTransaction] = None,
    completeness: Optional[SourceCompletenessReceipt] = None,
) -> bool:
    del work_tree
    revalidate_source_object_admission(source_git_dir, transaction)
    source_completeness = completeness or capture_source_completeness_receipt(
        source_git_dir
    )
    revalidate_source_completeness_receipt(
        source_git_dir,
        source_completeness,
    )
    revalidate_source_object_admission(source_git_dir, transaction)
    result = read_git(
        [
            *source_object_repo_args(source_git_dir),
            "cat-file",
            "-e",
            f"{sha}^{{commit}}",
        ],
        check=False,
    )
    revalidate_source_completeness_receipt(
        source_git_dir,
        source_completeness,
    )
    revalidate_source_object_admission(source_git_dir, transaction)
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
        if b"\n" in record:
            raw_key, raw_value = record.split(b"\n", 1)
            key = os.fsdecode(raw_key)
        else:
            raw_key = record
            key = os.fsdecode(raw_key)
            if key.casefold() not in VALUELESS_GIT_BOOLEAN_POLICY_KEYS:
                raise PlanError(
                    "source Git config returned an unsupported valueless entry\n"
                    f"  path: {source}\n"
                    f"  key: {key}"
                )
            # Git's native boolean grammar interprets a key without an equals
            # sign as true. Preserve that distinction from an explicit empty
            # value, which Git interprets as false.
            raw_value = b"true"
        if not raw_key:
            raise PlanError(
                f"source Git config returned an invalid entry\n  path: {source}"
            )
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


def object_write_child_inventory_at(
    directory_descriptor: int,
    object_directory: Path,
    purpose: str,
) -> tuple[tuple[str, Optional[FsFingerprint]], ...]:
    inventory: list[tuple[str, Optional[FsFingerprint]]] = []
    for name in OBJECT_WRITE_CHILD_NAMES:
        try:
            fingerprint = fingerprint_from_stat(
                os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            )
        except FileNotFoundError:
            inventory.append((name, None))
            continue
        except OSError as exc:
            raise PlanError(
                f"cannot inspect {purpose}\n"
                f"  path: {object_directory / name}\n"
                f"  error: {exc}"
            ) from exc
        if fingerprint.kind != stat.S_IFDIR:
            raise PlanError(
                f"{purpose} has an unsafe object type\n"
                f"  path: {object_directory / name}"
            )
        if not probe_access_at(
            directory_descriptor,
            name,
            os.R_OK | os.W_OK | os.X_OK,
        ):
            raise PlanError(
                f"{purpose} denies object writes\n  path: {object_directory / name}"
            )
        inventory.append((name, fingerprint))
    return tuple(inventory)


def capture_object_write_child_inventory(
    object_binding: AccessBinding,
    purpose: str,
) -> tuple[tuple[str, Optional[FsFingerprint]], ...]:
    object_descriptor = open_directory_descriptor(
        object_binding.path,
        purpose,
    )
    try:
        revalidate_directory_descriptor(object_binding, object_descriptor)
        first = object_write_child_inventory_at(
            object_descriptor,
            object_binding.path,
            purpose,
        )
        second = object_write_child_inventory_at(
            object_descriptor,
            object_binding.path,
            purpose,
        )
        revalidate_directory_descriptor(object_binding, object_descriptor)
        if first != second:
            raise PlanError(f"{purpose} changed during preflight binding")
        return first
    finally:
        os.close(object_descriptor)


def capture_object_write_child_lease(
    object_lease: DirectoryEntryLease,
    expected: tuple[tuple[str, Optional[FsFingerprint]], ...],
    purpose: str,
) -> DirectoryChildInventoryLease:
    if tuple(name for name, _ in expected) != OBJECT_WRITE_CHILD_NAMES:
        raise PlanError(f"{purpose} has an invalid receipt shape")
    revalidate_directory_entry_lease(object_lease)
    current = object_write_child_inventory_at(
        object_lease.descriptor,
        object_lease.path,
        purpose,
    )
    revalidate_directory_entry_lease(object_lease)
    if current != expected:
        raise PlanError(f"{purpose} changed after preflight")
    return DirectoryChildInventoryLease(
        directory_binding=object_lease.binding,
        directory_descriptor=object_lease.descriptor,
        entries=expected,
        purpose=purpose,
    )


FETCH_OBJECT_POLICY_KEYS = (
    "fetch.fsckobjects",
    "transfer.fsckobjects",
    "core.sharedrepository",
    "core.fsync",
    "core.fsyncmethod",
)
VALUELESS_GIT_BOOLEAN_POLICY_KEYS = frozenset(
    {
        "fetch.fsckobjects",
        "transfer.fsckobjects",
        "core.sharedrepository",
    }
)
FETCH_OBJECT_POLICY_CONFIG_NAMES = {
    "fetch.fsckobjects": "fetch.fsckObjects",
    "transfer.fsckobjects": "transfer.fsckObjects",
    "core.sharedrepository": "core.sharedRepository",
    "core.fsync": "core.fsync",
    "core.fsyncmethod": "core.fsyncMethod",
}
FSYNC_COMPONENTS = frozenset(
    {
        "none",
        "loose-object",
        "pack",
        "pack-metadata",
        "commit-graph",
        "index",
        "objects",
        "reference",
        "derived-metadata",
        "committed",
        "added",
        "all",
    }
)


def normalize_fetch_policy_boolean(value: str, key: str, source: Path) -> str:
    result = read_git_bounded(
        [
            "-c",
            f"codex.fetchpolicyboolean={value}",
            "config",
            "--type=bool",
            "--get",
            "codex.fetchpolicyboolean",
        ],
        check=False,
        stdout_limit=16,
    )
    normalized = os.fsdecode(result.stdout).strip()
    if result.returncode == 0 and normalized in {"true", "false"}:
        return normalized
    raise PlanError(
        "source Git config contains an invalid fetch object-policy boolean\n"
        f"  path: {source}\n"
        f"  key: {key}\n"
        f"  value: {value}"
    )


def normalize_shared_repository(value: str, source: Path) -> str:
    normalized = value.strip().casefold()
    aliases = {
        "": "umask",
        "0": "umask",
        "false": "umask",
        "umask": "umask",
        "1": "group",
        "true": "group",
        "group": "group",
        "2": "all",
        "all": "all",
        "world": "all",
        "everybody": "all",
    }
    if normalized in aliases:
        return aliases[normalized]
    if re.fullmatch(r"0[0-7]{3}", normalized):
        return normalized
    raise PlanError(
        "source Git config contains an unsupported core.sharedRepository policy\n"
        f"  path: {source}\n"
        f"  value: {value}"
    )


def wait_for_forked_child(
    child_pid: int,
    deadline: float,
) -> Optional[int]:
    while True:
        try:
            waited_pid, child_status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError as exc:
            raise PlanError(
                "process cleanup-incomplete: forked child was lost before reap"
            ) from exc
        except OSError as exc:
            raise PlanError(
                f"process cleanup-incomplete: cannot reap forked child: {exc}"
            ) from exc
        if waited_pid == child_pid:
            return child_status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.01, remaining))


def terminate_forked_child(child_pid: int) -> None:
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise PlanError(
            f"process cleanup-incomplete: cannot kill forked child: {exc}"
        ) from exc
    child_status = wait_for_forked_child(
        child_pid,
        time.monotonic() + PROCESS_CLEANUP_TIMEOUT_SECONDS,
    )
    if child_status is None:
        raise PlanError(
            "process cleanup-incomplete: forked child could not be reaped within "
            f"{PROCESS_CLEANUP_TIMEOUT_SECONDS:g} seconds"
        )


def capture_process_umask() -> int:
    if (
        os.name != "posix"
        or not all(
            hasattr(os, name)
            for name in ("fork", "pipe", "set_blocking", "umask", "waitpid")
        )
        or not hasattr(os, "WNOHANG")
    ):
        raise PlanError(
            "cannot safely capture the process umask for source shallow creation"
        )
    read_descriptor, write_descriptor = os.pipe()
    child_pid = -1
    child_status: Optional[int] = None
    try:
        child_pid = os.fork()
        if child_pid == 0:
            try:
                os.close(read_descriptor)
                process_umask = os.umask(0)
                payload = process_umask.to_bytes(2, "big")
                offset = 0
                while offset < len(payload):
                    written = os.write(write_descriptor, payload[offset:])
                    if written <= 0:
                        os._exit(1)
                    offset += written
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_descriptor)
        write_descriptor = -1
        os.set_blocking(read_descriptor, False)
        payload = bytearray()
        deadline = time.monotonic() + UMASK_CAPTURE_TIMEOUT_SECONDS
        while child_status is None:
            while len(payload) < 2:
                try:
                    chunk = os.read(read_descriptor, 2 - len(payload))
                except BlockingIOError:
                    break
                if not chunk:
                    break
                payload.extend(chunk)
            try:
                waited_pid, observed_status = os.waitpid(
                    child_pid,
                    os.WNOHANG,
                )
            except ChildProcessError as exc:
                raise PlanError(
                    "cannot safely capture the process umask: "
                    "forked child was lost before reap"
                ) from exc
            except OSError as exc:
                raise PlanError(
                    f"cannot safely capture the process umask: {exc}"
                ) from exc
            if waited_pid == child_pid:
                child_status = observed_status
                child_pid = -1
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlanError(
                    "process umask capture exceeded its "
                    f"{UMASK_CAPTURE_TIMEOUT_SECONDS:g}-second deadline"
                )
            time.sleep(min(0.01, remaining))

        while len(payload) < 2:
            try:
                chunk = os.read(read_descriptor, 2 - len(payload))
            except BlockingIOError:
                break
            if not chunk:
                break
            payload.extend(chunk)
        if (
            child_status is None
            or not os.WIFEXITED(child_status)
            or os.WEXITSTATUS(child_status) != 0
            or len(payload) != 2
        ):
            raise PlanError(
                "cannot safely capture the process umask for source shallow creation"
            )
        process_umask = int.from_bytes(payload, "big")
        if process_umask < 0 or process_umask > 0o777:
            raise PlanError("captured process umask is outside the POSIX mode range")
        return process_umask
    except OSError as exc:
        raise PlanError(f"cannot safely capture the process umask: {exc}") from exc
    finally:
        if read_descriptor >= 0:
            os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)
        if child_pid > 0:
            terminate_forked_child(child_pid)


def frozen_shared_repository(
    policy: tuple[tuple[str, str], ...],
) -> str:
    values = [
        value
        for key, value in policy
        if key == FETCH_OBJECT_POLICY_CONFIG_NAMES["core.sharedrepository"]
    ]
    if len(values) > 1:
        raise PlanError("frozen fetch object policy duplicates core.sharedRepository")
    return values[0] if values else "umask"


def source_shallow_permissions(shared_repository: str, process_umask: int) -> int:
    requested_mode = 0o666
    created_mode = requested_mode & ~process_umask
    if shared_repository == "umask":
        permissions = created_mode
    elif shared_repository == "group":
        permissions = created_mode | 0o660
    elif shared_repository == "all":
        permissions = created_mode | 0o664
    elif re.fullmatch(r"0[0-7]{3}", shared_repository):
        permissions = int(shared_repository, 8)
    else:
        raise PlanError(
            "frozen core.sharedRepository cannot determine source shallow mode"
        )
    if permissions & 0o600 != 0o600:
        raise PlanError(
            "core.sharedRepository and the frozen process umask do not leave "
            "the source shallow boundary owner-readable and owner-writable"
        )
    return permissions


def capture_source_shallow_creation_policy(
    parent_binding: AccessBinding,
    fetch_object_policy: tuple[tuple[str, str], ...],
) -> SourceShallowCreationPolicy:
    if parent_binding.fingerprint.kind != stat.S_IFDIR:
        raise PlanError("source shallow creation parent is not a directory")
    effective_owner = os.geteuid()
    effective_group = os.getegid()
    parent = parent_binding.fingerprint
    if parent.permissions & stat.S_ISGID:
        expected_group = parent.group
    elif parent.group == effective_group:
        expected_group = effective_group
    else:
        raise PlanError(
            "cannot prove source shallow group ownership before mutation\n"
            f"  parent: {parent_binding.path}\n"
            f"  parent group: {parent.group}\n"
            f"  effective group: {effective_group}\n"
            "  set the parent directory setgid bit or use the effective group"
        )
    process_umask = capture_process_umask()
    shared_repository = frozen_shared_repository(fetch_object_policy)
    return SourceShallowCreationPolicy(
        shared_repository=shared_repository,
        process_umask=process_umask,
        owner=effective_owner,
        group=expected_group,
        permissions=source_shallow_permissions(
            shared_repository,
            process_umask,
        ),
    )


def revalidate_source_shallow_creation_policy(
    receipt: TransportReceipt,
) -> None:
    policy = receipt.source_shallow_creation_policy
    if policy is None:
        return
    if receipt.source_shallow_binding is not None:
        raise PlanError(
            "source shallow creation policy is attached to an existing boundary"
        )
    current = capture_source_shallow_creation_policy(
        receipt.source_shallow_parent_binding,
        receipt.fetch_object_policy,
    )
    if current != policy:
        raise PlanError(
            "source shallow mode or ownership policy changed after preflight"
        )


def normalize_fsync_policy(value: str, source: Path) -> str:
    if not value.strip():
        return ""
    normalized: list[str] = []
    for raw_component in value.split(","):
        component = raw_component.strip().casefold()
        disabled = component.startswith("-")
        name = component[1:] if disabled else component
        if not name or name not in FSYNC_COMPONENTS or (disabled and name == "none"):
            raise PlanError(
                "source Git config contains an unsupported core.fsync component\n"
                f"  path: {source}\n"
                f"  value: {value}\n"
                f"  component: {raw_component}"
            )
        normalized.append(f"-{name}" if disabled else name)
    if "none" in normalized and len(normalized) != 1:
        raise PlanError(
            "source Git config mixes the core.fsync reset with components\n"
            f"  path: {source}\n"
            f"  value: {value}"
        )
    return ",".join(normalized)


def capture_fetch_object_policy(
    entries: tuple[tuple[str, str], ...],
    source: Path,
) -> tuple[tuple[str, str], ...]:
    collected: dict[str, list[str]] = {key: [] for key in FETCH_OBJECT_POLICY_KEYS}
    for key, value in entries:
        lowered = key.casefold()
        if lowered in collected:
            collected[lowered].append(value)
            continue
        unsupported = (
            lowered == "core.fsyncobjectfiles"
            or lowered == "core.createobject"
            or lowered in {"fetch.unpacklimit", "transfer.unpacklimit"}
            or lowered.startswith("fetch.fsck.")
            or lowered.startswith("transfer.fsck.")
            or (
                lowered.startswith("core.fsync")
                and lowered not in FETCH_OBJECT_POLICY_KEYS
            )
        )
        if unsupported:
            raise PlanError(
                "source Git config contains object-write policy that the isolated "
                "fetch cannot reproduce safely\n"
                f"  path: {source}\n"
                f"  key: {key}"
            )

    for key, values in collected.items():
        if len(values) > 1:
            raise PlanError(
                "source Git config contains duplicate object-write policy\n"
                f"  path: {source}\n"
                f"  key: {FETCH_OBJECT_POLICY_CONFIG_NAMES[key]}\n"
                f"  count: {len(values)}"
            )

    policy: list[tuple[str, str]] = []
    for key in FETCH_OBJECT_POLICY_KEYS:
        values = collected[key]
        if not values:
            continue
        value = values[0]
        if key in {"fetch.fsckobjects", "transfer.fsckobjects"}:
            normalized = normalize_fetch_policy_boolean(value, key, source)
        elif key == "core.sharedrepository":
            normalized = normalize_shared_repository(value, source)
        elif key == "core.fsync":
            normalized = normalize_fsync_policy(value, source)
        elif key == "core.fsyncmethod":
            normalized = value.strip().casefold()
            if normalized not in {"fsync", "writeout-only"}:
                raise PlanError(
                    "source Git config contains an unsupported core.fsyncMethod\n"
                    f"  path: {source}\n"
                    f"  value: {value}"
                )
        else:
            raise AssertionError(f"unhandled fetch object policy: {key}")
        policy.append((FETCH_OBJECT_POLICY_CONFIG_NAMES[key], normalized))
    return tuple(policy)


def render_fetch_object_policy(
    policy: tuple[tuple[str, str], ...],
) -> str:
    rendered: list[str] = []
    allowed_names = set(FETCH_OBJECT_POLICY_CONFIG_NAMES.values())
    for key, value in policy:
        if key not in allowed_names or (value and not value.isascii()):
            raise PlanError(f"invalid frozen fetch object policy entry: {key}")
        section, variable = key.split(".", 1)
        rendered.extend(
            [
                f"[{section}]",
                f"\t{variable} = {value}",
            ]
        )
    return "\n".join(rendered) + ("\n" if rendered else "")


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


def open_optional_bound_regular_file_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    *,
    maximum_bytes: int,
    mode: int,
    purpose: str,
    retain_content: bool,
) -> tuple[int, Optional[FileContentBinding], Optional[bytes]]:
    try:
        os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        require_absent_entry_at(
            directory_descriptor,
            name,
            display_path,
            purpose,
        )
        return -1, None, None
    except OSError as exc:
        raise PlanError(
            f"cannot inspect optional {purpose}\n  path: {display_path}\n  error: {exc}"
        ) from exc
    return open_bound_regular_file_at(
        directory_descriptor,
        name,
        display_path,
        maximum_bytes=maximum_bytes,
        mode=mode,
        purpose=purpose,
        retain_content=retain_content,
    )


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
    revalidate_source_shallow_creation_policy(receipt)
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
        ) = open_optional_bound_regular_file_at(
            private_directory_descriptor,
            SOURCE_SHALLOW_NAME,
            private_shallow_path,
            maximum_bytes=MAX_SOURCE_SHALLOW_BYTES,
            mode=os.R_OK | os.W_OK,
            purpose="post-fetch private shallow boundary",
            retain_content=True,
        )
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
        expected_shallow_descriptor = open_revalidated_source_shallow_at(
            receipt,
            source_directory_descriptor,
        )
        if receipt.source_shallow_binding is None and private_binding is None:
            require_absent_entry_at(
                source_directory_descriptor,
                SOURCE_SHALLOW_LOCK_NAME,
                receipt.source_shallow_path.with_name(SOURCE_SHALLOW_LOCK_NAME),
                "source shallow lock",
            )
            require_absent_entry_at(
                private_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                private_shallow_path,
                "post-fetch private shallow boundary",
            )
            revalidate_directory_descriptor(
                private_directory_binding,
                private_directory_descriptor,
            )
            revalidate_directory_descriptor(
                receipt.source_shallow_parent_binding,
                source_directory_descriptor,
            )
            return
        creation_policy = receipt.source_shallow_creation_policy
        if receipt.source_shallow_binding is not None:
            mode = receipt.source_shallow_binding.fingerprint.permissions
            create_mode = mode
        else:
            if creation_policy is None:
                raise PlanError(
                    "absent source shallow boundary lacks a frozen creation policy"
                )
            mode = creation_policy.permissions
            # Git creates ordinary lockfiles with 0666 and then applies the
            # frozen shared-repository adjustment. Match that shape instead of
            # making a fixed owner-private file.
            create_mode = 0o666
        try:
            lock_descriptor = os.open(
                SOURCE_SHALLOW_LOCK_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | os.O_NOFOLLOW,
                create_mode,
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
        if receipt.source_shallow_binding is not None:
            existing_policy = receipt.source_shallow_binding.fingerprint
            expected_owner = existing_policy.owner
            expected_group = existing_policy.group
            expected_permissions = existing_policy.permissions
        else:
            if creation_policy is None:
                raise PlanError(
                    "absent source shallow boundary lacks a frozen creation policy"
                )
            expected_owner = creation_policy.owner
            expected_group = creation_policy.group
            expected_permissions = creation_policy.permissions
        if (
            lock_fingerprint.owner != expected_owner
            or lock_fingerprint.group != expected_group
        ):
            raise PlanError(
                "source shallow lock ownership does not preserve the frozen "
                "boundary policy"
            )
        os.fchmod(lock_descriptor, mode)
        lock_fingerprint = fingerprint_from_stat(os.fstat(lock_descriptor))
        if (
            lock_fingerprint.owner != expected_owner
            or lock_fingerprint.group != expected_group
            or lock_fingerprint.permissions != expected_permissions
        ):
            raise PlanError(
                "source shallow lock mode or ownership does not match the "
                "frozen boundary policy"
            )
        pending = memoryview(private_content if private_content is not None else b"")
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

        if private_binding is None:
            require_absent_entry_at(
                private_directory_descriptor,
                SOURCE_SHALLOW_NAME,
                private_shallow_path,
                "post-fetch private shallow boundary",
            )
        else:
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
            except (OSError, PlanError) as exc:
                rollback_state, rollback_error = rollback_absent_source_shallow_publish(
                    receipt,
                    source_directory_descriptor,
                    lock_descriptor,
                    lock_binding,
                )
                preserve_fence = rollback_state.endswith("fence-retained")
                detail = str(exc)
                if isinstance(exc, OSError):
                    rollback_state = f"durability-unverified-{rollback_state}"
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
            if private_binding is not None and (
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
        except (OSError, PlanError) as exc:
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

        if private_binding is None:
            marker_unlinked = False
            old_lock_unlinked = False
            try:
                installed_path_fingerprint = fingerprint_from_stat(
                    os.stat(
                        SOURCE_SHALLOW_NAME,
                        dir_fd=source_directory_descriptor,
                        follow_symlinks=False,
                    )
                )
                if installed_path_fingerprint != lock_binding.fingerprint:
                    raise PlanError(
                        "source shallow deletion marker identity changed before unlink"
                    )
                old_path_fingerprint = fingerprint_from_stat(
                    os.stat(
                        SOURCE_SHALLOW_LOCK_NAME,
                        dir_fd=source_directory_descriptor,
                        follow_symlinks=False,
                    )
                )
                if old_path_fingerprint != receipt.source_shallow_binding.fingerprint:
                    raise PlanError(
                        "exchanged source shallow lock identity changed before "
                        "deletion commit"
                    )
                os.unlink(
                    SOURCE_SHALLOW_NAME,
                    dir_fd=source_directory_descriptor,
                )
                marker_unlinked = True
                os.fsync(source_directory_descriptor)

                old_path_fingerprint = fingerprint_from_stat(
                    os.stat(
                        SOURCE_SHALLOW_LOCK_NAME,
                        dir_fd=source_directory_descriptor,
                        follow_symlinks=False,
                    )
                )
                if old_path_fingerprint != receipt.source_shallow_binding.fingerprint:
                    raise PlanError(
                        "exchanged source shallow lock identity changed after "
                        "deletion commit"
                    )
                os.unlink(
                    SOURCE_SHALLOW_LOCK_NAME,
                    dir_fd=source_directory_descriptor,
                )
                old_lock_unlinked = True
                os.fsync(source_directory_descriptor)
            except (OSError, PlanError) as exc:
                if not marker_unlinked:
                    rollback_state, rollback_error = rollback_source_shallow_exchange(
                        receipt,
                        source_directory_descriptor,
                        lock_descriptor,
                        lock_binding,
                    )
                    preserve_fence = True
                elif not old_lock_unlinked:
                    rollback_state = "delete-durability-unverified-fence-retained"
                    rollback_error = (
                        "source shallow is absent and the receipt-bound old "
                        "boundary remains at shallow.lock"
                    )
                    preserve_fence = True
                else:
                    rollback_state = "delete-commit-durability-unverified"
                    rollback_error = (
                        "source shallow and the receipt-bound old lock are absent"
                    )
                detail = str(exc)
                if rollback_error is not None:
                    detail += f"; recovery: {rollback_error}"
                raise source_shallow_recovery_error(
                    receipt,
                    source_directory_descriptor,
                    rollback_state,
                    detail,
                ) from exc
            return

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


def write_owner_private_file(
    path: Path,
    content: bytes,
    purpose: str,
    *,
    final_mode: int = 0o600,
) -> None:
    if final_mode not in {0o400, 0o600}:
        raise PlanError(f"{purpose} requested an unsupported private-file mode")
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
        os.fchmod(descriptor, final_mode)
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


class CleanupGuardGroup:
    def __init__(self, *guards: object) -> None:
        self._guards = guards
        self._active = True

    def cleanup(self) -> None:
        if not self._active:
            return
        self._active = False
        first_error: Optional[BaseException] = None
        for guard in reversed(self._guards):
            cleanup = getattr(guard, "cleanup", None)
            if cleanup is None:
                continue
            try:
                cleanup()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise PlanError(
                f"temporary executable/control cleanup failed: {first_error}"
            ) from first_error


class OwnerPrivateTemporaryDirectory:
    """Descriptor-bind and conditionally remove one owner-private temp root.

    The protected property is the root/child object identity plus owner, group,
    mode, and required access. Entry churn inside the exact root is expected
    during cleanup; timestamps are deliberately excluded from the comparison.
    """

    def __init__(self, prefix: str) -> None:
        created_path = Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=True)
        self.name = str(created_path)
        self._path = created_path
        self._parent_path = created_path.parent
        self._entry_name = created_path.name
        self._fingerprint = filesystem_fingerprint(created_path)
        self._parent_binding = capture_typed_access(
            self._parent_path,
            os.R_OK | os.W_OK | os.X_OK,
            "owner-private temporary directory parent",
            stat.S_IFDIR,
        )
        self._binding = capture_owner_private_directory(
            created_path,
            "owner-private temporary directory",
        )
        self._parent_descriptor = -1
        self._descriptor = -1
        self._active = True
        self._guard_registration: Optional[str] = None
        try:
            if self._binding.fingerprint != self._fingerprint:
                raise PlanError(
                    "owner-private temporary directory changed during binding"
                )
            self._parent_descriptor = open_directory_descriptor(
                self._parent_path,
                "owner-private temporary directory parent",
            )
            self._descriptor = open_directory_descriptor(
                created_path,
                "owner-private temporary directory",
            )
            revalidate_directory_descriptor(
                self._parent_binding,
                self._parent_descriptor,
            )
            revalidate_directory_descriptor(
                self._binding,
                self._descriptor,
            )
            recovery_identity = json.dumps(
                {
                    "expected_identity": {
                        "device": self._fingerprint.device,
                        "group": self._fingerprint.group,
                        "inode": self._fingerprint.inode,
                        "kind": self._fingerprint.kind,
                        "mode": self._fingerprint.permissions,
                        "owner": self._fingerprint.owner,
                    },
                    "path": str(created_path),
                    "profile": "owner-private-temporary-cleanup-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            self._guard_registration = _ACTIVE_GUARDS.register(
                self,
                purpose=f"owner-private temporary root {prefix}",
                recovery_identity=recovery_identity,
            )
        except BaseException as exc:
            self._active = False
            self._close_descriptors()
            raise TemporaryDirectoryCleanupError(
                status="initial-binding-unavailable",
                location=created_path,
                expected=self._fingerprint,
                detail=str(exc),
            ) from exc

    def _close_descriptors(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1
        if self._parent_descriptor >= 0:
            os.close(self._parent_descriptor)
            self._parent_descriptor = -1

    @staticmethod
    def _entry_fingerprint(
        directory_descriptor: int,
        entry_name: str,
    ) -> FsFingerprint:
        return fingerprint_from_stat(
            os.stat(
                entry_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        )

    @classmethod
    def _remove_contents_at(
        cls,
        directory_descriptor: int,
        directory_fingerprint: FsFingerprint,
        display_path: Path,
    ) -> None:
        if fingerprint_from_stat(os.fstat(directory_descriptor)) != (
            directory_fingerprint
        ):
            raise PlanError(
                f"temporary cleanup directory changed before traversal: {display_path}"
            )
        try:
            entry_names = sorted(os.listdir(directory_descriptor))
        except OSError as exc:
            raise PlanError(
                f"cannot enumerate temporary cleanup directory: {display_path}\n"
                f"  error: {exc}"
            ) from exc
        for entry_name in entry_names:
            validate_descriptor_entry_name(entry_name)
            entry_path = display_path / entry_name
            try:
                entry_fingerprint = cls._entry_fingerprint(
                    directory_descriptor,
                    entry_name,
                )
            except OSError as exc:
                raise PlanError(
                    f"temporary cleanup entry became unavailable: {entry_path}\n"
                    f"  error: {exc}"
                ) from exc
            if entry_fingerprint.kind == stat.S_IFDIR:
                child_descriptor = -1
                try:
                    child_descriptor = os.open(
                        entry_name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                        dir_fd=directory_descriptor,
                    )
                    if (
                        fingerprint_from_stat(os.fstat(child_descriptor))
                        != entry_fingerprint
                        or cls._entry_fingerprint(
                            directory_descriptor,
                            entry_name,
                        )
                        != entry_fingerprint
                    ):
                        raise PlanError(
                            "temporary cleanup child directory identity or "
                            f"access policy changed: {entry_path}"
                        )
                    cls._remove_contents_at(
                        child_descriptor,
                        entry_fingerprint,
                        entry_path,
                    )
                    if (
                        fingerprint_from_stat(os.fstat(child_descriptor))
                        != entry_fingerprint
                        or cls._entry_fingerprint(
                            directory_descriptor,
                            entry_name,
                        )
                        != entry_fingerprint
                    ):
                        raise PlanError(
                            "temporary cleanup child directory changed after "
                            f"traversal: {entry_path}"
                        )
                    os.rmdir(entry_name, dir_fd=directory_descriptor)
                except OSError as exc:
                    raise PlanError(
                        f"cannot remove temporary cleanup directory: {entry_path}\n"
                        f"  error: {exc}"
                    ) from exc
                finally:
                    if child_descriptor >= 0:
                        os.close(child_descriptor)
            else:
                try:
                    if (
                        cls._entry_fingerprint(
                            directory_descriptor,
                            entry_name,
                        )
                        != entry_fingerprint
                    ):
                        raise PlanError(
                            "temporary cleanup entry object or access policy "
                            f"changed: {entry_path}"
                        )
                    os.unlink(entry_name, dir_fd=directory_descriptor)
                except OSError as exc:
                    raise PlanError(
                        f"cannot remove temporary cleanup entry: {entry_path}\n"
                        f"  error: {exc}"
                    ) from exc
            if fingerprint_from_stat(os.fstat(directory_descriptor)) != (
                directory_fingerprint
            ):
                raise PlanError(
                    "temporary cleanup parent object or access policy changed: "
                    f"{display_path}"
                )

    def _restore_unexpected_quarantine(
        self,
        quarantine_name: str,
    ) -> tuple[Path, str]:
        quarantine_path = self._parent_path / quarantine_name
        try:
            os.stat(
                self._entry_name,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                descriptor_atomic_rename_noreplace(
                    self._parent_descriptor,
                    quarantine_name,
                    self._entry_name,
                )
                os.fsync(self._parent_descriptor)
                return (
                    self._parent_path / self._entry_name,
                    "the unexpected replacement was restored to its original pathname",
                )
            except BaseException as exc:
                return (
                    quarantine_path,
                    "the unexpected replacement remains quarantined because "
                    f"pathname restoration failed: {exc}",
                )
        except OSError as exc:
            return (
                quarantine_path,
                "the original pathname could not be inspected while retaining "
                f"the unexpected replacement: {exc}",
            )
        return (
            quarantine_path,
            "the original pathname is occupied; the unexpected replacement "
            "remains quarantined",
        )

    def cleanup(self) -> None:
        if not self._active:
            _ACTIVE_GUARDS.unregister(self._guard_registration)
            self._guard_registration = None
            return
        quarantine_name: Optional[str] = None
        recovery_location = self._path
        status = "identity-unverified-retained"
        try:
            revalidate_directory_descriptor(
                self._parent_binding,
                self._parent_descriptor,
            )
            if (
                self._entry_fingerprint(
                    self._parent_descriptor,
                    self._entry_name,
                )
                != self._fingerprint
                or fingerprint_from_stat(os.fstat(self._descriptor))
                != self._fingerprint
            ):
                raise PlanError(
                    "temporary directory pathname, descriptor, or access policy "
                    "changed before cleanup"
                )

            # Atomic quarantine closes the pathname-check-to-recursive-open
            # interval. A replacement captured by the rename is restored or
            # retained; it is never passed to recursive removal.
            for _attempt in range(8):
                candidate = f".codex-cleanup-{secrets.token_hex(16)}"
                try:
                    descriptor_atomic_rename_noreplace(
                        self._parent_descriptor,
                        self._entry_name,
                        candidate,
                    )
                    quarantine_name = candidate
                    break
                except AtomicRenameError as exc:
                    if exc.error_number != errno.EEXIST:
                        raise
            if quarantine_name is None:
                raise PlanError(
                    "cannot reserve an owner-private cleanup quarantine name"
                )
            recovery_location = self._parent_path / quarantine_name
            try:
                quarantined_fingerprint = self._entry_fingerprint(
                    self._parent_descriptor,
                    quarantine_name,
                )
            except OSError as exc:
                raise PlanError(
                    f"cannot inspect quarantined temporary directory: {exc}"
                ) from exc
            if (
                quarantined_fingerprint != self._fingerprint
                or fingerprint_from_stat(os.fstat(self._descriptor))
                != self._fingerprint
            ):
                recovery_location, restore_detail = self._restore_unexpected_quarantine(
                    quarantine_name
                )
                quarantine_name = None
                status = "unexpected-replacement-retained"
                raise PlanError(
                    "the cleanup quarantine captured an unexpected replacement; "
                    f"{restore_detail}"
                )
            self._entry_name = quarantine_name
            self._path = recovery_location
            quarantine_name = None
            status = "partial-cleanup-recovery-retained"
            os.fsync(self._parent_descriptor)
            revalidate_directory_descriptor(
                self._parent_binding,
                self._parent_descriptor,
            )
            self._remove_contents_at(
                self._descriptor,
                self._fingerprint,
                self._path,
            )
            if (
                self._entry_fingerprint(
                    self._parent_descriptor,
                    self._entry_name,
                )
                != self._fingerprint
                or fingerprint_from_stat(os.fstat(self._descriptor))
                != self._fingerprint
            ):
                raise PlanError(
                    "quarantined temporary directory changed before final removal"
                )
            os.rmdir(
                self._entry_name,
                dir_fd=self._parent_descriptor,
            )
            os.fsync(self._parent_descriptor)
            try:
                os.stat(
                    self._entry_name,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise PlanError(
                    "temporary cleanup quarantine still exists after removal"
                )
            revalidate_directory_descriptor(
                self._parent_binding,
                self._parent_descriptor,
            )
        except BaseException as exc:
            self._active = False
            self._close_descriptors()
            _ACTIVE_GUARDS.unregister(self._guard_registration)
            self._guard_registration = None
            raise TemporaryDirectoryCleanupError(
                status=status,
                location=recovery_location,
                expected=self._fingerprint,
                detail=str(exc),
            ) from exc
        self._active = False
        self._close_descriptors()
        _ACTIVE_GUARDS.unregister(self._guard_registration)
        self._guard_registration = None


def capture_fetch_control_gitdir(
    object_format: str,
    submodule_path: str,
    initial_shallow_content: Optional[bytes],
    fetch_object_policy: tuple[tuple[str, str], ...],
) -> tuple[
    object,
    Path,
    tuple[AccessBinding, ...],
    tuple[FileContentBinding, ...],
]:
    guard = OwnerPrivateTemporaryDirectory(prefix="submodule-worktree-fetch.")
    fetch_git_dir = Path(guard.name).resolve(strict=True)
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
        config_content += render_fetch_object_policy(fetch_object_policy)
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
                MAX_SOURCE_CONFIG_BYTES,
            ),
            (
                head_path,
                f"isolated fetch HEAD for {submodule_path}",
                MAX_GITDIR_FILE_BYTES,
            ),
        ]
        if initial_shallow_content is not None:
            file_specs.append(
                (
                    private_shallow_path,
                    f"isolated fetch shallow boundary for {submodule_path}",
                    MAX_SOURCE_SHALLOW_BYTES,
                )
            )
        file_bindings = tuple(
            read_bound_regular_file(
                path,
                maximum_bytes=maximum_bytes,
                mode=os.R_OK,
                purpose=purpose,
                retain_content=False,
            )[0]
            for path, purpose, maximum_bytes in file_specs
        )
        return guard, fetch_git_dir, access_bindings, file_bindings
    except Exception:
        guard.cleanup()
        raise


SCP_STYLE_SSH_URL = re.compile(r"(?:[^@/:]+@)?[^/:]+:(?!//).+")


def transport_uses_ssh(url: str) -> bool:
    if SCP_STYLE_SSH_URL.fullmatch(url):
        return True
    scheme_match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", url)
    if scheme_match:
        return scheme_match.group(1).casefold() == "ssh"
    return False


def ssh_command_for_executable(executable: Path) -> str:
    return " ".join(
        [
            shlex.quote(str(executable)),
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


def validate_approved_fetch_url(url: str, submodule_path: str) -> None:
    if not url or "\x00" in url or "\n" in url or "\r" in url:
        raise PlanError(f"submodule {submodule_path} has an invalid approved fetch URL")
    if url.startswith("-") or "::" in url:
        raise PlanError(
            f"submodule {submodule_path} uses an unsupported fetch transport: {url}"
        )
    if Path(url).is_absolute():
        return
    if transport_uses_ssh(url):
        return
    scheme_match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", url)
    if scheme_match:
        if scheme_match.group(1).casefold() not in {
            "file",
            "git",
            "http",
            "https",
        }:
            raise PlanError(
                f"submodule {submodule_path} uses an unsupported fetch URL scheme: "
                f"{scheme_match.group(1)}"
            )
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
    fetch_object_policy = capture_fetch_object_policy(entries, config_path)
    validate_approved_fetch_url(submodule.url, submodule.path)
    if origin_url != submodule.url:
        raise PlanError(
            "source remote.origin.url does not match the task-approved "
            ".gitmodules URL\n"
            f"  submodule: {submodule.path}\n"
            f"  .gitmodules: {submodule.url}\n"
            f"  remote.origin.url: {origin_url}"
        )
    ssh_source: Optional[tuple[Path, FsFingerprint]] = None
    if transport_uses_ssh(origin_url):
        candidate = shutil.which("ssh")
        if not candidate:
            raise PlanError(
                f"cannot resolve SSH for the approved fetch transport: {origin_url}"
            )
        ssh_path = Path(candidate).resolve(strict=True)
        ssh_fingerprint = filesystem_fingerprint(ssh_path)
        if ssh_fingerprint.kind != stat.S_IFREG or not probe_access(
            ssh_path,
            os.R_OK | os.X_OK,
        ):
            raise PlanError(
                "resolved SSH executable is not an executable regular file\n"
                f"  executable: {ssh_path}"
            )
        ssh_source = (ssh_path, ssh_fingerprint)
    source_object_directory = (source_git_dir / "objects").resolve(strict=True)
    source_object_binding = capture_typed_access(
        source_object_directory,
        os.R_OK | os.W_OK | os.X_OK,
        f"authorized fetch object database for {submodule.path}",
        stat.S_IFDIR,
    )
    source_object_bindings = [source_object_binding]
    source_object_write_children = capture_object_write_child_inventory(
        source_object_binding,
        f"authorized fetch loose-object fanout and pack directory for {submodule.path}",
    )
    source_pack_directory = source_object_directory / "pack"
    source_pack_fingerprint = dict(source_object_write_children)["pack"]
    if source_pack_fingerprint is not None:
        source_pack_binding = capture_typed_access(
            source_pack_directory,
            os.R_OK | os.W_OK | os.X_OK,
            f"authorized fetch pack directory for {submodule.path}",
            stat.S_IFDIR,
        )
        if source_pack_binding.fingerprint != source_pack_fingerprint:
            raise PlanError(
                "authorized fetch pack directory changed during receipt binding"
            )
        source_object_bindings.append(source_pack_binding)
    (
        source_shallow_path,
        source_shallow_parent_binding,
        source_shallow_binding,
        source_shallow_content,
    ) = capture_source_shallow_state(source_git_dir, submodule.path)
    source_shallow_creation_policy = (
        capture_source_shallow_creation_policy(
            source_shallow_parent_binding,
            fetch_object_policy,
        )
        if source_shallow_binding is None
        else None
    )
    (
        fetch_guard,
        fetch_git_dir,
        private_access_bindings,
        fetch_file_bindings,
    ) = capture_fetch_control_gitdir(
        object_format,
        submodule.path,
        source_shallow_content,
        fetch_object_policy,
    )
    ssh_executable_snapshot: Optional[ExecutableSnapshotReceipt] = None
    ssh_command: Optional[str] = None
    combined_guard: object = fetch_guard
    try:
        if ssh_source is not None:
            ssh_path, ssh_fingerprint = ssh_source
            (
                ssh_guard,
                ssh_snapshot,
                ssh_source_state,
                ssh_snapshot_state,
                ssh_digest,
            ) = copy_executable_snapshot(
                ssh_path,
                ssh_fingerprint,
                prefix="submodule-worktree-ssh.",
                filename="ssh",
                maximum_bytes=MAX_TRANSPORT_EXECUTABLE_BYTES,
                description="SSH executable",
            )
            combined_guard = CleanupGuardGroup(fetch_guard, ssh_guard)
            ssh_executable_snapshot = ExecutableSnapshotReceipt(
                source_executable=ssh_path,
                source_state=ssh_source_state,
                executable=ssh_snapshot,
                executable_state=ssh_snapshot_state,
                content_sha256=ssh_digest,
            )
            ssh_command = ssh_command_for_executable(ssh_snapshot)
        runtime_receipt = git_runtime(require_transport_helpers=True)
        environment = git_environment(runtime=runtime_receipt)
        environment["GIT_OBJECT_DIRECTORY"] = str(source_object_directory)
        revalidate_file_content_binding(config_binding)
        return TransportReceipt(
            config_binding=config_binding,
            fetch_object_policy=fetch_object_policy,
            approved_url=submodule.url,
            origin_url=origin_url,
            ssh_executable_snapshot=ssh_executable_snapshot,
            ssh_command=ssh_command,
            source_object_directory=source_object_directory,
            source_shallow_path=source_shallow_path,
            source_shallow_parent_binding=source_shallow_parent_binding,
            source_shallow_binding=source_shallow_binding,
            source_shallow_creation_policy=source_shallow_creation_policy,
            source_object_write_children=source_object_write_children,
            fetch_git_dir=fetch_git_dir,
            fetch_access_bindings=(
                *source_object_bindings,
                *private_access_bindings,
            ),
            fetch_file_bindings=fetch_file_bindings,
            git_runtime_receipt=runtime_receipt,
            git_environment=tuple(sorted(environment.items())),
            fetch_guard=combined_guard,
        )
    except BaseException:
        cleanup = getattr(combined_guard, "cleanup", None)
        if cleanup is not None:
            cleanup()
        raise


def validate_frozen_git_environment(
    environment_items: tuple[tuple[str, str], ...],
    expected_object_directory: Path,
    expected_git_exec_path: Path,
) -> None:
    environment = dict(environment_items)
    if len(environment) != len(environment_items):
        raise PlanError("fetch transport environment contains duplicate keys")
    allowed = (
        set(GIT_ENV_PASSTHROUGH)
        | set(SAFE_GIT_ENV)
        | {
            "GIT_EXEC_PATH",
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
    if environment.get("GIT_EXEC_PATH") != str(expected_git_exec_path):
        raise PlanError("fetch transport environment changed the trusted helper path")
    if (
        not expected_object_directory.is_absolute()
        or not expected_git_exec_path.is_absolute()
    ):
        raise PlanError("fetch transport object/helper paths are not absolute")


def revalidate_transport_receipt(
    receipt: TransportReceipt,
    submodule: Submodule,
) -> None:
    revalidate_git_runtime(receipt.git_runtime_receipt)
    if (
        receipt.approved_url != submodule.url
        or receipt.origin_url != receipt.approved_url
    ):
        raise PlanError(f"fetch transport receipt no longer matches {submodule.path}")
    current_config_binding, current_config_content = read_bound_regular_file(
        receipt.config_binding.path,
        maximum_bytes=receipt.config_binding.maximum_bytes,
        mode=receipt.config_binding.mode,
        purpose=receipt.config_binding.purpose,
        retain_content=True,
    )
    require_matching_file_binding(
        receipt.config_binding,
        current_config_binding,
        "source Git config",
    )
    if current_config_content is None:
        raise PlanError("source Git config revalidation returned no content")
    current_entries = parse_bound_git_config(
        current_config_content,
        receipt.config_binding.path,
    )
    current_fetch_object_policy = capture_fetch_object_policy(
        current_entries,
        receipt.config_binding.path,
    )
    if current_fetch_object_policy != receipt.fetch_object_policy:
        raise PlanError("fetch object policy changed after preflight")
    ssh_snapshot = receipt.ssh_executable_snapshot
    if (ssh_snapshot is None) != (receipt.ssh_command is None):
        raise PlanError("fetch transport has an incomplete SSH executable receipt")
    if ssh_snapshot is not None:
        if receipt.ssh_command != ssh_command_for_executable(ssh_snapshot.executable):
            raise PlanError(
                "fetch transport SSH command no longer names the bound snapshot"
            )
        revalidate_executable_content(
            ssh_snapshot.source_executable,
            ssh_snapshot.source_state,
            ssh_snapshot.content_sha256,
            "source SSH executable",
        )
        revalidate_executable_content(
            ssh_snapshot.executable,
            ssh_snapshot.executable_state,
            ssh_snapshot.content_sha256,
            "owner-private SSH executable snapshot",
        )
    revalidate_source_shallow_state(receipt)
    revalidate_source_shallow_creation_policy(receipt)
    for binding in receipt.fetch_access_bindings:
        revalidate_access(binding)
    expected_object_binding = access_binding_for_path(
        receipt.fetch_access_bindings,
        receipt.source_object_directory,
        f"authorized fetch object database for {submodule.path}",
    )
    current_object_write_children = capture_object_write_child_inventory(
        expected_object_binding,
        f"authorized fetch loose-object fanout and pack directory for {submodule.path}",
    )
    if current_object_write_children != receipt.source_object_write_children:
        raise PlanError(
            "authorized fetch loose-object fanout or pack directory changed "
            "after preflight"
        )
    for binding in receipt.fetch_file_bindings:
        revalidate_file_content_binding(binding)
    validate_frozen_git_environment(
        receipt.git_environment,
        receipt.source_object_directory,
        receipt.git_runtime_receipt.exec_path,
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


@signal_owned_operation
def fetch_missing_commit(
    source_git_dir: Path,
    work_tree: Path,
    submodule: Submodule,
    sha: str,
    depth: int,
    dry_run: bool,
    transport_receipt: Optional[TransportReceipt] = None,
    source_completeness: Optional[SourceCompletenessReceipt] = None,
    fetch_missing: bool = False,
    terminal_completeness_receipts: Optional[list[SourceCompletenessReceipt]] = None,
) -> bool:
    if terminal_completeness_receipts is not None and terminal_completeness_receipts:
        raise PlanError("terminal source-completeness receipt output is not empty")
    completeness = source_completeness or capture_source_completeness_receipt(
        source_git_dir
    )
    if commit_exists(
        source_git_dir,
        work_tree,
        sha,
        completeness=completeness,
    ):
        if terminal_completeness_receipts is not None:
            terminal_completeness_receipts.append(completeness)
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
    revalidate_source_completeness_receipt(source_git_dir, completeness)
    revalidate_transport_receipt(receipt, submodule)
    expected_object_binding = access_binding_for_path(
        receipt.fetch_access_bindings,
        receipt.source_object_directory,
        f"authorized fetch object database for {submodule.path}",
    )
    source_object_lease = capture_directory_entry_lease(
        expected_object_binding.path,
        expected_object_binding.mode,
        expected_object_binding.purpose,
    )
    fetch_control_lease: Optional[FetchControlLease] = None
    try:
        if source_object_lease.binding != expected_object_binding:
            raise PlanError(
                "authorized fetch object database changed during descriptor binding"
            )
        revalidate_directory_entry_lease(source_object_lease)
        source_object_child_lease = capture_object_write_child_lease(
            source_object_lease,
            receipt.source_object_write_children,
            "authorized fetch loose-object fanout and pack directory for "
            f"{submodule.path}",
        )
        fetch_control_lease = capture_fetch_control_lease(
            receipt,
            submodule,
        )
        transaction = begin_source_fetch_transaction(receipt)
    except BaseException as exc:
        try:
            close_fetch_execution_leases(
                source_object_lease,
                fetch_control_lease,
            )
        except BaseException as cleanup_exc:
            raise PlanError(
                f"{exc}; fetch exec-lease cleanup failed: {cleanup_exc}"
            ) from exc
        raise
    if fetch_control_lease is None:
        raise PlanError("authorized fetch lacks a control-plane exec lease")
    try:
        print(
            f"fetch missing commit for {submodule.path}: {shell_join(command)}",
            flush=True,
        )
        bounded_result = run_bounded_bytes(
            command,
            check=False,
            timeout_seconds=GIT_ENUMERATION_TIMEOUT_SECONDS,
            stdout_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
            stderr_limit=GIT_ERROR_OUTPUT_LIMIT_BYTES,
            fixed_env=dict(receipt.git_environment),
            directory_identity_leases=(
                source_object_lease,
                *fetch_control_lease.directory_leases,
            ),
            directory_child_inventory_leases=(source_object_child_lease,),
            directory_absent_entry_leases=fetch_control_lease.absent_entry_leases,
            file_content_leases=fetch_control_lease.file_leases,
            git_runtime_receipt=receipt.git_runtime_receipt,
        )
        signal_checkpoint("fetch-process-complete")
    except BaseException as exc:
        failure_detail = str(exc)
        try:
            close_fetch_execution_leases(
                source_object_lease,
                fetch_control_lease,
            )
        except BaseException as cleanup_exc:
            failure_detail += f"; fetch exec-lease cleanup failed: {cleanup_exc}"
            if isinstance(exc, ForwardedProcessSignal):
                exc.add_cleanup_error(str(cleanup_exc))
        recovery_error = retain_source_fetch_transaction(
            transaction,
            f"fetch process failed before boundary installation: {failure_detail}",
        )
        if isinstance(exc, ForwardedProcessSignal):
            exc.add_recovery_detail(str(recovery_error))
            raise
        raise recovery_error from exc
    try:
        close_fetch_execution_leases(
            source_object_lease,
            fetch_control_lease,
        )
        result = subprocess.CompletedProcess(
            args=bounded_result.args,
            returncode=bounded_result.returncode,
            stdout=os.fsdecode(bounded_result.stdout),
            stderr=os.fsdecode(bounded_result.stderr),
        )
        terminal_completeness: Optional[SourceCompletenessReceipt] = None
        if result.returncode == 0:
            terminal_completeness = capture_source_completeness_receipt(source_git_dir)
            require_source_completeness_fetch_transition(
                completeness,
                terminal_completeness,
            )
        commit_available = terminal_completeness is not None and commit_exists(
            source_git_dir,
            work_tree,
            sha,
            transaction=transaction,
            completeness=terminal_completeness,
        )
        signal_checkpoint("fetch-post-fetch-object-available")
        if commit_available:
            install_post_fetch_shallow_state(receipt)
            signal_checkpoint("fetch-shallow-boundary-installed")
            revalidate_source_object_admission(source_git_dir, transaction)
            target_object_closure(
                source_git_dir,
                sha,
                terminal_completeness,
                transaction=transaction,
            )
            signal_checkpoint("fetch-object-closure-validated")
            if terminal_completeness_receipts is not None:
                terminal_completeness_receipts.append(terminal_completeness)
            complete_source_fetch_transaction(transaction)
            signal_checkpoint("fetch-fence-cleared")
            return True
    except BaseException as exc:
        if terminal_completeness_receipts is not None:
            terminal_completeness_receipts.clear()
        if transaction.active:
            recovery_error = retain_source_fetch_transaction(
                transaction,
                "fetch postprocess failed before a clean boundary/object terminal "
                f"state: {type(exc).__name__}: {exc}",
            )
            if isinstance(exc, ForwardedProcessSignal):
                exc.add_recovery_detail(str(recovery_error))
                raise
            raise recovery_error from exc
        raise
    stderr = (result.stderr or "").strip()
    branch_fetch_command = transport_fetch_command(
        source_git_dir,
        receipt,
        "<branch-or-tag>",
        100,
    )
    error = PlanError(
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
    raise retain_source_fetch_transaction(
        transaction,
        str(error),
    ) from error


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


def managed_gitdir_pointer_bytes(worktree_path: Path, content: bytes) -> bytes:
    raw = content.rstrip(b"\r\n")
    if b"\n" in raw or b"\r" in raw or b"\0" in raw or not raw.startswith(b"gitdir: "):
        raise PlanError(
            f"managed worktree has a malformed .git control file: {worktree_path}"
        )
    raw_path = raw[len(b"gitdir: ") :]
    if not raw_path or len(raw_path) > MAX_CHECKOUT_PATH_BYTES:
        raise PlanError(
            f"managed worktree has an empty or oversized admin path: {worktree_path}"
        )
    return raw_path


def plain_gitdir_pointer_bytes(content: bytes, purpose: str) -> bytes:
    raw_path = content.rstrip(b"\r\n")
    if (
        not raw_path
        or b"\n" in raw_path
        or b"\r" in raw_path
        or b"\0" in raw_path
        or len(raw_path) > MAX_CHECKOUT_PATH_BYTES
    ):
        raise PlanError(f"{purpose} is malformed")
    return raw_path


def nofollow_descriptor_chain_trace(
    raw_path: bytes,
    *,
    relative_base_descriptor: int,
    expected_kind: int,
    purpose: str,
) -> tuple[FsFingerprint, ...]:
    """Resolve one pointer with no-follow opens for every pathname component."""

    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise PlanError(
            f"cannot safely prove {purpose}: descriptor-relative O_NOFOLLOW and "
            "O_DIRECTORY are required"
        )
    if not raw_path or b"\0" in raw_path:
        raise PlanError(f"cannot safely prove {purpose}: pointer path is malformed")
    absolute = raw_path.startswith(b"/")
    if raw_path.startswith(b"//"):
        raise PlanError(
            f"cannot safely prove {purpose}: implementation-defined // paths "
            "are unsupported"
        )
    components = tuple(
        component for component in raw_path.split(b"/") if component not in {b"", b"."}
    )
    if expected_kind != stat.S_IFDIR and (not components or components[-1] == b".."):
        raise PlanError(
            f"cannot safely prove {purpose}: a regular-file endpoint must have "
            "an explicit final component"
        )

    descriptor = -1
    trace: list[FsFingerprint] = []
    try:
        if absolute:
            descriptor = os.open(
                b"/",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
        else:
            descriptor = os.dup(relative_base_descriptor)
        start_fingerprint = fingerprint_from_stat(os.fstat(descriptor))
        if start_fingerprint.kind != stat.S_IFDIR:
            raise PlanError(
                f"cannot safely prove {purpose}: pointer base is not a directory"
            )
        trace.append(start_fingerprint)

        for index, component in enumerate(components):
            final_component = index == len(components) - 1
            component_kind = expected_kind if final_component else stat.S_IFDIR
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            if component_kind == stat.S_IFDIR:
                flags |= os.O_DIRECTORY
            else:
                flags |= os.O_NONBLOCK
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            try:
                next_fingerprint = fingerprint_from_stat(os.fstat(next_descriptor))
                if next_fingerprint.kind != component_kind:
                    raise PlanError(
                        f"cannot safely prove {purpose}: descriptor-chain "
                        "component has an unsafe object type"
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            trace.append(next_fingerprint)
        if trace[-1].kind != expected_kind:
            raise PlanError(
                f"cannot safely prove {purpose}: pointer endpoint has an unsafe "
                "object type"
            )
        return tuple(trace)
    except OSError as exc:
        raise PlanError(
            f"cannot prove {purpose} through a no-follow descriptor chain\n"
            f"  pointer: {os.fsdecode(raw_path)}\n"
            f"  error: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def revalidate_nofollow_pointer_endpoint(
    raw_path: bytes,
    *,
    relative_base_descriptor: int,
    expected_fingerprint: FsFingerprint,
    expected_kind: int,
    purpose: str,
) -> None:
    """Prove a stable no-symlink path trace ending at the receipt-bound object."""

    first_trace = nofollow_descriptor_chain_trace(
        raw_path,
        relative_base_descriptor=relative_base_descriptor,
        expected_kind=expected_kind,
        purpose=purpose,
    )
    second_trace = nofollow_descriptor_chain_trace(
        raw_path,
        relative_base_descriptor=relative_base_descriptor,
        expected_kind=expected_kind,
        purpose=purpose,
    )
    if first_trace != second_trace:
        raise PlanError(
            f"{purpose} descriptor chain changed during endpoint revalidation"
        )
    if first_trace[-1] != expected_fingerprint:
        raise PlanError(
            f"{purpose} no-follow endpoint does not match the receipt-bound object"
        )


def parse_managed_gitdir_content(
    worktree_path: Path,
    source_git_dir: Path,
    content: bytes,
) -> Path:
    raw_path = managed_gitdir_pointer_bytes(worktree_path, content)
    admin_git_dir = Path(os.fsdecode(raw_path))
    if not admin_git_dir.is_absolute():
        admin_git_dir = worktree_path / admin_git_dir
    try:
        resolved_admin = admin_git_dir.resolve(strict=True)
        worktrees_root = (source_git_dir / "worktrees").resolve(strict=True)
        relative_admin = resolved_admin.relative_to(worktrees_root)
    except (OSError, ValueError) as exc:
        raise PlanError(
            "managed worktree admin directory is outside the selected source\n"
            f"  worktree: {worktree_path}\n"
            f"  admin: {admin_git_dir}\n"
            f"  source: {source_git_dir}"
        ) from exc
    if len(relative_admin.parts) != 1:
        raise PlanError(
            "managed worktree admin directory has an unexpected nested shape\n"
            f"  admin: {resolved_admin}"
        )
    return resolved_admin


def capture_directory_entry_lease(
    path: Path,
    mode: int,
    purpose: str,
    *,
    parent_mode: int = os.R_OK | os.X_OK,
) -> DirectoryEntryLease:
    resolved = path.resolve(strict=True)
    parent = resolved.parent
    parent_binding = capture_typed_access(
        parent,
        parent_mode,
        f"{purpose} parent",
        stat.S_IFDIR,
    )
    parent_descriptor = open_directory_descriptor(
        parent,
        f"{purpose} parent",
    )
    descriptor = -1
    try:
        revalidate_directory_descriptor(parent_binding, parent_descriptor)
        descriptor = os.open(
            resolved.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=parent_descriptor,
        )
        fingerprint = fingerprint_from_stat(os.fstat(descriptor))
        entry_fingerprint = fingerprint_from_stat(
            os.stat(
                resolved.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
        if fingerprint != entry_fingerprint:
            raise PlanError(f"{purpose} changed during descriptor binding")
        binding = AccessBinding(
            path=resolved,
            fingerprint=fingerprint,
            mode=mode,
            purpose=purpose,
        )
        revalidate_directory_descriptor(binding, descriptor)
        return DirectoryEntryLease(
            path=resolved,
            binding=binding,
            descriptor=descriptor,
            parent_binding=parent_binding,
            parent_descriptor=parent_descriptor,
            entry_name=resolved.name,
        )
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)
        raise


def revalidate_directory_entry_lease(lease: DirectoryEntryLease) -> None:
    revalidate_directory_descriptor(
        lease.parent_binding,
        lease.parent_descriptor,
    )
    revalidate_directory_descriptor(
        lease.binding,
        lease.descriptor,
    )
    try:
        entry_fingerprint = fingerprint_from_stat(
            os.stat(
                lease.entry_name,
                dir_fd=lease.parent_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError as exc:
        raise PlanError(
            f"cannot revalidate {lease.binding.purpose}\n"
            f"  path: {lease.path}\n"
            f"  error: {exc}"
        ) from exc
    if entry_fingerprint != lease.binding.fingerprint:
        raise PlanError(
            f"{lease.binding.purpose} directory entry changed\n  path: {lease.path}"
        )


def revalidate_descriptor_bound_file_lease(
    lease: DescriptorBoundFileLease,
) -> None:
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )
    observed, content = bind_regular_file_descriptor_at(
        lease.descriptor,
        lease.directory_descriptor,
        lease.entry_name,
        lease.binding.path,
        maximum_bytes=lease.binding.maximum_bytes,
        mode=lease.binding.mode,
        purpose=lease.binding.purpose,
        retain_content=True,
    )
    require_matching_file_binding(
        lease.binding,
        observed,
        lease.binding.purpose,
    )
    if content != lease.content:
        raise PlanError(
            f"{lease.binding.purpose} exact content changed after descriptor binding"
        )
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )


def revalidate_descriptor_bound_digest_file_lease(
    lease: DescriptorBoundDigestFileLease,
) -> None:
    # Protected properties are the same regular-file object, complete byte
    # content, and read policy. The fingerprint binds identity plus POSIX
    # owner/group/mode, size bounds the read, SHA-256 binds content, and the
    # access probe covers effective-credential/ACL effects. Timestamps are not
    # part of any selected property.
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )
    observed, _content = bind_regular_file_descriptor_at(
        lease.descriptor,
        lease.directory_descriptor,
        lease.entry_name,
        lease.binding.path,
        maximum_bytes=lease.binding.maximum_bytes,
        mode=lease.binding.mode,
        purpose=lease.binding.purpose,
        retain_content=False,
    )
    require_matching_file_binding(
        lease.binding,
        observed,
        lease.binding.purpose,
    )
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )


def directory_exact_inventory_at(
    directory_descriptor: int,
    directory_path: Path,
    purpose: str,
) -> tuple[tuple[str, FsFingerprint], ...]:
    """Capture the exact direct-child identity and POSIX-policy inventory.

    The protected property is the complete set of names plus each entry's
    device/inode/type/owner/group/mode. Additions, removals, replacements, and
    policy changes matter because stock Git may consume any published entry.
    Timestamps and bytes inside a retained regular file do not define this
    property; descriptor-bound content leases protect those bytes separately.
    """
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as exc:
        raise PlanError(
            f"cannot enumerate {purpose}\n  path: {directory_path}\n  error: {exc}"
        ) from exc
    if len(names) > MAX_CHECKOUT_OBJECTS:
        raise PlanError(
            f"{purpose} exceeds the {MAX_CHECKOUT_OBJECTS}-entry safety limit"
        )
    inventory: list[tuple[str, FsFingerprint]] = []
    for name in names:
        normalized = os.fsdecode(validate_descriptor_entry_name(name))
        try:
            fingerprint = fingerprint_from_stat(
                os.stat(
                    normalized,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            )
        except OSError as exc:
            raise PlanError(
                f"cannot inspect {purpose}\n"
                f"  path: {directory_path / normalized}\n"
                f"  error: {exc}"
            ) from exc
        inventory.append((normalized, fingerprint))
    return tuple(inventory)


def capture_directory_exact_inventory_lease(
    directory_lease: DirectoryEntryLease,
    expected_names: tuple[str, ...],
    purpose: str,
) -> DirectoryExactInventoryLease:
    normalized_names = tuple(
        sorted(
            os.fsdecode(validate_descriptor_entry_name(name)) for name in expected_names
        )
    )
    if len(set(normalized_names)) != len(normalized_names):
        raise PlanError(f"{purpose} has an invalid expected inventory")
    revalidate_directory_entry_lease(directory_lease)
    first = directory_exact_inventory_at(
        directory_lease.descriptor,
        directory_lease.path,
        purpose,
    )
    second = directory_exact_inventory_at(
        directory_lease.descriptor,
        directory_lease.path,
        purpose,
    )
    revalidate_directory_entry_lease(directory_lease)
    if (
        tuple(name for name, _fingerprint in first) != normalized_names
        or first != second
    ):
        raise PlanError(f"{purpose} changed or contains unexpected entries")
    return DirectoryExactInventoryLease(
        directory_binding=directory_lease.binding,
        directory_descriptor=directory_lease.descriptor,
        entries=first,
        purpose=purpose,
    )


def revalidate_directory_exact_inventory_lease(
    lease: DirectoryExactInventoryLease,
) -> None:
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )
    current = directory_exact_inventory_at(
        lease.directory_descriptor,
        lease.directory_binding.path,
        lease.purpose,
    )
    if current != lease.entries:
        raise PlanError(f"{lease.purpose} changed after descriptor binding")
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )


def require_absent_directory_entries(
    directory_descriptor: int,
    directory_path: Path,
    entry_names: tuple[str, ...],
    purpose: str,
) -> None:
    # Protected property: these names resolve to ENOENT at the pre-exec gate.
    # Any object type is unsafe before exec; Git-created output after exec is
    # intentionally outside this one-way launch receipt.
    for entry_name in entry_names:
        try:
            os.stat(
                entry_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PlanError(
                f"cannot prove an absent {purpose} entry\n"
                f"  path: {directory_path / entry_name}\n"
                f"  error: {exc}"
            ) from exc
        raise PlanError(
            f"{purpose} entry must be absent before exec\n"
            f"  path: {directory_path / entry_name}"
        )


def capture_directory_absent_entry_lease(
    directory_lease: DirectoryEntryLease,
    entry_names: tuple[str, ...],
    purpose: str,
) -> DirectoryAbsentEntryLease:
    if not entry_names or len(set(entry_names)) != len(entry_names):
        raise PlanError(f"{purpose} absent-entry names have an invalid shape")
    normalized_names = tuple(
        os.fsdecode(validate_descriptor_entry_name(entry_name))
        for entry_name in entry_names
    )
    revalidate_directory_entry_lease(directory_lease)
    require_absent_directory_entries(
        directory_lease.descriptor,
        directory_lease.path,
        normalized_names,
        purpose,
    )
    revalidate_directory_entry_lease(directory_lease)
    require_absent_directory_entries(
        directory_lease.descriptor,
        directory_lease.path,
        normalized_names,
        purpose,
    )
    revalidate_directory_entry_lease(directory_lease)
    return DirectoryAbsentEntryLease(
        directory_binding=directory_lease.binding,
        directory_descriptor=directory_lease.descriptor,
        entry_names=normalized_names,
        purpose=purpose,
    )


def revalidate_directory_absent_entry_lease(
    lease: DirectoryAbsentEntryLease,
) -> None:
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )
    require_absent_directory_entries(
        lease.directory_descriptor,
        lease.directory_binding.path,
        lease.entry_names,
        lease.purpose,
    )
    revalidate_directory_descriptor(
        lease.directory_binding,
        lease.directory_descriptor,
    )


def revalidate_fetch_control_lease(lease: FetchControlLease) -> None:
    if not lease.active or not lease.directory_leases:
        raise PlanError("fetch control-plane lease is inactive or incomplete")
    for directory_lease in lease.directory_leases:
        revalidate_directory_entry_lease(directory_lease)
    for absent_entry_lease in lease.absent_entry_leases:
        revalidate_directory_absent_entry_lease(absent_entry_lease)
    for file_lease in lease.file_leases:
        revalidate_descriptor_bound_file_lease(file_lease)
    for directory_lease in lease.directory_leases:
        revalidate_directory_entry_lease(directory_lease)
    for absent_entry_lease in lease.absent_entry_leases:
        revalidate_directory_absent_entry_lease(absent_entry_lease)


def capture_fetch_control_lease(
    receipt: TransportReceipt,
    submodule: Submodule,
) -> FetchControlLease:
    expected_directory_paths = tuple(
        receipt.fetch_git_dir.joinpath(*parts)
        for parts in FETCH_CONTROL_DIRECTORY_PARTS
    )
    directory_bindings = tuple(
        access_binding_for_path(
            receipt.fetch_access_bindings,
            path,
            f"isolated fetch control directory for {submodule.path}",
        )
        for path in expected_directory_paths
    )
    private_binding_paths = {
        binding.path
        for binding in receipt.fetch_access_bindings
        if binding.path == receipt.fetch_git_dir
        or receipt.fetch_git_dir in binding.path.parents
    }
    if private_binding_paths != set(expected_directory_paths):
        raise PlanError(
            f"isolated fetch control directory receipt has an invalid shape for "
            f"{submodule.path}"
        )

    with ExitStack() as cleanup:
        directory_leases: list[DirectoryEntryLease] = []
        file_leases: list[DescriptorBoundFileLease] = []
        for binding in directory_bindings:
            directory_lease = capture_directory_entry_lease(
                binding.path,
                binding.mode,
                binding.purpose,
            )
            if directory_lease.binding != binding:
                raise PlanError(f"{binding.purpose} changed during descriptor binding")
            directory_leases.append(directory_lease)
            cleanup.callback(directory_lease.close)
        root_lease = directory_leases[0]
        expected_file_names = FETCH_CONTROL_FILE_NAMES + (
            (SOURCE_SHALLOW_NAME,) if receipt.source_shallow_binding is not None else ()
        )
        observed_file_names = tuple(
            binding.path.name for binding in receipt.fetch_file_bindings
        )
        if observed_file_names != expected_file_names or any(
            binding.path.parent != receipt.fetch_git_dir
            for binding in receipt.fetch_file_bindings
        ):
            raise PlanError(
                f"isolated fetch control-file receipt has an invalid shape for "
                f"{submodule.path}"
            )
        for binding in receipt.fetch_file_bindings:
            entry_name = validate_descriptor_entry_name(binding.path.name)
            descriptor, observed, content = open_bound_regular_file_at(
                root_lease.descriptor,
                os.fsdecode(entry_name),
                binding.path,
                maximum_bytes=binding.maximum_bytes,
                mode=binding.mode,
                purpose=binding.purpose,
                retain_content=True,
            )
            cleanup.callback(os.close, descriptor)
            require_matching_file_binding(
                binding,
                observed,
                binding.purpose,
            )
            if content is None:
                raise PlanError(
                    f"{binding.purpose} descriptor binding returned no content"
                )
            file_leases.append(
                DescriptorBoundFileLease(
                    directory_binding=root_lease.binding,
                    directory_descriptor=root_lease.descriptor,
                    entry_name=binding.path.name,
                    descriptor=descriptor,
                    binding=binding,
                    content=content,
                )
            )
        absent_entry_names = (
            () if receipt.source_shallow_binding is not None else (SOURCE_SHALLOW_NAME,)
        ) + FETCH_CONTROL_LOCK_NAMES
        absent_entry_lease = capture_directory_absent_entry_lease(
            root_lease,
            absent_entry_names,
            f"isolated fetch control absence for {submodule.path}",
        )
        lease = FetchControlLease(
            directory_leases=tuple(directory_leases),
            file_leases=tuple(file_leases),
            absent_entry_leases=(absent_entry_lease,),
        )
        revalidate_fetch_control_lease(lease)
        cleanup.pop_all()
        return lease


def capture_managed_control_receipt(
    worktree_path: Path,
    source_git_dir: Path,
    target_descriptor: int,
) -> ManagedControlReceipt:
    descriptor, git_file_binding, content = open_bound_regular_file_at(
        target_descriptor,
        ".git",
        worktree_path / ".git",
        maximum_bytes=MAX_GITDIR_FILE_BYTES,
        mode=os.R_OK,
        purpose="descriptor-bound managed worktree control file",
        retain_content=True,
    )
    admin_lease: Optional[DirectoryEntryLease] = None
    try:
        if content is None:
            raise PlanError("managed worktree control-file binding returned no content")
        admin_git_dir = parse_managed_gitdir_content(
            worktree_path,
            source_git_dir,
            content,
        )
        admin_lease = capture_directory_entry_lease(
            admin_git_dir,
            os.R_OK | os.W_OK | os.X_OK,
            "managed worktree administration",
            parent_mode=os.R_OK | os.W_OK | os.X_OK,
        )
        if not owner_mode_permits_write_search(admin_lease.parent_binding.fingerprint):
            raise PlanError(
                "managed worktree administration parent owner policy denies "
                "write/search\n"
                f"  path: {admin_lease.parent_binding.path}"
            )
    except BaseException:
        try:
            if admin_lease is not None:
                admin_lease.close()
        finally:
            os.close(descriptor)
        raise
    assert admin_lease is not None
    backlink_descriptor = -1
    try:
        backlink_descriptor = os.open(
            "gitdir",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=admin_lease.descriptor,
        )
        admin_gitdir_binding, backlink_content = bind_regular_file_descriptor_at(
            backlink_descriptor,
            admin_lease.descriptor,
            "gitdir",
            admin_git_dir / "gitdir",
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree admin backlink",
            retain_content=True,
        )
        if backlink_content is None:
            raise PlanError("managed worktree admin backlink returned no content")
        raw_backlink = plain_gitdir_pointer_bytes(
            backlink_content,
            "managed worktree admin backlink",
        )
        backlink_path = Path(os.fsdecode(raw_backlink))
        if not backlink_path.is_absolute():
            backlink_path = admin_git_dir / backlink_path
        expected_gitfile = (worktree_path / ".git").resolve(strict=True)
        if backlink_path.resolve(strict=True) != expected_gitfile:
            raise PlanError(
                "managed worktree admin backlink points at a different worktree\n"
                f"  worktree: {worktree_path}\n"
                f"  admin: {admin_git_dir}\n"
                f"  backlink: {backlink_path}\n"
                f"  expected: {expected_gitfile}"
            )
        revalidate_nofollow_pointer_endpoint(
            managed_gitdir_pointer_bytes(worktree_path, content),
            relative_base_descriptor=target_descriptor,
            expected_fingerprint=admin_lease.binding.fingerprint,
            expected_kind=stat.S_IFDIR,
            purpose="managed worktree .git-to-admin pointer",
        )
        revalidate_nofollow_pointer_endpoint(
            raw_backlink,
            relative_base_descriptor=admin_lease.descriptor,
            expected_fingerprint=git_file_binding.fingerprint,
            expected_kind=stat.S_IFREG,
            purpose="managed worktree admin backlink endpoint",
        )
        observed_git_file, _ = bind_regular_file_descriptor_at(
            descriptor,
            target_descriptor,
            ".git",
            git_file_binding.path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="descriptor-bound managed worktree control file",
            retain_content=False,
        )
        require_matching_file_binding(
            git_file_binding,
            observed_git_file,
            "descriptor-bound managed worktree control file",
        )
        observed_backlink, _ = bind_regular_file_descriptor_at(
            backlink_descriptor,
            admin_lease.descriptor,
            "gitdir",
            admin_gitdir_binding.path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree admin backlink",
            retain_content=False,
        )
        require_matching_file_binding(
            admin_gitdir_binding,
            observed_backlink,
            "managed worktree admin backlink",
        )
        return ManagedControlReceipt(
            git_file_descriptor=descriptor,
            git_file_binding=git_file_binding,
            git_file_content=content,
            admin_git_dir=admin_git_dir,
            admin_lease=admin_lease,
            admin_gitdir_descriptor=backlink_descriptor,
            admin_gitdir_binding=admin_gitdir_binding,
            admin_gitdir_content=backlink_content,
        )
    except BaseException:
        try:
            if backlink_descriptor >= 0:
                os.close(backlink_descriptor)
        finally:
            try:
                admin_lease.close()
            finally:
                os.close(descriptor)
        raise


def revalidate_managed_admin_backlink(
    receipt: ManagedControlReceipt,
) -> None:
    revalidate_directory_entry_lease(receipt.admin_lease)
    current_backlink, _ = bind_regular_file_descriptor_at(
        receipt.admin_gitdir_descriptor,
        receipt.admin_lease.descriptor,
        "gitdir",
        receipt.admin_gitdir_binding.path,
        maximum_bytes=MAX_GITDIR_FILE_BYTES,
        mode=os.R_OK,
        purpose="managed worktree admin backlink",
        retain_content=False,
    )
    require_matching_file_binding(
        receipt.admin_gitdir_binding,
        current_backlink,
        "managed worktree admin backlink",
    )
    revalidate_directory_entry_lease(receipt.admin_lease)


def revalidate_managed_control_endpoints(
    receipt: ManagedControlReceipt,
    target_descriptor: int,
) -> None:
    revalidate_nofollow_pointer_endpoint(
        managed_gitdir_pointer_bytes(
            receipt.git_file_binding.path.parent,
            receipt.git_file_content,
        ),
        relative_base_descriptor=target_descriptor,
        expected_fingerprint=receipt.admin_lease.binding.fingerprint,
        expected_kind=stat.S_IFDIR,
        purpose="managed worktree .git-to-admin pointer",
    )
    revalidate_nofollow_pointer_endpoint(
        plain_gitdir_pointer_bytes(
            receipt.admin_gitdir_content,
            "managed worktree admin backlink",
        ),
        relative_base_descriptor=receipt.admin_lease.descriptor,
        expected_fingerprint=receipt.git_file_binding.fingerprint,
        expected_kind=stat.S_IFREG,
        purpose="managed worktree admin backlink endpoint",
    )


def revalidate_managed_control_receipt(
    receipt: ManagedControlReceipt,
    target_descriptor: int,
) -> None:
    revalidate_managed_admin_backlink(receipt)
    current_binding, _ = bind_regular_file_descriptor_at(
        receipt.git_file_descriptor,
        target_descriptor,
        ".git",
        receipt.git_file_binding.path,
        maximum_bytes=MAX_GITDIR_FILE_BYTES,
        mode=os.R_OK,
        purpose="descriptor-bound managed worktree control file",
        retain_content=False,
    )
    require_matching_file_binding(
        receipt.git_file_binding,
        current_binding,
        "descriptor-bound managed worktree control file",
    )
    revalidate_managed_admin_backlink(receipt)
    revalidate_managed_control_endpoints(receipt, target_descriptor)
    current_binding, _ = bind_regular_file_descriptor_at(
        receipt.git_file_descriptor,
        target_descriptor,
        ".git",
        receipt.git_file_binding.path,
        maximum_bytes=MAX_GITDIR_FILE_BYTES,
        mode=os.R_OK,
        purpose="descriptor-bound managed worktree control file",
        retain_content=False,
    )
    require_matching_file_binding(
        receipt.git_file_binding,
        current_binding,
        "descriptor-bound managed worktree control file",
    )
    revalidate_managed_admin_backlink(receipt)


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


def has_local_changes(
    worktree_path: Path,
    current_head: str,
    checkout_view: CheckoutExecutionView,
    managed_preflight: ManagedPreflightReceipt,
) -> bool:
    result = run_managed_preflight_probe(
        worktree_path,
        current_head,
        checkout_view,
        managed_preflight,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
            "--no-renames",
        ],
    )
    records = bounded_records(
        result.stdout,
        "managed worktree tracked-status inventory",
    )
    return bool(records)


def run_managed_preflight_probe(
    worktree_path: Path,
    attribute_source: str,
    checkout_view: CheckoutExecutionView,
    managed_preflight: ManagedPreflightReceipt,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    expected_worktree = managed_preflight.control.git_file_binding.path.parent
    if worktree_path.resolve(strict=True) != expected_worktree.resolve(strict=True):
        raise PlanError("managed preflight probe names a different worktree")
    if checkout_view.private_index_path is None:
        raise PlanError("managed preflight probe lacks an isolated index snapshot")

    revalidate_managed_preflight_receipt(managed_preflight)
    revalidate_checkout_execution_view(checkout_view)
    environment = checkout_execution_environment(
        checkout_view,
        attribute_source,
    )
    result: Optional[subprocess.CompletedProcess[bytes]] = None
    outcome_exception: Optional[BaseException] = None
    try:
        result = read_git_bounded(
            [
                f"--git-dir={managed_preflight.control.admin_git_dir}",
                "--work-tree=.",
                *args,
            ],
            check=check,
            extra_env=environment,
            directory_descriptor=managed_preflight.target_descriptor,
            directory_identity_leases=(
                managed_preflight.control.admin_lease,
                *checkout_view.directory_leases,
            ),
            directory_exact_inventory_leases=(checkout_view.exact_inventory_leases),
            directory_absent_entry_leases=checkout_view.absent_entry_leases,
            file_content_leases=checkout_view.file_leases,
            digest_file_leases=checkout_view.digest_file_leases,
        )
    except BaseException as exc:
        outcome_exception = exc

    terminal_errors: list[str] = []
    for purpose, revalidator in (
        (
            "managed worktree control/index",
            lambda: revalidate_managed_preflight_receipt(managed_preflight),
        ),
        (
            "isolated checkout execution view",
            lambda: revalidate_checkout_execution_view(checkout_view),
        ),
    ):
        try:
            revalidator()
        except BaseException as exc:
            terminal_errors.append(
                f"{purpose} terminal revalidation failed: {type(exc).__name__}: {exc}"
            )
    if terminal_errors:
        detail = "\n".join(terminal_errors)
        if isinstance(outcome_exception, ForwardedProcessSignal):
            outcome_exception.add_cleanup_error(detail)
            raise outcome_exception
        if outcome_exception is not None:
            raise PlanError(f"{outcome_exception}\n{detail}") from outcome_exception
        raise PlanError(detail)
    if outcome_exception is not None:
        raise outcome_exception
    if result is None:
        raise PlanError("managed preflight probe returned no process result")
    return result


def revalidate_source_registry_lease(
    source_git_dir: Path,
    source_lease: DirectoryEntryLease,
) -> None:
    """Prove that a registry query still names the held source directory."""

    try:
        resolved_source = source_git_dir.resolve(strict=True)
    except OSError as exc:
        raise PlanError(
            "cannot resolve the source worktree registry while its lease is held\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  error: {exc}"
        ) from exc
    if source_lease.path != resolved_source:
        raise PlanError(
            "source worktree registry query does not match its held lease\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  leased source: {source_lease.path}"
        )
    revalidate_directory_entry_lease(source_lease)


def revalidate_worktree_admin_root_descriptor(
    source_git_dir: Path,
    source_lease: DirectoryEntryLease,
    root_descriptor: int,
    expected: FsFingerprint,
) -> None:
    """Keep raw registry reads on the source-lease-bound worktrees object."""

    revalidate_source_registry_lease(source_git_dir, source_lease)
    descriptor_fingerprint = fingerprint_from_stat(os.fstat(root_descriptor))
    if descriptor_fingerprint != expected:
        raise PlanError(
            "source worktree administration descriptor object or access policy "
            "changed\n"
            f"  path: {source_git_dir / 'worktrees'}"
        )
    try:
        entry_fingerprint = fingerprint_from_stat(
            os.stat(
                "worktrees",
                dir_fd=source_lease.descriptor,
                follow_symlinks=False,
            )
        )
    except OSError as exc:
        raise PlanError(
            "cannot revalidate the source worktree administration directory\n"
            f"  path: {source_git_dir / 'worktrees'}\n"
            f"  error: {exc}"
        ) from exc
    if entry_fingerprint != expected:
        raise PlanError(
            "source worktree administration directory entry changed\n"
            f"  path: {source_git_dir / 'worktrees'}"
        )
    # Protected property: the receipt-bound owner can both publish and remove
    # administration entries without relying on DAC override. Directory entry
    # churn is expected during worktree add/remove; identity and the recorded
    # POSIX access policy remain exact.
    if not owner_mode_permits_write_search(expected):
        raise PlanError(
            "source worktree administration owner policy denies write/search\n"
            f"  path: {source_git_dir / 'worktrees'}"
        )
    if not probe_access_at(
        root_descriptor,
        ".",
        os.R_OK | os.W_OK | os.X_OK,
    ):
        raise PlanError(
            "source worktree administration access policy now denies "
            "read/write/search\n"
            f"  path: {source_git_dir / 'worktrees'}"
        )
    revalidate_source_registry_lease(source_git_dir, source_lease)


def worktree_admin_entries_at(
    root_descriptor: int,
    root_path: Path,
) -> tuple[tuple[bytes, FsFingerprint], ...]:
    """Inventory raw direct children without treating their internal churn as drift."""

    entries: list[tuple[bytes, FsFingerprint]] = []
    retained_name_bytes = 0
    try:
        with os.scandir(root_descriptor) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_WORKTREE_ADMIN_ENTRIES:
                    raise PlanError(
                        "source worktree administration inventory exceeds the "
                        f"{MAX_WORKTREE_ADMIN_ENTRIES}-entry limit"
                    )
                raw_name = validate_descriptor_entry_name(entry.name)
                if len(raw_name) > MAX_WORKTREE_ADMIN_NAME_BYTES:
                    raise PlanError(
                        "source worktree administration inventory contains an "
                        "oversized entry name"
                    )
                retained_name_bytes += len(raw_name)
                if retained_name_bytes > MAX_WORKTREE_ADMIN_NAMES_BYTES:
                    raise PlanError(
                        "source worktree administration inventory exceeds the "
                        f"{MAX_WORKTREE_ADMIN_NAMES_BYTES}-byte name budget"
                    )
                try:
                    entry_fingerprint = fingerprint_from_stat(
                        os.stat(
                            raw_name,
                            dir_fd=root_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except OSError as exc:
                    raise PlanError(
                        "cannot bind a source worktree administration entry\n"
                        f"  path: {root_path / os.fsdecode(raw_name)}\n"
                        f"  error: {exc}"
                    ) from exc
                entries.append((raw_name, entry_fingerprint))
    except OSError as exc:
        raise PlanError(
            "cannot inventory the source worktree administration directory\n"
            f"  path: {root_path}\n"
            f"  error: {exc}"
        ) from exc
    entries.sort(key=lambda item: item[0])
    return tuple(entries)


def capture_worktree_admin_inventory(
    source_git_dir: Path,
    source_lease: DirectoryEntryLease,
) -> WorktreeAdminInventory:
    """Take a stable, bounded raw worktrees/ snapshot through the source lease."""

    revalidate_source_registry_lease(source_git_dir, source_lease)
    root_path = source_git_dir / "worktrees"
    root_descriptor = -1
    try:
        root_descriptor = os.open(
            "worktrees",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=source_lease.descriptor,
        )
    except FileNotFoundError:
        revalidate_source_registry_lease(source_git_dir, source_lease)
        try:
            os.stat(
                "worktrees",
                dir_fd=source_lease.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            revalidate_source_registry_lease(source_git_dir, source_lease)
            return WorktreeAdminInventory(
                source_fingerprint=source_lease.binding.fingerprint,
                root_fingerprint=None,
                entries=(),
            )
        except OSError as exc:
            raise PlanError(
                "cannot confirm the absent source worktree administration "
                "directory\n"
                f"  path: {root_path}\n"
                f"  error: {exc}"
            ) from exc
        raise PlanError(
            "source worktree administration directory appeared during inventory\n"
            f"  path: {root_path}"
        )
    except OSError as exc:
        raise PlanError(
            "cannot descriptor-bind the source worktree administration directory\n"
            f"  path: {root_path}\n"
            f"  error: {exc}"
        ) from exc

    try:
        root_fingerprint = fingerprint_from_stat(os.fstat(root_descriptor))
        if root_fingerprint.kind != stat.S_IFDIR:
            raise PlanError(
                "source worktree administration entry is not a directory\n"
                f"  path: {root_path}"
            )
        revalidate_worktree_admin_root_descriptor(
            source_git_dir,
            source_lease,
            root_descriptor,
            root_fingerprint,
        )
        first_entries = worktree_admin_entries_at(root_descriptor, root_path)
        revalidate_worktree_admin_root_descriptor(
            source_git_dir,
            source_lease,
            root_descriptor,
            root_fingerprint,
        )
        second_entries = worktree_admin_entries_at(root_descriptor, root_path)
        if first_entries != second_entries:
            raise PlanError(
                "source worktree administration entries changed during inventory\n"
                f"  path: {root_path}"
            )
        revalidate_worktree_admin_root_descriptor(
            source_git_dir,
            source_lease,
            root_descriptor,
            root_fingerprint,
        )
        return WorktreeAdminInventory(
            source_fingerprint=source_lease.binding.fingerprint,
            root_fingerprint=root_fingerprint,
            entries=second_entries,
        )
    finally:
        os.close(root_descriptor)


def worktree_admin_inventory_drift(
    source_git_dir: Path,
    before: WorktreeAdminInventory,
    after: WorktreeAdminInventory,
) -> Optional[tuple[str, Path, str]]:
    root_path = source_git_dir / "worktrees"
    if before.source_fingerprint != after.source_fingerprint:
        return (
            "source-object-changed",
            source_git_dir,
            "the raw administration inventories were captured from different "
            "source directory objects",
        )
    if before.root_fingerprint is not None:
        if after.root_fingerprint is None:
            return (
                "admin-root-removed",
                root_path,
                "the pre-add administration directory disappeared",
            )
        if before.root_fingerprint != after.root_fingerprint:
            return (
                "admin-root-changed",
                root_path,
                "the administration directory object or access policy changed",
            )

    before_entries = dict(before.entries)
    after_entries = dict(after.entries)
    for raw_name in sorted(after_entries.keys() - before_entries.keys()):
        return (
            "admin-entry-added",
            root_path / os.fsdecode(raw_name),
            "a raw administration entry appeared after the add attempt",
        )
    for raw_name in sorted(before_entries.keys() & after_entries.keys()):
        if before_entries[raw_name] != after_entries[raw_name]:
            return (
                "admin-entry-changed",
                root_path / os.fsdecode(raw_name),
                "a raw administration entry object or access policy changed "
                "after the add attempt",
            )
    for raw_name in sorted(before_entries.keys() - after_entries.keys()):
        return (
            "admin-entry-removed",
            root_path / os.fsdecode(raw_name),
            "a pre-existing raw administration entry disappeared after the add attempt",
        )
    return None


def require_unchanged_worktree_admin_inventory(
    source_git_dir: Path,
    before: WorktreeAdminInventory,
    after: WorktreeAdminInventory,
    *,
    preserved_scope: str,
) -> None:
    drift = worktree_admin_inventory_drift(source_git_dir, before, after)
    if drift is None:
        return
    status, location, detail = drift
    raise WorktreeRegistrationRecoveryError(
        status=status,
        location=location,
        preserved_scope=preserved_scope,
        detail=detail,
    )


def validate_expected_worktree_admin_add(
    source_git_dir: Path,
    before: WorktreeAdminInventory,
    after: WorktreeAdminInventory,
    control: ManagedControlReceipt,
) -> None:
    """Prove that add introduced only the receipt-bound administration entry."""

    root_path = source_git_dir / "worktrees"
    expected_parent_path = source_git_dir.resolve(strict=True) / "worktrees"
    expected_name = validate_descriptor_entry_name(control.admin_lease.entry_name)
    before_entries = dict(before.entries)
    after_entries = dict(after.entries)
    if before.source_fingerprint != after.source_fingerprint:
        raise PlanError("source object changed across worktree administration add")
    if control.admin_lease.parent_binding.path != expected_parent_path:
        raise PlanError(
            "managed worktree control receipt names a different administration "
            "parent\n"
            f"  expected: {expected_parent_path}\n"
            f"  observed: {control.admin_lease.parent_binding.path}"
        )
    if after.root_fingerprint is None:
        raise PlanError(
            "source worktree administration directory is absent after add\n"
            f"  path: {root_path}"
        )
    if after.root_fingerprint != control.admin_lease.parent_binding.fingerprint:
        raise PlanError(
            "post-add administration inventory and managed control receipt "
            "name different parent objects\n"
            f"  path: {root_path}"
        )
    if (
        before.root_fingerprint is not None
        and before.root_fingerprint != after.root_fingerprint
    ):
        raise PlanError(
            "source worktree administration directory changed across add\n"
            f"  path: {root_path}"
        )
    if expected_name in before_entries:
        raise PlanError(
            "new managed worktree administration name existed before add\n"
            f"  path: {control.admin_git_dir}"
        )
    expected_fingerprint = after_entries.pop(expected_name, None)
    if expected_fingerprint != control.admin_lease.binding.fingerprint:
        raise PlanError(
            "raw worktree administration inventory does not contain the "
            "receipt-bound added entry\n"
            f"  path: {control.admin_git_dir}"
        )
    if after_entries != before_entries:
        raise PlanError(
            "raw worktree administration inventory changed beyond the "
            "receipt-bound added entry\n"
            f"  path: {root_path}"
        )


def registered_worktree_paths(
    source_git_dir: Path,
    *,
    source_lease: Optional[DirectoryEntryLease] = None,
) -> list[Path]:
    identity_leases: tuple[DirectoryEntryLease, ...] = ()
    if source_lease is not None:
        revalidate_source_registry_lease(source_git_dir, source_lease)
        identity_leases = (source_lease,)
    try:
        result = read_git_bounded(
            [
                *source_object_repo_args(source_git_dir),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
            directory_identity_leases=identity_leases,
        )
    except BaseException as query_exc:
        if source_lease is not None:
            try:
                revalidate_source_registry_lease(source_git_dir, source_lease)
            except BaseException as lease_exc:
                raise PlanError(
                    f"{query_exc}\n"
                    "source worktree registry query lease changed while the "
                    f"query failed: {lease_exc}"
                ) from query_exc
        raise
    if source_lease is not None:
        # Close the subprocess identity gate before trusting any query bytes.
        revalidate_source_registry_lease(source_git_dir, source_lease)
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
    if source_lease is not None:
        # Parsing can be bounded but non-trivial; close that interval as well.
        revalidate_source_registry_lease(source_git_dir, source_lease)
    return paths


def registered_target_path(
    source_git_dir: Path,
    target_path: Path,
    *,
    source_lease: Optional[DirectoryEntryLease] = None,
) -> Optional[Path]:
    resolved_target = target_path.resolve(strict=False)
    for registered_path in registered_worktree_paths(
        source_git_dir,
        source_lease=source_lease,
    ):
        if registered_path == resolved_target:
            if source_lease is not None:
                revalidate_source_registry_lease(source_git_dir, source_lease)
            return registered_path
    if source_lease is not None:
        revalidate_source_registry_lease(source_git_dir, source_lease)
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


@signal_owned_operation
def checkout_existing_worktree(
    worktree_path: Path,
    sha: str,
    dry_run: bool,
    *,
    target_descriptor: Optional[int] = None,
    source_git_dir: Optional[Path] = None,
    source_lease: Optional[DirectoryEntryLease] = None,
    checkout_view: Optional[CheckoutExecutionView] = None,
    finalize_checkout: Optional[
        Callable[[ManagedControlReceipt, DirectoryEntryLease], None]
    ] = None,
) -> None:
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
    if target_descriptor is None:
        raise PlanError("managed checkout requires a descriptor-bound target directory")
    if source_git_dir is None:
        raise PlanError("managed checkout requires an explicit common gitdir")
    if source_lease is None or source_lease.path != source_git_dir.resolve(strict=True):
        raise PlanError("managed checkout requires a matching source gitdir lease")
    if checkout_view is None:
        raise PlanError("managed checkout requires an isolated execution view")
    control = capture_managed_control_receipt(
        worktree_path,
        source_git_dir,
        target_descriptor,
    )
    try:
        revalidate_managed_control_receipt(control, target_descriptor)
        revalidate_directory_entry_lease(source_lease)
        environment = checkout_execution_environment(checkout_view, sha)
        run_git_at_directory_descriptor(
            [
                "git",
                f"--git-dir={control.admin_git_dir}",
                "--work-tree=.",
                "checkout",
                "--no-overwrite-ignore",
                "--no-recurse-submodules",
                "--detach",
                sha,
            ],
            target_descriptor,
            extra_env=environment,
            directory_identity_leases=(
                source_lease,
                control.admin_lease,
                *checkout_view.directory_leases,
            ),
            directory_exact_inventory_leases=checkout_view.exact_inventory_leases,
            directory_absent_entry_leases=checkout_view.absent_entry_leases,
            file_content_leases=checkout_view.file_leases,
            digest_file_leases=checkout_view.digest_file_leases,
        )
        signal_checkpoint("checkout-git-complete")
        revalidate_managed_control_receipt(control, target_descriptor)
        revalidate_directory_entry_lease(source_lease)
        if finalize_checkout is not None:
            finalize_checkout(control, source_lease)
    except BaseException as exc:
        try:
            control.close()
        except BaseException as cleanup_exc:
            if isinstance(exc, ForwardedProcessSignal):
                exc.add_cleanup_error(
                    f"managed control receipt cleanup failed: {cleanup_exc}"
                )
            else:
                raise PlanError(
                    f"{exc}\nmanaged control receipt cleanup failed: {cleanup_exc}"
                ) from exc
        if isinstance(exc, ForwardedProcessSignal):
            exc.add_recovery_detail(
                json.dumps(
                    {
                        "operation": "managed-worktree-checkout",
                        "profile": "worktree-signal-recovery-v1",
                        "recovery_status": "interrupted-checkout-retained",
                        "source_git_dir": str(source_git_dir),
                        "target": str(worktree_path),
                        "target_commit": sha,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        raise
    else:
        outcome_exception: Optional[BaseException] = None
        try:
            signal_checkpoint("checkout-final-validation-complete")
            signal_checkpoint("checkout-receipt-published")
        except BaseException as exc:
            outcome_exception = exc
            if isinstance(exc, ForwardedProcessSignal):
                exc.add_recovery_detail(
                    json.dumps(
                        {
                            "operation": "managed-worktree-checkout",
                            "profile": "worktree-signal-recovery-v1",
                            "recovery_status": "completed-checkout-published",
                            "source_git_dir": str(source_git_dir),
                            "target": str(worktree_path),
                            "target_commit": sha,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            raise
        finally:
            finish_explicit_cleanup(
                control.close,
                outcome_exception=outcome_exception,
                purpose="managed control receipt",
                recovery_identity=str(control.admin_git_dir),
            )


def stage_retained_control_file_for_rollback(
    *,
    directory_descriptor: int,
    entry_name: str,
    display_path: Path,
    original_descriptor: int,
    original_binding: FileContentBinding,
    original_content: bytes,
    purpose: str,
) -> Optional[ManagedControlRollbackSwap]:
    """Expose retained control bytes while preserving an unexpected replacement."""

    try:
        observed, _ = bind_regular_file_descriptor_at(
            original_descriptor,
            directory_descriptor,
            entry_name,
            display_path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose=purpose,
            retain_content=False,
        )
        require_matching_file_binding(
            original_binding,
            observed,
            purpose,
        )
        return None
    except PlanError:
        pass

    temporary_name = f".codex-control-rollback-{secrets.token_hex(16)}"
    temporary_descriptor = -1
    unexpected_descriptor = -1
    exchanged = False
    try:
        (
            unexpected_descriptor,
            unexpected_binding,
            _unexpected_content,
        ) = open_bound_regular_file_at(
            directory_descriptor,
            entry_name,
            display_path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose=f"unexpected {purpose} replacement",
            retain_content=False,
        )
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            original_binding.fingerprint.permissions,
            dir_fd=directory_descriptor,
        )
        os.fchmod(
            temporary_descriptor,
            original_binding.fingerprint.permissions,
        )
        remaining = memoryview(original_content)
        while remaining:
            written = os.write(temporary_descriptor, remaining)
            if written <= 0:
                raise PlanError(f"cannot write the retained {purpose} for rollback")
            remaining = remaining[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        descriptor_atomic_rename_exchange(
            directory_descriptor,
            temporary_name,
            entry_name,
        )
        exchanged = True
        observed_unexpected, _ = bind_regular_file_descriptor_at(
            unexpected_descriptor,
            directory_descriptor,
            temporary_name,
            display_path.parent / temporary_name,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose=f"preserved unexpected {purpose}",
            retain_content=False,
        )
        require_matching_file_binding(
            unexpected_binding,
            observed_unexpected,
            f"preserved unexpected {purpose}",
        )
        restored_descriptor, restored_binding, _ = open_bound_regular_file_at(
            directory_descriptor,
            entry_name,
            display_path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose=f"rollback-restored {purpose}",
            retain_content=False,
        )
        os.close(restored_descriptor)
        if (
            restored_binding.size != original_binding.size
            or restored_binding.content_sha256 != original_binding.content_sha256
            or restored_binding.fingerprint.owner != original_binding.fingerprint.owner
            or restored_binding.fingerprint.group != original_binding.fingerprint.group
            or restored_binding.fingerprint.permissions
            != original_binding.fingerprint.permissions
        ):
            raise PlanError(
                f"rollback-restored {purpose} does not match the retained "
                "content or access policy"
            )
        os.fsync(directory_descriptor)
        swap = ManagedControlRollbackSwap(
            directory_descriptor=directory_descriptor,
            entry_name=entry_name,
            display_path=display_path,
            temporary_name=temporary_name,
            unexpected_descriptor=unexpected_descriptor,
            unexpected_binding=unexpected_binding,
        )
        unexpected_descriptor = -1
        return swap
    except BaseException as exc:
        cleanup_error: Optional[BaseException] = None
        if exchanged:
            try:
                descriptor_atomic_rename_exchange(
                    directory_descriptor,
                    temporary_name,
                    entry_name,
                )
                os.unlink(
                    temporary_name,
                    dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            except BaseException as restore_exc:
                cleanup_error = restore_exc
        elif temporary_name:
            try:
                os.unlink(
                    temporary_name,
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                cleanup_error = cleanup_exc
        if cleanup_error is not None:
            raise PlanError(
                f"{exc}\n{purpose} rollback staging cleanup failed: {cleanup_error}"
            ) from exc
        raise
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if unexpected_descriptor >= 0:
            os.close(unexpected_descriptor)


def restore_control_file_after_failed_rollback(
    swap: ManagedControlRollbackSwap,
) -> None:
    """Restore one unexpected control object when Git cannot finish rollback."""

    descriptor_atomic_rename_exchange(
        swap.directory_descriptor,
        swap.temporary_name,
        swap.entry_name,
    )
    observed, _ = bind_regular_file_descriptor_at(
        swap.unexpected_descriptor,
        swap.directory_descriptor,
        swap.entry_name,
        swap.display_path,
        maximum_bytes=MAX_GITDIR_FILE_BYTES,
        mode=os.R_OK,
        purpose="restored unexpected managed control file",
        retain_content=False,
    )
    require_matching_file_binding(
        swap.unexpected_binding,
        observed,
        "restored unexpected managed worktree control file",
    )
    os.unlink(
        swap.temporary_name,
        dir_fd=swap.directory_descriptor,
    )
    os.fsync(swap.directory_descriptor)


def stage_managed_control_files_for_added_worktree_rollback(
    receipt: ManagedControlReceipt,
    lease: MaterializedTargetLease,
) -> tuple[ManagedControlRollbackSwap, ...]:
    """Make both sides of our retained control pair visible for unregister."""

    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(receipt.admin_lease)
    swaps: list[ManagedControlRollbackSwap] = []
    try:
        marker_swap = stage_retained_control_file_for_rollback(
            directory_descriptor=lease.target_descriptor,
            entry_name=".git",
            display_path=receipt.git_file_binding.path,
            original_descriptor=receipt.git_file_descriptor,
            original_binding=receipt.git_file_binding,
            original_content=receipt.git_file_content,
            purpose="managed worktree control file",
        )
        if marker_swap is not None:
            swaps.append(marker_swap)
        backlink_swap = stage_retained_control_file_for_rollback(
            directory_descriptor=receipt.admin_lease.descriptor,
            entry_name="gitdir",
            display_path=receipt.admin_gitdir_binding.path,
            original_descriptor=receipt.admin_gitdir_descriptor,
            original_binding=receipt.admin_gitdir_binding,
            original_content=receipt.admin_gitdir_content,
            purpose="managed worktree admin backlink",
        )
        if backlink_swap is not None:
            swaps.append(backlink_swap)
        revalidate_materialized_target_lease(lease)
        revalidate_directory_entry_lease(receipt.admin_lease)
        return tuple(swaps)
    except BaseException as exc:
        restoration_errors: list[str] = []
        for swap in reversed(swaps):
            try:
                restore_control_file_after_failed_rollback(swap)
            except BaseException as restore_exc:
                restoration_errors.append(str(restore_exc))
            try:
                swap.close()
            except BaseException as close_exc:
                restoration_errors.append(str(close_exc))
        if restoration_errors:
            raise PlanError(
                f"{exc}\n"
                "managed control rollback staging restoration failed: "
                + "; ".join(restoration_errors)
            ) from exc
        raise


def verify_removed_managed_admin_entry(
    source_git_dir: Path,
    source_lease: DirectoryEntryLease,
    control: ManagedControlReceipt,
) -> None:
    """Prove the exact receipt-bound admin name vanished from its held parent."""

    location = control.admin_git_dir
    revalidate_source_registry_lease(source_git_dir, source_lease)
    if control.admin_lease.parent_binding.path != source_lease.path / "worktrees":
        raise WorktreeRegistrationRecoveryError(
            status="admin-parent-mismatch",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the receipt-bound admin parent is not the selected source "
            "worktrees directory",
        )
    parent_fingerprint = fingerprint_from_stat(
        os.fstat(control.admin_lease.parent_descriptor)
    )
    if parent_fingerprint != control.admin_lease.parent_binding.fingerprint:
        raise WorktreeRegistrationRecoveryError(
            status="admin-parent-changed",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the held administration parent object or access policy changed",
        )
    if not probe_access_at(
        control.admin_lease.parent_descriptor,
        ".",
        control.admin_lease.parent_binding.mode,
    ):
        raise WorktreeRegistrationRecoveryError(
            status="admin-parent-access-denied",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the held administration parent no longer permits the access "
            "required by its receipt",
        )
    admin_fingerprint = fingerprint_from_stat(os.fstat(control.admin_lease.descriptor))
    if admin_fingerprint != control.admin_lease.binding.fingerprint:
        raise WorktreeRegistrationRecoveryError(
            status="removed-admin-object-changed",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the held administration object or access policy changed during "
            "rollback",
        )
    try:
        observed_fingerprint = fingerprint_from_stat(
            os.stat(
                control.admin_lease.entry_name,
                dir_fd=control.admin_lease.parent_descriptor,
                follow_symlinks=False,
            )
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorktreeRegistrationRecoveryError(
            status="admin-entry-removal-unreadable",
            location=location,
            preserved_scope="transaction-created target parents",
            detail=str(exc),
        ) from exc
    else:
        raise WorktreeRegistrationRecoveryError(
            status="admin-entry-still-present",
            location=location,
            preserved_scope="transaction-created target parents",
            detail=(
                "the exact receipt-bound administration name resolves after "
                "worktree remove: "
                f"dev={observed_fingerprint.device},"
                f"ino={observed_fingerprint.inode},"
                f"kind={observed_fingerprint.kind},"
                f"uid={observed_fingerprint.owner},"
                f"gid={observed_fingerprint.group},"
                f"mode={observed_fingerprint.permissions:04o}"
            ),
        )
    final_admin_fingerprint = fingerprint_from_stat(
        os.fstat(control.admin_lease.descriptor)
    )
    if final_admin_fingerprint != control.admin_lease.binding.fingerprint:
        raise WorktreeRegistrationRecoveryError(
            status="removed-admin-object-changed",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the detached administration object or access policy changed "
            "during removal verification",
        )
    final_parent_fingerprint = fingerprint_from_stat(
        os.fstat(control.admin_lease.parent_descriptor)
    )
    if final_parent_fingerprint != control.admin_lease.parent_binding.fingerprint:
        raise WorktreeRegistrationRecoveryError(
            status="admin-parent-changed",
            location=location,
            preserved_scope="transaction-created target parents",
            detail="the detached administration parent object or access policy "
            "changed during removal verification",
        )
    revalidate_source_registry_lease(source_git_dir, source_lease)


def rollback_added_worktree(
    source_git_dir: Path,
    lease: MaterializedTargetLease,
    source_lease: DirectoryEntryLease,
    control: Optional[ManagedControlReceipt] = None,
) -> None:
    """Remove one just-registered worktree through its held parent object."""

    validate_descriptor_entry_name(lease.entry_name)
    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(source_lease)
    rollback_swaps = (
        stage_managed_control_files_for_added_worktree_rollback(
            control,
            lease,
        )
        if control is not None
        else ()
    )
    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(source_lease)
    if control is not None:
        revalidate_directory_entry_lease(control.admin_lease)
    target_exec_lease = materialized_target_exec_identity_lease(lease)
    try:
        run_git_at_directory_descriptor(
            [
                "git",
                f"--git-dir={source_git_dir}",
                "worktree",
                "remove",
                "--force",
                "--",
                lease.entry_name,
            ],
            lease.parent_descriptor,
            directory_identity_leases=(
                (source_lease, control.admin_lease, target_exec_lease)
                if control is not None
                else (source_lease, target_exec_lease)
            ),
        )
    except BaseException as exc:
        restoration_errors: list[str] = []
        if rollback_swaps:
            try:
                revalidate_materialized_target_lease(lease)
                if control is not None:
                    revalidate_directory_entry_lease(control.admin_lease)
            except BaseException as restore_preflight_exc:
                restoration_errors.append(str(restore_preflight_exc))
            for swap in reversed(rollback_swaps):
                try:
                    restore_control_file_after_failed_rollback(swap)
                except BaseException as restore_exc:
                    restoration_errors.append(str(restore_exc))
        for swap in rollback_swaps:
            try:
                swap.close()
            except BaseException as close_exc:
                restoration_errors.append(str(close_exc))
        if restoration_errors:
            raise PlanError(
                f"{exc}\n"
                "worktree control-file rollback restoration failed: "
                + "; ".join(restoration_errors)
            ) from exc
        raise
    close_errors: list[str] = []
    for swap in rollback_swaps:
        try:
            swap.close()
        except BaseException as close_exc:
            close_errors.append(str(close_exc))
    if close_errors:
        raise PlanError(
            "worktree control-file rollback receipt cleanup failed: "
            + "; ".join(close_errors)
        )
    if control is not None:
        verify_removed_managed_admin_entry(
            source_git_dir,
            source_lease,
            control,
        )
    revalidate_directory_descriptor(
        lease.parent_binding,
        lease.parent_descriptor,
    )
    revalidate_directory_entry_lease(source_lease)
    try:
        os.stat(
            lease.entry_name,
            dir_fd=lease.parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PlanError(
            "cannot verify rolled-back worktree target absence\n"
            f"  path: {lease.target}\n"
            f"  error: {exc}"
        ) from exc
    else:
        raise PlanError(
            f"rolled-back worktree target still exists\n  path: {lease.target}"
        )
    try:
        registered_path = registered_target_path(
            source_git_dir,
            lease.target,
            source_lease=source_lease,
        )
    except BaseException as registry_exc:
        raise PlanError(
            "rolled-back worktree registry state is uncertain; preserving "
            "transaction-created target parents for recovery\n"
            f"  source gitdir: {source_git_dir}\n"
            f"  target: {lease.target}\n"
            f"  registry error: {registry_exc}"
        ) from registry_exc
    if registered_path is not None:
        raise PlanError(
            "rolled-back worktree remains in the source registry\n"
            f"  path: {lease.target}"
        )


@signal_owned_operation
def add_worktree(
    source_git_dir: Path,
    worktree_path: Path,
    sha: str,
    dry_run: bool,
    *,
    lease: Optional[MaterializedTargetLease] = None,
    source_lease: Optional[DirectoryEntryLease] = None,
    checkout_view: Optional[CheckoutExecutionView] = None,
    finalize_checkout: Optional[
        Callable[[ManagedControlReceipt, DirectoryEntryLease], None]
    ] = None,
    pre_checkout: Optional[Callable[[], None]] = None,
    adopt_materialization: Optional[Callable[[], None]] = None,
) -> None:
    command = [
        "git",
        f"--git-dir={source_git_dir}",
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(worktree_path),
        sha,
    ]
    if dry_run:
        print(f"would add worktree: {shell_join(command)}")
        return
    if lease is None or lease.target != worktree_path:
        raise PlanError("new worktree creation requires a matching target lease")
    if source_lease is None or source_lease.path != source_git_dir.resolve(strict=True):
        raise PlanError("new worktree creation requires a matching source gitdir lease")
    if checkout_view is None:
        raise PlanError("new worktree creation requires an isolated execution view")
    control: Optional[ManagedControlReceipt] = None
    admin_inventory_before: Optional[WorktreeAdminInventory] = None
    admin_inventory_after: Optional[WorktreeAdminInventory] = None
    admin_ownership_proven = False
    registration_attempted = False
    rollback_delivery_deferred = False
    try:
        revalidate_materialized_target_lease(lease)
        revalidate_directory_entry_lease(source_lease)
        admin_inventory_before = capture_worktree_admin_inventory(
            source_git_dir,
            source_lease,
        )
        if adopt_materialization is not None:
            adopt_materialization()
        # Run from the held target directory and name that exact object as ".".
        # Passing the final pathname from its parent would let Git follow a
        # same-UID symlink replacement after our precheck and write outside the
        # planned target before postvalidation could detect the race.
        registration_attempted = True
        run_git_at_directory_descriptor(
            [
                "git",
                f"--git-dir={source_git_dir}",
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                ".",
                sha,
            ],
            lease.target_descriptor,
            extra_env={"GIT_COMMON_DIR": str(source_git_dir)},
            directory_identity_leases=(source_lease,),
        )
        signal_checkpoint("add-registration-git-complete")
        revalidate_materialized_target_lease(lease)
        revalidate_directory_entry_lease(source_lease)
        admin_inventory_after = capture_worktree_admin_inventory(
            source_git_dir,
            source_lease,
        )
        control = capture_managed_control_receipt(
            worktree_path,
            source_git_dir,
            lease.target_descriptor,
        )
        revalidate_managed_control_receipt(control, lease.target_descriptor)
        validate_expected_worktree_admin_add(
            source_git_dir,
            admin_inventory_before,
            admin_inventory_after,
            control,
        )
        admin_ownership_proven = True
        signal_checkpoint("add-admin-ownership-complete")
        # `worktree add --no-checkout` must leave the administration index
        # absent. Bind that one-way precondition before the potentially long
        # caller preflight and carry it into the child's final exec gate. Git
        # is expected to create the index after exec; an earlier appearance is
        # a real control-input replacement, not benign directory-entry churn.
        admin_index_absence = capture_directory_absent_entry_lease(
            control.admin_lease,
            ("index",),
            "new worktree pre-checkout admin index",
        )
        if pre_checkout is not None:
            pre_checkout()
        revalidate_managed_control_receipt(control, lease.target_descriptor)
        revalidate_directory_entry_lease(source_lease)
        revalidate_materialized_target_lease(lease)
        environment = checkout_execution_environment(checkout_view, sha)
        run_git_at_directory_descriptor(
            [
                "git",
                f"--git-dir={control.admin_git_dir}",
                "--work-tree=.",
                "checkout",
                "--no-overwrite-ignore",
                "--no-recurse-submodules",
                "--detach",
                sha,
            ],
            lease.target_descriptor,
            extra_env=environment,
            directory_identity_leases=(
                source_lease,
                control.admin_lease,
                *checkout_view.directory_leases,
            ),
            directory_exact_inventory_leases=checkout_view.exact_inventory_leases,
            directory_absent_entry_leases=(
                *checkout_view.absent_entry_leases,
                admin_index_absence,
            ),
            file_content_leases=checkout_view.file_leases,
            digest_file_leases=checkout_view.digest_file_leases,
        )
        signal_checkpoint("add-checkout-git-complete")
        revalidate_managed_control_receipt(control, lease.target_descriptor)
        revalidate_directory_entry_lease(source_lease)
        revalidate_materialized_target_lease(lease)
        if finalize_checkout is not None:
            finalize_checkout(control, source_lease)
    except BaseException as exc:
        rollback_delivery_deferred = defer_managed_signal_delivery()
        signal_checkpoint("add-rollback-enter")
        rollback_error: Optional[BaseException] = None
        should_rollback = False
        registry_known_clean = not registration_attempted

        if registration_attempted and admin_inventory_before is None:
            rollback_error = WorktreeRegistrationRecoveryError(
                status="missing-pre-add-inventory",
                location=source_git_dir / "worktrees",
                preserved_scope="the target",
                detail="the add attempt started without a retained raw "
                "administration baseline",
            )

        if (
            registration_attempted
            and control is not None
            and not admin_ownership_proven
            and rollback_error is None
        ):
            rollback_error = WorktreeRegistrationRecoveryError(
                status="admin-ownership-unproven",
                location=source_git_dir / "worktrees",
                preserved_scope="the target",
                detail="the managed control receipt was captured, but the raw "
                f"expected-add ownership proof did not complete: {exc}",
            )

        if (
            registration_attempted
            and admin_inventory_after is None
            and rollback_error is None
        ):
            try:
                admin_inventory_after = capture_worktree_admin_inventory(
                    source_git_dir,
                    source_lease,
                )
            except BaseException as inventory_exc:
                rollback_error = WorktreeRegistrationRecoveryError(
                    status="post-add-inventory-unreadable",
                    location=source_git_dir / "worktrees",
                    preserved_scope="the target",
                    detail=str(inventory_exc),
                )

        registered_path: Optional[Path] = None
        if registration_attempted and control is None and rollback_error is None:
            try:
                registered_path = registered_target_path(
                    source_git_dir,
                    worktree_path,
                    source_lease=source_lease,
                )
            except BaseException as registry_exc:
                rollback_error = WorktreeRegistrationRecoveryError(
                    status="registry-query-unreadable",
                    location=source_git_dir / "worktrees",
                    preserved_scope="the target",
                    detail=str(registry_exc),
                )

        if registered_path is not None and control is None and rollback_error is None:
            try:
                control = capture_managed_control_receipt(
                    worktree_path,
                    source_git_dir,
                    lease.target_descriptor,
                )
                revalidate_managed_control_receipt(
                    control,
                    lease.target_descriptor,
                )
                if admin_inventory_before is None or admin_inventory_after is None:
                    raise PlanError(
                        "recovered managed control receipt lacks complete raw "
                        "administration inventories"
                    )
                validate_expected_worktree_admin_add(
                    source_git_dir,
                    admin_inventory_before,
                    admin_inventory_after,
                    control,
                )
                admin_ownership_proven = True
            except BaseException as control_exc:
                drift = (
                    worktree_admin_inventory_drift(
                        source_git_dir,
                        admin_inventory_before,
                        admin_inventory_after,
                    )
                    if (
                        admin_inventory_before is not None
                        and admin_inventory_after is not None
                    )
                    else None
                )
                status, location, drift_detail = drift or (
                    "managed-control-unreadable",
                    source_git_dir / "worktrees",
                    "the target is registered but its descriptor-bound control "
                    "pair cannot be captured",
                )
                rollback_error = WorktreeRegistrationRecoveryError(
                    status=status,
                    location=location,
                    preserved_scope="the target",
                    detail=f"{drift_detail}; control error: {control_exc}",
                )

        if registration_attempted and rollback_error is None:
            if control is not None and admin_ownership_proven:
                should_rollback = True
            elif control is not None:
                rollback_error = WorktreeRegistrationRecoveryError(
                    status="admin-ownership-unproven",
                    location=source_git_dir / "worktrees",
                    preserved_scope="the target",
                    detail="the managed control receipt is not bound to the raw "
                    "expected-add inventory transition",
                )
            else:
                if admin_inventory_before is None or admin_inventory_after is None:
                    rollback_error = WorktreeRegistrationRecoveryError(
                        status="incomplete-admin-inventory",
                        location=source_git_dir / "worktrees",
                        preserved_scope="the target",
                        detail="the add attempt does not have complete before and "
                        "after raw administration inventories",
                    )
                else:
                    try:
                        require_unchanged_worktree_admin_inventory(
                            source_git_dir,
                            admin_inventory_before,
                            admin_inventory_after,
                            preserved_scope="the target",
                        )
                        registry_known_clean = True
                    except BaseException as inventory_exc:
                        rollback_error = inventory_exc

        if should_rollback and rollback_error is None:
            if control is None or admin_inventory_before is None:
                rollback_error = WorktreeRegistrationRecoveryError(
                    status="rollback-receipt-incomplete",
                    location=source_git_dir / "worktrees",
                    preserved_scope="the target",
                    detail="the registered worktree does not have both its managed "
                    "control receipt and raw administration baseline",
                )
            else:
                try:
                    rollback_added_worktree(
                        source_git_dir,
                        lease,
                        source_lease,
                        control,
                    )
                    final_admin_inventory = capture_worktree_admin_inventory(
                        source_git_dir,
                        source_lease,
                    )
                    require_unchanged_worktree_admin_inventory(
                        source_git_dir,
                        admin_inventory_before,
                        final_admin_inventory,
                        preserved_scope="transaction-created target parents",
                    )
                    registry_known_clean = True
                except BaseException as cleanup_exc:
                    location = (
                        cleanup_exc.location
                        if isinstance(
                            cleanup_exc,
                            WorktreeRegistrationRecoveryError,
                        )
                        else control.admin_git_dir
                    )
                    status = (
                        cleanup_exc.status
                        if isinstance(
                            cleanup_exc,
                            WorktreeRegistrationRecoveryError,
                        )
                        else "rollback-incomplete"
                    )
                    rollback_error = WorktreeRegistrationRecoveryError(
                        status=status,
                        location=location,
                        preserved_scope="the target or transaction-created target "
                        "parents",
                        detail=str(cleanup_exc),
                    )
                    registry_known_clean = False
        if (
            registry_known_clean
            and lease.materialization_target is not None
            and lease.created_nodes
        ):
            try:
                cleanup_materialized_target_nodes(
                    lease.materialization_target,
                    lease.created_nodes,
                )
            except BaseException as cleanup_exc:
                rollback_error = cleanup_exc

        control_cleanup_error: Optional[BaseException] = None
        if control is not None:
            try:
                control.close()
            except BaseException as cleanup_exc:
                control_cleanup_error = cleanup_exc
            control = None

        cleanup_details = []
        if control_cleanup_error is not None:
            cleanup_details.append(
                f"control receipt cleanup failed: {control_cleanup_error}"
            )
        if rollback_error is not None:
            cleanup_details.append(
                f"worktree/materialization rollback failed: {rollback_error}"
            )
        try:
            resume_managed_signal_delivery(rollback_delivery_deferred)
            rollback_delivery_deferred = False
            signal_checkpoint("add-rollback-complete")
        except ForwardedProcessSignal as signal_exc:
            exc = signal_exc
        except BaseException as signal_cleanup_exc:
            cleanup_details.append(
                f"managed signal rollback release failed: {signal_cleanup_exc}"
            )
        if isinstance(exc, ForwardedProcessSignal):
            exc.add_recovery_detail(
                json.dumps(
                    {
                        "operation": "worktree-add",
                        "profile": "worktree-signal-recovery-v1",
                        "registration_attempted": registration_attempted,
                        "registry_known_clean": registry_known_clean,
                        "rollback_status": (
                            "complete"
                            if not cleanup_details
                            else "incomplete-preserved"
                        ),
                        "source_git_dir": str(source_git_dir),
                        "target": str(worktree_path),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            for cleanup_detail in cleanup_details:
                exc.add_cleanup_error(cleanup_detail)
            raise exc
        if cleanup_details:
            raise PlanError(f"{exc}\n" + "\n".join(cleanup_details)) from exc
        raise
    else:
        published_control = control
        control = None
        if published_control is None:
            raise PlanError("published worktree lacks its managed control receipt")
        outcome_exception: Optional[BaseException] = None
        try:
            signal_checkpoint("add-final-validation-complete")
            signal_checkpoint("add-receipt-published")
        except BaseException as exc:
            outcome_exception = exc
            if isinstance(exc, ForwardedProcessSignal):
                exc.add_recovery_detail(
                    json.dumps(
                        {
                            "operation": "worktree-add",
                            "profile": "worktree-signal-recovery-v1",
                            "recovery_status": "completed-checkout-published",
                            "source_git_dir": str(source_git_dir),
                            "target": str(worktree_path),
                            "target_commit": sha,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            raise
        finally:
            finish_explicit_cleanup(
                published_control.close,
                outcome_exception=outcome_exception,
                purpose="managed control receipt",
                recovery_identity=str(published_control.admin_git_dir),
            )
    finally:
        if rollback_delivery_deferred:
            resume_managed_signal_delivery(True)
        if control is not None:
            control.close()


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


def planned_source_gitdir_binding(entry: PlannedWorktree) -> AccessBinding:
    candidates = tuple(
        binding
        for binding in entry.source_bindings
        if binding.path == entry.source_git_dir
        and binding.purpose == "source gitdir administration and registry writes"
    )
    if len(candidates) != 1:
        raise PlanError(
            "preflight source access bindings do not name exactly one source gitdir"
        )
    binding = candidates[0]
    completeness_binding = entry.source_completeness.gitdir_binding
    if (
        completeness_binding.path != entry.source_git_dir
        or completeness_binding.fingerprint != binding.fingerprint
    ):
        raise PlanError(
            "preflight source access and completeness receipts disagree on the "
            "source gitdir"
        )
    return binding


def revalidate_planned_source_lease(
    entry: PlannedWorktree,
    lease: DirectoryEntryLease,
) -> None:
    expected = planned_source_gitdir_binding(entry)
    if lease.binding != expected:
        raise PlanError(
            "fresh source gitdir lease does not match the preflight source "
            "access binding"
        )
    if (
        lease.binding.fingerprint
        != entry.source_completeness.gitdir_binding.fingerprint
    ):
        raise PlanError(
            "fresh source gitdir lease does not match the preflight source "
            "completeness binding"
        )
    revalidate_directory_entry_lease(lease)
    revalidate_access(expected)
    revalidate_source_completeness_receipt(
        entry.source_git_dir,
        entry.source_completeness,
    )
    if (
        lease.binding.fingerprint != expected.fingerprint
        or lease.binding.fingerprint
        != entry.source_completeness.gitdir_binding.fingerprint
    ):
        raise PlanError(
            "fresh source gitdir lease diverged from the preflight source receipts"
        )
    revalidate_directory_entry_lease(lease)


def capture_planned_source_lease(
    entry: PlannedWorktree,
) -> DirectoryEntryLease:
    expected = planned_source_gitdir_binding(entry)
    lease = capture_directory_entry_lease(
        entry.source_git_dir,
        expected.mode,
        expected.purpose,
    )
    try:
        revalidate_planned_source_lease(entry, lease)
        return lease
    except BaseException:
        lease.close()
        raise


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


def decode_index_v4_strip_count(
    content: bytes,
    offset: int,
    body_end: int,
    *,
    purpose: str = "captured managed worktree index",
) -> tuple[int, int]:
    if offset >= body_end:
        raise PlanError(f"{purpose} has a truncated v4 path")
    byte = content[offset]
    offset += 1
    value = byte & 0x7F
    while byte & 0x80:
        if offset >= body_end:
            raise PlanError(f"{purpose} has a truncated v4 path offset")
        byte = content[offset]
        offset += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
        if value > MAX_CHECKOUT_PATH_BYTES:
            raise PlanError(f"{purpose} has an oversized v4 path offset")
    return value, offset


def validate_captured_cache_tree(
    content: bytes,
    extension_start: int,
    extension_end: int,
    object_id_bytes: int,
    expected_nodes: dict[bytes, tuple[int, bytes]],
) -> None:
    """Require every cache-tree node to describe the planned target tree."""

    offset = extension_start
    seen_paths: set[bytes] = set()
    valid_paths: set[bytes] = set()
    # Each frame is [full_path, remaining_children, prior_child_component].
    frames: list[list[object]] = []
    node_index = 0
    while offset < extension_end:
        if node_index >= MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "captured managed worktree cache-tree exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-node safety limit"
            )
        component_end = content.find(b"\0", offset, extension_end)
        if component_end < 0:
            raise PlanError(
                "captured managed worktree cache-tree has an unterminated path"
            )
        if component_end - offset > MAX_CHECKOUT_PATH_BYTES:
            raise PlanError(
                "captured managed worktree cache-tree has an oversized path component"
            )
        component = content[offset:component_end]
        line_end = content.find(b"\n", component_end + 1, extension_end)
        if line_end < 0:
            raise PlanError(
                "captured managed worktree cache-tree has a truncated node header"
            )
        header_size = line_end - component_end
        if header_size > 64:
            raise PlanError(
                "captured managed worktree cache-tree has an oversized node header"
            )
        header = content[component_end + 1 : line_end + 1]
        match = re.fullmatch(rb"(-1|0|[1-9][0-9]*) (0|[1-9][0-9]*)\n", header)
        if match is None:
            raise PlanError(
                "captured managed worktree cache-tree has an invalid node header"
            )
        entry_count = int(match.group(1))
        subtree_count = int(match.group(2))
        if subtree_count > MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "captured managed worktree cache-tree has too many subtrees"
            )
        offset = line_end + 1

        if node_index == 0:
            if component:
                raise PlanError("captured managed worktree cache-tree has no root node")
            full_path = b""
        else:
            while frames and frames[-1][1] == 0:
                frames.pop()
            if not frames:
                raise PlanError(
                    "captured managed worktree cache-tree has an extra node"
                )
            if (
                not component
                or b"/" in component
                or component in {b".", b"..", b".git"}
            ):
                raise PlanError(
                    "captured managed worktree cache-tree has an invalid path component"
                )
            parent_path = frames[-1][0]
            remaining_children = frames[-1][1]
            prior_component = frames[-1][2]
            if not isinstance(parent_path, bytes) or not isinstance(
                remaining_children, int
            ):
                raise PlanError("captured managed worktree cache-tree stack is invalid")
            # Git cache-tree.c uses subtree_name_cmp/write_one order: component
            # length first, then raw name bytes. This is intentionally distinct
            # from Git tree-entry ordering.
            if prior_component is not None and (
                not isinstance(prior_component, bytes)
                or (len(component), component)
                <= (len(prior_component), prior_component)
            ):
                raise PlanError(
                    "captured managed worktree cache-tree children are not "
                    "in canonical order\n"
                    f"  prior: {prior_component!r}\n"
                    f"  current: {component!r}"
                )
            frames[-1][1] = remaining_children - 1
            frames[-1][2] = component
            separator_bytes = 1 if parent_path else 0
            if (
                len(parent_path) + separator_bytes + len(component)
                > MAX_CHECKOUT_PATH_BYTES
            ):
                raise PlanError(
                    "captured managed worktree cache-tree path exceeds the "
                    f"{MAX_CHECKOUT_PATH_BYTES}-byte limit"
                )
            full_path = parent_path + b"/" + component if parent_path else component
            validate_checkout_path(
                full_path,
                "captured managed worktree cache-tree",
            )

        if full_path in seen_paths:
            raise PlanError(
                "captured managed worktree cache-tree contains a duplicate path"
            )
        expected = expected_nodes.get(full_path)
        if expected is None:
            raise PlanError(
                "captured managed worktree cache-tree names a path outside "
                "the planned target tree"
            )
        seen_paths.add(full_path)
        expected_entry_count, expected_object_id = expected
        if entry_count >= 0:
            if offset + object_id_bytes > extension_end:
                raise PlanError(
                    "captured managed worktree cache-tree has a truncated object id"
                )
            object_id = content[offset : offset + object_id_bytes]
            offset += object_id_bytes
            if entry_count != expected_entry_count or object_id != expected_object_id:
                raise PlanError(
                    "captured managed worktree cache-tree does not match "
                    "the planned target tree"
                )
            valid_paths.add(full_path)
        elif entry_count != -1:
            raise PlanError(
                "captured managed worktree cache-tree has an invalid entry count"
            )

        if subtree_count:
            frames.append([full_path, subtree_count, None])
        node_index += 1

    while frames and frames[-1][1] == 0:
        frames.pop()
    if frames:
        raise PlanError(
            "captured managed worktree cache-tree is missing declared subtrees"
        )
    if not seen_paths or len(seen_paths) != len(expected_nodes):
        raise PlanError(
            "captured managed worktree cache-tree directory set does not "
            "match the planned target tree"
        )
    if any(
        path and entry_count == 0 and path not in valid_paths
        for path, (entry_count, _object_id) in expected_nodes.items()
    ):
        raise PlanError(
            "captured managed worktree cache-tree does not preserve a non-root "
            "empty target tree"
        )


def captured_index_entries(
    content: bytes,
    object_id_bytes: int,
    *,
    expected_cache_tree: Optional[dict[bytes, tuple[int, bytes]]] = None,
    require_cache_tree: bool = False,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    """Parse immutable index bytes and reject conflicts or hidden entry state."""

    if object_id_bytes not in {20, 32}:
        raise PlanError("captured managed worktree index uses an unsupported hash")
    if len(content) < 12 + object_id_bytes:
        raise PlanError("captured managed worktree index is truncated")
    body_end = len(content) - object_id_bytes
    body = memoryview(content)[:body_end]
    expected_checksum = content[body_end:]
    if expected_checksum == bytes(object_id_bytes):
        raise PlanError(
            "captured managed worktree index uses an unsupported skip-hash checksum"
        )
    digest = hashlib.sha1() if object_id_bytes == 20 else hashlib.sha256()
    digest.update(body)
    if digest.digest() != expected_checksum:
        raise PlanError("captured managed worktree index checksum is invalid")
    if content[:4] != b"DIRC":
        raise PlanError("captured managed worktree index has an invalid signature")
    version = int.from_bytes(content[4:8], "big")
    if version not in {2, 3, 4}:
        raise PlanError(
            f"captured managed worktree index uses an unsupported version: {version}"
        )
    entry_count = int.from_bytes(content[8:12], "big")
    if entry_count > MAX_CHECKOUT_PATHS:
        raise PlanError(
            "captured managed worktree index exceeds the "
            f"{MAX_CHECKOUT_PATHS}-entry safety limit"
        )

    offset = 12
    prior_path: Optional[bytes] = None
    expanded_path_bytes = 0
    entries: list[tuple[bytes, bytes, bytes]] = []
    empty_path_seen = False
    fixed_prefix_bytes = 40 + object_id_bytes + 2
    for _entry_index in range(entry_count):
        entry_start = offset
        if offset + fixed_prefix_bytes > body_end:
            raise PlanError("captured managed worktree index has a truncated entry")
        mode_value = int.from_bytes(content[offset + 24 : offset + 28], "big")
        object_start = offset + 40
        object_end = object_start + object_id_bytes
        object_id = content[object_start:object_end].hex().encode("ascii")
        flags = int.from_bytes(content[object_end : object_end + 2], "big")
        offset = object_end + 2
        stage = (flags >> 12) & 0x3
        if stage != 0:
            raise PlanError(
                "captured managed worktree index has unresolved conflict entries"
            )
        if flags & 0x8000:
            raise PlanError(
                "captured managed worktree index has hidden assume-valid state"
            )
        extended = bool(flags & 0x4000)
        if extended:
            if version == 2 or offset + 2 > body_end:
                raise PlanError(
                    "captured managed worktree index has invalid extended flags"
                )
            extended_flags = int.from_bytes(content[offset : offset + 2], "big")
            offset += 2
            if extended_flags != 0:
                raise PlanError(
                    "captured managed worktree index has hidden skip-worktree, "
                    "intent-to-add, or fsmonitor state"
                )

        if version == 4:
            strip_count, offset = decode_index_v4_strip_count(
                content,
                offset,
                body_end,
            )
            retained_path = prior_path or b""
            if strip_count > len(retained_path):
                raise PlanError(
                    "captured managed worktree index has an invalid v4 path prefix"
                )
            suffix_end = content.find(b"\0", offset, body_end)
            if suffix_end < 0:
                raise PlanError(
                    "captured managed worktree index has an unterminated v4 path"
                )
            retained_size = len(retained_path) - strip_count
            suffix_size = suffix_end - offset
            if retained_size + suffix_size > MAX_CHECKOUT_PATH_BYTES:
                raise PlanError(
                    "captured managed worktree index has an oversized v4 path"
                )
            suffix = content[offset:suffix_end]
            retained = retained_path[:retained_size] if strip_count else retained_path
            raw_path = retained + suffix
            offset = suffix_end + 1
        else:
            path_end = content.find(b"\0", offset, body_end)
            if path_end < 0:
                raise PlanError(
                    "captured managed worktree index has an unterminated path"
                )
            if path_end - offset > MAX_CHECKOUT_PATH_BYTES:
                raise PlanError("captured managed worktree index has an oversized path")
            raw_path = content[offset:path_end]
            relative_size = path_end + 1 - entry_start
            padded_size = (relative_size + 7) & ~7
            offset = entry_start + padded_size
            if offset > body_end or any(content[path_end + 1 : offset]):
                raise PlanError(
                    "captured managed worktree index has invalid entry padding"
                )

        declared_length = flags & 0x0FFF
        if declared_length != min(len(raw_path), 0x0FFF):
            raise PlanError(
                "captured managed worktree index path length does not match its flags"
            )
        if not raw_path:
            # Split-index replacement entries may deliberately omit their path
            # and recover it from the shared index named by the later `link`
            # extension. Defer the generic empty-path failure so that hidden
            # split-index state is classified explicitly.
            empty_path_seen = True
            continue
        validate_checkout_path(raw_path, "captured managed worktree index")
        expanded_path_bytes += len(raw_path) + 1
        if expanded_path_bytes > GIT_ENUMERATION_OUTPUT_LIMIT_BYTES:
            raise PlanError(
                "captured managed worktree index expanded paths exceed the "
                f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
            )
        if prior_path is not None and raw_path == prior_path:
            raise PlanError(
                "captured managed worktree index contains duplicate stage-0 paths"
            )
        if prior_path is not None and raw_path < prior_path:
            raise PlanError(
                "captured managed worktree index paths are not in canonical order"
            )
        prior_path = raw_path
        entries.append(
            (
                raw_path,
                f"{mode_value:o}".encode("ascii"),
                object_id,
            )
        )

    unsupported_extensions = {
        b"link": "split-index",
        b"REUC": "resolve-undo",
        b"sdir": "sparse-index",
        b"FSMN": "fsmonitor",
        b"UNTR": "untracked-cache",
        b"EOIE": "end-of-index-entry",
        b"IEOT": "index-entry-offset-table",
    }
    seen_extensions: set[bytes] = set()
    cache_tree_seen = False
    if empty_path_seen:
        extension_probe_offset = offset
        while extension_probe_offset + 8 <= body_end:
            signature = content[extension_probe_offset : extension_probe_offset + 4]
            extension_size = int.from_bytes(
                content[extension_probe_offset + 4 : extension_probe_offset + 8],
                "big",
            )
            extension_probe_offset += 8
            if extension_probe_offset + extension_size > body_end:
                break
            if signature == b"link":
                raise PlanError(
                    "captured managed worktree index has hidden split-index state"
                )
            if signature != b"TREE":
                break
            extension_probe_offset += extension_size
    while offset < body_end:
        if offset + 8 > body_end:
            raise PlanError("captured managed worktree index has a truncated extension")
        signature = content[offset : offset + 4]
        extension_size = int.from_bytes(content[offset + 4 : offset + 8], "big")
        offset += 8
        if offset + extension_size > body_end:
            raise PlanError(
                "captured managed worktree index has an oversized extension"
            )
        hidden_name = unsupported_extensions.get(signature)
        if hidden_name is not None:
            raise PlanError(
                f"captured managed worktree index has hidden {hidden_name} state"
            )
        if signature in seen_extensions:
            raise PlanError(
                "captured managed worktree index contains a duplicate "
                f"extension: {signature!r}"
            )
        seen_extensions.add(signature)
        if signature == b"TREE":
            if expected_cache_tree is None:
                raise PlanError(
                    "captured managed worktree index cache-tree cannot be "
                    "validated without the planned target tree"
                )
            if extension_size > GIT_ENUMERATION_OUTPUT_LIMIT_BYTES:
                raise PlanError(
                    "captured managed worktree cache-tree exceeds the "
                    f"{GIT_ENUMERATION_OUTPUT_LIMIT_BYTES}-byte aggregate limit"
                )
            validate_captured_cache_tree(
                content,
                offset,
                offset + extension_size,
                object_id_bytes,
                expected_cache_tree,
            )
            cache_tree_seen = True
            offset += extension_size
            continue
        if not signature or not 0x41 <= signature[0] <= 0x5A:
            raise PlanError(
                "captured managed worktree index has an unsupported mandatory "
                f"extension: {signature!r}"
            )
        raise PlanError(
            "captured managed worktree index has an unsupported optional "
            f"extension: {signature!r}"
        )
    if empty_path_seen:
        raise PlanError("captured managed worktree index contains an empty Git path")
    if require_cache_tree and not cache_tree_seen:
        raise PlanError(
            "captured managed worktree index requires a cache-tree extension "
            "to preserve a non-root empty target tree"
        )
    return tuple(entries)


def target_tree_index_semantics(
    source_git_dir: Path,
    target_sha: str,
) -> tuple[
    tuple[tuple[bytes, bytes, bytes], ...],
    dict[bytes, tuple[int, bytes]],
]:
    object_id_length = len(target_sha)
    if object_id_length not in {40, 64} or not re.fullmatch(
        r"[0-9a-f]+",
        target_sha,
    ):
        raise PlanError("planned target index tree has an invalid commit id")
    root_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "rev-parse",
            "--verify",
            f"{target_sha}^{{tree}}",
        ],
        stdout_limit=object_id_length + 2,
    )
    root_object_id = root_result.stdout.rstrip(b"\r\n")
    if len(root_object_id) != object_id_length or not re.fullmatch(
        rb"[0-9a-f]+", root_object_id
    ):
        raise PlanError("planned target index tree has an invalid root object id")
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            "-r",
            "-t",
            "-z",
            "--full-tree",
            target_sha,
        ]
    )
    entries: list[tuple[bytes, bytes, bytes]] = []
    tree_object_ids: dict[bytes, bytes] = {b"": bytes.fromhex(root_object_id.decode())}
    component_count = 0
    for record in bounded_records(
        result.stdout,
        "planned target index tree",
        maximum_records=MAX_CHECKOUT_PATH_COMPONENTS,
    ):
        try:
            header, raw_path = record.split(b"\t", 1)
        except ValueError as exc:
            raise PlanError("planned target index tree has an invalid record") from exc
        fields = header.split()
        if len(fields) != 3:
            raise PlanError("planned target index tree has an invalid header")
        mode, object_type, object_id = fields
        if (
            not re.fullmatch(rb"[0-9a-f]+", object_id)
            or len(object_id) != object_id_length
        ):
            raise PlanError("planned target index tree has an invalid object id")
        path_parts = validate_checkout_path(raw_path, "planned target index tree")
        component_count += len(path_parts)
        if component_count > MAX_CHECKOUT_PATH_COMPONENTS:
            raise PlanError(
                "planned target index tree exceeds the "
                f"{MAX_CHECKOUT_PATH_COMPONENTS}-component safety limit"
            )
        if mode == b"040000" and object_type == b"tree":
            if raw_path in tree_object_ids:
                raise PlanError(
                    "planned target index tree contains a duplicate directory"
                )
            tree_object_ids[raw_path] = bytes.fromhex(object_id.decode())
            continue
        if not (
            (mode in {b"100644", b"100755", b"120000"} and object_type == b"blob")
            or (mode == b"160000" and object_type == b"commit")
        ):
            raise PlanError(
                "planned target index tree contains an unsupported entry type"
            )
        if len(entries) >= MAX_CHECKOUT_PATHS:
            raise PlanError(
                "planned target index tree exceeds the "
                f"{MAX_CHECKOUT_PATHS}-entry safety limit"
            )
        if entries and raw_path == entries[-1][0]:
            raise PlanError("planned target index tree contains a duplicate path")
        if entries and raw_path < entries[-1][0]:
            raise PlanError(
                "planned target index tree paths are not in canonical order"
            )
        entries.append((raw_path, mode, object_id))

    tree_entry_counts = {path: 0 for path in tree_object_ids}
    for raw_path, _mode, _object_id in entries:
        tree_entry_counts[b""] += 1
        parts = raw_path.split(b"/")
        prefix = b""
        for component in parts[:-1]:
            prefix = prefix + b"/" + component if prefix else component
            if prefix not in tree_entry_counts:
                raise PlanError(
                    "planned target index tree is missing a parent directory"
                )
            tree_entry_counts[prefix] += 1
    cache_nodes = {
        path: (tree_entry_counts[path], object_id)
        for path, object_id in tree_object_ids.items()
    }
    return tuple(entries), cache_nodes


def target_tree_index_entries(
    source_git_dir: Path,
    target_sha: str,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    entries, _cache_nodes = target_tree_index_semantics(source_git_dir, target_sha)
    return entries


def validate_captured_index_matches_tree(
    entry: PlannedWorktree,
    index_content: bytes,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    return validate_captured_index_matches_commit(
        entry.source_git_dir,
        entry.sha,
        entry.target.path,
        index_content,
    )


def validate_captured_index_matches_commit(
    source_git_dir: Path,
    target_sha: str,
    worktree_path: Path,
    index_content: bytes,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    object_id_bytes = len(target_sha) // 2
    expected, expected_cache_tree = target_tree_index_semantics(
        source_git_dir,
        target_sha,
    )
    captured = captured_index_entries(
        index_content,
        object_id_bytes,
        expected_cache_tree=expected_cache_tree,
        require_cache_tree=any(
            path and entry_count == 0
            for path, (entry_count, _object_id) in expected_cache_tree.items()
        ),
    )
    if captured != expected:
        raise PlanError(
            "captured managed worktree index stage-0 mode/OID/path set does "
            "not match the planned target tree\n"
            f"  worktree: {worktree_path}\n"
            f"  target: {target_sha}"
        )
    return captured


def managed_head_from_control(
    worktree_path: Path,
    target_descriptor: int,
    control: ManagedControlReceipt,
    index_lease: DescriptorBoundFileLease,
) -> str:
    revalidate_managed_control_receipt(control, target_descriptor)
    revalidate_descriptor_bound_file_lease(index_lease)
    result = read_git_bounded(
        [
            f"--git-dir={control.admin_git_dir}",
            "--work-tree=.",
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ],
        stdout_limit=256,
        directory_descriptor=target_descriptor,
        directory_identity_leases=(control.admin_lease,),
        file_content_leases=(index_lease,),
    )
    revalidate_descriptor_bound_file_lease(index_lease)
    revalidate_managed_control_receipt(control, target_descriptor)
    value = os.fsdecode(result.stdout).strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise PlanError(f"managed worktree returned an invalid HEAD: {worktree_path}")
    return value


def managed_index_semantic_digest(
    entries: tuple[tuple[bytes, bytes, bytes], ...],
) -> str:
    digest = hashlib.sha256()
    for raw_path, mode, object_id in entries:
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(mode)
        digest.update(b"\0")
        digest.update(object_id)
        digest.update(b"\0")
    return digest.hexdigest()


def capture_managed_preflight_receipt(
    worktree_path: Path,
    source_git_dir: Path,
) -> ManagedPreflightReceipt:
    """Bind and parse the original raw index before any status/read-tree probe."""

    target_descriptor = open_directory_descriptor(
        worktree_path,
        "managed worktree preflight target",
    )
    control: Optional[ManagedControlReceipt] = None
    index_descriptor = -1
    try:
        control = capture_managed_control_receipt(
            worktree_path,
            source_git_dir,
            target_descriptor,
        )
        (
            index_descriptor,
            index_binding,
            index_content,
        ) = open_bound_regular_file_at(
            control.admin_lease.descriptor,
            "index",
            control.admin_git_dir / "index",
            maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
            mode=os.R_OK,
            purpose="managed worktree preflight index",
            retain_content=True,
        )
        if index_content is None:
            raise PlanError("managed worktree preflight index returned no content")
        index_lease = DescriptorBoundFileLease(
            directory_binding=control.admin_lease.binding,
            directory_descriptor=control.admin_lease.descriptor,
            entry_name="index",
            descriptor=index_descriptor,
            binding=index_binding,
            content=index_content,
        )
        current_head = managed_head_from_control(
            worktree_path,
            target_descriptor,
            control,
            index_lease,
        )
        # Protected property: the raw index's supported semantic state,
        # object identity, bytes, and read policy are fixed before a status
        # process exists. Stat-cache timestamps are intentionally ignored.
        entries = validate_captured_index_matches_commit(
            source_git_dir,
            current_head,
            worktree_path,
            index_content,
        )
        index_blob_paths = tuple(
            validate_checkout_path(raw_path, "managed worktree preflight index")
            for raw_path, mode, _object_id in entries
            if mode.startswith(b"100")
        )
        revalidate_descriptor_bound_file_lease(index_lease)
        revalidate_managed_control_receipt(control, target_descriptor)
        receipt = ManagedPreflightReceipt(
            target_descriptor=target_descriptor,
            control=control,
            index_lease=index_lease,
            current_head=current_head,
            index_digest=managed_index_semantic_digest(entries),
            index_entry_count=len(entries),
            index_blob_paths=index_blob_paths,
        )
        target_descriptor = -1
        control = None
        index_descriptor = -1
        return receipt
    finally:
        if index_descriptor >= 0:
            os.close(index_descriptor)
        if control is not None:
            control.close()
        if target_descriptor >= 0:
            os.close(target_descriptor)


def revalidate_managed_preflight_receipt(
    receipt: ManagedPreflightReceipt,
) -> None:
    if not receipt.active:
        raise PlanError("managed preflight receipt is inactive")
    revalidate_managed_control_receipt(
        receipt.control,
        receipt.target_descriptor,
    )
    revalidate_descriptor_bound_file_lease(receipt.index_lease)
    current_head = managed_head_from_control(
        receipt.control.git_file_binding.path.parent,
        receipt.target_descriptor,
        receipt.control,
        receipt.index_lease,
    )
    if current_head != receipt.current_head:
        raise PlanError(
            "managed worktree HEAD changed during preflight\n"
            f"  worktree: {receipt.control.git_file_binding.path.parent}"
        )
    revalidate_descriptor_bound_file_lease(receipt.index_lease)
    revalidate_managed_control_receipt(
        receipt.control,
        receipt.target_descriptor,
    )


def managed_head(worktree_path: Path) -> str:
    result = read_git_bounded(
        ["-C", str(worktree_path), "rev-parse", "--verify", "HEAD^{commit}"],
        stdout_limit=256,
    )
    value = os.fsdecode(result.stdout).strip()
    if not value or any(character not in "0123456789abcdef" for character in value):
        raise PlanError(f"managed worktree returned an invalid HEAD: {worktree_path}")
    return value


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
    checkout_view: CheckoutExecutionView,
    target_sha: str,
) -> tuple[tuple[str, ...], ...]:
    # Protected property: the queried path set belongs to the exact verified
    # tree bytes in the owner-private checkout object pack. Live-source
    # ls-tree output is not authoritative because an object-path ABA could
    # otherwise omit a filtered blob while preserving the repeated closure
    # receipt.
    result = read_checkout_execution_view_git(
        checkout_view,
        [
            *source_object_repo_args(checkout_view.common_git_dir),
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            target_sha,
        ],
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


def verify_target_object_payloads(
    source_git_dir: Path,
    ordered: list[str],
    expected_types: dict[str, str],
) -> tuple[ObjectClosureReceipt, tuple[int, ...]]:
    # `cat-file --batch-check` can read a corrupt loose object's header while
    # never decompressing its payload. Stream every required object instead,
    # independently recompute its object id, and discard payload bytes after
    # hashing so retained memory remains bounded.
    object_input = b"".join(object_id.encode("ascii") + b"\n" for object_id in ordered)
    if len(object_input) > GIT_INPUT_LIMIT_BYTES:
        raise PlanError(
            "target checkout object closure exceeds the "
            f"{GIT_INPUT_LIMIT_BYTES}-byte object-query input limit"
        )
    input_file = tempfile.TemporaryFile()
    try:
        input_file.write(object_input)
        input_file.seek(0)
        (
            command,
            executable_lease,
            helper_leases,
            selected_runtime,
        ) = prepare_command_execution(
            [
                "git",
                "--no-optional-locks",
                *source_object_repo_args(source_git_dir),
                "cat-file",
                "--batch",
            ]
        )
    except BaseException:
        input_file.close()
        raise

    def executable_gate() -> None:
        for helper_lease in helper_leases:
            revalidate_executable_execution_lease_in_child(helper_lease)
        if executable_lease is not None:
            revalidate_executable_execution_lease_in_child(executable_lease)

    inherited_descriptor_set: set[int] = set()
    for helper_lease in helper_leases:
        inherited_descriptor_set.add(helper_lease.parent_descriptor)
        inherited_descriptor_set.add(helper_lease.descriptor)
    if executable_lease is not None:
        inherited_descriptor_set.add(executable_lease.parent_descriptor)
        inherited_descriptor_set.add(executable_lease.descriptor)
    inherited_descriptors = tuple(sorted(inherited_descriptor_set))
    signal_lease = SignalOwnershipLease()
    try:
        signal_ownership = signal_lease.__enter__()
    except BaseException:
        close_executable_execution_leases(
            executable_lease,
            helper_leases,
        )
        input_file.close()
        raise
    signal_gate = signal_ownership.gate
    supervisor_signal_mask = signal_ownership.supervisor_mask

    def supervised_executable_gate() -> None:
        executable_gate()
        for signum in MANAGED_CHILD_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, supervisor_signal_mask)

    process: Optional[subprocess.Popen[bytes]] = None
    stdout_pipe: Optional[BinaryIO] = None
    stderr_pipe: Optional[BinaryIO] = None
    selector: Optional[selectors.BaseSelector] = None
    pipe_tracking_complete = False
    spawn_signals_blocked = False
    try:
        signal_checkpoint("object-payload-before-spawn")
        signal.pthread_sigmask(signal.SIG_BLOCK, MANAGED_CHILD_SIGNALS)
        spawn_signals_blocked = True
        process = subprocess.Popen(
            command,
            env=git_environment(
                runtime=selected_runtime,
                include_git_exec_path=bool(helper_leases),
            ),
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=inherited_descriptors,
            executable=(
                None
                if executable_lease is None
                else executable_subprocess_path(executable_lease)
            ),
            preexec_fn=supervised_executable_gate,
        )
        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            raise GitError("target object payload verification lacks capture pipes")
        selector = selectors.DefaultSelector()
        selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
        selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
        pipe_tracking_complete = True
        signal.pthread_sigmask(
            signal.SIG_SETMASK,
            supervisor_signal_mask,
        )
        spawn_signals_blocked = False
        signal_gate.raise_if_pending()
    except BaseException as exc:
        start_error = GitError(
            f"failed to start target object payload verification: {exc}"
        )
        cleanup_errors: list[str] = []
        if spawn_signals_blocked:
            try:
                signal.pthread_sigmask(
                    signal.SIG_SETMASK,
                    supervisor_signal_mask,
                )
            except BaseException as cleanup_exc:
                cleanup_errors.append(
                    "cannot restore the supervisor signal mask after object "
                    f"verification spawn: {cleanup_exc}"
                )
            spawn_signals_blocked = False
        if process is not None:
            if not pipe_tracking_complete:
                cleanup_errors.append(
                    "process cleanup-incomplete: target object verifier did not "
                    "establish both inherited-pipe receipts"
                )
            try:
                terminate_process_group(
                    process,
                    initial_signal=(
                        exc.signum
                        if isinstance(exc, ForwardedProcessSignal)
                        else signal.SIGTERM
                    ),
                    selector=selector,
                )
            except PlanError as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
        try:
            close_executable_execution_leases(
                executable_lease,
                helper_leases,
            )
        except BaseException as cleanup_exc:
            cleanup_errors.append(str(cleanup_exc))
        if selector is not None:
            selector.close()
        if stdout_pipe is not None:
            stdout_pipe.close()
        if stderr_pipe is not None:
            stderr_pipe.close()
        input_file.close()
        effective_error: BaseException = (
            exc if isinstance(exc, ForwardedProcessSignal) else start_error
        )
        if isinstance(effective_error, ForwardedProcessSignal):
            for cleanup_error in cleanup_errors:
                effective_error.add_cleanup_error(cleanup_error)
        elif cleanup_errors:
            effective_error = GitError(
                f"{start_error}; executable/process cleanup failed: "
                + "; ".join(cleanup_errors)
            )
        signal_lease.finish(effective_error)
        raise effective_error from (None if effective_error is exc else exc)

    if process is None or stdout_pipe is None or stderr_pipe is None:
        raise PlanError("object payload verifier did not retain its child process")
    stdout_buffer = bytearray()
    retained_stderr = bytearray()
    producer_stdout_bytes = 0
    maximum_producer_bytes = MAX_CHECKOUT_LOGICAL_BYTES + len(ordered) * 256 + 1
    deadline = time.monotonic() + GIT_ENUMERATION_TIMEOUT_SECONDS
    inventory_digest = hashlib.sha256()
    observed_sizes: list[int] = []
    logical_bytes = 0
    object_index = 0
    current_id: Optional[str] = None
    current_type: Optional[str] = None
    current_size = 0
    current_remaining: Optional[int] = None
    current_object_digest = None
    failure: Optional[str] = None
    returncode: Optional[int] = None
    main_lease_open = executable_lease is not None
    helper_leases_open = bool(helper_leases)
    outcome_exception: Optional[BaseException] = None

    def consume_stdout_buffer() -> None:
        nonlocal current_id
        nonlocal current_object_digest
        nonlocal current_remaining
        nonlocal current_size
        nonlocal current_type
        nonlocal failure
        nonlocal logical_bytes
        nonlocal object_index
        while failure is None:
            if current_remaining is None:
                if object_index >= len(ordered):
                    if stdout_buffer:
                        failure = (
                            "target checkout object payload stream returned "
                            "unexpected trailing bytes"
                        )
                    return
                header_end = stdout_buffer.find(b"\n")
                if header_end < 0:
                    if len(stdout_buffer) > 256:
                        failure = (
                            "target checkout object payload stream returned an "
                            "oversized header"
                        )
                    return
                header = bytes(stdout_buffer[:header_end])
                del stdout_buffer[: header_end + 1]
                fields = header.split()
                if len(fields) != 3:
                    failure = (
                        "target checkout object payload stream contains a "
                        "missing or malformed object"
                    )
                    return
                actual_id = os.fsdecode(fields[0])
                actual_type = os.fsdecode(fields[1])
                try:
                    object_size = int(fields[2])
                except ValueError:
                    failure = (
                        "target checkout object payload stream returned an "
                        "invalid object size"
                    )
                    return
                expected_id = ordered[object_index]
                expected_type = expected_types[expected_id]
                if actual_id != expected_id or actual_type != expected_type:
                    failure = (
                        "target checkout object payload stream changed object "
                        "identity or type"
                    )
                    return
                if object_size < 0:
                    failure = (
                        "target checkout object payload stream returned a "
                        "negative object size"
                    )
                    return
                logical_bytes += object_size
                if logical_bytes > MAX_CHECKOUT_LOGICAL_BYTES:
                    failure = (
                        "target checkout object closure exceeds the "
                        f"{MAX_CHECKOUT_LOGICAL_BYTES}-byte logical-size safety limit"
                    )
                    return
                current_id = actual_id
                current_type = actual_type
                current_size = object_size
                current_remaining = object_size
                current_object_digest = (
                    hashlib.sha1() if len(actual_id) == 40 else hashlib.sha256()
                )
                current_object_digest.update(
                    f"{actual_type} {object_size}\0".encode("ascii")
                )
            if current_remaining:
                if not stdout_buffer:
                    return
                consumed = min(current_remaining, len(stdout_buffer))
                current_object_digest.update(stdout_buffer[:consumed])
                del stdout_buffer[:consumed]
                current_remaining -= consumed
                if current_remaining:
                    return
            if not stdout_buffer:
                return
            if stdout_buffer[0] != 0x0A:
                failure = (
                    "target checkout object payload stream omitted its object separator"
                )
                return
            del stdout_buffer[0]
            if current_object_digest.hexdigest() != current_id:
                failure = (
                    "target checkout object payload bytes do not match their "
                    "declared object id"
                )
                return
            inventory_digest.update(current_id.encode("ascii"))
            inventory_digest.update(b"\0")
            inventory_digest.update(current_type.encode("ascii"))
            inventory_digest.update(b"\0")
            inventory_digest.update(str(current_size).encode("ascii"))
            inventory_digest.update(b"\0")
            observed_sizes.append(current_size)
            object_index += 1
            current_id = None
            current_type = None
            current_size = 0
            current_remaining = None
            current_object_digest = None

    try:
        signal_gate.raise_if_pending()
        close_executable_execution_leases(
            executable_lease,
            (),
        )
        main_lease_open = False
        signal_gate.raise_if_pending()
        while selector.get_map() and failure is None:
            signal_gate.raise_if_pending()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = (
                    "target checkout object payload verification exceeded the "
                    f"{GIT_ENUMERATION_TIMEOUT_SECONDS:g}-second deadline"
                )
                break
            events = selector.select(min(remaining, 0.25))
            signal_gate.raise_if_pending()
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "stderr":
                    if len(retained_stderr) + len(chunk) > GIT_ERROR_OUTPUT_LIMIT_BYTES:
                        failure = (
                            "target checkout object payload verification stderr "
                            f"exceeds the {GIT_ERROR_OUTPUT_LIMIT_BYTES}-byte "
                            "retained-output limit"
                        )
                        break
                    retained_stderr.extend(chunk)
                    continue
                producer_stdout_bytes += len(chunk)
                if producer_stdout_bytes > maximum_producer_bytes:
                    failure = (
                        "target checkout object payload stream exceeds its "
                        "producer-byte safety limit"
                    )
                    break
                stdout_buffer.extend(chunk)
                consume_stdout_buffer()
                if failure is not None:
                    break
        if failure is None:
            signal_gate.raise_if_pending()
            consume_stdout_buffer()
        if failure is None and (
            object_index != len(ordered)
            or current_remaining is not None
            or stdout_buffer
        ):
            failure = (
                "target checkout object payload stream ended before every "
                "required object was verified"
            )
        if failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = (
                    "target checkout object payload verification exceeded the "
                    f"{GIT_ENUMERATION_TIMEOUT_SECONDS:g}-second deadline"
                )
            else:
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = (
                        "target checkout object payload verification exceeded the "
                        f"{GIT_ENUMERATION_TIMEOUT_SECONDS:g}-second deadline"
                    )
        signal_gate.raise_if_pending()
        if failure is not None:
            raise PlanError(failure)
        revalidate_helper_execution_leases(helper_leases)
        try:
            close_executable_execution_leases(None, helper_leases)
        finally:
            helper_leases_open = False
    except BaseException as exc:
        effective_exception = exc
        try:
            signal_gate.raise_if_pending()
        except ForwardedProcessSignal as signal_exc:
            effective_exception = signal_exc
        cleanup_errors: list[str] = []
        if main_lease_open:
            try:
                close_executable_execution_leases(
                    executable_lease,
                    (),
                )
            except BaseException as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
            main_lease_open = False
        try:
            terminate_process_group(
                process,
                initial_signal=(
                    effective_exception.signum
                    if isinstance(effective_exception, ForwardedProcessSignal)
                    else signal.SIGTERM
                ),
                selector=selector,
            )
        except PlanError as cleanup_exc:
            cleanup_errors.append(str(cleanup_exc))
        if helper_leases_open:
            try:
                revalidate_helper_execution_leases(helper_leases)
            except BaseException as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
            try:
                close_executable_execution_leases(
                    None,
                    helper_leases,
                )
            except BaseException as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
            helper_leases_open = False
        try:
            signal_gate.raise_if_pending()
        except ForwardedProcessSignal as signal_exc:
            effective_exception = signal_exc
        if isinstance(effective_exception, ForwardedProcessSignal):
            for cleanup_error in cleanup_errors:
                effective_exception.add_cleanup_error(cleanup_error)
            outcome_exception = effective_exception
            raise effective_exception from (None if effective_exception is exc else exc)
        if cleanup_errors:
            combined_error = PlanError(
                f"{effective_exception}\n" + "\n".join(cleanup_errors)
            )
            outcome_exception = combined_error
            raise combined_error from effective_exception
        outcome_exception = effective_exception
        if effective_exception is exc:
            raise
        raise effective_exception from exc
    finally:
        if selector is not None:
            selector.close()
        if stdout_pipe is not None:
            stdout_pipe.close()
        if stderr_pipe is not None:
            stderr_pipe.close()
        input_file.close()
        if spawn_signals_blocked:
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                supervisor_signal_mask,
            )
        signal_lease.finish(outcome_exception)

    if returncode != 0:
        detail = os.fsdecode(retained_stderr).strip()
        raise PlanError(
            "target checkout object payload verification failed"
            + (f": {detail}" if detail else "")
        )
    return (
        ObjectClosureReceipt(
            object_count=len(ordered),
            logical_bytes=logical_bytes,
            digest=inventory_digest.hexdigest(),
        ),
        tuple(observed_sizes),
    )


def target_object_manifest(
    source_git_dir: Path,
    target_sha: str,
    completeness: SourceCompletenessReceipt,
    *,
    transaction: Optional[SourceFetchTransaction] = None,
) -> ObjectClosureManifest:
    # Protected property: every commit/tree/blob byte needed by checkout is
    # already readable from this source object database without lazy fetch or
    # alternates. Count/size caps bound the proof; the digest binds the exact
    # object/type/size inventory across preflight and mutation.
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", target_sha):
        raise PlanError("target checkout uses an invalid commit object id")
    revalidate_source_object_admission(source_git_dir, transaction)
    revalidate_source_completeness_receipt(source_git_dir, completeness)
    tree_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            "-r",
            "-t",
            "-z",
            "--full-tree",
            target_sha,
        ]
    )
    expected_types: dict[str, str] = {target_sha: "commit"}
    for record in bounded_records(
        tree_result.stdout,
        "target checkout object closure",
        maximum_records=MAX_CHECKOUT_OBJECTS,
    ):
        try:
            header, raw_path = record.split(b"\t", 1)
        except ValueError as exc:
            raise PlanError(
                "target checkout object closure has an invalid record"
            ) from exc
        validate_checkout_path(raw_path, "target checkout object closure")
        fields = header.split()
        if len(fields) != 3:
            raise PlanError("target checkout object closure has an invalid header")
        mode, raw_type, raw_object_id = fields
        object_type = os.fsdecode(raw_type)
        object_id = os.fsdecode(raw_object_id)
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id):
            raise PlanError(
                "target checkout object closure returned an invalid object id"
            )
        if object_type not in {"blob", "tree", "commit"}:
            raise PlanError(
                "target checkout object closure returned an unsupported type: "
                f"{object_type}"
            )
        if mode == b"160000" and object_type == "commit":
            # A gitlink is metadata for a separately planned nested source. The
            # parent checkout does not require the child commit object.
            continue
        if object_type not in {"blob", "tree"}:
            raise PlanError(
                "target checkout tree contains an unexpected non-gitlink commit"
            )
        prior = expected_types.setdefault(object_id, object_type)
        if prior != object_type:
            raise PlanError("target checkout object id appeared with conflicting types")

    root_tree_result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "rev-parse",
            "--verify",
            f"{target_sha}^{{tree}}",
        ],
        stdout_limit=256,
    )
    root_tree = os.fsdecode(root_tree_result.stdout).strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", root_tree):
        raise PlanError("target checkout returned an invalid root tree id")
    prior_root = expected_types.setdefault(root_tree, "tree")
    if prior_root != "tree":
        raise PlanError("target root tree id appeared with a conflicting type")
    if len(expected_types) > MAX_CHECKOUT_OBJECTS:
        raise PlanError(
            "target checkout object closure exceeds the "
            f"{MAX_CHECKOUT_OBJECTS}-object safety limit"
        )

    ordered = sorted(expected_types)
    receipt, object_sizes = verify_target_object_payloads(
        source_git_dir,
        ordered,
        expected_types,
    )
    revalidate_source_completeness_receipt(source_git_dir, completeness)
    revalidate_source_object_admission(source_git_dir, transaction)
    return ObjectClosureManifest(
        root=target_sha,
        receipt=receipt,
        objects=tuple(
            (object_id, expected_types[object_id], object_size)
            for object_id, object_size in zip(ordered, object_sizes)
        ),
    )


def target_object_closure(
    source_git_dir: Path,
    target_sha: str,
    completeness: SourceCompletenessReceipt,
    *,
    transaction: Optional[SourceFetchTransaction] = None,
) -> ObjectClosureReceipt:
    return target_object_manifest(
        source_git_dir,
        target_sha,
        completeness,
        transaction=transaction,
    ).receipt


def target_root_tree_id(source_git_dir: Path, target_sha: str) -> str:
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "rev-parse",
            "--verify",
            f"{target_sha}^{{tree}}",
        ],
        stdout_limit=256,
    )
    root_tree_id = os.fsdecode(result.stdout).strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", root_tree_id):
        raise PlanError("recursive metadata returned an invalid root tree id")
    return root_tree_id


def capture_recursive_metadata_receipt(
    entry: PlannedWorktree,
    budget: Optional[GitmodulesReadBudget] = None,
) -> RecursiveMetadataReceipt:
    checkout_receipt = entry.checkout_preflight
    if checkout_receipt is None:
        raise PlanError(
            f"recursive metadata lacks checkout preflight for {entry.submodule.path}"
        )
    expected_object_closure = checkout_object_receipt(checkout_receipt)
    initial_object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    require_object_closure_receipt(
        initial_object_closure,
        expected_object_closure,
        f"recursive metadata for {entry.submodule.path}",
    )
    root_tree_id = target_root_tree_id(entry.source_git_dir, entry.sha)
    modules, gitmodules_receipt = _capture_commit_gitmodules_with_receipt(
        entry.source_git_dir,
        entry.target.path,
        entry.sha,
        budget,
    )
    selections: list[RecursiveSubmoduleSelection] = []
    for module in modules:
        mode, object_id = _read_tree_gitlink(
            entry.source_git_dir,
            entry.target.path,
            entry.sha,
            module.path,
        )
        selections.append(
            RecursiveSubmoduleSelection(
                name=module.name,
                path=module.path,
                url=module.url,
                mode=mode,
                sha=object_id,
            )
        )
    repeated_object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    require_object_closure_receipt(
        repeated_object_closure,
        expected_object_closure,
        f"recursive metadata for {entry.submodule.path}",
    )
    return RecursiveMetadataReceipt(
        root_tree_id=root_tree_id,
        gitmodules=gitmodules_receipt,
        selections=tuple(selections),
    )


def revalidate_recursive_metadata_receipt(
    entry: PlannedWorktree,
    budget: Optional[GitmodulesReadBudget] = None,
) -> None:
    expected = entry.recursive_metadata
    if expected is None:
        return
    observed = capture_recursive_metadata_receipt(entry, budget)
    if observed != expected:
        raise PlanError(
            f"recursive metadata changed after preflight: {entry.submodule.path}"
        )


def capture_checkout_attributes_receipt(
    source_git_dir: Path,
) -> CheckoutAttributesReceipt:
    info_path = source_git_dir / "info"
    info_binding = capture_typed_access(
        info_path,
        os.R_OK | os.X_OK,
        "checkout attributes directory",
        stat.S_IFDIR,
    )
    info_descriptor = open_directory_descriptor(
        info_path,
        "checkout attributes directory",
    )
    attributes_descriptor = -1
    try:
        revalidate_directory_descriptor(info_binding, info_descriptor)
        (
            attributes_descriptor,
            attributes_binding,
            _attributes_content,
        ) = open_optional_bound_regular_file_at(
            info_descriptor,
            "attributes",
            info_path / "attributes",
            maximum_bytes=MAX_CHECKOUT_ATTRIBUTES_BYTES,
            mode=os.R_OK,
            purpose="common checkout attributes",
            retain_content=False,
        )
        revalidate_directory_descriptor(info_binding, info_descriptor)
        if attributes_binding is None:
            require_absent_entry_at(
                info_descriptor,
                "attributes",
                info_path / "attributes",
                "common checkout attributes",
            )
        else:
            observed, _ = bind_regular_file_descriptor_at(
                attributes_descriptor,
                info_descriptor,
                "attributes",
                info_path / "attributes",
                maximum_bytes=MAX_CHECKOUT_ATTRIBUTES_BYTES,
                mode=os.R_OK,
                purpose="common checkout attributes",
                retain_content=False,
            )
            require_matching_file_binding(
                attributes_binding,
                observed,
                "common checkout attributes",
            )
        return CheckoutAttributesReceipt(
            info_binding=info_binding,
            attributes_binding=attributes_binding,
        )
    finally:
        if attributes_descriptor >= 0:
            os.close(attributes_descriptor)
        os.close(info_descriptor)


def require_checkout_attributes_receipt(
    expected: CheckoutAttributesReceipt,
    observed: CheckoutAttributesReceipt,
) -> None:
    if observed.info_binding != expected.info_binding:
        raise PlanError(
            "checkout attributes directory changed after preflight\n"
            f"  path: {expected.info_binding.path}"
        )
    if (observed.attributes_binding is None) != (expected.attributes_binding is None):
        raise PlanError(
            "common checkout attributes presence changed after preflight\n"
            f"  path: {expected.info_binding.path / 'attributes'}"
        )
    if (
        expected.attributes_binding is not None
        and observed.attributes_binding is not None
    ):
        require_matching_file_binding(
            expected.attributes_binding,
            observed.attributes_binding,
            "common checkout attributes",
        )


def selected_checkout_filters(
    checkout_view: CheckoutExecutionView,
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
    result = read_checkout_execution_view_git(
        checkout_view,
        [
            *source_object_repo_args(checkout_view.common_git_dir),
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
    checkout_view: CheckoutExecutionView,
    paths: tuple[tuple[str, ...], ...],
) -> tuple[FilterSelection, ...]:
    if not paths:
        return ()
    if checkout_view.private_index_path is None:
        raise PlanError("index filter query lacks an isolated index snapshot")
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
    result = read_checkout_execution_view_git(
        checkout_view,
        [
            *source_object_repo_args(checkout_view.common_git_dir),
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
    checkout_view: CheckoutExecutionView,
) -> dict[str, set[str]]:
    result = read_checkout_execution_view_git(
        checkout_view,
        [
            *source_object_repo_args(checkout_view.common_git_dir),
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
            f"  source gitdir: {checkout_view.common_git_dir}\n"
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
    worktree_path: Path,
    checkout_view: CheckoutExecutionView,
    tree_paths: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...],
    index_paths: tuple[tuple[str, ...], ...] = (),
) -> tuple[FilterSelection, ...]:
    selections: list[FilterSelection] = []
    selections.extend(
        selected_index_filters(
            checkout_view,
            index_paths,
        )
    )
    for treeish, paths in tree_paths:
        selections.extend(
            selected_checkout_filters(
                checkout_view,
                treeish,
                paths,
            )
        )
    configured_commands = configured_filter_commands(checkout_view)
    if not selections:
        return ()
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


def bind_checkout_filter_selection(
    entry: PlannedWorktree,
    checkout_view: CheckoutExecutionView,
    tree_paths: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...],
    index_paths: tuple[tuple[str, ...], ...] = (),
    *,
    expected_attributes: CheckoutAttributesReceipt,
    expected_selections: Optional[tuple[FilterSelection, ...]] = None,
) -> tuple[CheckoutAttributesReceipt, tuple[FilterSelection, ...]]:
    # Protected properties: the retained managed-index bytes, captured common
    # info/attributes bytes, private object closure, and resulting filter
    # selection stay exact across the authoritative check-attr query. Every
    # pathname read by Git is owner-private and descriptor-bound through the
    # child pre-exec gate. Any selected filter is rejected regardless of
    # whether its driver currently has a command.
    revalidate_file_content_binding(entry.source_completeness.config_binding)
    revalidate_checkout_execution_view(checkout_view)
    attributes = capture_checkout_attributes_receipt(entry.source_git_dir)
    require_checkout_attributes_receipt(expected_attributes, attributes)
    selections = reject_checkout_filters(
        entry.target.path,
        checkout_view,
        tree_paths,
        index_paths,
    )
    revalidate_checkout_execution_view(checkout_view)
    repeated_attributes = capture_checkout_attributes_receipt(entry.source_git_dir)
    require_checkout_attributes_receipt(attributes, repeated_attributes)
    require_checkout_attributes_receipt(
        expected_attributes,
        repeated_attributes,
    )
    revalidate_file_content_binding(entry.source_completeness.config_binding)
    if expected_selections is not None and selections != expected_selections:
        raise PlanError(
            f"checkout filter selection changed after preflight: {entry.submodule.path}"
        )
    return attributes, selections


def normalize_checkout_object_roots(
    object_roots: tuple[str, ...],
) -> tuple[str, ...]:
    roots = tuple(dict.fromkeys(object_roots))
    if not roots or len(roots) > MAX_CHECKOUT_OBJECT_ROOTS:
        raise PlanError(
            "checkout execution view requires between one and "
            f"{MAX_CHECKOUT_OBJECT_ROOTS} object roots"
        )
    object_id_widths = {len(root) for root in roots}
    if object_id_widths not in ({40}, {64}) or any(
        re.fullmatch(r"[0-9a-f]+", root) is None for root in roots
    ):
        raise PlanError("checkout execution view contains an invalid object root")
    return roots


def checked_bounded_size_add(
    total: int,
    addition: int,
    maximum: int,
    purpose: str,
) -> int:
    """Add non-negative sizes without relying on machine-integer wraparound."""

    if total < 0 or addition < 0 or maximum < 0 or total > maximum:
        raise PlanError(f"{purpose} has an invalid size bound")
    if addition > maximum - total:
        raise PlanError(f"{purpose} exceeds its {maximum}-byte safety limit")
    return total + addition


def zlib_compress_bound(
    source_size: int,
    *,
    maximum_output_bytes: int = MAX_SIGNED_FILE_SIZE_BYTES,
) -> int:
    """Return zlib's default-parameter compressBound without integer overflow."""

    if source_size < 0:
        raise PlanError("zlib input size is negative")
    bound = source_size
    for addition in (
        source_size >> 12,
        source_size >> 14,
        source_size >> 25,
        ZLIB_COMPRESS_BOUND_FIXED_BYTES,
    ):
        bound = checked_bounded_size_add(
            bound,
            addition,
            maximum_output_bytes,
            "zlib compressed object",
        )
    return bound


def git_pack_object_header_size(object_size: int) -> int:
    if object_size < 0:
        raise PlanError("Git pack object size is negative")
    header_bytes = 1
    remaining = object_size >> 4
    while remaining:
        header_bytes += 1
        remaining >>= 7
    return header_bytes


def checkout_pack_output_safety_limit(object_id_bytes: int) -> int:
    if object_id_bytes not in {20, 32}:
        raise PlanError("checkout pack uses an unsupported object-id width")
    maximum_objects = MAX_CHECKOUT_OBJECTS * MAX_CHECKOUT_OBJECT_ROOTS
    maximum_logical_bytes = MAX_CHECKOUT_LOGICAL_BYTES * MAX_CHECKOUT_OBJECT_ROOTS
    total = GIT_PACK_HEADER_BYTES
    for addition in (
        maximum_logical_bytes,
        maximum_logical_bytes >> 12,
        maximum_logical_bytes >> 14,
        maximum_logical_bytes >> 25,
        maximum_objects * ZLIB_COMPRESS_BOUND_FIXED_BYTES,
        maximum_objects * git_pack_object_header_size(MAX_CHECKOUT_LOGICAL_BYTES),
        object_id_bytes,
    ):
        total = checked_bounded_size_add(
            total,
            addition,
            MAX_SIGNED_FILE_SIZE_BYTES,
            "checkout pack aggregate",
        )
    return total


def checkout_pack_size_bound(
    object_sizes: Iterable[int],
    object_id_bytes: int,
    *,
    maximum_output_bytes: int,
) -> int:
    """Bound one undeltified pack using exact headers and per-object zlib bounds."""

    if object_id_bytes not in {20, 32}:
        raise PlanError("checkout pack uses an unsupported object-id width")
    total = checked_bounded_size_add(
        0,
        GIT_PACK_HEADER_BYTES,
        maximum_output_bytes,
        "checkout pack",
    )
    object_count = 0
    logical_bytes = 0
    for object_size in object_sizes:
        if object_size < 0 or object_size > MAX_CHECKOUT_LOGICAL_BYTES:
            raise PlanError("checkout pack object has an invalid logical size")
        object_count += 1
        if object_count > MAX_CHECKOUT_OBJECTS * MAX_CHECKOUT_OBJECT_ROOTS:
            raise PlanError("checkout pack exceeds its aggregate object-count limit")
        logical_bytes = checked_bounded_size_add(
            logical_bytes,
            object_size,
            MAX_CHECKOUT_LOGICAL_BYTES * MAX_CHECKOUT_OBJECT_ROOTS,
            "checkout pack logical payload",
        )
        total = checked_bounded_size_add(
            total,
            git_pack_object_header_size(object_size),
            maximum_output_bytes,
            "checkout pack",
        )
        compressed_bound = zlib_compress_bound(
            object_size,
            maximum_output_bytes=maximum_output_bytes - total,
        )
        total = checked_bounded_size_add(
            total,
            compressed_bound,
            maximum_output_bytes,
            "checkout pack",
        )
    if object_count == 0:
        raise PlanError("checkout pack object inventory is empty")
    return checked_bounded_size_add(
        total,
        object_id_bytes,
        maximum_output_bytes,
        "checkout pack",
    )


def checkout_pack_index_size_bound(
    object_count: int,
    object_id_bytes: int,
) -> int:
    if (
        object_count < 0
        or object_count > MAX_CHECKOUT_OBJECTS * MAX_CHECKOUT_OBJECT_ROOTS
        or object_id_bytes not in {20, 32}
    ):
        raise PlanError("checkout pack index has an invalid bound input")
    # Version 2 header/fanout, OIDs, CRCs, 32-bit offsets, worst-case 64-bit
    # offsets, and the pack/index checksums.
    return (
        8
        + (256 * 4)
        + object_count * (object_id_bytes + 4 + 4 + 8)
        + (2 * object_id_bytes)
    )


def parse_checkout_pack_index(
    content: bytes,
    object_id_bytes: int,
) -> tuple[str, ...]:
    if object_id_bytes not in {20, 32}:
        raise PlanError("checkout pack index uses an unsupported object-id width")
    if len(content) < 8 + (256 * 4) + (2 * object_id_bytes):
        raise PlanError("checkout pack index is truncated")
    if content[:4] != b"\xfftOc" or struct.unpack(">I", content[4:8])[0] != 2:
        raise PlanError("checkout pack index is not canonical version 2")
    fanout = struct.unpack(">256I", content[8 : 8 + (256 * 4)])
    if any(left > right for left, right in zip(fanout, fanout[1:])):
        raise PlanError("checkout pack index has a non-monotonic fanout table")
    object_count = fanout[-1]
    if object_count > MAX_CHECKOUT_OBJECTS * MAX_CHECKOUT_OBJECT_ROOTS:
        raise PlanError(
            "checkout pack index exceeds the aggregate object-count safety limit"
        )
    object_table_start = 8 + (256 * 4)
    object_table_end = object_table_start + (object_count * object_id_bytes)
    crc_table_end = object_table_end + (object_count * 4)
    offset_table_end = crc_table_end + (object_count * 4)
    if offset_table_end > len(content):
        raise PlanError("checkout pack index has truncated object tables")
    raw_object_ids = tuple(
        content[offset : offset + object_id_bytes]
        for offset in range(
            object_table_start,
            object_table_end,
            object_id_bytes,
        )
    )
    if raw_object_ids != tuple(sorted(set(raw_object_ids))):
        raise PlanError("checkout pack index object ids are not unique and sorted")
    expected_fanout: list[int] = []
    observed = 0
    object_index = 0
    for first_byte in range(256):
        while (
            object_index < len(raw_object_ids)
            and raw_object_ids[object_index][0] == first_byte
        ):
            observed += 1
            object_index += 1
        expected_fanout.append(observed)
    if tuple(expected_fanout) != fanout:
        raise PlanError("checkout pack index fanout does not match its object table")
    offset_words = struct.unpack(
        f">{object_count}I",
        content[crc_table_end:offset_table_end],
    )
    large_indexes = sorted(
        word & 0x7FFFFFFF for word in offset_words if word & 0x80000000
    )
    large_offset_count = 0 if not large_indexes else large_indexes[-1] + 1
    if large_indexes != list(range(large_offset_count)):
        raise PlanError("checkout pack index has invalid large-offset references")
    expected_size = offset_table_end + (large_offset_count * 8) + (2 * object_id_bytes)
    if len(content) != expected_size:
        raise PlanError("checkout pack index has trailing or truncated bytes")
    index_digest = hashlib.sha1() if object_id_bytes == 20 else hashlib.sha256()
    index_digest.update(content[:-object_id_bytes])
    if index_digest.digest() != content[-object_id_bytes:]:
        raise PlanError("checkout pack index checksum is invalid")
    return tuple(object_id.hex() for object_id in raw_object_ids)


def materialize_checkout_object_pack(
    source_git_dir: Path,
    source_completeness: SourceCompletenessReceipt,
    object_roots: tuple[str, ...],
    private_pack: Path,
    source_directory_leases: tuple[DirectoryEntryLease, ...],
    source_file_leases: tuple[DescriptorBoundFileLease, ...],
    submodule_path: str,
) -> tuple[
    tuple[ObjectClosureManifest, ...],
    Path,
    Path,
    int,
    int,
]:
    roots = normalize_checkout_object_roots(object_roots)
    manifests = tuple(
        target_object_manifest(
            source_git_dir,
            root,
            source_completeness,
        )
        for root in roots
    )
    expected_objects: dict[str, tuple[str, int]] = {}
    for manifest in manifests:
        for object_id, object_type, object_size in manifest.objects:
            prior = expected_objects.setdefault(
                object_id,
                (object_type, object_size),
            )
            if prior != (object_type, object_size):
                raise PlanError(
                    "checkout object roots contain conflicting object metadata"
                )
    maximum_objects = MAX_CHECKOUT_OBJECTS * len(manifests)
    if len(expected_objects) > maximum_objects:
        raise PlanError(
            "checkout execution view exceeds the aggregate "
            f"{maximum_objects}-object safety limit"
        )
    ordered = tuple(sorted(expected_objects))
    object_input = b"".join(object_id.encode("ascii") + b"\n" for object_id in ordered)
    if len(object_input) > GIT_INPUT_LIMIT_BYTES:
        raise PlanError(
            "checkout execution view exceeds the "
            f"{GIT_INPUT_LIMIT_BYTES}-byte pack input limit"
        )
    object_id_bytes = len(roots[0]) // 2
    maximum_pack_bytes = checkout_pack_size_bound(
        (expected_objects[object_id][1] for object_id in ordered),
        object_id_bytes,
        maximum_output_bytes=checkout_pack_output_safety_limit(object_id_bytes),
    )
    maximum_index_bytes = checkout_pack_index_size_bound(
        len(ordered),
        object_id_bytes,
    )
    pack_base = private_pack / "checkout"
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "-c",
            "pack.indexVersion=2",
            "-c",
            "pack.writeReverseIndex=false",
            "pack-objects",
            "--non-empty",
            "--no-reuse-delta",
            "--no-reuse-object",
            "--no-use-bitmap-index",
            "--window=0",
            "--depth=1",
            "--threads=1",
            "--compression=0",
            str(pack_base),
        ],
        input_bytes=object_input,
        stdout_limit=256,
        directory_identity_leases=source_directory_leases,
        file_content_leases=source_file_leases,
        file_size_limit_bytes=max(maximum_pack_bytes, maximum_index_bytes),
    )
    pack_id = os.fsdecode(result.stdout).strip()
    if not re.fullmatch(rf"[0-9a-f]{{{len(roots[0])}}}", pack_id):
        raise PlanError("checkout object pack returned an invalid pack id")
    pack_path = private_pack / f"checkout-{pack_id}.pack"
    index_path = private_pack / f"checkout-{pack_id}.idx"
    expected_names = {pack_path.name, index_path.name}
    observed_names = set(os.listdir(private_pack))
    if observed_names != expected_names:
        raise PlanError("checkout object pack produced an unexpected private inventory")
    for path, maximum_bytes in (
        (pack_path, maximum_pack_bytes),
        (index_path, maximum_index_bytes),
    ):
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            path_stat = os.fstat(descriptor)
            if (
                stat.S_IFMT(path_stat.st_mode) != stat.S_IFREG
                or path_stat.st_uid != os.geteuid()
                or path_stat.st_size <= 0
                or path_stat.st_size > maximum_bytes
            ):
                raise PlanError(
                    f"checkout object pack output violates its bound: {path}"
                )
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    pack_directory_descriptor = open_directory_descriptor(
        private_pack,
        f"isolated checkout pack directory for {submodule_path}",
    )
    try:
        os.fsync(pack_directory_descriptor)
    finally:
        os.close(pack_directory_descriptor)
    revalidate_source_completeness_receipt(
        source_git_dir,
        source_completeness,
    )
    return (
        manifests,
        pack_path,
        index_path,
        maximum_pack_bytes,
        maximum_index_bytes,
    )


def capture_checkout_execution_view(
    source_git_dir: Path,
    source_completeness: SourceCompletenessReceipt,
    attributes_receipt: CheckoutAttributesReceipt,
    submodule_path: str,
    object_roots: tuple[str, ...],
    managed_index_content: Optional[bytes] = None,
) -> CheckoutExecutionView:
    # The actual checkout reads config, common info/attributes, and every
    # required object only from this owner-private snapshot. The exact private
    # pack is content-bound again in the child pre-exec gate; live source
    # object bytes can no longer influence checkout after materialization.
    roots = normalize_checkout_object_roots(object_roots)
    if managed_index_content is not None and (
        not managed_index_content
        or len(managed_index_content) > MAX_SUPERPROJECT_INDEX_BYTES
    ):
        raise PlanError(
            f"managed checkout index snapshot is invalid for {submodule_path}"
        )
    resolved_source_git_dir = source_git_dir.resolve(strict=True)
    if (
        source_completeness.gitdir_binding.path.resolve(strict=True)
        != resolved_source_git_dir
    ):
        raise PlanError(
            f"checkout execution view names the wrong source for {submodule_path}"
        )
    if (
        source_completeness.config_binding.path.resolve(strict=True)
        != resolved_source_git_dir / "config"
    ):
        raise PlanError(
            f"checkout execution view has the wrong config for {submodule_path}"
        )
    if (
        attributes_receipt.info_binding.path.resolve(strict=True)
        != resolved_source_git_dir / "info"
    ):
        raise PlanError(
            f"checkout execution view has the wrong attributes directory for "
            f"{submodule_path}"
        )

    with ExitStack() as cleanup:
        directory_leases: list[DirectoryEntryLease] = []
        exact_inventory_leases: list[DirectoryExactInventoryLease] = []
        file_leases: list[DescriptorBoundFileLease] = []
        digest_file_leases: list[DescriptorBoundDigestFileLease] = []
        absent_entry_leases: list[DirectoryAbsentEntryLease] = []

        def capture_directory(binding: AccessBinding) -> DirectoryEntryLease:
            lease = capture_directory_entry_lease(
                binding.path,
                binding.mode,
                binding.purpose,
            )
            cleanup.callback(lease.close)
            if (
                lease.binding.path != binding.path.resolve(strict=True)
                or lease.binding.fingerprint != binding.fingerprint
                or lease.binding.mode != binding.mode
                or lease.binding.purpose != binding.purpose
            ):
                raise PlanError(
                    f"{binding.purpose} changed during checkout-view binding"
                )
            directory_leases.append(lease)
            return lease

        def capture_file(
            directory_lease: DirectoryEntryLease,
            binding: FileContentBinding,
        ) -> tuple[DescriptorBoundFileLease, bytes]:
            if binding.path.parent.resolve(strict=True) != directory_lease.path:
                raise PlanError(
                    f"{binding.purpose} does not belong to its checkout-view directory"
                )
            entry_name = os.fsdecode(validate_descriptor_entry_name(binding.path.name))
            descriptor, observed, content = open_bound_regular_file_at(
                directory_lease.descriptor,
                entry_name,
                binding.path,
                maximum_bytes=binding.maximum_bytes,
                mode=binding.mode,
                purpose=binding.purpose,
                retain_content=True,
            )
            cleanup.callback(os.close, descriptor)
            require_matching_file_binding(
                binding,
                observed,
                binding.purpose,
            )
            if content is None:
                raise PlanError(f"{binding.purpose} returned no checkout-view content")
            lease = DescriptorBoundFileLease(
                directory_binding=directory_lease.binding,
                directory_descriptor=directory_lease.descriptor,
                entry_name=entry_name,
                descriptor=descriptor,
                binding=binding,
                content=content,
            )
            file_leases.append(lease)
            return lease, content

        def capture_digest_file(
            directory_lease: DirectoryEntryLease,
            path: Path,
            maximum_bytes: int,
            purpose: str,
        ) -> DescriptorBoundDigestFileLease:
            if path.parent.resolve(strict=True) != directory_lease.path:
                raise PlanError(
                    f"{purpose} does not belong to its checkout-view directory"
                )
            entry_name = os.fsdecode(validate_descriptor_entry_name(path.name))
            descriptor, binding, content = open_bound_regular_file_at(
                directory_lease.descriptor,
                entry_name,
                path,
                maximum_bytes=maximum_bytes,
                mode=os.R_OK,
                purpose=purpose,
                retain_content=False,
            )
            cleanup.callback(os.close, descriptor)
            if content is not None:
                raise PlanError(f"{purpose} retained unexpected in-memory content")
            lease = DescriptorBoundDigestFileLease(
                directory_binding=directory_lease.binding,
                directory_descriptor=directory_lease.descriptor,
                entry_name=entry_name,
                descriptor=descriptor,
                binding=binding,
            )
            digest_file_leases.append(lease)
            return lease

        source_root_lease = capture_directory(
            source_completeness.gitdir_binding,
        )
        _source_config_lease, source_config_content = capture_file(
            source_root_lease,
            source_completeness.config_binding,
        )
        source_info_lease = capture_directory(attributes_receipt.info_binding)
        source_attributes_content: Optional[bytes] = None
        if attributes_receipt.attributes_binding is None:
            absent_entry_leases.append(
                capture_directory_absent_entry_lease(
                    source_info_lease,
                    ("attributes",),
                    f"source checkout attributes for {submodule_path}",
                )
            )
        else:
            _source_attributes_lease, source_attributes_content = capture_file(
                source_info_lease,
                attributes_receipt.attributes_binding,
            )
        capture_directory(source_completeness.objects_binding)

        guard = OwnerPrivateTemporaryDirectory(prefix="submodule-worktree-checkout.")
        cleanup.callback(guard.cleanup)
        private_root = Path(guard.name).resolve(strict=True)
        private_info = private_root / "info"
        private_objects = private_root / "objects"
        private_object_info = private_objects / "info"
        private_pack = private_objects / "pack"
        private_refs = private_root / "refs"
        for directory in (
            private_info,
            private_objects,
            private_object_info,
            private_pack,
            private_refs,
        ):
            os.mkdir(directory, mode=0o700)
        private_config = private_root / "config"
        write_owner_private_file(
            private_config,
            source_config_content,
            f"isolated checkout config for {submodule_path}",
        )
        private_head = private_root / "HEAD"
        write_owner_private_file(
            private_head,
            b"ref: refs/heads/checkout-isolated\n",
            f"isolated checkout HEAD for {submodule_path}",
        )
        private_managed_index = private_root / "index"
        if managed_index_content is not None:
            write_owner_private_file(
                private_managed_index,
                managed_index_content,
                f"isolated checkout managed index for {submodule_path}",
                final_mode=0o400,
            )
        private_attributes = private_info / "attributes"
        if source_attributes_content is not None:
            write_owner_private_file(
                private_attributes,
                source_attributes_content,
                f"isolated checkout attributes for {submodule_path}",
            )

        (
            expected_manifests,
            private_pack_path,
            private_index_path,
            maximum_pack_bytes,
            maximum_index_bytes,
        ) = materialize_checkout_object_pack(
            source_git_dir,
            source_completeness,
            roots,
            private_pack,
            tuple(directory_leases),
            tuple(file_leases),
            submodule_path,
        )
        private_root_binding = capture_owner_private_directory(
            private_root,
            f"isolated checkout common gitdir for {submodule_path}",
        )
        private_info_binding = capture_owner_private_directory(
            private_info,
            f"isolated checkout info directory for {submodule_path}",
        )
        private_objects_binding = capture_owner_private_directory(
            private_objects,
            f"isolated checkout object directory for {submodule_path}",
        )
        private_object_info_binding = capture_owner_private_directory(
            private_object_info,
            f"isolated checkout object-info directory for {submodule_path}",
        )
        private_pack_binding = capture_owner_private_directory(
            private_pack,
            f"isolated checkout pack directory for {submodule_path}",
        )
        private_refs_binding = capture_owner_private_directory(
            private_refs,
            f"isolated checkout refs directory for {submodule_path}",
        )
        private_root_lease = capture_directory(private_root_binding)
        private_info_lease = capture_directory(private_info_binding)
        private_objects_lease = capture_directory(private_objects_binding)
        private_object_info_lease = capture_directory(private_object_info_binding)
        private_pack_lease = capture_directory(private_pack_binding)
        private_refs_lease = capture_directory(private_refs_binding)
        private_config_binding = read_bound_regular_file(
            private_config,
            maximum_bytes=MAX_SOURCE_CONFIG_BYTES,
            mode=os.R_OK,
            purpose=f"isolated checkout config for {submodule_path}",
            retain_content=False,
        )[0]
        capture_file(private_root_lease, private_config_binding)
        private_head_binding = read_bound_regular_file(
            private_head,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose=f"isolated checkout HEAD for {submodule_path}",
            retain_content=False,
        )[0]
        capture_file(private_root_lease, private_head_binding)
        if managed_index_content is not None:
            private_managed_index_binding = read_bound_regular_file(
                private_managed_index,
                maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
                mode=os.R_OK,
                purpose=f"isolated checkout managed index for {submodule_path}",
                retain_content=False,
            )[0]
            (
                _private_managed_index_lease,
                private_managed_index_content,
            ) = capture_file(private_root_lease, private_managed_index_binding)
            if (
                private_managed_index_binding.fingerprint.permissions != 0o400
                or private_managed_index_content != managed_index_content
            ):
                raise PlanError(
                    f"isolated checkout managed index changed during capture for "
                    f"{submodule_path}"
                )
        if source_attributes_content is None:
            absent_entry_leases.append(
                capture_directory_absent_entry_lease(
                    private_info_lease,
                    ("attributes",),
                    f"isolated checkout attributes for {submodule_path}",
                )
            )
        else:
            private_attributes_binding = read_bound_regular_file(
                private_attributes,
                maximum_bytes=MAX_CHECKOUT_ATTRIBUTES_BYTES,
                mode=os.R_OK,
                purpose=f"isolated checkout attributes for {submodule_path}",
                retain_content=False,
            )[0]
            capture_file(private_info_lease, private_attributes_binding)
        private_index_binding = read_bound_regular_file(
            private_index_path,
            maximum_bytes=maximum_index_bytes,
            mode=os.R_OK,
            purpose=f"isolated checkout pack index for {submodule_path}",
            retain_content=False,
        )[0]
        _private_index_lease, private_index_content = capture_file(
            private_pack_lease,
            private_index_binding,
        )
        _private_pack_digest_lease = capture_digest_file(
            private_pack_lease,
            private_pack_path,
            maximum_pack_bytes,
            f"isolated checkout object pack for {submodule_path}",
        )
        exact_inventory_leases.extend(
            (
                capture_directory_exact_inventory_lease(
                    private_root_lease,
                    (
                        "HEAD",
                        "config",
                        *(("index",) if managed_index_content is not None else ()),
                        "info",
                        "objects",
                        "refs",
                    ),
                    f"isolated checkout exact common-gitdir inventory for "
                    f"{submodule_path}",
                ),
                capture_directory_exact_inventory_lease(
                    private_info_lease,
                    (() if source_attributes_content is None else ("attributes",)),
                    f"isolated checkout exact info inventory for {submodule_path}",
                ),
                capture_directory_exact_inventory_lease(
                    private_objects_lease,
                    ("info", "pack"),
                    f"isolated checkout exact object inventory for {submodule_path}",
                ),
                capture_directory_exact_inventory_lease(
                    private_object_info_lease,
                    (),
                    f"isolated checkout exact object-info inventory for "
                    f"{submodule_path}",
                ),
                capture_directory_exact_inventory_lease(
                    private_refs_lease,
                    (),
                    f"isolated checkout exact refs inventory for {submodule_path}",
                ),
                capture_directory_exact_inventory_lease(
                    private_pack_lease,
                    (private_pack_path.name, private_index_path.name),
                    f"isolated checkout exact pack inventory for {submodule_path}",
                ),
            )
        )
        object_id_bytes = len(roots[0]) // 2
        indexed_object_ids = parse_checkout_pack_index(
            private_index_content,
            object_id_bytes,
        )
        expected_object_ids = tuple(
            sorted(
                {
                    object_id
                    for manifest in expected_manifests
                    for object_id, _object_type, _object_size in manifest.objects
                }
            )
        )
        if indexed_object_ids != expected_object_ids:
            raise PlanError(
                f"isolated checkout pack inventory changed for {submodule_path}"
            )
        # Exact inventories leave no repository-local excludes, loose objects,
        # refs, alternates, promisor markers, or acceleration side channels in
        # the private view. A narrower ignore set can only make Git refuse an
        # additional overwrite; it cannot silently bless mutable source input.

        view = CheckoutExecutionView(
            common_git_dir=private_root,
            object_directory=private_objects,
            private_index_path=(
                private_managed_index if managed_index_content is not None else None
            ),
            directory_leases=tuple(directory_leases),
            exact_inventory_leases=tuple(exact_inventory_leases),
            file_leases=tuple(file_leases),
            digest_file_leases=tuple(digest_file_leases),
            absent_entry_leases=tuple(absent_entry_leases),
            guard=guard,
        )
        revalidate_checkout_execution_view(view)
        read_git_bounded(
            [
                *source_object_repo_args(private_root),
                "verify-pack",
                "-s",
                str(private_index_path),
            ],
            stdout_limit=MAX_CHECKOUT_PACK_INDEX_OVERHEAD_BYTES,
            extra_env=checkout_execution_environment(view, roots[-1]),
            directory_identity_leases=view.directory_leases,
            directory_exact_inventory_leases=view.exact_inventory_leases,
            directory_absent_entry_leases=view.absent_entry_leases,
            file_content_leases=view.file_leases,
            digest_file_leases=view.digest_file_leases,
        )
        private_completeness = capture_source_completeness_receipt(private_root)
        for expected_manifest in expected_manifests:
            observed_manifest = target_object_manifest(
                private_root,
                expected_manifest.root,
                private_completeness,
            )
            if observed_manifest != expected_manifest:
                raise PlanError(
                    f"isolated checkout object closure changed for {submodule_path}"
                )
        revalidate_checkout_execution_view(view)
        cleanup.pop_all()
        return view


def revalidate_checkout_execution_view(view: CheckoutExecutionView) -> None:
    if not view.active:
        raise PlanError("checkout execution view is inactive")
    if not view.common_git_dir.is_absolute():
        raise PlanError("checkout execution-view common gitdir is not absolute")
    if not view.object_directory.is_absolute():
        raise PlanError("checkout execution-view object directory is not absolute")
    if (
        not view.directory_leases
        or not view.exact_inventory_leases
        or not view.file_leases
        or not view.digest_file_leases
    ):
        raise PlanError("checkout execution view is incomplete")
    if (
        sum(lease.path == view.common_git_dir for lease in view.directory_leases) != 1
        or sum(lease.path == view.object_directory for lease in view.directory_leases)
        != 1
    ):
        raise PlanError("checkout execution view has an invalid directory shape")
    if view.private_index_path is not None and (
        not view.private_index_path.is_absolute()
        or view.private_index_path.parent != view.common_git_dir
        or sum(
            lease.binding.path == view.private_index_path for lease in view.file_leases
        )
        != 1
    ):
        raise PlanError("checkout execution view has an unbound private index")
    for directory_lease in view.directory_leases:
        revalidate_directory_entry_lease(directory_lease)
    for inventory_lease in view.exact_inventory_leases:
        revalidate_directory_exact_inventory_lease(inventory_lease)
    for absent_entry_lease in view.absent_entry_leases:
        revalidate_directory_absent_entry_lease(absent_entry_lease)
    for file_lease in view.file_leases:
        revalidate_descriptor_bound_file_lease(file_lease)
    for file_lease in view.digest_file_leases:
        revalidate_descriptor_bound_digest_file_lease(file_lease)
    for directory_lease in view.directory_leases:
        revalidate_directory_entry_lease(directory_lease)
    for inventory_lease in view.exact_inventory_leases:
        revalidate_directory_exact_inventory_lease(inventory_lease)
    for absent_entry_lease in view.absent_entry_leases:
        revalidate_directory_absent_entry_lease(absent_entry_lease)


def checkout_execution_environment(
    view: CheckoutExecutionView,
    sha: Optional[str],
) -> dict[str, str]:
    revalidate_checkout_execution_view(view)
    environment = {
        "GIT_COMMON_DIR": str(view.common_git_dir),
        "GIT_OBJECT_DIRECTORY": str(view.object_directory),
    }
    if sha is not None:
        environment["GIT_ATTR_SOURCE"] = sha
    if view.private_index_path is not None:
        environment["GIT_INDEX_FILE"] = str(view.private_index_path)
    return environment


def read_checkout_execution_view_git(
    view: CheckoutExecutionView,
    args: list[str],
    *,
    check: bool = True,
    input_bytes: Optional[bytes] = None,
) -> subprocess.CompletedProcess[bytes]:
    result = read_git_bounded(
        args,
        check=check,
        input_bytes=input_bytes,
        extra_env=checkout_execution_environment(view, None),
        directory_identity_leases=view.directory_leases,
        directory_exact_inventory_leases=view.exact_inventory_leases,
        directory_absent_entry_leases=view.absent_entry_leases,
        file_content_leases=view.file_leases,
        digest_file_leases=view.digest_file_leases,
    )
    revalidate_checkout_execution_view(view)
    return result


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


def probe_managed_checkout(
    worktree_path: Path,
    target_sha: str,
    checkout_view: CheckoutExecutionView,
    managed_preflight: ManagedPreflightReceipt,
) -> None:
    result = run_managed_preflight_probe(
        worktree_path,
        target_sha,
        checkout_view,
        managed_preflight,
        [
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


def checkout_object_receipt(receipt: CheckoutPreflight) -> ObjectClosureReceipt:
    return ObjectClosureReceipt(
        object_count=receipt.object_count,
        logical_bytes=receipt.object_logical_bytes,
        digest=receipt.object_digest,
    )


def require_object_closure_receipt(
    observed: ObjectClosureReceipt,
    expected: ObjectClosureReceipt,
    purpose: str,
) -> None:
    if observed != expected:
        raise PlanError(f"{purpose} object closure changed")


def capture_checkout_preflight(
    entry: PlannedWorktree,
) -> tuple[CheckoutPreflight, tuple[AccessBinding, ...]]:
    changes: Optional[tuple[TreeChange, ...]] = None
    current_head: Optional[str] = None
    index_digest: Optional[str] = None
    index_entry_count: Optional[int] = None
    write_bindings: tuple[AccessBinding, ...] = ()
    object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    target_blob_paths: Optional[tuple[tuple[str, ...], ...]] = None
    if entry.state == "managed":
        managed_preflight = capture_managed_preflight_receipt(
            entry.target.path,
            entry.source_git_dir,
        )
        managed_preflight_outcome: Optional[BaseException] = None
        try:
            current_head = managed_preflight.current_head
            index_digest = managed_preflight.index_digest
            index_entry_count = managed_preflight.index_entry_count
            changes = parse_managed_tree_changes(
                entry.target.path,
                current_head,
                entry.sha,
            )
            final_index_paths = managed_preflight.index_blob_paths
            attributes_receipt = capture_checkout_attributes_receipt(
                entry.source_git_dir
            )
            # Status and read-tree can invoke clean/process conversion even
            # during plan-only preflight. The explicit admin binding selects
            # the worktree, while an owner-private exact index snapshot makes
            # live assume-unchanged ABA irrelevant to both probes.
            checkout_view = capture_checkout_execution_view(
                entry.source_git_dir,
                entry.source_completeness,
                attributes_receipt,
                entry.submodule.path,
                (current_head, entry.sha),
                managed_preflight.index_lease.content,
            )
            checkout_view_outcome: Optional[BaseException] = None
            try:
                current_blob_paths = target_tree_blob_paths(
                    checkout_view,
                    current_head,
                )
                target_blob_paths = target_tree_blob_paths(
                    checkout_view,
                    entry.sha,
                )
                final_tree_paths = (
                    (current_head, current_blob_paths),
                    (entry.sha, target_blob_paths),
                )
                _observed_attributes, filter_selections = (
                    bind_checkout_filter_selection(
                        entry,
                        checkout_view,
                        final_tree_paths,
                        final_index_paths,
                        expected_attributes=attributes_receipt,
                    )
                )
                if has_local_changes(
                    entry.target.path,
                    current_head,
                    checkout_view,
                    managed_preflight,
                ):
                    raise PlanError(
                        f"{entry.target.path} has local changes; clean it before syncing"
                    )
                write_bindings = checkout_write_access_bindings(
                    entry.target.path,
                    changes,
                )
                reject_managed_ignored_conflicts(entry.target.path, changes)
                probe_managed_checkout(
                    entry.target.path,
                    entry.sha,
                    checkout_view,
                    managed_preflight,
                )
                revalidate_managed_preflight_receipt(managed_preflight)
            except BaseException as exc:
                checkout_view_outcome = exc
                raise
            finally:
                finish_explicit_cleanup(
                    checkout_view.close,
                    outcome_exception=checkout_view_outcome,
                    purpose="managed preflight checkout execution view",
                    recovery_identity=str(checkout_view.common_git_dir),
                )
        except BaseException as exc:
            managed_preflight_outcome = exc
            raise
        finally:
            finish_explicit_cleanup(
                managed_preflight.close,
                outcome_exception=managed_preflight_outcome,
                purpose="managed preflight control/index receipt",
                recovery_identity=str(
                    managed_preflight.control.git_file_binding.path.parent
                ),
            )
    else:
        final_index_paths = ()
        attributes_receipt = capture_checkout_attributes_receipt(entry.source_git_dir)
        checkout_view = capture_checkout_execution_view(
            entry.source_git_dir,
            entry.source_completeness,
            attributes_receipt,
            entry.submodule.path,
            (entry.sha,),
        )
        checkout_view_outcome = None
        try:
            target_blob_paths = target_tree_blob_paths(
                checkout_view,
                entry.sha,
            )
            final_tree_paths = ((entry.sha, target_blob_paths),)
            _observed_attributes, filter_selections = bind_checkout_filter_selection(
                entry,
                checkout_view,
                final_tree_paths,
                expected_attributes=attributes_receipt,
            )
        except BaseException as exc:
            checkout_view_outcome = exc
            raise
        finally:
            finish_explicit_cleanup(
                checkout_view.close,
                outcome_exception=checkout_view_outcome,
                purpose="new preflight checkout filter view",
                recovery_identity=str(checkout_view.common_git_dir),
            )
    if target_blob_paths is None:
        raise PlanError(
            f"checkout path selection is incomplete for {entry.submodule.path}"
        )
    repeated_object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    require_object_closure_receipt(
        repeated_object_closure,
        object_closure,
        f"target checkout metadata for {entry.submodule.path}",
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
            target_blob_paths=target_blob_paths,
            attributes_receipt=attributes_receipt,
            filter_selections=filter_selections,
            path_count=path_count,
            path_digest=path_digest,
            object_count=object_closure.object_count,
            object_logical_bytes=object_closure.logical_bytes,
            object_digest=object_closure.digest,
            changes=changes or (),
        ),
        write_bindings,
    )


def revalidate_checkout_preflight(entry: PlannedWorktree) -> None:
    receipt = entry.checkout_preflight
    if receipt is None:
        raise PlanError(f"checkout preflight is incomplete for {entry.submodule.path}")
    expected_object_closure = checkout_object_receipt(receipt)
    object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    require_object_closure_receipt(
        object_closure,
        expected_object_closure,
        f"target checkout for {entry.submodule.path}",
    )
    if receipt.kind == "managed":
        managed_preflight = capture_managed_preflight_receipt(
            entry.target.path,
            entry.source_git_dir,
        )
        managed_preflight_outcome: Optional[BaseException] = None
        try:
            current_head = managed_preflight.current_head
            if current_head != receipt.current_head:
                raise PlanError(
                    f"managed worktree HEAD changed after preflight: "
                    f"{entry.target.path}"
                )
            if (
                managed_preflight.index_digest != receipt.index_digest
                or managed_preflight.index_entry_count != receipt.index_entry_count
            ):
                raise PlanError(
                    f"managed worktree index changed after preflight: "
                    f"{entry.target.path}"
                )
            final_index_paths = managed_preflight.index_blob_paths
            checkout_view = capture_checkout_execution_view(
                entry.source_git_dir,
                entry.source_completeness,
                receipt.attributes_receipt,
                entry.submodule.path,
                (current_head, entry.sha),
                managed_preflight.index_lease.content,
            )
            checkout_view_outcome: Optional[BaseException] = None
            try:
                current_blob_paths = target_tree_blob_paths(
                    checkout_view,
                    current_head,
                )
                target_blob_paths = target_tree_blob_paths(
                    checkout_view,
                    entry.sha,
                )
                if target_blob_paths != receipt.target_blob_paths:
                    raise PlanError(
                        f"target checkout path selection changed after preflight: "
                        f"{entry.submodule.path}"
                    )
                final_tree_paths = (
                    (current_head, current_blob_paths),
                    (entry.sha, target_blob_paths),
                )
                bind_checkout_filter_selection(
                    entry,
                    checkout_view,
                    final_tree_paths,
                    final_index_paths,
                    expected_attributes=receipt.attributes_receipt,
                    expected_selections=receipt.filter_selections,
                )
                repeated_changes = parse_managed_tree_changes(
                    entry.target.path,
                    current_head,
                    entry.sha,
                )
                if repeated_changes != receipt.changes:
                    raise PlanError(
                        f"managed checkout write set changed after preflight: "
                        f"{entry.target.path}"
                    )
                digest_paths = (change.relative_parts for change in repeated_changes)
                if has_local_changes(
                    entry.target.path,
                    current_head,
                    checkout_view,
                    managed_preflight,
                ):
                    raise PlanError(
                        f"{entry.target.path} has local changes; "
                        "clean it before syncing"
                    )
                reject_managed_ignored_conflicts(
                    entry.target.path,
                    receipt.changes,
                )
                probe_managed_checkout(
                    entry.target.path,
                    entry.sha,
                    checkout_view,
                    managed_preflight,
                )
                revalidate_managed_preflight_receipt(managed_preflight)
                bind_checkout_filter_selection(
                    entry,
                    checkout_view,
                    final_tree_paths,
                    final_index_paths,
                    expected_attributes=receipt.attributes_receipt,
                    expected_selections=receipt.filter_selections,
                )
            except BaseException as exc:
                checkout_view_outcome = exc
                raise
            finally:
                finish_explicit_cleanup(
                    checkout_view.close,
                    outcome_exception=checkout_view_outcome,
                    purpose="managed preflight checkout execution view",
                    recovery_identity=str(checkout_view.common_git_dir),
                )
        except BaseException as exc:
            managed_preflight_outcome = exc
            raise
        finally:
            finish_explicit_cleanup(
                managed_preflight.close,
                outcome_exception=managed_preflight_outcome,
                purpose="managed preflight control/index receipt",
                recovery_identity=str(
                    managed_preflight.control.git_file_binding.path.parent
                ),
            )
    elif receipt.kind == "new":
        checkout_view = capture_checkout_execution_view(
            entry.source_git_dir,
            entry.source_completeness,
            receipt.attributes_receipt,
            entry.submodule.path,
            (entry.sha,),
        )
        checkout_view_outcome: Optional[BaseException] = None
        try:
            target_blob_paths = target_tree_blob_paths(
                checkout_view,
                entry.sha,
            )
            if target_blob_paths != receipt.target_blob_paths:
                raise PlanError(
                    f"target checkout path selection changed after preflight: "
                    f"{entry.submodule.path}"
                )
            final_tree_paths = ((entry.sha, target_blob_paths),)
            final_index_paths = ()
            digest_paths = target_blob_paths
            bind_checkout_filter_selection(
                entry,
                checkout_view,
                final_tree_paths,
                final_index_paths,
                expected_attributes=receipt.attributes_receipt,
                expected_selections=receipt.filter_selections,
            )
        except BaseException as exc:
            checkout_view_outcome = exc
            raise
        finally:
            finish_explicit_cleanup(
                checkout_view.close,
                outcome_exception=checkout_view_outcome,
                purpose="new preflight filter revalidation view",
                recovery_identity=str(checkout_view.common_git_dir),
            )
    else:
        raise PlanError(
            f"checkout preflight has an invalid kind for {entry.submodule.path}"
        )
    path_count, path_digest = checkout_path_digest(digest_paths)
    if path_count != receipt.path_count or path_digest != receipt.path_digest:
        raise PlanError(
            f"checkout path receipt changed after preflight: {entry.submodule.path}"
        )
    repeated_object_closure = target_object_closure(
        entry.source_git_dir,
        entry.sha,
        entry.source_completeness,
    )
    require_object_closure_receipt(
        repeated_object_closure,
        expected_object_closure,
        f"target checkout for {entry.submodule.path}",
    )


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
    pending_transport_receipts: list[TransportReceipt] = []

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
        source_completeness = capture_source_completeness_receipt(source_git_dir)
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
        commit_available = commit_exists(
            source_git_dir,
            target.path,
            sha,
            completeness=source_completeness,
        )
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
        transport_receipt: Optional[TransportReceipt] = None
        if needs_fetch:
            transport_receipt = capture_transport_receipt(
                source_git_dir,
                submodule,
            )
            pending_transport_receipts.append(transport_receipt)
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
            source_completeness=source_completeness,
            target_bindings=target_bindings,
            checkout_preflight=None,
            recursive_metadata=None,
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
        if transport_receipt is not None:
            pending_transport_receipts.remove(transport_receipt)
        planned_path_components += candidate_component_count

        if not recursive:
            return
        entry.recursive_metadata = capture_recursive_metadata_receipt(
            entry,
            gitmodules_budget,
        )
        active_ancestor_indexes.add(current_index)
        try:
            for selection in entry.recursive_metadata.selections:
                add_entry(
                    Submodule(
                        name=selection.name,
                        path=selection.path,
                        url=selection.url,
                    ),
                    selection.sha,
                    target.relative_parts,
                    source_git_dir,
                    current_index,
                )
        finally:
            active_ancestor_indexes.remove(current_index)

    try:
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
            shared_missing_ancestors=capture_shared_missing_ancestors(entries),
        )
    except BaseException as exc:
        cleanup_errors: list[str] = []
        seen: set[int] = set()
        receipts = [
            *pending_transport_receipts,
            *(
                entry.transport_receipt
                for entry in entries
                if entry.transport_receipt is not None
            ),
        ]
        for receipt in reversed(receipts):
            if id(receipt) in seen:
                continue
            seen.add(id(receipt))
            try:
                receipt.close()
            except BaseException as cleanup_exc:
                cleanup_errors.append(
                    "transport receipt cleanup failed while abandoning sync plan\n"
                    f"  recovery_identity: {receipt.fetch_git_dir}\n"
                    f"  detail: {type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        if isinstance(exc, ForwardedProcessSignal):
            for cleanup_error in cleanup_errors:
                exc.add_cleanup_error(cleanup_error)
            raise
        if cleanup_errors:
            raise PlanError(f"{exc}\n" + "\n".join(cleanup_errors)) from exc
        raise


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
    revalidate_source_completeness_receipt(
        entry.source_git_dir,
        entry.source_completeness,
    )
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
    revalidate_source_completeness_receipt(
        entry.source_git_dir,
        entry.source_completeness,
    )

    shared_target = target_with_materialized_shared_ancestors(plan, entry)
    if allow_parent_materialization and entry.parent_index is not None:
        parent_receipt = revalidate_applied_target_root(
            plan,
            entry.parent_index,
        )
        parent_parts = parent_receipt.relative_parts
        if (
            len(entry.target.relative_parts) <= len(parent_parts)
            or entry.target.relative_parts[: len(parent_parts)] != parent_parts
        ):
            raise PlanError(
                "recursive target is not nested under its receipt-bound parent"
            )
        for node in shared_target.existing_nodes:
            current = filesystem_fingerprint(node.path)
            if current != node.fingerprint:
                raise PlanError(f"target-path object or policy changed: {node.path}")
        current_target = bind_target_path(
            plan.root,
            entry.target.relative_parts,
            f"worktree path for submodule {entry.submodule.path}",
        )
        observed_parent = next(
            (
                node
                for node in current_target.existing_nodes
                if node.path == parent_receipt.node.path
            ),
            None,
        )
        if (
            observed_parent is None
            or observed_parent.fingerprint != parent_receipt.node.fingerprint
        ):
            raise PlanError(
                "recursive target no longer traverses its applied parent root\n"
                f"  parent: {parent_receipt.node.path}"
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
        revalidate_bound_target(shared_target)
        current_state = classify_planned_target(
            shared_target,
            entry.source_git_dir,
            plan.force_replace_empty,
        )
        if current_state != entry.state:
            raise PlanError(
                f"target state changed after preflight for {entry.target.path}: "
                f"{entry.state} -> {current_state}"
            )
        if shared_target is not entry.target:
            target_access_bindings(
                shared_target,
                current_state,
                entry.source_git_dir,
            )
        target = shared_target

    if not entry.needs_fetch and not commit_exists(
        entry.source_git_dir,
        target.path,
        entry.sha,
        completeness=entry.source_completeness,
    ):
        raise PlanError(
            f"target commit disappeared after preflight for {entry.submodule.path}: "
            f"{entry.sha}"
        )
    if not entry.needs_fetch:
        revalidate_checkout_preflight(entry)
    return target


def validate_recursive_plan_structure(plan: SyncPlan) -> None:
    children_by_parent: dict[int, list[PlannedWorktree]] = {}
    for index, entry in enumerate(plan.entries):
        parent_index = entry.parent_index
        if parent_index is None:
            if entry.parent_source_git_dir is not None:
                raise PlanError(
                    f"top-level plan entry has a recursive parent source: "
                    f"{entry.submodule.path}"
                )
            continue
        if parent_index < 0 or parent_index >= index:
            raise PlanError(
                f"recursive plan has an invalid parent index for {entry.submodule.path}"
            )
        children_by_parent.setdefault(parent_index, []).append(entry)

    for parent_index, parent in enumerate(plan.entries):
        children = children_by_parent.get(parent_index, [])
        receipt = parent.recursive_metadata
        if receipt is None:
            if children:
                raise PlanError(
                    f"recursive plan parent lacks metadata: {parent.submodule.path}"
                )
            continue
        if receipt.gitmodules.commit != parent.sha:
            raise PlanError(
                f"recursive metadata names the wrong commit for {parent.submodule.path}"
            )
        if len(children) != len(receipt.selections):
            raise PlanError(
                f"recursive child set changed after preflight: {parent.submodule.path}"
            )
        for child, selection in zip(children, receipt.selections):
            if (
                child.submodule.name != selection.name
                or child.submodule.path != selection.path
                or child.submodule.url != selection.url
                or selection.mode != "160000"
                or child.sha != selection.sha
                or child.parent_source_git_dir != parent.source_git_dir
            ):
                raise PlanError(
                    f"recursive child selection changed after preflight: "
                    f"{parent.submodule.path} -> {child.submodule.path}"
                )


def revalidate_recursive_metadata_for_entry(
    plan: SyncPlan,
    index: int,
) -> None:
    validate_recursive_plan_structure(plan)
    entry = plan.entries[index]
    budget = GitmodulesReadBudget.start()
    checked_indexes: set[int] = set()
    if entry.parent_index is not None:
        checked_indexes.add(entry.parent_index)
        revalidate_recursive_metadata_receipt(
            plan.entries[entry.parent_index],
            budget,
        )
    if entry.recursive_metadata is not None and index not in checked_indexes:
        revalidate_recursive_metadata_receipt(entry, budget)


def validate_sync_plan(plan: SyncPlan) -> None:
    revalidate_plan_input_receipt(plan)
    revalidate_shared_missing_ancestors(plan)
    validate_recursive_plan_structure(plan)
    gitmodules_budget = GitmodulesReadBudget.start()
    for entry in plan.entries:
        revalidate_recursive_metadata_receipt(entry, gitmodules_budget)
    for entry in plan.entries:
        revalidate_planned_entry(plan, entry)


def revalidate_managed_final_file_bindings(
    receipt: ManagedFinalStateReceipt,
    control: ManagedControlReceipt,
) -> None:
    for descriptor, expected, name, maximum_bytes, purpose in (
        (
            receipt.head_descriptor,
            receipt.head_binding,
            "HEAD",
            MAX_GITDIR_FILE_BYTES,
            "managed worktree final HEAD",
        ),
        (
            receipt.common_descriptor,
            receipt.common_binding,
            "commondir",
            MAX_GITDIR_FILE_BYTES,
            "managed worktree common-gitdir pointer",
        ),
        (
            receipt.index_descriptor,
            receipt.index_binding,
            "index",
            MAX_SUPERPROJECT_INDEX_BYTES,
            "managed worktree final index",
        ),
    ):
        observed, _ = bind_regular_file_descriptor_at(
            descriptor,
            control.admin_lease.descriptor,
            name,
            expected.path,
            maximum_bytes=maximum_bytes,
            mode=os.R_OK,
            purpose=purpose,
            retain_content=False,
        )
        require_matching_file_binding(expected, observed, purpose)


def revalidate_managed_final_common_endpoint(
    receipt: ManagedFinalStateReceipt,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> None:
    """Sandwich one endpoint proof between exact pointer-file bindings."""

    for _phase in range(2):
        observed, _ = bind_regular_file_descriptor_at(
            receipt.common_descriptor,
            control.admin_lease.descriptor,
            "commondir",
            receipt.common_binding.path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree common-gitdir pointer",
            retain_content=False,
        )
        require_matching_file_binding(
            receipt.common_binding,
            observed,
            "managed worktree common-gitdir pointer",
        )
        if _phase == 0:
            revalidate_nofollow_pointer_endpoint(
                plain_gitdir_pointer_bytes(
                    receipt.common_content,
                    "managed worktree common-gitdir pointer",
                ),
                relative_base_descriptor=control.admin_lease.descriptor,
                expected_fingerprint=source_lease.binding.fingerprint,
                expected_kind=stat.S_IFDIR,
                purpose="managed worktree commondir-to-source pointer",
            )


def capture_managed_final_state_receipt(
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> ManagedFinalStateReceipt:
    """Bind the exact admin files that define the published checkout state."""

    revalidate_managed_control_receipt(control, lease.target_descriptor)
    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(source_lease)
    head_descriptor = -1
    common_descriptor = -1
    index_descriptor = -1
    try:
        (
            head_descriptor,
            head_binding,
            head_content,
        ) = open_bound_regular_file_at(
            control.admin_lease.descriptor,
            "HEAD",
            control.admin_git_dir / "HEAD",
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree final HEAD",
            retain_content=True,
        )
        if head_content is None:
            raise PlanError("managed worktree final HEAD binding returned no content")
        raw_head = head_content.rstrip(b"\r\n")
        if (
            not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", raw_head)
            or os.fsdecode(raw_head) != entry.sha
        ):
            raise PlanError(
                "descriptor-bound worktree HEAD does not match the planned target\n"
                f"  path: {entry.target.path}\n"
                f"  expected: {entry.sha}"
            )

        (
            common_descriptor,
            common_binding,
            common_content,
        ) = open_bound_regular_file_at(
            control.admin_lease.descriptor,
            "commondir",
            control.admin_git_dir / "commondir",
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree common-gitdir pointer",
            retain_content=True,
        )
        if common_content is None:
            raise PlanError(
                "managed worktree common-gitdir binding returned no content"
            )
        raw_common = plain_gitdir_pointer_bytes(
            common_content,
            "managed worktree common-gitdir pointer",
        )
        revalidate_nofollow_pointer_endpoint(
            raw_common,
            relative_base_descriptor=control.admin_lease.descriptor,
            expected_fingerprint=source_lease.binding.fingerprint,
            expected_kind=stat.S_IFDIR,
            purpose="managed worktree commondir-to-source pointer",
        )
        observed_common, _ = bind_regular_file_descriptor_at(
            common_descriptor,
            control.admin_lease.descriptor,
            "commondir",
            common_binding.path,
            maximum_bytes=MAX_GITDIR_FILE_BYTES,
            mode=os.R_OK,
            purpose="managed worktree common-gitdir pointer",
            retain_content=False,
        )
        require_matching_file_binding(
            common_binding,
            observed_common,
            "managed worktree common-gitdir pointer",
        )

        (
            index_descriptor,
            index_binding,
            index_content,
        ) = open_bound_regular_file_at(
            control.admin_lease.descriptor,
            "index",
            control.admin_git_dir / "index",
            maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
            mode=os.R_OK,
            purpose="managed worktree final index",
            retain_content=True,
        )
        if index_content is None:
            raise PlanError("managed worktree final index returned no content")
        validate_captured_index_matches_tree(entry, index_content)
        observed_index, _ = bind_regular_file_descriptor_at(
            index_descriptor,
            control.admin_lease.descriptor,
            "index",
            index_binding.path,
            maximum_bytes=MAX_SUPERPROJECT_INDEX_BYTES,
            mode=os.R_OK,
            purpose="managed worktree final index",
            retain_content=False,
        )
        require_matching_file_binding(
            index_binding,
            observed_index,
            "managed worktree final index",
        )
        receipt = ManagedFinalStateReceipt(
            head_descriptor=head_descriptor,
            head_binding=head_binding,
            common_descriptor=common_descriptor,
            common_binding=common_binding,
            common_content=common_content,
            index_descriptor=index_descriptor,
            index_binding=index_binding,
            index_content=index_content,
        )
        # Close the capture around every retained file and pointer endpoint
        # after the potentially long target-tree/index validation above.
        revalidate_managed_final_state_receipt(
            entry,
            receipt,
            lease,
            control,
            source_lease,
        )
        return receipt
    except BaseException:
        for descriptor in (
            head_descriptor,
            common_descriptor,
            index_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)
        raise


def revalidate_managed_final_state_receipt(
    entry: PlannedWorktree,
    receipt: ManagedFinalStateReceipt,
    lease: MaterializedTargetLease,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> None:
    """Revalidate final state only through retained target/admin descriptors."""

    revalidate_managed_control_receipt(control, lease.target_descriptor)
    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(source_lease)
    revalidate_managed_final_file_bindings(receipt, control)
    revalidate_managed_final_common_endpoint(receipt, control, source_lease)
    validate_captured_index_matches_tree(entry, receipt.index_content)
    revalidate_managed_final_file_bindings(receipt, control)
    revalidate_directory_entry_lease(source_lease)
    revalidate_materialized_target_lease(lease)
    revalidate_managed_control_receipt(control, lease.target_descriptor)
    # The long index/tree validation and control revalidation above can race
    # independently with a pointer-chain replacement. Re-prove commondir last,
    # then close all three retained state files and both control endpoints.
    revalidate_managed_final_common_endpoint(receipt, control, source_lease)
    revalidate_managed_final_file_bindings(receipt, control)
    revalidate_directory_entry_lease(source_lease)
    revalidate_materialized_target_lease(lease)
    revalidate_managed_control_receipt(control, lease.target_descriptor)


def postvalidate_applied_entry(
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> ManagedFinalStateReceipt:
    revalidate_managed_control_receipt(control, lease.target_descriptor)
    revalidate_materialized_target_lease(lease)
    revalidate_directory_entry_lease(source_lease)
    for binding in entry.source_bindings:
        revalidate_access(binding)
    revalidate_source_completeness_receipt(
        entry.source_git_dir,
        entry.source_completeness,
    )
    revalidate_source_object_admission(entry.source_git_dir)
    final_state = capture_managed_final_state_receipt(
        entry,
        lease,
        control,
        source_lease,
    )
    receipt = entry.checkout_preflight
    if receipt is None:
        final_state.close()
        raise PlanError(f"checkout preflight is incomplete for {entry.submodule.path}")
    try:
        closure = target_object_closure(
            entry.source_git_dir,
            entry.sha,
            entry.source_completeness,
        )
        if (
            closure.object_count != receipt.object_count
            or closure.logical_bytes != receipt.object_logical_bytes
            or closure.digest != receipt.object_digest
        ):
            raise PlanError(
                f"target object closure changed during checkout: {entry.submodule.path}"
            )
        revalidate_managed_final_state_receipt(
            entry,
            final_state,
            lease,
            control,
            source_lease,
        )
        return final_state
    except BaseException:
        final_state.close()
        raise


def finalize_leaf_checkout(
    plan: SyncPlan,
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> None:
    """Commit leaf receipts only after every final checkout check passes."""

    ancestors = getattr(plan, "shared_missing_ancestors", {})
    prior_shared_nodes = {
        relative_parts: ancestor.materialized_node
        for relative_parts, ancestor in ancestors.items()
    }
    final_state: Optional[ManagedFinalStateReceipt] = None
    try:
        final_state = postvalidate_applied_entry(
            entry,
            lease,
            control,
            source_lease,
        )
        revalidate_managed_final_state_receipt(
            entry,
            final_state,
            lease,
            control,
            source_lease,
        )
        record_materialized_shared_ancestors(
            plan,
            entry,
            lease.created_nodes,
        )
        revalidate_managed_final_state_receipt(
            entry,
            final_state,
            lease,
            control,
            source_lease,
        )
    except BaseException:
        for relative_parts, materialized_node in prior_shared_nodes.items():
            ancestors[relative_parts].materialized_node = materialized_node
        raise
    finally:
        if final_state is not None:
            final_state.close()


def finalize_recursive_parent_checkout(
    plan: SyncPlan,
    owner_index: int,
    entry: PlannedWorktree,
    lease: MaterializedTargetLease,
    control: ManagedControlReceipt,
    source_lease: DirectoryEntryLease,
) -> None:
    """Commit parent-checkout receipts only after every binding validates."""

    ancestors = getattr(plan, "shared_missing_ancestors", {})
    prior_shared_nodes = {
        relative_parts: ancestor.materialized_node
        for relative_parts, ancestor in ancestors.items()
    }
    receipts = getattr(plan, "applied_target_roots", {})
    prior_target_roots = dict(receipts)
    final_state: Optional[ManagedFinalStateReceipt] = None
    try:
        final_state = postvalidate_applied_entry(
            entry,
            lease,
            control,
            source_lease,
        )
        checkout_created = capture_checkout_materialized_shared_ancestors(
            plan,
            owner_index,
            entry,
            lease,
        )
        updates = prepare_materialized_shared_ancestor_updates(
            plan,
            checkout_created,
            authorized_targets=recursive_parent_subtree_target_parts(
                plan,
                owner_index,
            ),
            require_all_participants_authorized=True,
        )
        updates = prepare_materialized_shared_ancestor_updates(
            plan,
            lease.created_nodes,
            authorized_targets=frozenset((entry.target.relative_parts,)),
            require_all_participants_authorized=False,
            existing_updates=updates,
        )
        revalidate_managed_final_state_receipt(
            entry,
            final_state,
            lease,
            control,
            source_lease,
        )
        commit_materialized_shared_ancestor_updates(plan, updates)
        record_applied_target_root(
            plan,
            owner_index,
            entry,
            lease,
        )
        revalidate_managed_final_state_receipt(
            entry,
            final_state,
            lease,
            control,
            source_lease,
        )
    except BaseException:
        for relative_parts, materialized_node in prior_shared_nodes.items():
            ancestors[relative_parts].materialized_node = materialized_node
        receipts.clear()
        receipts.update(prior_target_roots)
        raise
    finally:
        if final_state is not None:
            final_state.close()


@signal_owned_operation
def apply_sync_plan(plan: SyncPlan) -> None:
    validate_sync_plan(plan)
    plan_state_changed = False
    for index, entry in enumerate(plan.entries):
        if not entry.needs_fetch:
            continue
        revalidate_plan_input_receipt(plan)
        revalidate_recursive_metadata_for_entry(plan, index)
        revalidate_runtime_source_access(entry)
        terminal_completeness_receipts: list[SourceCompletenessReceipt] = []
        fetch_missing_commit(
            entry.source_git_dir,
            entry.target.path,
            entry.submodule,
            entry.sha,
            plan.depth,
            dry_run=False,
            transport_receipt=getattr(entry, "transport_receipt", None),
            source_completeness=entry.source_completeness,
            fetch_missing=True,
            terminal_completeness_receipts=terminal_completeness_receipts,
        )
        if len(terminal_completeness_receipts) != 1:
            raise PlanError(
                "authorized fetch returned no exact terminal "
                "source-completeness receipt"
            )
        entry.source_completeness = terminal_completeness_receipts[0]
        entry.needs_fetch = False
        plan_state_changed = True

    for entry in plan.entries:
        if entry.checkout_preflight is not None:
            continue
        checkout_preflight, write_bindings = capture_checkout_preflight(entry)
        entry.checkout_preflight = checkout_preflight
        entry.target_bindings = (*entry.target_bindings, *write_bindings)
        plan_state_changed = True

    if plan_state_changed:
        validate_sync_plan(plan)
    recursive_parent_indexes = {
        entry.parent_index for entry in plan.entries if entry.parent_index is not None
    }
    first_mutation = True
    for index, entry in enumerate(plan.entries):
        target = revalidate_planned_entry(
            plan,
            entry,
            allow_parent_materialization=(
                entry.parent_index is not None
                and entry.parent_index in getattr(plan, "applied_target_roots", {})
            ),
        )
        if first_mutation:
            revalidate_plan_input_receipt(plan)
            first_mutation = False
        source_lease = capture_planned_source_lease(entry)
        try:
            revalidate_recursive_metadata_for_entry(plan, index)
            lease = materialize_bound_target_directory(target)
            caller_owns_materialization = True

            def adopt_current_materialization() -> None:
                nonlocal caller_owns_materialization
                if not caller_owns_materialization:
                    raise PlanError(
                        "target materialization ownership was already transferred"
                    )
                caller_owns_materialization = False

            try:
                # The fresh source lease is fingerprint-equal to both preflight
                # source receipts before either managed checkout or new
                # registration can mutate Git state. Both held directory
                # objects stay live through checkout and rollback.
                revalidate_planned_source_lease(entry, source_lease)
                revalidate_materialized_target_lease(lease)
                revalidate_source_object_admission(entry.source_git_dir)
                if index in recursive_parent_indexes:

                    def finalize_recursive_current_checkout(
                        control: ManagedControlReceipt,
                        callback_source_lease: DirectoryEntryLease,
                        current_plan: SyncPlan = plan,
                        current_index: int = index,
                        current_entry: PlannedWorktree = entry,
                        current_lease: MaterializedTargetLease = lease,
                    ) -> None:
                        finalize_recursive_parent_checkout(
                            current_plan,
                            current_index,
                            current_entry,
                            current_lease,
                            control,
                            callback_source_lease,
                        )

                    finalize_current_checkout = finalize_recursive_current_checkout
                else:

                    def finalize_leaf_current_checkout(
                        control: ManagedControlReceipt,
                        callback_source_lease: DirectoryEntryLease,
                        current_plan: SyncPlan = plan,
                        current_entry: PlannedWorktree = entry,
                        current_lease: MaterializedTargetLease = lease,
                    ) -> None:
                        finalize_leaf_checkout(
                            current_plan,
                            current_entry,
                            current_lease,
                            control,
                            callback_source_lease,
                        )

                    finalize_current_checkout = finalize_leaf_current_checkout
                revalidate_recursive_metadata_for_entry(plan, index)
                revalidate_checkout_preflight(entry)
                checkout_receipt = entry.checkout_preflight
                if checkout_receipt is None:
                    raise PlanError(
                        f"checkout preflight is incomplete for {entry.submodule.path}"
                    )
                checkout_view = capture_checkout_execution_view(
                    entry.source_git_dir,
                    entry.source_completeness,
                    checkout_receipt.attributes_receipt,
                    entry.submodule.path,
                    (
                        (
                            checkout_receipt.current_head,
                            entry.sha,
                        )
                        if checkout_receipt.current_head is not None
                        else (entry.sha,)
                    ),
                )
                checkout_outcome: Optional[BaseException] = None
                try:
                    if entry.state == "managed":
                        checkout_existing_worktree(
                            target.path,
                            entry.sha,
                            dry_run=False,
                            target_descriptor=lease.target_descriptor,
                            source_git_dir=entry.source_git_dir,
                            source_lease=source_lease,
                            checkout_view=checkout_view,
                            finalize_checkout=finalize_current_checkout,
                        )
                    else:

                        def revalidate_new_checkout(
                            current_entry: PlannedWorktree = entry,
                            current_view: CheckoutExecutionView = checkout_view,
                        ) -> None:
                            revalidate_checkout_preflight(current_entry)
                            revalidate_checkout_execution_view(current_view)

                        add_worktree(
                            entry.source_git_dir,
                            target.path,
                            entry.sha,
                            dry_run=False,
                            lease=lease,
                            source_lease=source_lease,
                            checkout_view=checkout_view,
                            finalize_checkout=finalize_current_checkout,
                            pre_checkout=revalidate_new_checkout,
                            adopt_materialization=adopt_current_materialization,
                        )
                except BaseException as exc:
                    checkout_outcome = exc
                    raise
                finally:
                    finish_explicit_cleanup(
                        checkout_view.close,
                        outcome_exception=checkout_outcome,
                        purpose="checkout execution view",
                        recovery_identity=str(checkout_view.common_git_dir),
                    )
            except BaseException as exc:
                cleanup_error: Optional[BaseException] = None
                if (
                    caller_owns_materialization
                    and lease.materialization_target is not None
                    and lease.created_nodes
                ):
                    try:
                        cleanup_materialized_target_nodes(
                            lease.materialization_target,
                            lease.created_nodes,
                        )
                    except BaseException as cleanup_exc:
                        cleanup_error = cleanup_exc
                if cleanup_error is not None:
                    cleanup_detail = (
                        f"worktree/materialization rollback failed: {cleanup_error}"
                    )
                    if isinstance(exc, ForwardedProcessSignal):
                        exc.add_cleanup_error(cleanup_detail)
                        raise
                    raise PlanError(f"{exc}\n{cleanup_detail}") from exc
                raise
            finally:
                lease.close()
        finally:
            source_lease.close()


@signal_owned_operation
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
    outcome_exception: Optional[BaseException] = None
    try:
        print_sync_plan(plan)
        if not dry_run:
            apply_sync_plan(plan)
    except BaseException as exc:
        outcome_exception = exc
        raise
    finally:
        finish_explicit_cleanup(
            plan.close,
            outcome_exception=outcome_exception,
            purpose="sync plan transport",
            recovery_identity=str(plan.display_root),
        )


@signal_owned_operation
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
    outcome_exception: Optional[BaseException] = None
    try:
        print_sync_plan(plan)

        if dry_run:
            print("preflight complete; no worktrees changed", flush=True)
            return

        print("preflight complete; applying plan", flush=True)
        apply_sync_plan(plan)
    except BaseException as exc:
        outcome_exception = exc
        raise
    finally:
        finish_explicit_cleanup(
            plan.close,
            outcome_exception=outcome_exception,
            purpose="sync plan transport",
            recovery_identity=str(plan.display_root),
        )


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


def _main_impl() -> int:
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


@signal_owned_operation
def main() -> int:
    outcome_exception: Optional[BaseException] = None
    try:
        return _main_impl()
    except BaseException as exc:
        outcome_exception = exc
        raise
    finally:
        finish_explicit_cleanup(
            cleanup_cli_resources,
            outcome_exception=outcome_exception,
            purpose="CLI runtime and active guards",
            recovery_identity=(
                "Git executable/helper snapshots, transport roots, and "
                "source-transaction descriptors"
            ),
        )


def redeliver_forwarded_signal(forwarded: ForwardedProcessSignal) -> NoReturn:
    signal_name = signal.Signals(forwarded.signum).name
    print(
        f"interrupted by {signal_name} after child cleanup and recovery publication",
        file=sys.stderr,
    )
    for detail in forwarded.recovery_details:
        print(f"recovery: {detail}", file=sys.stderr)
    for detail in forwarded.cleanup_errors:
        print(f"cleanup-incomplete: {detail}", file=sys.stderr)
    signal.signal(forwarded.signum, signal.SIG_DFL)
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if callable(pthread_sigmask):
        pthread_sigmask(signal.SIG_UNBLOCK, {forwarded.signum})
    os.kill(os.getpid(), forwarded.signum)
    os._exit(128 + forwarded.signum)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ForwardedProcessSignal as exc:
        redeliver_forwarded_signal(exc)
    except (GitError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
